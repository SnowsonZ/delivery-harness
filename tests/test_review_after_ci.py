"""CI 通过后由派发进程接上独立评审（B77③，T703：dispatch.review_after_ci 与 review_with_chain）。

夹具沿用 tests/test_ci_workflows.py 的端到端模式：匿名临时 git 仓库（含匿名 bare 远端）、隔离
events_db.ROOT、假执行方宿主与假 verify；GitHub 子类把 wait_ci 打桩为立即通过/失败，评论查询
（gh pr view --json comments）按工厂应答（head 用 wait_ci 实际收到的值，标记才能命中当前 head；
comments_error=True 时抛 RuntimeError，覆盖读取失败按 missing 处理的路径），评审方用 mock 掉的
engine.agents.review.review_pr 或注入 review_with_chain 的假评审模块。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ENGINE_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_REPO))

from engine.agents import dispatch
from engine.agents import review as review_module
from engine.core import common, events_db

PR_NUMBER = 7

T005_TASKBOOK = """---
task: T005
class: K2
risk: R0
designer: claude-code
size: small
architecture: false
spec_refs: [DR14]
budget:
  wall_clock_min: 5
  ci_rounds: 1
  retries: 2
  tokens: null
rollback: git revert
---

# 任务：补一个测试

## 目标终态

