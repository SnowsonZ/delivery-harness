"""CI 工作流名可配置：dispatch 等 CI、合并路由的 CI 轮次都按 rules.toml [dispatch] ci_workflows 取工作流。"""

from __future__ import annotations

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
from engine.agents import github as github_module
from engine.core import common, events_db
from engine.routing import policy

SHA = "a" * 40


def run_row(sha=SHA, status="completed", conclusion="success", event="pull_request"):
    return {"headSha": sha, "status": status, "conclusion": conclusion, "databaseId": 1, "url": "u", "event": event}


class FakeGitHub(dispatch.GitHub):
    """按工作流名返回预置的运行列表；记录查询过的工作流。"""

    def __init__(self, runs_by_workflow):
        self.root = ENGINE_REPO
        self.runs_by_workflow = runs_by_workflow
        self.queried = []

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        workflow = argv[argv.index("--workflow") + 1]
        self.queried.append(workflow)
        return json.dumps(self.runs_by_workflow.get(workflow, []))


class FlakyGitHub(FakeGitHub):
    """前 failures 次查询抛 gh 失败，之后正常。"""

    def __init__(self, failures, runs_by_workflow):
        super().__init__(runs_by_workflow)
        self.failures = failures

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("gh run list 失败：EOF")
        return super()._run(argv, cwd, agent, stdin)


class WorkflowsFetchFlakyGitHub(FakeGitHub):
    """前 failures 次查询抛 gh 拉取 Actions workflows 列表的失败（couldn't fetch workflows），之后正常。"""

    def __init__(self, failures, runs_by_workflow):
        super().__init__(runs_by_workflow)
        self.failures = failures
        self.attempts = 0

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("gh run list 失败：couldn't fetch workflows: unexpected EOF")
        return super()._run(argv, cwd, agent, stdin)


class RecordingArgvGitHub(FakeGitHub):
    """在 FakeGitHub 之上记录 _run 收到的完整 argv，供正常路径调用序列断言。"""

    def __init__(self, runs_by_workflow):
        super().__init__(runs_by_workflow)
        self.argvs = []

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        self.argvs.append(list(argv))
        return super()._run(argv, cwd, agent, stdin)


