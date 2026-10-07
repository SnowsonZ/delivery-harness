"""pre-push 的轻量档（verify --push）：

- 档位成员：push = quick + 标了 push 的检查；不含全量测试与回放，且和本仓库实际清单对得上；
- `verify --push` 真实入口只跑属于该档的检查；
- 钩子按受信任规则（origin/main 的 rules.toml）[guard] pre_push_tier 选档：只有明确写 "push" 才用轻量档，
  缺省、写错、类型不对一律按默认档；只删分支的推送也走同一条规则；拒绝逻辑不受影响。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.checks import verify
from engine.checks.verify import ALL_TIERS, Check
from engine.core import events_db
from engine.guards import git_guard
from tests.test_prepush_reuse import GIT_ENV, LOCK_TREE, RULES_TOML, ZERO_SHA, write_lock

ENGINE_REPO = Path(__file__).resolve().parents[1]


class TierMembershipTest(unittest.TestCase):
    def test_push_tier_is_quick_plus_marked_checks(self):
        def names(tier):
            return {c.name for c in verify.builtin_checks() if verify.in_tier(c, tier)}

        self.assertEqual(names("push"), {"tools", "integrity", "hygiene", "quality", "docs", "acceptance", "taskbook"})
        self.assertNotIn("replay", names("push"))
        self.assertTrue(names("quick") <= names("push") <= names("default"))  # 轻量档夹在快速档与默认档之间
        self.assertEqual(names("default") | {"replay"}, names("full"))  # 既有两档不变

    def test_project_checks_join_push_when_marked_quick_or_push(self):
        quick = Check("lint", ("quick", "default", "full"))
        pushed = Check("fast-extra", ("push", "default", "full"))
        slow = Check("tests", ("default", "full"))
        full_only = Check("deep", ("full",))
        self.assertEqual([c.name for c in (quick, pushed, slow, full_only) if verify.in_tier(c, "push")],
                         ["lint", "fast-extra"])
        self.assertTrue(all(verify.in_tier(c, t) for c in (quick,) for t in ("quick", "default", "full")))
        self.assertFalse(verify.in_tier(slow, "quick"))

    def test_repo_checks_toml_keeps_tests_out_of_push(self):
        # 本仓库：lint 标了 quick（进轻量档），tests 只在 default/full（不进）
        names = {c.name for c in verify.build_checks() if verify.in_tier(c, "push")}
        self.assertIn("lint", names)
        self.assertNotIn("tests", names)
        self.assertNotIn("replay", names)


class PrepushTierTest(unittest.TestCase):
    def make_repo(self, guard_extra: str = "") -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-prepush-tier-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        config = self.repo / ".harness" / "config"
        config.mkdir(parents=True)
        (config / "rules.toml").write_text(RULES_TOML + guard_extra, encoding="utf-8")
        write_lock(self.repo, LOCK_TREE)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        origin = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", ".", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("fetch", "-q", "origin")
        for patcher in (mock.patch.object(events_db, "ROOT", self.repo), mock.patch.dict(os.environ)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in list(os.environ):
            if key.startswith(("HARNESS_", "GIT_", "GITHUB_")) or key == "CI":
                os.environ.pop(key, None)

    def git(self, *args: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=True)

    def pre_push(self, line: str) -> tuple[int, list, str]:
        calls: list = []

        def fake_run_verify(repo, *args):
            calls.append(args)
            return 0

        err = io.StringIO()
        with mock.patch.object(git_guard, "_run_verify", fake_run_verify), contextlib.redirect_stderr(err):
            code = git_guard.cmd_pre_push(self.repo, line + "\n")
        return code, calls, err.getvalue()

    def branch_push_line(self) -> str:
        self.git("checkout", "-q", "-b", "task/x")
        (self.repo / "note.txt").write_text("n\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "x")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        return f"refs/heads/task/x {sha} refs/heads/task/x {ZERO_SHA}"

    def test_only_explicit_push_selects_the_light_tier(self):
        for extra, expected in (('pre_push_tier = "push"\n', ("--push",)),
                                ("", ()),                              # 缺省：和以前一样
                                ('pre_push_tier = "default"\n', ()),
                                ('pre_push_tier = "full"\n', ()),      # 不认识的值一律按默认档
                                ('pre_push_tier = "PUSH"\n', ()),
                                ("pre_push_tier = 5\n", ()),
                                ("pre_push_tier = true\n", ())):
            with self.subTest(config=extra.strip() or "缺省"):
                self.make_repo(extra)
                code, calls, err = self.pre_push(self.branch_push_line())
                self.assertEqual(code, 0, err)
                self.assertEqual(calls, [expected])

    def test_branch_deletion_uses_the_same_rule_and_stays_cheap(self):
        self.make_repo('pre_push_tier = "push"\n')
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        code, calls, err = self.pre_push(f"(delete) {ZERO_SHA} refs/heads/task/old {sha}")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [("--push",)])  # 删分支不再等全量测试

    def test_denials_run_before_and_regardless_of_tier(self):
        self.make_repo('pre_push_tier = "push"\n')
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        code, calls, _err = self.pre_push(f"refs/heads/main {sha} refs/heads/main {sha}")  # 推保护分支
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])  # 拒绝在 verify 之前，不跑 verify
        code, calls, _err = self.pre_push(f"(delete) {ZERO_SHA} refs/heads/main {sha}")  # 删保护分支
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_rules_come_from_origin_main_not_the_worktree(self):
        # 工作区改成 push 不算：规则以 origin/main 为准（执行方可写工作区，不能靠改配置放松自己的钩子）
        self.make_repo("")
        rules = self.repo / ".harness" / "config" / "rules.toml"
        rules.write_text(rules.read_text(encoding="utf-8") + 'pre_push_tier = "push"\n', encoding="utf-8")
        self.git("checkout", "-q", "-b", "task/x")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        code, calls, err = self.pre_push(f"refs/heads/task/x {sha} refs/heads/task/x {ZERO_SHA}")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [()])


class VerifyPushEntryTest(unittest.TestCase):
    def test_push_flag_runs_exactly_the_push_tier(self):
        tmp = Path(tempfile.mkdtemp(prefix="dh-verify-push-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True, env={**os.environ, **GIT_ENV})
        subprocess.run(["git", "-C", str(tmp), "commit", "-q", "--allow-empty", "-m", "i"], check=True,
                       env={**os.environ, **GIT_ENV})
        ran: list[str] = []

        def make(name, tiers):
            def func():
                ran.append(name)
                return True, "ok"
            return Check(name, tiers, func=func)

        checks = [make("a-quick", ALL_TIERS), make("b-push", ("push", "default", "full")),
                  make("c-default", ("default", "full")), make("d-full", ("full",)),
                  make("e-quick-only", ("quick", "default", "full"))]  # 项目检查只标 quick（如 lint）：也属于轻量档
        real_git = verify.git
        patches = [mock.patch.object(verify, "LOG_DIR", tmp / "build" / "verify"),
                   mock.patch.object(verify, "ROOT", tmp),
                   mock.patch.object(verify, "build_checks", lambda strict=False: list(checks)),
                   mock.patch.object(verify, "git", lambda *a, **k: real_git(*a, **{**k, "cwd": tmp})),
                   mock.patch.object(events_db, "ROOT", tmp)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(["--push"]), 0)
        self.assertEqual(sorted(ran), ["a-quick", "b-push", "e-quick-only"])
        ran.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main([]), 0)
        self.assertEqual(sorted(ran), ["a-quick", "b-push", "c-default", "e-quick-only"])  # 默认档不变


if __name__ == "__main__":
    unittest.main()