DR14 有测试。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或验证步骤） |
|---|---|---|---|
| DR14 | 合并取较大值 | 单测 | `test_mod.Case` |
"""

T005_SPEC = "| 编号 | 内容 | 证据类型 | 覆盖 |\n|---|---|---|---|\n| DR14 | 合并 | 单测 | `test_mod` |\n"

T005_EXECUTOR = textwrap.dedent('''
    import subprocess, sys
    from pathlib import Path
    def commit(name, text):
        Path(name).write_text(text)
        subprocess.run(["git", "add", name], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "step\\n\\nTask: T005"], check=True)
    print('{"type": "session"}', flush=True)
    n = len(list(Path(".").glob("tests_new*.txt")))
    commit(f"tests_new{n}.txt", "ok")
''')

T005_ERROR_EXECUTOR = "import sys\nprint('{\"type\": \"session\"}')\nsys.exit(3)\n"

T005_VERIFY = "import sys\nprint('local verify ok')\n"

RULES_TOML = "[review]\nreviewer = \"opencode\"\n"

CHECKS_TOML = "[identity]\nagent_login = \"agent-bot\"\n"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


class FakeHost:
    """假执行方宿主：argv 启动夹具脚本；parse 返回固定用量，不触真实模型。"""

    name = "fake"

    def __init__(self, script: Path):
        self.script, self.prompts = script, []

    def version(self):
        return "fake 1.0"

    def argv(self, prompt, guard):
        self.prompts.append(prompt)
        return [sys.executable, str(self.script)]

    def parse(self, events):
        return "fake-model", {"input_tokens": 10, "output_tokens": 2}, {}


class ReviewGitHub(dispatch.GitHub):
    """端到端夹具的 GitHub：wait_ci 按脚本应答并记下 head，评论查询按工厂应答，写操作只记录不触网。"""

    def __init__(self, root, comments_factory=None, ci_ok=True, ci_summary="", comments_error=False):
        super().__init__(root)
        self.comments_factory = comments_factory or (lambda head: [])
        self.ci_ok, self.ci_summary, self.comments_error = ci_ok, ci_summary, comments_error
        self.heads = []
        self.pushes, self.prs, self.posts, self.labels, self.issues, self.argvs = [], [], [], [], [], []

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        self.argvs.append(list(argv))
        if "view" in argv and "comments" in argv:  # _reviewed 的评论查询（skip 检查）
            if self.comments_error:
                raise RuntimeError("gh pr view 失败：模拟读取失败")
            return json.dumps({"comments": self.comments_factory(self.heads[-1] if self.heads else "")})
        raise AssertionError(f"未预期的 gh 调用：{' '.join(argv)}")

    def remote_branch_exists(self, branch):
        return False

    def push(self, slot, branch):
        subprocess.run(["git", "push", "-q", "-u", "origin", branch], cwd=slot, check=True, capture_output=True)
        self.pushes.append(branch)

    def existing_pr(self, branch):
        return None

    def open_pr(self, slot, branch, title, body):
        self.prs.append((branch, title, body))
        return PR_NUMBER

    def wait_ci(self, branch, head, timeout, detail=None):
        self.heads.append(head)
        return self.ci_ok, self.ci_summary

    def comment(self, pr, body, label=None):
        self.posts.append((pr, body, label))

    def add_label(self, pr, label):
        self.labels.append((pr, label))

    def create_issue(self, title, body, labels):
        self.issues.append((title, body, labels))


def review_marker_comment(head: str, login: str) -> dict:
    """agent 账号形状的评审结论评论：标记独占一行、指向给定 head（review_status 命中当前 head 的形状）。"""
    data = {"verdict": "通过", "reviewer": "opencode", "model": "m", "head": head,
            "findings": 0, "flagged": False, "parsed": True}
    body = "\n".join(["### 独立评审（试行）：通过", "", "**结论**：没有发现。", "",
                      f"<!-- independent-review {json.dumps(data, ensure_ascii=False)} -->"]) + "\n"
    return {"author": {"login": login}, "body": body}


class FakeReview:
    """注入 review_with_chain 的假评审模块：按名字脚本化返回码或异常，记录调用序。"""

    def __init__(self, plan: dict):
        self.plan, self.calls = plan, []

    def review_pr(self, pr, name, root, github):
        self.calls.append((pr, name))
        action = self.plan.get(name, 0)
        if isinstance(action, BaseException):
            raise action
        return action


class ReviewAfterCiTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-review-after-ci-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.slots_dir = self.tmp / "slots"
        self.slots_dir.mkdir()
        self.make_repo()  # 缺省仓库；同一测试里多次派发各自重建（同分支重复 push 会冲突）

    def make_repo(self) -> None:
        """新建匿名仓库（含 bare 远端）并接入隔离补丁：每个派发场景一份，分支/评论互不干扰。"""
        origin = self.tmp / f"origin-{len(list(self.tmp.glob('origin-*')))}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.root = self.tmp / f"repo-{origin.stem}"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.root)], check=True, capture_output=True)
        for name, target in (("ROOT", self.root),):
            patch = mock.patch.object(events_db, name, target)
            patch.start()
            self.addCleanup(patch.stop)
        # identity 与 rules 都指向临时仓库：skip 检查的 agent_login、评审链配置不读引擎仓库自己的值。
        config_dir = self.root / ".harness/config"
        config_dir.mkdir(parents=True)
        (config_dir / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (config_dir / "checks.toml").write_text(CHECKS_TOML, encoding="utf-8")
        for name, target in (("CONFIG_DIR", config_dir), ("RULES_PATH", config_dir / "rules.toml")):
            patch = mock.patch.object(common, name, target)
            patch.start()
            self.addCleanup(patch.stop)
        (self.root / "docs/specs").mkdir(parents=True)
        (self.root / "docs/specs/daily-report.md").write_text(T005_SPEC)
        (self.root / "docs/plans").mkdir(parents=True)
        (self.root / "docs/plans/task-005-new-test.md").write_text(T005_TASKBOOK)
        (self.root / "fake.py").write_text(T005_EXECUTOR)
        (self.root / "error.py").write_text(T005_ERROR_EXECUTOR)
        (self.root / "verify.py").write_text(T005_VERIFY)
        (self.root / ".gitignore").write_text("build/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("push", "-q", "origin", "HEAD:main")
        self.git("fetch", "-q", "origin")
        guard = mock.patch.object(dispatch, "prepare_guard", return_value=(Path("guard.ts"), "abc123"))
        guard.start()
        self.addCleanup(guard.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, check=True, capture_output=True, text=True).stdout

    def run_dispatch(self, github, host=None, review_after_ci=True) -> int:
        config = dispatch.Config(slots=1, slot_root=self.slots_dir, stall_seconds=30, poll_seconds=0.05,
                                 ci_timeout_seconds=60, verify=[sys.executable, "verify.py"],
                                 review_after_ci=review_after_ci)
        host = host or FakeHost(self.root / "fake.py")
        return dispatch.Dispatcher(self.root, config, github, host, identity=dict(GIT_ENV)).run(
            "docs/plans/task-005-new-test.md")

    def slot_lock_held(self) -> bool:
        return (dispatch.state_dir(self.root) / "slots" / "1.json").exists()

    def dispatch_with_stub_review(self, github, review_result=0):
        """跑一次派发，用记录型假 review_pr 替掉真实评审；返回 (退出码, [(PR, 评审方)])。"""
        calls = []

        def fake_review_pr(number, reviewer_name, root, gh):
            calls.append((number, reviewer_name))
            return review_result

        with mock.patch.object(review_module, "review_pr", fake_review_pr):
            code = self.run_dispatch(github)
        return code, calls

    def test_review_runs_after_slot_release(self):
        """开关打开、CI 通过：review_pr 对该 PR 恰好调用一次，且调用时槽位锁已释放（B77③ 的原病灶）。"""
        github = ReviewGitHub(self.root)
        seen = []

        def fake_review_pr(number, reviewer_name, root, gh):
            seen.append({"pr": number, "reviewer": reviewer_name, "lock_held": self.slot_lock_held()})
            return 0

        with mock.patch.object(review_module, "review_pr", fake_review_pr):
            code = self.run_dispatch(github)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [{"pr": PR_NUMBER, "reviewer": "opencode", "lock_held": False}])
        self.assertTrue(github.prs)  # PR 已开出，评审发生在真实链路之后

    def test_skip_only_on_trusted_review(self):
        """agent_login 已写结论时跳过；只有其他账号写的标记时照常评审（不核对作者就会被诱导漏评）；
        评论读取失败按 missing 处理、照常评审（宁可重复评审，不漏评）。"""
        for login, expect_call in (("agent-bot", False), ("someone-else", True)):
            with self.subTest(login=login):
                self.make_repo()
                github = ReviewGitHub(
                    self.root, comments_factory=lambda head, login=login: [review_marker_comment(head, login)])
                code, calls = self.dispatch_with_stub_review(github)
                self.assertEqual(code, 0)
                self.assertEqual([(PR_NUMBER, "opencode")] if expect_call else [], calls)
        # 读取失败（gh pr view 报错）按 missing 处理，照常评审：去掉 _reviewed 的容错、
        # 让异常落到 _review_after_ci 的兜底（不评审、只提示）时，这里必须失败。
        self.make_repo()
        github = ReviewGitHub(self.root, comments_error=True)
        code, calls = self.dispatch_with_stub_review(github)
        self.assertEqual(code, 0)
        self.assertEqual([(PR_NUMBER, "opencode")], calls)

    def test_review_chain_falls_back(self):
        """评审链：1/2/异常都换下一家，返回 0 即停（否决也算完成）；未配置 chain 时沿用 reviewer。"""
        for rules, plan, expected in (
            ({"review": {"chain": ["codex", "claude-code", "opencode"]}},
             {"codex": 1, "claude-code": 2, "opencode": 0},
             [(PR_NUMBER, "codex"), (PR_NUMBER, "claude-code"), (PR_NUMBER, "opencode")]),
            ({"review": {"chain": ["codex", "claude-code"]}},
             {"codex": FileNotFoundError("没有评审程序"), "claude-code": 0},
             [(PR_NUMBER, "codex"), (PR_NUMBER, "claude-code")]),
            ({"review": {"chain": ["codex", "opencode", "claude-code"]}},
             {"codex": 0},
             [(PR_NUMBER, "codex")]),
            ({"review": {"reviewer": "opencode"}}, {}, [(PR_NUMBER, "opencode")]),
        ):
            with self.subTest(rules=rules):
                fake = FakeReview(plan)
                errors = io.StringIO()
                with mock.patch.object(dispatch, "load_rules", lambda rules=rules: rules), \
                        contextlib.redirect_stderr(errors):
                    code = dispatch.review_with_chain(PR_NUMBER, self.root, object(), review=fake)
                self.assertEqual(code, 0)
                self.assertEqual(fake.calls, expected)
        # 全部失败：返回 1，stderr 汇总每一家的结果
        rules = {"review": {"chain": ["codex", "claude-code"]}}
        fake = FakeReview({"codex": 1, "claude-code": 2})
        errors = io.StringIO()
        with mock.patch.object(dispatch, "load_rules", lambda: rules), contextlib.redirect_stderr(errors):
            code = dispatch.review_with_chain(PR_NUMBER, self.root, object(), review=fake)
        self.assertEqual(code, 1)
        self.assertIn("codex：评审方失败", errors.getvalue())
        self.assertIn("claude-code：返回 2", errors.getvalue())

    def test_review_failure_is_loud_not_fatal(self):
        """评审链全部失败：派发仍返回 0（CI 已通过），stderr 含「评审未能自动接上」与 PR 号。"""
        github = ReviewGitHub(self.root)
        errors = io.StringIO()
        with mock.patch.object(review_module, "review_pr", mock.Mock(return_value=1)), \
                contextlib.redirect_stderr(errors):
            code = self.run_dispatch(github)
        self.assertEqual(code, 0)
        self.assertIn("评审未能自动接上", errors.getvalue())
        self.assertIn(f"bin/dispatch review {PR_NUMBER}", errors.getvalue())

    def test_off_and_failure_paths_untouched(self):
        """开关关闭不读评论不评审；开关打开时 CI 失败与本地未完成（升级）也不触发评审。"""
        # 开关关闭（缺省）：CI 通过也不读评论、不评审
        self.make_repo()
        github = ReviewGitHub(self.root)
        calls = []
        with mock.patch.object(review_module, "review_pr",
                               lambda number, name, root, gh: calls.append(number) or 0):
            code = self.run_dispatch(github, review_after_ci=False)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertNotIn("comments", [arg for argv in github.argvs for arg in argv])
        # 开关打开、CI 失败（预算 1 轮即预算耗尽）：升级、返回 1，不评审
        self.make_repo()
        github = ReviewGitHub(self.root, ci_ok=False, ci_summary="CI 未通过：boom")
        calls = []
        with mock.patch.object(review_module, "review_pr",
                               lambda number, name, root, gh: calls.append(number) or 0):
            code = self.run_dispatch(github)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual([post[2] for post in github.posts], ["escalation"])  # 走原有升级路径
        self.assertEqual(github.labels, [(PR_NUMBER, "budget-exceeded")])
        # 开关打开、本地未完成（执行方异常退出）：升级到议题、返回 1，不评审
        self.make_repo()
        github = ReviewGitHub(self.root)
        calls = []
        with mock.patch.object(review_module, "review_pr",
                               lambda number, name, root, gh: calls.append(number) or 0):
            code = self.run_dispatch(github, host=FakeHost(self.root / "error.py"))
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(len(github.issues), 1)  # 无 PR 时升级开议题


if __name__ == "__main__":
    unittest.main()
