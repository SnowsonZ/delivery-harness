"""B126 第①项：评论已发出、加标签失败时，不把已送达的评论当成未送达。

经真实 GitHub 类与真实产品入口 Dispatcher.escalate 驱动；只在 gh 子进程边界（GitHub._run）打桩：
评论命令成功并返回 URL，加标签命令（gh pr edit --add-label）按 GitHub._run 的方式抛 RuntimeError。
本机事件旁路（observation.escalate、alerts.stage_lines）打桩隔离；本机落盘位置是匿名临时 git 仓库的 dispatch 状态目录。
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agents import dispatch
from engine.agents.github import GitHub

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
PR = 4321
COMMENT_URL = f"https://github.com/o/r/pull/{PR}#issuecomment-987"
REASON = "HTTP 403: Resource not accessible by integration"


class FakeGh:
    """gh 子进程边界：按顺序记录命令；label_fail / comment_fail 时对应命令抛 RuntimeError。"""

    def __init__(self, label_fail: bool = False, comment_fail: bool = False):
        self.label_fail, self.comment_fail, self.calls = label_fail, comment_fail, []

    def __call__(self, argv, cwd=None, agent=False, stdin=None):
        self.calls.append(argv[:3])
        if argv[:3] == ["gh", "pr", "comment"]:
            if self.comment_fail:
                raise RuntimeError(f"gh pr comment 失败：{REASON}")
            return COMMENT_URL
        if argv[:3] == ["gh", "pr", "edit"] and "--add-label" in argv and self.label_fail:
            raise RuntimeError(f"gh pr edit 失败：{REASON}")
        return ""


class CommentLabelFailureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env={**os.environ, **GIT_ENV})

    def github(self, **fail):
        github = GitHub(self.root)
        github._run = FakeGh(**fail)
        return github

    def call(self, fn, *args):
        """返回 (返回值, stdout, stderr, 抛出的 RuntimeError 或 None)。"""
        out, err, value, raised = io.StringIO(), io.StringIO(), None, None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                value = fn(*args)
            except RuntimeError as exc:
                raised = exc
        return value, out.getvalue(), err.getvalue(), raised

    def test_label_failure_after_comment_returns_url_and_warns(self):
        github = self.github(label_fail=True)
        url, _, err, raised = self.call(github.comment, PR, "正文", "escalation")
        self.assertIsNone(raised, "评论已发出，加标签失败不抛异常")
        self.assertEqual(url, COMMENT_URL, "返回已发出评论的 URL")
        self.assertIn(["gh", "pr", "edit"], github._run.calls, "确实尝试了加标签")
        self.assertIn(str(PR), err, f"stderr 警告写明 PR 号：{err!r}")
        self.assertIn("escalation", err, "stderr 警告写明标签名")
        self.assertIn(REASON, err, "stderr 警告写明失败原因")
        # 直接调用 add_label 的行为不变：失败照旧抛出（预算超限等调用方依赖它）
        _, _, _, raised = self.call(self.github(label_fail=True).add_label, PR, "budget-exceeded")
        self.assertIsInstance(raised, RuntimeError)

    def test_escalate_label_failure_counts_as_delivered(self):
        observed = []
        for patch in (mock.patch.object(dispatch.observation, "escalate", side_effect=lambda *a: observed.append(a)),
                      mock.patch("engine.core.alerts.stage_lines", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        task = dispatch.Task(path="docs/plans/task-992-x.md", id="T992", klass="feature", risk="R1",
                             budget={"ci_rounds": 2}, spec_refs=[], branch="task-992-x")
        attempt = dispatch.Attempt(ok=False, exit="retries", verify_summary="失败行 FAIL", executor_note="")
        github = self.github(label_fail=True)
        dispatcher = dispatch.Dispatcher(self.root, dispatch.Config(), github, host=None,
                                         identity={"GIT_AUTHOR_NAME": "t"})
        _, out, err, raised = self.call(dispatcher.escalate, task, PR, attempt, "重试次数用尽")
        self.assertIsNone(raised, "评论已送达，升级不因标签失败而失败")
        self.assertEqual(github._run.calls.count(["gh", "pr", "comment"]), 1, "只评论一次，不重发")
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0][5], "观察事件记为已送达")
        escalations = dispatch.state_dir(self.root) / "escalations"
        self.assertFalse(escalations.exists() and any(escalations.iterdir()), "正文未超限且已送达：不落盘")
        self.assertNotIn("未能发布", out + err)
        self.assertIn("已升级", out)

    def test_comment_failure_still_raises_without_label(self):
        github = self.github(comment_fail=True)
        _, _, _, raised = self.call(github.comment, PR, "正文", "escalation")
        self.assertIsInstance(raised, RuntimeError, "评论本身失败照旧抛出")
        self.assertNotIn(["gh", "pr", "edit"], github._run.calls, "评论失败不加标签")

    def test_success_path_comments_then_labels(self):
        github = self.github()
        url, _, err, raised = self.call(github.comment, PR, "正文", "escalation")
        self.assertIsNone(raised)
        self.assertEqual(url, COMMENT_URL)
        calls = github._run.calls
        self.assertLess(calls.index(["gh", "pr", "comment"]), calls.index(["gh", "pr", "edit"]), "先评论后加标签")
        self.assertEqual(err, "", "成功路径不打印警告")


if __name__ == "__main__":
    unittest.main()