class CiWorkflowsTest(unittest.TestCase):
    def workflows(self, names):
        return mock.patch.object(common, "setting", lambda *a, **k: names)

    def test_default_is_the_template_workflow(self):
        with mock.patch.object(common, "load_rules", return_value={}), \
                mock.patch.object(common, "RULES_PATH", ENGINE_REPO / "pyproject-does-not-exist"):
            self.assertEqual(common.ci_workflows(), ["harness"])

    def test_invalid_value_is_an_explicit_error(self):
        for bad in ([], "harness", [""], [1]):
            with self.workflows(bad), self.assertRaises(common.ConfigError):
                common.ci_workflows()

    def test_wait_ci_requires_every_listed_workflow_on_the_commit(self):
        github = FakeGitHub({"ci": [run_row()], "harness": [run_row()]})
        with mock.patch.object(github_module, "ci_workflows", return_value=["ci", "harness"]):
            self.assertEqual(github.wait_ci("b", SHA, 60), (True, ""))
        self.assertEqual(github.queried, ["ci", "harness"])

    def test_wait_ci_fails_when_any_listed_workflow_fails(self):
        github = FakeGitHub({"ci": [run_row()], "harness": [run_row(conclusion="failure")]})
        with mock.patch.object(github_module, "ci_workflows", return_value=["ci", "harness"]), \
                mock.patch.object(dispatch.subprocess, "run", return_value=mock.Mock(stdout="boom")):
            ok, summary = github.wait_ci("b", SHA, 60)
        self.assertFalse(ok)
        self.assertIn("CI 未通过", summary)
        self.assertIn("boom", summary)

    def test_wait_ci_ignores_runs_of_other_commits_and_times_out(self):
        github = FakeGitHub({"harness": [run_row(sha="b" * 40)]})
        with mock.patch.object(github_module, "ci_workflows", return_value=["harness"]), \
                mock.patch.object(dispatch.time, "sleep"), \
                mock.patch.object(dispatch.time, "monotonic", side_effect=[0, 0, 1, 2, 999]):
            ok, summary = github.wait_ci("b", SHA, 10)
        self.assertFalse(ok)
        self.assertIn("等待 CI 超过", summary)

    def test_wait_ci_waits_for_a_workflow_that_has_not_started(self):
        github = FakeGitHub({"ci": [run_row()], "harness": []})
        with mock.patch.object(github_module, "ci_workflows", return_value=["ci", "harness"]), \
                mock.patch.object(dispatch.time, "sleep"), \
                mock.patch.object(dispatch.time, "monotonic", side_effect=[0, 0, 1, 999]):
            self.assertFalse(github.wait_ci("b", SHA, 10)[0])

    def test_wait_ci_survives_transient_gh_failures(self):
        github = FlakyGitHub(github_module.CI_QUERY_ATTEMPTS - 1, {"harness": [run_row()]})
        with mock.patch.object(github_module, "ci_workflows", return_value=["harness"]), \
                mock.patch.object(dispatch.time, "sleep") as sleep:
            self.assertEqual(github.wait_ci("b", SHA, 60), (True, ""))
        self.assertEqual(sleep.call_count, github_module.CI_QUERY_ATTEMPTS - 1)

    def test_wait_ci_returns_ci_unknown_after_exhausted_gh_failures(self):
        """B71 ①：重试耗尽后 wait_ci 不再抛异常（原样上抛会崩掉派发进程），按「CI 结果未知」返回，
        摘要带已推 head 与失败摘要，run_ids 记为空。"""
        github = FlakyGitHub(github_module.CI_QUERY_ATTEMPTS, {"harness": [run_row()]})
        detail: dict = {}
        with mock.patch.object(github_module, "ci_workflows", return_value=["harness"]), \
                mock.patch.object(dispatch.time, "sleep") as sleep:
            ok, summary = github.wait_ci("b", SHA, 60, detail)
        self.assertFalse(ok)
        self.assertIn("CI 结果未知", summary)
        self.assertIn(SHA, summary)
        self.assertIn("gh run list 失败：EOF", summary)
        self.assertEqual(detail["run_ids"], [])
        self.assertEqual(sleep.call_count, github_module.CI_QUERY_ATTEMPTS - 1)  # 重试次数与现状一致

    def test_wait_ci_query_call_sequence_unchanged(self):
        """B71 ②：正常路径的 gh 调用序列逐字不变——每个工作流一次 gh run list（参数原样），无其他调用。"""
        github = RecordingArgvGitHub({"ci": [run_row()], "harness": [run_row()]})
        with mock.patch.object(github_module, "ci_workflows", return_value=["ci", "harness"]):
            self.assertEqual(github.wait_ci("feature", SHA, 60), (True, ""))
        self.assertEqual(github.argvs, [
            ["gh", "run", "list", "--workflow", "ci", "--branch", "feature",
             "--json", "headSha,status,conclusion,databaseId,url", "--limit", "10"],
            ["gh", "run", "list", "--workflow", "harness", "--branch", "feature",
             "--json", "headSha,status,conclusion,databaseId,url", "--limit", "10"],
        ])

    def test_wait_ci_failure_call_sequence_unchanged(self):
        """B71 ②：结论失败路径照旧在 run list 之后取一次 gh run view --log-failed，序列不变。"""
        github = RecordingArgvGitHub({"harness": [run_row(conclusion="failure")]})
        with mock.patch.object(github_module, "ci_workflows", return_value=["harness"]), \
                mock.patch.object(dispatch.subprocess, "run", return_value=mock.Mock(stdout="boom")) as run:
            ok, _summary = github.wait_ci("b", SHA, 60)
        self.assertFalse(ok)
        self.assertEqual(run.call_args[0][0][:4], ["gh", "run", "view", "1"])
        self.assertEqual([argv[argv.index("--workflow") + 1] for argv in github.argvs], ["harness"])

    def test_workflows_fetch_retried(self):
        github = WorkflowsFetchFlakyGitHub(github_module.CI_QUERY_ATTEMPTS - 1, {"harness": [run_row()]})
        with mock.patch.object(dispatch.time, "sleep") as sleep:
            self.assertEqual(github._ci_runs("harness", "b"), [run_row()])
        self.assertEqual(github.attempts, github_module.CI_QUERY_ATTEMPTS)
        self.assertEqual(sleep.call_count, github_module.CI_QUERY_ATTEMPTS - 1)

    def test_retry_exhausted_raises(self):
        github = WorkflowsFetchFlakyGitHub(github_module.CI_QUERY_ATTEMPTS, {"harness": [run_row()]})
        with mock.patch.object(dispatch.time, "sleep"), self.assertRaises(RuntimeError) as raised:
            github._ci_runs("harness", "b")
        self.assertEqual(github.attempts, github_module.CI_QUERY_ATTEMPTS)
        self.assertIn("couldn't fetch workflows", str(raised.exception))

    def test_branch_rounds_counts_distinct_commits_across_listed_workflows(self):
        table = {"ci": [run_row(SHA), run_row("b" * 40)], "harness": [run_row(SHA), run_row("c" * 40, event="push")]}

        def gh(*args):
            return json.dumps(table[args[args.index("--workflow") + 1]])

        with mock.patch.object(policy, "ci_workflows", return_value=["ci", "harness"]):
            self.assertEqual(policy.branch_rounds("b", gh), 2)

    def test_branch_rounds_is_unknown_when_github_cannot_be_read(self):
        def gh(*args):
            raise RuntimeError("no gh")

        with mock.patch.object(policy, "ci_workflows", return_value=["harness"]):
            self.assertIsNone(policy.branch_rounds("b", gh))


