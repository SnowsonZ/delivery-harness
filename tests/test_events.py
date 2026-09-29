"""事件库测试：建库与迁移、哈希链与链校验、分来源链、并发、产物与锚点、公共 API 与隐私过滤。

全部用临时 git 仓库与临时目录，不碰真实仓库的库（库在临时仓库的 .git/ 下）。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from engine.core import events_db

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
EVENT_COLUMNS = ["id", "ts", "source", "trace_id", "seq", "prev_hash", "hash", "stage", "step", "status",
                 "duration_ms", "actor_role", "actor_host", "model", "decision_by", "decision_rule",
                 "decision_reason", "error_kind", "error_signature", "outputs", "engine_version", "redacted"]


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and not k.startswith("HARNESS_")}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def insert(self, *, trace_id="t", source="local", step="x", stage="guard", status="ok", inputs=None):
        """直接向存储层写一个事件（步骤 1 的驱动器；步骤 2 起改用 events.emit）。"""
        payload = {
            "source": source, "trace_id": trace_id, "stage": stage, "step": step, "status": status,
            "duration_ms": None, "actor_role": "engine", "actor_host": source, "model": None,
            "decision_by": None, "decision_rule": None, "decision_reason": None,
            "error_kind": None, "error_signature": None, "outputs": {},
            "engine_version": "test", "redacted": 0,
        }
        return events_db.insert_event(payload, inputs or [])

    def rows(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def run_py(self, script: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run([sys.executable, "-c", script], cwd=ENGINE_REPO, capture_output=True,
                              text=True, env={**env, "PYTHONPATH": str(ENGINE_REPO), **GIT_ENV}, check=False)

    def test_schema_has_four_tables_and_source_column(self):
        self.assertIsNotNone(self.insert())
        self.assertTrue(self.db_path.exists())
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self.assertEqual(tables, {"events", "refs", "artifacts", "anchors"})
            self.assertEqual([row[1] for row in conn.execute("PRAGMA table_info(events)")], EVENT_COLUMNS)
            self.assertEqual([row[1] for row in conn.execute("PRAGMA table_info(refs)")],
                             ["event_id", "direction", "kind", "ref", "sha256", "size"])
            self.assertEqual([row[1] for row in conn.execute("PRAGMA table_info(artifacts)")],
                             ["sha256", "size", "path", "created"])
            self.assertEqual([row[1] for row in conn.execute("PRAGMA table_info(anchors)")],
                             ["id", "source", "trace_id", "stage", "head_hash", "fixed_in", "ts"])
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_verify_chain_detects_edit_delete_and_reorder(self):
        def fresh():
            events_db.db_path().unlink(missing_ok=True)
            self.insert(step="s1")
            self.insert(step="s2")
            self.insert(step="s3")
            self.assertEqual(events_db.verify("t"), [])

        def tamper(*statements):
            with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
                for statement in statements:
                    conn.execute(statement)
                conn.commit()

        fresh()
        tamper("UPDATE events SET step='tampered' WHERE seq=2")
        self.assertTrue(events_db.verify("t"))
        fresh()
        tamper("DELETE FROM events WHERE seq=2")
        self.assertTrue(events_db.verify("t"))
        fresh()
        tamper("UPDATE events SET seq=99 WHERE seq=2",
               "UPDATE events SET seq=2 WHERE seq=3",
               "UPDATE events SET seq=3 WHERE seq=99")
        self.assertTrue(events_db.verify("t"))
        fresh()
        self.assertEqual(events_db.verify(), [])
        self.assertEqual(events_db.verify(source="local"), [])

    def test_chains_are_per_source_and_trace(self):
        for _ in range(2):
            self.insert(trace_id="t1", source="ci", stage="ci")
            self.insert(trace_id="t1", source="local")
        self.insert(trace_id="t2", source="local")
        for source in ("local", "ci"):
            rows = self.rows(
                "SELECT seq,prev_hash,hash FROM events WHERE source=? AND trace_id=? ORDER BY seq",
                (source, "t1"))
            self.assertEqual([row[0] for row in rows], [1, 2])
            self.assertEqual(rows[0][1], "")
            self.assertEqual(rows[1][1], rows[0][2])
        local_rows = self.rows(
            "SELECT hash FROM events WHERE source='local' AND trace_id='t1' ORDER BY seq")
        ci_rows = self.rows("SELECT hash FROM events WHERE source='ci' AND trace_id='t1' ORDER BY seq")
        self.assertNotEqual(local_rows[0][0], ci_rows[0][0])
        self.assertEqual(events_db.chain_head("local", "t1"), local_rows[-1][0])
        self.assertEqual(events_db.chain_head("ci", "t1"), ci_rows[-1][0])
        self.assertIsNone(events_db.chain_head("local", "missing"))
        self.assertEqual(events_db.verify(), [])

    def test_newer_database_version_is_left_untouched(self):
        self.assertIsNotNone(self.insert())
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA user_version=9")
            conn.commit()
        before = self.db_path.read_bytes()
        count = self.rows("SELECT COUNT(*) FROM events")[0][0]
        self.assertIsNone(self.insert(step="newer"))
        events_db.add_anchor("local", "t", "verify", "deadbeef", "run_record")
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM events")[0][0], count)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM anchors")[0][0], 0)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_database_lives_in_git_common_dir_and_is_shared_by_worktrees(self):
        worktree = self.tmp / "wt2"
        self.git("worktree", "add", "-b", "wt2", str(worktree))
        with mock.patch.object(events_db, "ROOT", worktree):
            self.assertEqual(events_db.db_path(), self.db_path)
            self.assertIsNotNone(self.insert(step="from-wt", trace_id="t"))
        self.assertIsNotNone(self.insert(step="from-main", trace_id="t"))
        self.assertEqual([row[0] for row in self.rows("SELECT seq FROM events WHERE trace_id='t' ORDER BY seq")],
                         [1, 2])
        self.assertEqual(events_db.verify("t"), [])

    def test_concurrent_processes_keep_a_valid_chain(self):
        script = textwrap.dedent("""
            from pathlib import Path
            from unittest import mock
            from engine.core import events_db
            with mock.patch.object(events_db, "ROOT", Path({repo!r})):
                for i in range(25):
                    payload = {{"source": "local", "trace_id": "conc", "stage": "verify", "step": f"p{{i}}",
                                "status": "ok", "outputs": {{}}, "engine_version": "test", "redacted": 0,
                                "actor_role": "engine", "actor_host": "local"}}
                    assert events_db.insert_event(payload, []) is not None
        """)
        procs = []
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        for _ in range(4):
            procs.append(subprocess.Popen(
                [sys.executable, "-c", script.format(repo=str(self.repo))], cwd=ENGINE_REPO,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**env, "PYTHONPATH": str(ENGINE_REPO), **GIT_ENV}))
        for proc in procs:
            _, stderr = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, stderr)
        seqs = [row[0] for row in self.rows("SELECT seq FROM events WHERE trace_id='conc' ORDER BY seq")]
        self.assertEqual(seqs, list(range(1, 101)))
        self.assertEqual(events_db.verify("conc"), [])

    def test_store_artifact_is_content_addressed_and_idempotent(self):
        digest = hashlib.sha256(b"hello").hexdigest()
        events_db.save_artifact(digest, 5, b"hello")
        artifact = events_db.artifacts_dir() / digest
        self.assertEqual(artifact.read_bytes(), b"hello")
        artifact.write_bytes(b"tampered")  # 已存在则不重复写
        events_db.save_artifact(digest, 5, b"hello")
        self.assertEqual(artifact.read_bytes(), b"tampered")
        rows = self.rows("SELECT sha256,size,path,created FROM artifacts")
        self.assertEqual(rows[0][0], digest)
        self.assertEqual(rows[0][1], 5)
        self.assertEqual(rows[0][2], artifact.as_posix())
        self.assertTrue(rows[0][3])

    def test_anchor_records_chain_head(self):
        self.insert()
        self.insert()
        head = events_db.chain_head("local", "t")
        self.assertEqual(head, self.rows("SELECT hash FROM events WHERE trace_id='t' ORDER BY seq DESC LIMIT 1")[0][0])
        events_db.add_anchor("local", "t", "verify", head, "run_record")
        self.insert(source="ci", stage="ci")
        ci_head = events_db.chain_head("ci", "t")
        events_db.add_anchor("ci", "t", "ci", ci_head, "ci_artifact")
        rows = self.rows("SELECT source,trace_id,stage,head_hash,fixed_in,ts FROM anchors ORDER BY id")
        self.assertEqual(rows[0][:5], ("local", "t", "verify", head, "run_record"))
        self.assertEqual(rows[1][:5], ("ci", "t", "ci", ci_head, "ci_artifact"))
        self.assertTrue(rows[0][5])
        self.assertTrue(rows[1][5])
