"""T109 事件库加固测试：prev_hash/seq 校验、隐私与失败隔离、产物原子写、来源链隔离。

规范化哈希由本文件独立计算（不调用 events_db 的哈希函数）：证明 prev_hash 确实进入摘要、
内容与 prev_hash 都合法但留 seq 缺口的链仍被抓住。夹具全部用匿名临时 git 仓库、隔离 ROOT
与冻结时钟，不碰真实库、PR 或工作流；被测模块不调用 gh，无需桩。
"""

from __future__ import annotations

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
GITHUB_KEYS = ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB")
FROZEN_TS = "2026-01-02T03:04:05.678Z"
WARN_LINE = "harness：事件写入失败，已跳过（不影响本次运行）"
REF_KEYS = ("kind", "ref", "sha256", "size")


def canonical_digest(row: dict, inputs: list[dict]) -> str:
    """独立实现的规范化哈希规则：除 id、hash 外全部列加 inputs，键排序、紧凑分隔符、UTF-8 后取 sha256。"""
    payload = {key: value for key, value in row.items() if key not in ("id", "hash")}
    payload["inputs"] = inputs
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-hardening-"))
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
        clock = mock.patch.object(events_db, "_now", return_value=FROZEN_TS)  # 冻结时钟，摘要可比
        clock.start()
        self.addCleanup(clock.stop)
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME", *GITHUB_KEYS):
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

    def row_with_refs(self, event_id: int) -> tuple[dict, list[dict]]:
        """读一行事件与其规范化 inputs（None 值省略），供独立摘要计算。"""
        refs = self.rows("SELECT kind,ref,sha256,size FROM refs WHERE event_id=? ORDER BY rowid", (event_id,))
        inputs = [{key: value for key, value in zip(REF_KEYS, values) if value is not None} for values in refs]
        return self.event(event_id), inputs

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

    def test_prev_hash_is_in_canonical_digest(self):
        arguments = {"trace_id": "t", "duration_ms": 7,
                     "inputs": [events.ref("policy", "main@a1b2c3d", sha256="h" * 64, size=3)],
                     "outputs": {"n": 1}}
        first = events.emit("verify", "lint", "ok", **arguments)
        second = events.emit("verify", "lint", "ok", **arguments)
        self.assertIsInstance(first, int)
        self.assertIsInstance(second, int)
        row1, inputs1 = self.row_with_refs(first)
        row2, inputs2 = self.row_with_refs(second)
        # 冻结时钟下两行除 id、seq、prev_hash、hash 外逐列相同
        for key in row1:
            if key not in ("id", "seq", "prev_hash", "hash"):
                self.assertEqual(row1[key], row2[key], key)
        self.assertEqual((row1["seq"], row1["prev_hash"]), (1, ""))
        self.assertEqual(row2["prev_hash"], row1["hash"])
        # 独立计算的规范化摘要与库一致（prev_hash 在摘要内）
        self.assertEqual(canonical_digest(row1, inputs1), row1["hash"])
        self.assertEqual(canonical_digest(row2, inputs2), row2["hash"])
        self.assertNotEqual(row1["hash"], row2["hash"])
        # 相同内容仅 prev_hash 不同 → 独立摘要不同（去掉 prev_hash 的变异在这里失配）
        variant_a, variant_b = dict(row2), dict(row2)
        variant_a["prev_hash"] = "a" * 64
        variant_b["prev_hash"] = "b" * 64
        self.assertNotEqual(canonical_digest(variant_a, inputs2), canonical_digest(variant_b, inputs2))
        self.assertNotEqual(canonical_digest(variant_a, inputs2), row2["hash"])
        self.assertEqual(events.verify_chain("t"), [])

    def test_seq_gap_with_valid_hashes_is_detected(self):
        ids = [events.emit("verify", f"s{index}", "ok", trace_id="t") for index in range(3)]
        self.assertEqual(events.verify_chain("t"), [])
        row1, _ = self.row_with_refs(ids[0])
        row3, inputs3 = self.row_with_refs(ids[2])
        rewritten = {**row3, "prev_hash": row1["hash"]}
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:  # 人为构造 seq 1/3 且哈希合法
            conn.execute("DELETE FROM refs WHERE event_id=?", (ids[1],))
            conn.execute("DELETE FROM events WHERE id=?", (ids[1],))
            conn.execute("UPDATE events SET prev_hash=? WHERE id=?", (row1["hash"], ids[2]))
            conn.execute("UPDATE events SET hash=? WHERE id=?", (canonical_digest(rewritten, inputs3), ids[2]))
            conn.commit()
        problems = events.verify_chain("t")
        self.assertEqual(len(problems), 1, problems)  # 删掉 seq 检查的变异在此返回空列表
        self.assertIn("seq", problems[0])
        self.assertNotIn("prev_hash", problems[0])  # 不是靠 prev_hash/哈希错误兜底
        self.assertNotIn("内容与 hash 不符", problems[0])

    def test_step_span_refs_and_input_io_fail_safe(self):
        # 非法 step（超长、换行、形似本机路径）：整条不写
        for bad_step in ("x" * 121, "a\nb", "/Users/someone/file", "C:\\Users\\x"):
            self.assertIsNone(events.emit("guard", bad_step, "ok", trace_id="t"))
        self.assertFalse(self.db_path.exists())
        self.assertIsInstance(events.emit("guard", "ok-step", "ok", trace_id="t"), int)

        # span 的 fixed 未知键：不传播、不遮盖业务异常，事件仍记录
        with self.assertRaises(RuntimeError), \
                events.span("guard", "hook", trace_id="t", bogus_key="x", another=1) as holder:
            holder.outputs = {"n": 1}
            raise RuntimeError("boom")
        failed = self.event(self.rows("SELECT MAX(id) FROM events")[0][0])
        self.assertEqual((failed["status"], failed["error_kind"]), ("error", "RuntimeError"))
        self.assertEqual(json.loads(failed["outputs"]), {"n": 1})
        with events.span("verify", "lint", trace_id="t", nope=2):  # 成功路径同样不因未知键报错
            pass

        # 全被过滤的引用：不入 refs 表、不进哈希，计入 redacted；部分过滤的仍保留合法列
        event_id = events.emit("ci", "check", "ok", trace_id="t",
                               inputs=[{}, "junk", {"sha256": None}, {"unknown_key": "x"},
                                       events.ref("policy", "main@a1b2c3d")])
        self.assertIsInstance(event_id, int)
        self.assertEqual(self.rows("SELECT kind,ref,sha256,size FROM refs WHERE event_id=?", (event_id,)),
                         [("policy", "main@a1b2c3d", None, None)])
        self.assertEqual(self.event(event_id)["redacted"], 4)
        self.assertEqual(events.verify_chain("t"), [])

        # store_artifact 的 Path 读取/bytes 转换失败：不传播、返回 None、无产物
        artifacts = events_db.artifacts_dir()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertIsNone(events.store_artifact(self.tmp / "missing.bin"))
            self.assertIsNone(events.store_artifact("not-bytes"))
            with self.assertRaises(RuntimeError), \
                    events.span("ci", "pack", trace_id="t") as holder:  # 观察失败也不遮盖业务异常
                holder.inputs = [events.store_artifact(self.tmp / "missing.bin")]
                raise RuntimeError("boom2")
        self.assertEqual(stderr.getvalue().splitlines(), [WARN_LINE])
        pack = self.event(self.rows("SELECT MAX(id) FROM events")[0][0])
        self.assertEqual((pack["step"], pack["status"], pack["error_kind"]), ("pack", "error", "RuntimeError"))
        self.assertEqual((pack["redacted"], self.rows(
            "SELECT COUNT(*) FROM refs WHERE event_id=?", (pack["id"],))[0][0]), (1, 0))
        self.assertTrue(not artifacts.exists() or not any(artifacts.iterdir()))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM artifacts")[0][0], 0)
        self.assertEqual(events.verify_chain("t"), [])

    def test_step_written_value_is_stripped(self):
        """T110：emit 校验与写入用同一 step 值——首尾空白（含换行）strip 后合法并写清洗值，
        内部换行仍整条不写，「非法整条不写、合法写清洗值」在边界一致。"""
        event_id = events.emit("guard", " ok \n", "ok", trace_id="t")
        self.assertIsInstance(event_id, int)
        self.assertEqual(self.event(event_id)["step"], "ok")
        self.assertIsNone(events.emit("guard", "a\nb", "ok", trace_id="t"))  # 内部换行整条不写
        self.assertEqual(self.rows("SELECT COUNT(*) FROM events WHERE trace_id=?", ("t",))[0][0], 1)
        self.assertEqual(events.verify_chain("t"), [])

    def test_artifact_atomicity_and_newer_schema_read(self):
        artifacts = events_db.artifacts_dir()
        digest = hashlib.sha256(b"payload").hexdigest()
        real_replace = os.replace

        def broken_replace(src, dst, *args, **kwargs):
            if Path(dst).parent == artifacts:  # 只在替换产物时注入失败
                raise OSError("injected")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch.object(events_db.os, "replace", broken_replace), contextlib.redirect_stderr(io.StringIO()):
            entry = events.store_artifact(b"payload")
        self.assertEqual(entry["ref"], digest)
        self.assertEqual([p.name for p in artifacts.iterdir()], [])  # 无半文件、无临时残留
        self.assertFalse(self.db_path.exists())  # 登记未发生（库向未建立）
        self.assertEqual(events.store_artifact(b"payload"), entry)  # 注入解除后重试成功
        self.assertEqual((artifacts / digest).read_bytes(), b"payload")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM artifacts")[0][0], 1)

        # 双进程并发存同一产物：最终内容完整、无临时残留、登记仅一行
        script = self.runner_script("""
            for _ in range(20):
                assert events.store_artifact(b"shared") is not None
        """)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        procs = [subprocess.Popen([sys.executable, "-c", script], cwd=ENGINE_REPO,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env={**env, "PYTHONPATH": str(ENGINE_REPO), **GIT_ENV})
                 for _ in range(2)]
        for proc in procs:
            _, stderr = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, stderr)
        shared_digest = hashlib.sha256(b"shared").hexdigest()
        self.assertEqual((artifacts / shared_digest).read_bytes(), b"shared")
        self.assertEqual(sorted(p.name for p in artifacts.iterdir()), sorted([digest, shared_digest]))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM artifacts WHERE sha256=?", (shared_digest,))[0][0], 1)

        # 新版库：链校验先看版本，明确报告不可校验，不修改、不当作完好
        self.assertIsInstance(events.emit("guard", "x", "ok", trace_id="t"), int)
        self.assertEqual(events.verify_chain("t"), [])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA user_version=9")
            conn.commit()
        before = self.db_path.read_bytes()
        for problems in (events.verify_chain("t"), events.verify_chain()):
            self.assertTrue(problems)
            self.assertTrue(any("不可校验" in problem for problem in problems), problems)
            self.assertEqual(self.db_path.read_bytes(), before)

    def test_observation_failures_do_not_change_business_outcome(self):
        """C0：事件开启、关闭、写入失败三态下，业务 stdout、退出码与业务 stderr 逐字相同。

        只排除 T101 固定的一次写入失败提示；stderr 其余差异不允许。
        """
        body = """
            events.emit("verify", "warm", "ok", trace_id="t")
            print("out:business")
            with events.span("verify", "step", trace_id="t") as holder:
                holder.outputs = {"n": 2}
                print("in-span")
                raise SystemExit(7)
        """
        os.environ["HARNESS_EVENTS"] = "off"  # 三态之一：关闭
        off = self.run_py(body)
        del os.environ["HARNESS_EVENTS"]  # 三态之二：开启且库健康
        on = self.run_py(body)
        self.assertTrue(self.db_path.exists())
        self.db_path.unlink()  # 三态之三：写入失败（库路径被目录占用）
        self.db_path.mkdir(parents=True)
        try:
            broken = self.run_py(body)
        finally:
            shutil.rmtree(self.db_path)
        self.assertEqual((off.returncode, on.returncode, broken.returncode), (7, 7, 7))
        self.assertEqual(off.stdout, on.stdout)
        self.assertEqual(broken.stdout, on.stdout)
        self.assertEqual((off.stderr, on.stderr), ("", ""))
        self.assertEqual(broken.stderr.splitlines(), [WARN_LINE])  # 仅固定的一次提示
        self.assertEqual(on.stdout, "out:business\nin-span\n")

    def test_ci_run_attempt_job_isolates_chains(self):
        def ci_env(run_id=None, attempt=None, job=None):
            os.environ["CI"] = "true"
            for key in GITHUB_KEYS:
                os.environ.pop(key, None)
            if run_id is not None:
                os.environ["GITHUB_RUN_ID"] = run_id
            if attempt is not None:
                os.environ["GITHUB_RUN_ATTEMPT"] = attempt
            if job is not None:
                os.environ["GITHUB_JOB"] = job

        ci_env(run_id="111", attempt="1", job="j1")
        self.assertEqual(events.default_source(), "ci:111:1:j1")
        a1 = events.emit("ci", "check", "ok", trace_id="t")
        ci_env(run_id="222", attempt="1", job="j1")  # 两个 run
        self.assertEqual(events.default_source(), "ci:222:1:j1")
        b1 = events.emit("ci", "check", "ok", trace_id="t")
        ci_env(run_id="111", attempt="2", job="j1")  # 同 run 两次 attempt
        self.assertEqual(events.default_source(), "ci:111:2:j1")
        c1 = events.emit("ci", "check", "ok", trace_id="t")
        ci_env(run_id="111", attempt="1", job="j2")  # 同 run 同 attempt 的另一个 job
        self.assertEqual(events.default_source(), "ci:111:1:j2")
        d1 = events.emit("ci", "check", "ok", trace_id="t")
        ci_env(run_id="111", attempt="1")  # 缺 job 键：兼容回退 ci
        self.assertEqual(events.default_source(), "ci")
        ci_env()  # 无 Actions 三键的 CI：兼容返回 ci
        self.assertEqual(events.default_source(), "ci")
        e1 = events.emit("ci", "check", "ok", trace_id="t")
        os.environ.pop("CI", None)  # local
        self.assertEqual(events.default_source(), "local")
        l1 = events.emit("ci", "local-check", "ok", trace_id="t")

        heads = {}
        for event_id, expected_source in {a1: "ci:111:1:j1", b1: "ci:222:1:j1", c1: "ci:111:2:j1",
                                          d1: "ci:111:1:j2", e1: "ci", l1: "local"}.items():
            row = self.event(event_id)
            self.assertEqual(row["source"], expected_source)
            self.assertEqual((row["seq"], row["prev_hash"]), (1, ""))  # 每条来源链独立从 seq=1 开始
            self.assertEqual(row["actor_host"], "ci" if expected_source.startswith("ci") else "local")
            self.assertEqual(row["actor_role"], "engine")
            heads[expected_source] = row["hash"]
        self.assertEqual(len(set(heads.values())), len(heads))  # 来源隔离使同内容摘要互不相同
        self.assertEqual(events.chain_head("t", source="ci:222:1:j1"), heads["ci:222:1:j1"])

        ci_env(run_id="111", attempt="1", job="j1")  # 回到第一条链：seq=2 且 prev_hash 接上
        a2 = events.emit("ci", "recheck", "fail", trace_id="t")
        row_a2 = self.event(a2)
        self.assertEqual((row_a2["source"], row_a2["seq"]), ("ci:111:1:j1", 2))
        self.assertEqual(row_a2["prev_hash"], heads["ci:111:1:j1"])
        self.assertNotEqual(row_a2["hash"], heads["ci:111:1:j1"])

        explicit = events.emit("ci", "explicit", "ok", trace_id="t", source="ci")  # 显式 source 原样保留
        row_explicit = self.event(explicit)
        self.assertEqual((row_explicit["source"], row_explicit["seq"], row_explicit["actor_host"]),
                         ("ci", 2, "ci"))
        self.assertEqual(events.verify_chain(), [])