# ---- B71 ① 端到端：真实 wait_ci 遇 gh 查询重试耗尽，派发不崩、记升级、非零退出、槽位归还 ----
# 夹具沿用 tests/test_harness_contract_dispatch.py 的模式：匿名临时 git 仓库（含匿名 bare 远端）、
# 隔离 events_db.ROOT、冻结时钟、假执行方宿主与假 verify；差异仅一处——GitHub 子类继承真实
# wait_ci/_ci_runs，只把 gh 查询层（_run）打桩为恒定失败，重试必耗尽。

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

T005_VERIFY = "import sys\nprint('local verify ok')\n"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
FROZEN_TS = "2026-02-03T04:05:06.789Z"


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


class ExhaustedGitHub(dispatch.GitHub):
    """继承真实 wait_ci/_ci_runs；gh 查询层恒定失败（重试必耗尽），写操作记录调用不触网。"""

    def __init__(self):
        super().__init__(ENGINE_REPO)
        self.pushes, self.prs, self.comments, self.labels, self.pr_queries = [], [], [], [], []
        self.disabled: list[int] = []

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        raise RuntimeError("gh run list 失败：couldn't fetch workflows: unexpected EOF")

    def remote_branch_exists(self, branch):
        return False

    def push(self, slot, branch):
        subprocess.run(["git", "push", "-q", "-u", "origin", branch], cwd=slot, check=True, capture_output=True)
        self.pushes.append(branch)

    def existing_pr(self, branch):
        self.pr_queries.append(branch)

    def open_pr(self, slot, branch, title, body):
        self.prs.append((branch, title, body))
        return 7

    def comment(self, pr, body, label=None):
        self.comments.append((pr, body, label))

    def add_label(self, pr, label):
        self.labels.append((pr, label))

    def disable_auto_merge(self, pr):
        self.disabled.append(pr)
        return True


class DispatchQueryExhaustionTest(unittest.TestCase):
    """B71 ①：gh 查询重试耗尽后，派发进程不崩——按「CI 结果未知」记升级（PR 评论含已推 head 与
    失败摘要），走既有预算路径以非零码有序退出，槽位正常归还。"""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.root = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.root)], check=True, capture_output=True)
        root_patch = mock.patch.object(events_db, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", return_value=FROZEN_TS)
        clock.start()
        self.addCleanup(clock.stop)
        retry = mock.patch.object(github_module, "CI_QUERY_RETRY_SECONDS", 0)
        retry.start()
        self.addCleanup(retry.stop)
        (self.root / "docs/specs").mkdir(parents=True)
        (self.root / "docs/specs/daily-report.md").write_text(T005_SPEC)
        (self.root / "docs/plans").mkdir(parents=True, exist_ok=True)
        (self.root / "docs/plans/task-005-new-test.md").write_text(T005_TASKBOOK)
        (self.root / "fake.py").write_text(T005_EXECUTOR)
        (self.root / "verify.py").write_text(T005_VERIFY)
        (self.root / ".gitignore").write_text("build/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("push", "-q", "origin", "HEAD:main")
        self.git("fetch", "-q", "origin")
        self.config = dispatch.Config(slots=1, slot_root=self.tmp / "slots", stall_seconds=30, poll_seconds=0.05,
                                      ci_timeout_seconds=60, verify=[sys.executable, "verify.py"])
        (self.tmp / "slots").mkdir()
        guard = mock.patch.object(dispatch, "prepare_guard", return_value=(Path("guard.ts"), "abc123"))
        guard.start()
        self.addCleanup(guard.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, check=True, capture_output=True, text=True).stdout

    def test_query_exhaustion_escalates_ci_unknown_and_exits_non_zero(self):
        github = ExhaustedGitHub()
        host = FakeHost(self.root / "fake.py")
        code = dispatch.Dispatcher(self.root, self.config, github, host,
                                   identity=dict(GIT_ENV)).run("docs/plans/task-005-new-test.md")
        self.assertEqual(code, 1)  # 非零有序退出：异常不再出 wait_ci 崩进程
        slot = dispatch.slot_path(self.root, self.config, 1)
        head = self.git("rev-parse", "HEAD", cwd=slot).strip()  # wait_ci 收到的已推 head
        self.assertEqual([comment[2] for comment in github.comments], ["escalation"])  # 升级已记（PR 评论）
        body = github.comments[0][1]
        self.assertIn("CI 结果未知", body)
        self.assertIn(head, body)  # 升级含已推 head
        self.assertIn("couldn't fetch workflows", body)  # 与失败摘要
        self.assertEqual(github.labels, [(7, "budget-exceeded")])
        self.assertFalse(list((dispatch.state_dir(self.root) / "slots").glob("*.json")))  # 槽位正常归还


if __name__ == "__main__":
    unittest.main()
