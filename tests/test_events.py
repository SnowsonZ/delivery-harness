"""事件库测试：建库与迁移、哈希链与链校验、分来源链、并发、产物与锚点、公共 API 与隐私过滤。

全部用临时 git 仓库与临时目录，不碰真实仓库的库（库在临时仓库的 .git/ 下）。
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from engine.core import events, events_db

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
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and not k.startswith("HARNESS_")}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def rows(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def event(self, event_id: int) -> dict:
        return dict(zip(EVENT_COLUMNS, self.rows("SELECT * FROM events WHERE id=?", (event_id,))[0]))

    def run_py(self, body: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run([sys.executable, "-c", self.runner_script(body)],
                              cwd=ENGINE_REPO, capture_output=True, text=True,
                              env={**env, "PYTHONPATH": str(ENGINE_REPO), **GIT_ENV}, check=False)

    def runner_script(self, body: str) -> str:
        """子进程脚本：把 events_db.ROOT 指到临时仓库，body 缩进 8 格后进入 with 块。"""
        body = textwrap.indent(textwrap.dedent(body).strip("\n"), "        ")
        return ("from pathlib import Path\n"
                "from unittest import mock\n"
                "from engine.core import events, events_db\n"
                f"with mock.patch.object(events_db, 'ROOT', Path({str(self.repo)!r})):\n"
                f"{body}\n")

    def test_schema_has_four_tables_and_source_column(self):
        self.assertIsNotNone(events.emit("guard", "deny-1", "deny", trace_id="t"))
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

    def test_emit_writes_event_with_refs_and_hash_chain(self):
        first = events.emit("verify", "lint", "ok", trace_id="t", duration_ms=12,
                            inputs=[events.ref("policy", "main@a1b2c3d", sha256="h" * 64, size=3)],
                            outputs={"ok": True})
        second = events.emit("verify", "lint", "fail", trace_id="t")
        self.assertIsInstance(first, int)
        self.assertIsInstance(second, int)
        row1, row2 = self.event(first), self.event(second)
        self.assertEqual(row1["seq"], 1)
        self.assertEqual(row1["prev_hash"], "")
        self.assertEqual(row2["seq"], 2)
        self.assertEqual(row2["prev_hash"], row1["hash"])
        self.assertEqual((row1["source"], row1["stage"], row1["step"], row1["status"], row1["duration_ms"]),
                         ("local", "verify", "lint", "ok", 12))
        self.assertEqual(row1["actor_role"], "engine")
        self.assertEqual(row1["actor_host"], "local")
        self.assertEqual(row1["engine_version"], events.ENGINE_VERSION)
        self.assertRegex(row1["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual(json.loads(row1["outputs"]), {"ok": True})
        self.assertEqual(json.loads(row2["outputs"]), {})
        self.assertEqual(self.rows("SELECT event_id,direction,kind,ref,sha256,size FROM refs"),
                         [(first, "in", "policy", "main@a1b2c3d", "h" * 64, 3)])
        self.assertEqual(events.verify_chain("t"), [])

    def test_verify_chain_detects_edit_delete_and_reorder(self):
        def fresh():
            events_db.db_path().unlink(missing_ok=True)
            for index in range(3):
                self.assertIsNotNone(events.emit("verify", f"s{index}", "ok", trace_id="t"))
            self.assertEqual(events.verify_chain("t"), [])

        def tamper(*statements):
            with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
                for statement in statements:
                    conn.execute(statement)
                conn.commit()

        fresh()
        tamper("UPDATE events SET step='tampered' WHERE seq=2")
        self.assertTrue(events.verify_chain("t"))
        fresh()
        tamper("DELETE FROM events WHERE seq=2")
        self.assertTrue(events.verify_chain("t"))
        fresh()
        tamper("UPDATE events SET seq=99 WHERE seq=2",
               "UPDATE events SET seq=2 WHERE seq=3",
               "UPDATE events SET seq=3 WHERE seq=99")
        self.assertTrue(events.verify_chain("t"))
        fresh()
        self.assertEqual(events.verify_chain(), [])
        self.assertEqual(events.verify_chain(source="local"), [])

    def test_chains_are_per_source_and_trace(self):
        for _ in range(2):
            events.emit("ci", "check", "ok", trace_id="t1", source="ci")
            events.emit("verify", "lint", "ok", trace_id="t1")
        events.emit("verify", "lint", "ok", trace_id="t2")
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
        self.assertEqual(events.chain_head("t1"), local_rows[-1][0])
        self.assertEqual(events.chain_head("t1", source="ci"), ci_rows[-1][0])
        self.assertIsNone(events.chain_head("missing"))
        self.assertEqual(events.verify_chain("t1"), [])

    def test_privacy_filter_drops_forbidden_values_and_counts_them(self):
        outputs = {
            "kept_int": 5, "kept_bool": True, "kept_none": None, "kept_str": "ok值",
            "too_long": "x" * 121,
            "users_path": "/Users/someone/file",
            "home_path": "/home/user/file",
            "win_path": "C:\\Users\\x",
            "newline": "a\nb",
            "nested": {"a": 1},
            "list_val": [1],
            "bad key!": 1,
        }
        event_id = events.emit(
            "route", "budget", "ok", trace_id="t",
            outputs=outputs,
            decision={"by": "policy", "rule": "预算", "reason": "r" * 150},
            error={"kind": "ValueError", "signature": "a\nb"},
            actor={"role": "user", "host": "local", "model": "m"},
            inputs=[events.ref("blob", "a" * 250),
                    events.ref("policy", "main@a1b2c3d", sha256="h" * 64, size=3)])
        self.assertIsInstance(event_id, int)
        row = self.event(event_id)
        self.assertEqual(json.loads(row["outputs"]),
                         {"kept_int": 5, "kept_bool": True, "kept_none": None, "kept_str": "ok值"})
        self.assertEqual((row["decision_by"], row["decision_rule"], row["decision_reason"]),
                         ("policy", "预算", "r" * 150))
        self.assertEqual((row["error_kind"], row["error_signature"]), ("ValueError", None))
        self.assertEqual((row["actor_role"], row["actor_host"], row["model"]), (None, "local", "m"))
        self.assertEqual(row["redacted"], 11)
        self.assertEqual(self.rows("SELECT kind,ref,sha256,size FROM refs WHERE event_id=?", (event_id,)),
                         [("blob", None, None, None), ("policy", "main@a1b2c3d", "h" * 64, 3)])
        self.assertEqual(events.verify_chain("t"), [])

    def test_unknown_stage_or_status_is_rejected_without_raising(self):
        self.assertIsNone(events.emit("nope", "x", "ok", trace_id="t"))
        self.assertIsNone(events.emit("guard", "", "ok", trace_id="t"))
        self.assertIsNone(events.emit("guard", "  ", "ok", trace_id="t"))
        self.assertIsNone(events.emit("guard", "x", "nope", trace_id="t"))
        self.assertFalse(self.db_path.exists())

    def test_off_switch_writes_nothing(self):
        os.environ["HARNESS_EVENTS"] = "off"
        self.assertFalse(events.enabled())
        self.assertIsNone(events.emit("guard", "x", "ok", trace_id="t"))
        events.set_anchor("t", "verify", "deadbeef", "run_record")
        self.assertFalse(self.db_path.exists())

    def test_config_can_disable_events(self):
        self.assertTrue(events.enabled())
        with mock.patch.object(events, "setting", lambda *args, **kwargs: False):
            self.assertFalse(events.enabled())
            self.assertIsNone(events.emit("guard", "x", "ok", trace_id="t"))
            self.assertFalse(self.db_path.exists())
            os.environ["HARNESS_EVENTS"] = "on"  # 环境变量优先于配置
            self.assertTrue(events.enabled())
            self.assertIsNotNone(events.emit("guard", "x", "ok", trace_id="t"))
            self.assertTrue(self.db_path.exists())

    def test_write_failure_is_swallowed_and_reported_once(self):
        body = """
            out = [events.emit('guard', 'x', 'ok', trace_id='t'),
                   events.emit('guard', 'y', 'ok', trace_id='t')]
            print(out)
        """
        self.db_path.parent.mkdir(parents=True)
        self.db_path.mkdir()  # 库路径不可写（被目录占用）
        result = self.run_py(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[None, None]", result.stdout)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        shutil.rmtree(self.db_path)
        self.db_path.write_bytes(b"this is not a sqlite database at all")  # 库文件损坏
        result = self.run_py(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[None, None]", result.stdout)
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_current_trace_follows_branch_detached_main_and_ci(self):
        def short():
            return self.git("rev-parse", "--short=7", "HEAD").stdout.strip()

        self.assertEqual(events.current_trace(), f"main@{short()}")
        self.git("checkout", "-q", "-b", "task/T9-x")
        self.assertEqual(events.current_trace(), "task/T9-x")
        self.git("checkout", "-q", "--detach", "HEAD")
        self.assertEqual(events.current_trace(), f"HEAD@{short()}")
        self.git("checkout", "-q", "main")
        os.environ["CI"] = "true"
        os.environ["GITHUB_HEAD_REF"] = "pr-branch"
        self.assertEqual(events.current_trace(), "pr-branch")
        del os.environ["GITHUB_HEAD_REF"]
        os.environ["GITHUB_REF_NAME"] = "main"
        self.assertEqual(events.current_trace(), f"main@{short()}")
        os.environ["GITHUB_REF_NAME"] = "feat/y"
        self.assertEqual(events.current_trace(), "feat/y")

    def test_span_times_and_records_errors_then_reraises(self):
        with events.span("verify", "lint", trace_id="t") as holder:
            holder.outputs = {"n": 1}
        ok = self.event(self.rows("SELECT MAX(id) FROM events")[0][0])
        self.assertEqual((ok["stage"], ok["step"], ok["status"]), ("verify", "lint", "ok"))
        self.assertEqual(json.loads(ok["outputs"]), {"n": 1})
        self.assertIsInstance(ok["duration_ms"], int)
        self.assertGreaterEqual(ok["duration_ms"], 0)

        with self.assertRaises(RuntimeError), \
                events.span("guard", "hook", trace_id="t", source="ci") as holder:
                holder.status = "fail"
                raise RuntimeError("boom")
        failed = self.event(self.rows("SELECT MAX(id) FROM events")[0][0])
        self.assertEqual((failed["status"], failed["error_kind"], failed["source"]),
                         ("error", "RuntimeError", "ci"))
        self.assertIsInstance(failed["duration_ms"], int)
        self.assertGreaterEqual(failed["duration_ms"], 0)

    def test_file_ref_and_ref_shapes(self):
        target = self.repo / "docs" / "a.txt"
        target.parent.mkdir()
        target.write_text("内容x", encoding="utf-8")
        content = target.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        self.assertEqual(events.file_ref("doc", target),
                         {"kind": "doc", "ref": "docs/a.txt", "sha256": digest, "size": len(content)})
        self.assertEqual(events.file_ref("doc", target, rev="a1b2c3d")["ref"], "docs/a.txt@a1b2c3d")
        self.assertEqual(events.file_ref("doc", target, rev="")["ref"], "docs/a.txt")
        self.assertEqual(events.ref("class", "K3"), {"kind": "class", "ref": "K3"})
        self.assertEqual(events.ref("blob", "f", sha256="h", size=9),
                         {"kind": "blob", "ref": "f", "sha256": "h", "size": 9})

    def test_store_artifact_is_content_addressed_and_idempotent(self):
        entry = events.store_artifact(b"hello")
        digest = hashlib.sha256(b"hello").hexdigest()
        artifacts = events_db.artifacts_dir()
        self.assertEqual(entry, {"kind": "artifact", "ref": digest, "sha256": digest, "size": 5})
        self.assertEqual((artifacts / digest).read_bytes(), b"hello")
        (artifacts / digest).write_bytes(b"tampered")  # 已存在则不重复写
        self.assertEqual(events.store_artifact(b"hello"), entry)
        self.assertEqual((artifacts / digest).read_bytes(), b"tampered")
        rows = self.rows("SELECT sha256,size,path,created FROM artifacts")
        self.assertEqual(rows[0][0], digest)
        self.assertEqual(rows[0][1], 5)
        self.assertEqual(rows[0][2], (artifacts / digest).as_posix())
        self.assertTrue(rows[0][3])
        source = self.tmp / "blob.bin"
        source.write_bytes(b"world")
        entry = events.store_artifact(source)
        digest = hashlib.sha256(b"world").hexdigest()
        self.assertEqual((entry["ref"], entry["size"]), (digest, 5))
        self.assertTrue((artifacts / digest).exists())

    def test_concurrent_processes_keep_a_valid_chain(self):
        script = self.runner_script("""
            for i in range(25):
                assert events.emit("verify", f"p{i}", "ok", trace_id="conc") is not None
        """)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        procs = [subprocess.Popen([sys.executable, "-c", script],
                                  cwd=ENGINE_REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env={**env, "PYTHONPATH": str(ENGINE_REPO), **GIT_ENV})
                 for _ in range(4)]
        for proc in procs:
            _, stderr = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, stderr)
        seqs = [row[0] for row in self.rows("SELECT seq FROM events WHERE trace_id='conc' ORDER BY seq")]
        self.assertEqual(seqs, list(range(1, 101)))
        self.assertEqual(events.verify_chain("conc"), [])

    def test_newer_database_version_is_left_untouched(self):
        self.assertIsNotNone(events.emit("guard", "x", "ok", trace_id="t"))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA user_version=9")
            conn.commit()
        before = self.db_path.read_bytes()
        count = self.rows("SELECT COUNT(*) FROM events")[0][0]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertIsNone(events.emit("guard", "y", "ok", trace_id="t"))
            events.set_anchor("t", "verify", "deadbeef", "run_record")
        self.assertEqual(stderr.getvalue(), "")
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
            self.assertIsNotNone(events.emit("guard", "from-wt", "ok", trace_id="t"))
        self.assertIsNotNone(events.emit("guard", "from-main", "ok", trace_id="t"))
        self.assertEqual([row[0] for row in self.rows("SELECT seq FROM events WHERE trace_id='t' ORDER BY seq")],
                         [1, 2])
        self.assertEqual(events.verify_chain("t"), [])

    def test_anchor_records_chain_head(self):
        events.emit("guard", "a", "ok", trace_id="t")
        events.emit("guard", "b", "ok", trace_id="t")
        head = events.chain_head("t")
        self.assertEqual(head, self.rows(
            "SELECT hash FROM events WHERE trace_id='t' ORDER BY seq DESC LIMIT 1")[0][0])
        events.set_anchor("t", "verify", head, "run_record")
        events.emit("ci", "c", "ok", trace_id="t", source="ci")
        ci_head = events.chain_head("t", source="ci")
        events.set_anchor("t", "ci", ci_head, "ci_artifact", source="ci")
        rows = self.rows("SELECT source,trace_id,stage,head_hash,fixed_in,ts FROM anchors ORDER BY id")
        self.assertEqual(rows[0][:5], ("local", "t", "verify", head, "run_record"))
        self.assertEqual(rows[1][:5], ("ci", "t", "ci", ci_head, "ci_artifact"))
        self.assertTrue(rows[0][5])
        self.assertTrue(rows[1][5])

    def test_checks_template_documents_events_section(self):
        path = ENGINE_REPO / "templates" / ".harness" / "config" / "checks.toml"
        text = path.read_text(encoding="utf-8")
        self.assertIsNotNone(tomllib.loads(text))  # 注释掉的节不影响 TOML 合法性
        lines = text.splitlines()
        heads = [index for index, line in enumerate(lines) if line.strip() == "# [events]"]
        self.assertEqual(len(heads), 1)
        block = lines[heads[0]:heads[0] + 5]
        self.assertTrue(any(line.strip().startswith("# enabled") for line in block))

    def test_modules_import_only_stdlib_and_engine(self):
        allowed = set(sys.stdlib_module_names) | {"engine"}
        for module in (events, events_db):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], allowed, module.__name__)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertIn(node.module.split(".")[0], allowed, module.__name__)
