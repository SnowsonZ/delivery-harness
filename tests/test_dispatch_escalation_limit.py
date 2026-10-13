"""B91 升级说明超长与发布失败：正文不超过 GitHub 上限；发布失败时正文留在本机并打印路径，原异常照常传播。

经真实产品入口 Dispatcher.escalate 与真实 GitHub 类驱动；只在 gh 子进程边界（GitHub._run）打桩，
截断放在派发层还是 GitHub 层都能被验证。本机事件旁路（observation.escalate、alerts.stage_lines）
打桩隔离，不碰真实事件库；本机落盘位置是匿名临时 git 仓库的 dispatch 状态目录。
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agents import dispatch
from engine.agents.github import GitHub

GITHUB_BODY_LIMIT = 65536  # GitHub 议题与评论正文的字符上限
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
PATH_TOKEN = re.compile(r"/[^\s`'\"，。：；、（）()<>]+")


class FakeGh:
    """gh 子进程边界：记录发布正文；fail=True 时发布命令按 GitHub._run 的方式抛 RuntimeError。"""

    def __init__(self, fail: bool = False):
        self.fail, self.published, self.attempted = fail, [], []

    def __call__(self, argv, cwd=None, agent=False, stdin=None):
        if argv[:3] in (["gh", "pr", "comment"], ["gh", "issue", "create"]):
            self.attempted.append(stdin)
            if self.fail:
                raise RuntimeError(f"{' '.join(argv[:3])} 失败：HTTP 422 Validation Failed")
            self.published.append(stdin)
            return "https://github.com/o/r/issues/1#issuecomment-1"
        return ""


class EscalationLimitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env={**os.environ, **GIT_ENV})
        self.task = dispatch.Task(path="docs/plans/task-991-x.md", id="T991", klass="feature", risk="R1",
                                  budget={"ci_rounds": 2}, spec_refs=[], branch="task-991-x")
        # 超长且含多字节字符：按字符和按字节计都超过上限
        self.attempt = dispatch.Attempt(ok=False, exit="retries", verify_summary="失败行 FAIL\n" * 9000,
                                        executor_note="可选方案：A。" * 2000)
        self.observed = []
        for patch in (mock.patch.object(dispatch.observation, "escalate",
                                        side_effect=lambda *a: self.observed.append(a)),
                      mock.patch("engine.core.alerts.stage_lines", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def escalate(self, pr, fail=False):
        """返回 (gh 边界, 合并输出, 抛出的异常或 None)。"""
        github = GitHub(self.root)
        gh = FakeGh(fail)
        github._run = gh
        dispatcher = dispatch.Dispatcher(self.root, dispatch.Config(), github, host=None,
                                         identity={"GIT_AUTHOR_NAME": "t"})
        out, err, raised = io.StringIO(), io.StringIO(), None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                dispatcher.escalate(self.task, pr, self.attempt, "重试次数用尽")
            except RuntimeError as exc:
                raised = exc
        return gh, out.getvalue() + err.getvalue(), raised

    def full_body(self, pr):
        return dispatch.escalation_body(self.task, self.attempt, "重试次数用尽", pr=pr)

    def test_oversized_body_is_truncated_with_note(self):
        for pr in (12, None):  # PR 评论与新开议题两条发布路径
            with self.subTest(pr=pr):
                self.observed.clear()
                full = self.full_body(pr)
                self.assertGreater(len(full), GITHUB_BODY_LIMIT)  # 前提：正文确实超限
                gh, _, raised = self.escalate(pr)
                self.assertIsNone(raised)
                self.assertEqual(len(gh.published), 1)
                sent = gh.published[0]
                self.assertLessEqual(len(sent), GITHUB_BODY_LIMIT)
                self.assertTrue(sent.startswith(f"### 升级：{self.task.id}"), sent[:80])
                self.assertIn("截断", sent)
                # 本机事件流仍留完整正文，截断说明才有处可查
                self.assertEqual(len(self.observed), 1)
                self.assertEqual(self.observed[0][4], full)
                self.assertTrue(self.observed[0][5])

    def test_publish_failure_keeps_body_locally(self):
        for pr in (12, None):
            with self.subTest(pr=pr):
                self.observed.clear()
                full = self.full_body(pr)
                gh, output, raised = self.escalate(pr, fail=True)
                self.assertIsInstance(raised, RuntimeError)  # 既有约定：原异常照常传播（test_events_agents、test_dispatch_alerts）
                self.assertTrue(gh.attempted)
                self.assertEqual(gh.published, [])
                state = dispatch.state_dir(self.root)
                saved = [Path(p) for p in PATH_TOKEN.findall(output)
                         if Path(p).is_file() and Path(p).resolve().is_relative_to(state)]
                self.assertTrue(saved, f"输出里没有指向本机状态目录中已保存正文的路径：{output!r}")
                self.assertEqual(saved[0].read_text(encoding="utf-8"), full)
                self.assertNotIn("已升级", output)
                self.assertEqual(len(self.observed), 1)
                self.assertFalse(self.observed[0][5])

if __name__ == "__main__":
    unittest.main()
