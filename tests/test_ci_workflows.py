"""CI 工作流名可配置：dispatch 等 CI、合并路由的 CI 轮次都按 rules.toml [dispatch] ci_workflows 取工作流。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_REPO))

from engine.agents import dispatch
from engine.agents import github as github_module
from engine.core import common
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

    def test_wait_ci_raises_after_repeated_gh_failures(self):
        github = FlakyGitHub(github_module.CI_QUERY_ATTEMPTS, {"harness": [run_row()]})
        with mock.patch.object(github_module, "ci_workflows", return_value=["harness"]), \
                mock.patch.object(dispatch.time, "sleep"), self.assertRaises(RuntimeError):
            github.wait_ci("b", SHA, 60)

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


if __name__ == "__main__":
    unittest.main()
