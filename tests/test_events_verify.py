"""T102 埋点测试：cli 分派边界、verify 逐项与汇总、integrity 内容事件与失败隔离。

验收调用真实产品入口（cli.main、verify.main），只隔离下游副作用（检查清单、git 位置、命令实现），
不 mock 包装层与 emit。夹具全部用匿名临时 git 仓库、隔离 ROOT 与冻结时钟，不碰真实库、PR 或工作流。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import cli
from engine.checks import integrity, verify
from engine.checks.verify import ALL_TIERS, Check
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
FROZEN_TS = "2026-01-02T03:04:05.678Z"
# T101 固定的一次写入失败提示（逐字）：三态比对只允许这一行、至多一次的差异。
WRITE_WARN_LINE = "harness：事件写入失败，已跳过（不影响本次运行）\n"
# C1 的阶段映射与例外（测试独立编码，不读 cli 的表，防止实现与断言同源）。
EXPECTED_STAGE = {
    "verify": "verify", "integrity": "verify",
    "risk": "route", "policy": "route",
    "dispatch": "dispatch",
    "review": "review", "review-pack": "review", "review-plan": "review",
}
QUIET_COMMANDS = {"guard-command", "guard-git"}


class _CorruptedCore:
    """模拟引擎副本损坏的 engine.core：包在，但加载不出任何子模块。"""

    __name__ = "engine.core"

    def __getattr__(self, name):
        raise ImportError(f"engine copy corrupted: cannot load {name}")


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-verify-"))
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
        clock = mock.patch.object(events_db, "_now", return_value=FROZEN_TS)
        clock.start()
        self.addCleanup(clock.stop)
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_STEP_SUMMARY"):
            os.environ.pop(key, None)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    # ---------- 夹具与查询 ----------

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and not k.startswith("HARNESS_")}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def rows(self, sql, params=()):
        if not self.db_path.exists():  # 首次 emit 前库尚未建立：没有任何事件
            return []
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def event_count(self) -> int:
        return self.rows("SELECT COUNT(*) FROM events")[0][0] if self.db_path.exists() else 0

    def max_event_id(self) -> int:
        return self.rows("SELECT COALESCE(MAX(id), 0) FROM events")[0][0] if self.db_path.exists() else 0

    def events_after(self, min_id: int) -> list[dict]:
        return [dict(zip(EVENT_COLUMNS, row)) for row in
                self.rows("SELECT * FROM events WHERE id>? ORDER BY id", (min_id,))]

    def input_refs(self, event_id: int) -> list[tuple]:
        return self.rows("SELECT kind,ref,sha256,size FROM refs WHERE event_id=? AND direction='in' ORDER BY rowid",
                         (event_id,))

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_verify_fixture(self, checks=None):
        """隔离 verify 的项目面：检查清单、平台、日志目录与 git 位置都指到临时仓库。"""
        if checks is None:
            checks = [
                Check("alpha", ALL_TIERS, func=lambda: (True, "正常")),
                Check("beta", ALL_TIERS, func=lambda: (False, "坏了")),
                Check("gamma", ALL_TIERS, requires="macos", why_skipped="仅 macOS"),
                Check("delta", ALL_TIERS, func=lambda: (True, "正常")),
            ]
        real_git = verify.git
        self.patch(verify, "LOG_DIR", self.repo / "build" / "verify")
        self.patch(verify, "ROOT", self.repo)
        self.patch(verify, "build_checks", lambda strict=False: list(checks))
        self.patch(verify, "_is_macos", lambda: False)
        self.patch(verify, "git", lambda *args, **kwargs: real_git(*args, **{**kwargs, "cwd": self.repo}))

    def install_integrity_fixture(self):
        """临时业务仓库布局：.harness/engine/ + engine.lock，并提交进 git（变更计数以 HEAD 为基线）。"""
        engine_dir = self.repo / ".harness" / "engine"
        (engine_dir / "core").mkdir(parents=True)
        (engine_dir / "a.py").write_text("x = 1\n")
        (engine_dir / "core" / "b.py").write_text("y = 2\n")
        lock_file = self.repo / ".harness" / "engine.lock"
        integrity.write_lock(lock_file, "0.1.0", "a1b2c3d4", integrity.tree_hash(engine_dir))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "engine copy")
        self.patch(integrity, "ENGINE_DIR", engine_dir)
        self.patch(integrity, "LOCK_FILE", lock_file)
        return engine_dir, lock_file

    def run_verify(self, argv: list[str]):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = verify.main(argv)
        return code, out.getvalue()

    def run_cli(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue()

    def run_cli_full(self, *args: str):
        """三态运行：返回（返回码、stdout、stderr）。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def run_verify_full(self, argv: list[str]):
        """三态运行 verify：返回（返回码、stdout、stderr）。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = verify.main(argv)
        return code, out.getvalue(), err.getvalue()

    def stderr_without_warn(self, stderr: str) -> str:
        """去掉至多一行的 T101 固定提示后的 stderr（固定提示超过一次即不合格）。"""
        self.assertLessEqual(stderr.count(WRITE_WARN_LINE), 1, stderr)
        return stderr.replace(WRITE_WARN_LINE, "")

    def assert_three_state_equal(self, on, off):
        """观察开启与关闭的三态等价：stdout、stderr、返回码分别比对（stderr 仅容 T101 固定提示行）。"""
        self.assertEqual(on[0], off[0])
        self.assertEqual(on[1], off[1])
        self.assertEqual(self.stderr_without_warn(on[2]), self.stderr_without_warn(off[2]))

    def head_sha(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    # ---------- 验收 1：四种入口行为保持且有 cli 事件 ----------

    def test_cli_preserves_return_and_exceptions(self):
        from engine.checks import hygiene

        def raises(exc):
            def stub(rest):
                raise exc
            return stub

        scenarios = [
            ("成功", lambda rest: 0, 0, None, None, "ok", 0, None),
            ("返回非零", lambda rest: 3, 3, None, None, "fail", 3, None),
            ("SystemExit", raises(SystemExit(2)), None, SystemExit, 2, "fail", 2, "SystemExit"),
            ("业务异常", raises(RuntimeError("boom")), None, RuntimeError, "boom", "error", None, "RuntimeError"),
        ]
        for label, stub, expected_return, exc_type, exc_detail, status, code, error_kind in scenarios:
            with self.subTest(label):
                before = self.max_event_id()
                with mock.patch.object(hygiene, "main", stub):
                    if exc_type is None:
                        self.assertEqual(cli.main(["hygiene"]), expected_return)
                    else:
                        with self.assertRaises(exc_type) as caught:
                            cli.main(["hygiene"])
                        if exc_type is SystemExit:
                            self.assertEqual(caught.exception.code, exc_detail)
                        else:
                            self.assertEqual(str(caught.exception), exc_detail)
                rows = self.events_after(before)
                self.assertEqual([row["step"] for row in rows], ["cli.hygiene"])
                row = rows[0]
                self.assertEqual((row["stage"], row["status"], row["error_kind"]), ("ci", status, error_kind))
                self.assertEqual(json.loads(row["outputs"]), {"code": code})
                self.assertIsInstance(row["duration_ms"], int)
                self.assertGreaterEqual(row["duration_ms"], 0)

        # 帮助与未知命令：不记录任何事件，返回码不变。
        before = self.max_event_id()
        self.assertEqual(self.run_cli("")[0], 2)
        self.assertEqual(self.run_cli("-h")[0], 0)
        self.assertEqual(self.run_cli("--help")[0], 0)
        self.assertEqual(self.run_cli("nope")[0], 2)
        self.assertEqual(self.events_after(before), [])

    # ---------- 验收 2：verify 逐项事件、真实退出码与唯一汇总 ----------

    def test_verify_each_check_and_summary(self):
        self.install_verify_fixture()
        # 一轮：通过、失败、平台 skip、--skip；退出码 1。
        before = self.max_event_id()
        code, _ = self.run_verify(["--skip", "delta"])
        self.assertEqual(code, 1)
        by_step = {row["step"]: row for row in self.events_after(before)}
        self.assertEqual(set(by_step), {"verify.alpha", "verify.beta", "verify.gamma", "verify.delta", "verify.summary"})
        head = self.head_sha()
        head_input = ("head", head, None, None)
        tier_input = ("tier", "default", None, None)

        alpha = by_step["verify.alpha"]
        self.assertEqual((alpha["stage"], alpha["status"]), ("verify", "ok"))
        self.assertEqual(json.loads(alpha["outputs"]),
                         {"exit": 0, "signature": None,
                          "log.sha256": self._log_digest("alpha"), "log.size": self._log_size("alpha"),
                          "log.ref": self._log_digest("alpha")})
        self.assertEqual(self.input_refs(alpha["id"]), [head_input, tier_input])
        self.assertIsInstance(alpha["duration_ms"], int)
        self.assertGreaterEqual(alpha["duration_ms"], 0)

        beta = by_step["verify.beta"]
        self.assertEqual(beta["status"], "fail")
        self.assertEqual(json.loads(beta["outputs"]),
                         {"exit": 1, "signature": "退出码 1",
                          "log.sha256": self._log_digest("beta"), "log.size": self._log_size("beta"),
                          "log.ref": self._log_digest("beta")})

        gamma = by_step["verify.gamma"]
        self.assertEqual(gamma["status"], "skip")
        self.assertEqual(json.loads(gamma["outputs"]), {"exit": None, "signature": "仅 macOS"})

        delta = by_step["verify.delta"]
        self.assertEqual(delta["status"], "skip")
        self.assertEqual(json.loads(delta["outputs"]), {"exit": None, "signature": "--skip 手动跳过"})

        summary = by_step["verify.summary"]
        self.assertEqual((summary["stage"], summary["status"]), ("verify", "fail"))
        self.assertEqual(json.loads(summary["outputs"]), {"tier": "default", "ok": False, "dirty": False})
        self.assertEqual(self.input_refs(summary["id"]), [head_input, tier_input])
        self.assertEqual(self._summary_count(), 1)

        # strict：平台 skip 转换为 fail 后再记事件。
        before = self.max_event_id()
        code, _ = self.run_verify(["--strict", "--only", "gamma"])
        self.assertEqual(code, 1)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["verify.gamma", "verify.summary"])
        self.assertEqual(rows[0]["status"], "fail")
        self.assertEqual(json.loads(rows[0]["outputs"]),
                         {"exit": None, "signature": "--strict 下不允许跳过（仅 macOS）"})
        self.assertEqual(json.loads(rows[1]["outputs"])["ok"], False)
        self.assertEqual(self._summary_count(), 2)

        # 通过的一轮：退出码 0，汇总 ok。
        before = self.max_event_id()
        code, _ = self.run_verify(["--only", "alpha"])
        self.assertEqual(code, 0)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["verify.alpha", "verify.summary"])
        self.assertEqual(json.loads(rows[1]["outputs"]), {"tier": "default", "ok": True, "dirty": False})
        self.assertEqual(self._summary_count(), 3)

    def _log_digest(self, name: str) -> str:
        return hashlib.sha256((self.repo / "build" / "verify" / f"{name}.log").read_bytes()).hexdigest()

    def _log_size(self, name: str) -> int:
        return (self.repo / "build" / "verify" / f"{name}.log").stat().st_size

    def _summary_count(self) -> int:
        return self.rows("SELECT COUNT(*) FROM events WHERE step='verify.summary'")[0][0]

    # ---------- 验收 3：integrity 失败元数据 ----------

    def test_integrity_failure_metadata(self):
        engine_dir, lock_file = self.install_integrity_fixture()
        locked_tree = integrity.tree_hash(engine_dir)
        lock_bytes = lock_file.read_bytes()
        # 成功副本：内容事件 ok，changed 为 0，lock 引用按原始字节核对。
        before = self.max_event_id()
        code, out = self.run_cli("integrity")
        self.assertEqual(code, 0)
        self.assertIn("一致", out)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["integrity", "cli.integrity"])
        content = rows[0]
        self.assertEqual((content["stage"], content["status"], content["error_kind"]), ("verify", "ok", None))
        outputs = json.loads(content["outputs"])
        self.assertEqual(outputs["changed"], 0)
        self.assertEqual(outputs["files"], 2)
        self.assertEqual(outputs["tree"], locked_tree)
        self.assertEqual(outputs["lock.tree"], locked_tree)
        self.assertEqual(self.input_refs(content["id"]),
                         [("lock", ".harness/engine.lock", hashlib.sha256(lock_bytes).hexdigest(), len(lock_bytes))])
        # 篡改一个文件：原检查失败，事件含 mismatch、lock 哈希与变更文件计数。
        (engine_dir / "core" / "b.py").write_text("y = 999\n")
        before = self.max_event_id()
        code, out = self.run_cli("integrity")
        self.assertEqual(code, 1)
        self.assertIn("不一致", out)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["integrity", "cli.integrity"])
        content = rows[0]
        self.assertEqual((content["status"], content["error_kind"], content["error_signature"]),
                         ("fail", "integrity_mismatch", "tree_mismatch"))
        outputs = json.loads(content["outputs"])
        self.assertEqual(outputs["changed"], 1)
        self.assertEqual(outputs["files"], 2)
        self.assertNotEqual(outputs["tree"], outputs["lock.tree"])
        self.assertEqual(outputs["lock.tree"], locked_tree)
        self.assertEqual(self.input_refs(content["id"])[0][2], hashlib.sha256(lock_bytes).hexdigest())
        wrapper = rows[1]
        self.assertEqual((wrapper["stage"], wrapper["status"]), ("verify", "fail"))

    # ---------- 验收 4：引用读取与产物保存失败不影响原判定 ----------

    def test_reference_failure_keeps_check_result(self):
        def boom(*args, **kwargs):
            raise OSError("disk on fire")

        # integrity：file_ref 抛 OSError 时，stdout、stderr 与返回码和事件关闭时逐字一致。
        self.install_integrity_fixture()
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}):
            off = self.run_cli_full("integrity")
        with mock.patch.object(events, "file_ref", boom):
            on = self.run_cli_full("integrity")
        self.assert_three_state_equal(on, off)

        # verify：store_artifact 抛 OSError 时同样一致。
        self.install_verify_fixture()
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}):
            off = self.run_verify_full(["--skip", "delta"])
        with mock.patch.object(events, "store_artifact", boom):
            on = self.run_verify_full(["--skip", "delta"])
        self.assert_three_state_equal(on, off)

        # 检查自身的业务异常不被观察层遮盖：开与关都抛同一异常。
        def exploding():
            raise ValueError("check exploded")

        self.install_verify_fixture([Check("boom-check", ALL_TIERS, func=exploding)])
        for label, env in (("off", {"HARNESS_EVENTS": "off"}), ("on", {})):
            with self.subTest(label), mock.patch.dict(os.environ, env), \
                    self.assertRaises(ValueError) as caught:
                self.run_verify([])
            self.assertEqual(str(caught.exception), "check exploded")

    # ---------- 验收 4b：events 导入失败时入口观察静默丢弃，原行为逐字不变 ----------

    def test_record_dispatch_import_failure_preserves_outcome(self):
        """events 延迟导入失败（如引擎副本损坏）：观察静默丢弃，

        原 SystemExit/业务异常/返回码与 stderr 逐字不变（与事件关闭时的三态逐项比对）。
        """
        from engine.checks import hygiene

        def raises(exc):
            def stub(rest):
                raise exc
            return stub

        fake_core = _CorruptedCore()  # engine.core 在但加载不出 events：from-import 抛 ImportError

        def run(stub, *, broken: bool):
            """broken 时用坏包替换 sys.modules 里的 engine.core，否则用 HARNESS_EVENTS=off 作基线。"""
            out, err = io.StringIO(), io.StringIO()
            code, caught = None, None
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                guard = mock.patch.dict(sys.modules, {"engine.core": fake_core}) if broken \
                    else mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"})
                with guard, mock.patch.object(hygiene, "main", stub):
                    try:
                        code = cli.main(["hygiene"])
                    except BaseException as exc:  # noqa: BLE001  SystemExit 与业务异常都原样接住再比对
                        caught = exc
            return code, out.getvalue(), err.getvalue(), caught

        # 夹具有效性：from-import 的 IMPORT_FROM 首步（包属性加载）确实抛 ImportError。
        def load_events():
            return fake_core.events

        with self.assertRaises(ImportError):
            load_events()

        scenarios = [
            ("返回非零", lambda rest: 3, 3, None, None),
            ("SystemExit", raises(SystemExit(2)), None, SystemExit, 2),
            ("业务异常", raises(RuntimeError("boom")), None, RuntimeError, "boom"),
        ]
        for label, stub, expected_return, exc_type, exc_detail in scenarios:
            with self.subTest(label):
                off = run(stub, broken=False)
                before = self.max_event_id()
                broken = run(stub, broken=True)
                if exc_type is None:
                    self.assertEqual(broken[0], expected_return)
                    self.assertIsNone(off[3])
                else:
                    self.assertIsNone(broken[0])
                    for state in (off, broken):
                        self.assertIs(type(state[3]), exc_type)
                        if exc_type is SystemExit:
                            self.assertEqual(state[3].code, exc_detail)
                        else:
                            self.assertEqual(str(state[3]), exc_detail)
                self.assertEqual(broken[1], off[1])  # stdout 逐字一致
                self.assertEqual(broken[2], off[2])  # stderr 逐字一致（无导入失败提示）
                self.assertEqual(self.max_event_id(), before)  # 没有事件写入

    # ---------- 验收 5：逐个入口核对 C1 映射与守卫/查询例外 ----------

    def test_all_registered_entrypoint_mappings_and_guard_exceptions(self):
        def stub(rest):
            return 7

        old_argv = sys.argv
        try:
            for name, target in cli.COMMANDS.items():
                module_name, _, function = target.partition(":")
                module = importlib.import_module(module_name)
                entry = function or "main"
                before = self.max_event_id()
                if hasattr(module, entry):
                    with mock.patch.object(module, entry, stub):
                        self.assertEqual(cli.main([name]), 7, name)
                    rows = self.events_after(before)
                    if name in QUIET_COMMANDS:
                        self.assertEqual(rows, [], name)
                        continue
                    self.assertEqual(len(rows), 1, name)
                    row = rows[0]
                    self.assertEqual(row["status"], "fail", name)
                    self.assertEqual(json.loads(row["outputs"]), {"code": 7}, name)
                else:
                    # 注册了但模块没有入口（现状 r1）：业务异常原样重抛，仍有一条入口事件。
                    with self.assertRaises(AttributeError, msg=name):
                        cli.main([name])
                    rows = self.events_after(before)
                    self.assertEqual(len(rows), 1, name)
                    row = rows[0]
                    self.assertEqual((row["status"], row["error_kind"]), ("error", "AttributeError"), name)
                self.assertEqual(row["step"], f"cli.{name}", name)
                self.assertEqual(row["stage"], EXPECTED_STAGE.get(name, "ci"), name)
        finally:
            sys.argv = old_argv

        # install/upgrade/identity/weekly 各明确恰有一次入口事件（阶段归 ci）。
        for name in ("install", "upgrade", "identity", "weekly"):
            count = self.rows("SELECT COUNT(*) FROM events WHERE step=?", (f"cli.{name}",))[0][0]
            self.assertEqual(count, 1, name)

        # guard 放行不逐条记：真实调用两个守卫入口，事件必须为零。
        before = self.max_event_id()
        with mock.patch.object(sys, "stdin", io.StringIO('{"command": "git status --porcelain"}')):
            code, _ = self.run_cli("guard-command", "--format", "json", "--role", "designer")
            self.assertEqual(code, 0)
        code, _ = self.run_cli("guard-git", "status")  # 返回码随环境，不产生事件
        self.assertEqual(self.events_after(before), [])

        # events/trace 查询不追加自身事件（命令尚未注册，按合同注册后必须落在静默集合）。
        from engine.checks import hygiene
        before = self.max_event_id()
        with mock.patch.dict(cli.COMMANDS, {"events": "engine.checks.hygiene", "trace": "engine.checks.hygiene"}), \
                mock.patch.object(hygiene, "main", stub):
            self.assertEqual(self.run_cli("events")[0], 7)
            self.assertEqual(self.run_cli("trace")[0], 7)
        rows = self.events_after(before)
        self.assertEqual(rows, [])

        # 每个已注册命令恰有一条事件（静默的 guard 除外），没有多余事件。
        self.assertEqual(self.event_count(), len(cli.COMMANDS) - len(QUIET_COMMANDS & set(cli.COMMANDS)))


if __name__ == "__main__":
    unittest.main()
