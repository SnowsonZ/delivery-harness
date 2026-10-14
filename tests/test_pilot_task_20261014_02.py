"""B97 verify 建事件临时目录失败：观察旁路失败不改变执行。

经真实 verify 的 _ProjectEventsSink 与 run_check 驱动：只把 events_db.common_dir 固定为临时路径（让 sink 走到建目录），
把 tempfile.mkdtemp 换成抛 OSError；ROOT 与 LOG_DIR 指向临时目录，项目检查是一条真实子进程命令，
它按环境里有没有重定向变量决定退出码。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.checks import verify

REASON = "No space left on device"
PROBE = "import os, sys; sys.exit(3 if 'HARNESS_EVENTS_REDIRECT' in os.environ else 0)"


class EventsSinkFailureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.mkdtemp = mock.Mock(side_effect=OSError(28, REASON))
        self.stderr = io.StringIO()
        for patcher in (mock.patch.object(verify.events_db, "common_dir", return_value=self.root / ".git"),
                        mock.patch.object(verify.tempfile, "mkdtemp", self.mkdtemp),
                        mock.patch.object(verify, "ROOT", self.root),
                        mock.patch.object(verify, "LOG_DIR", self.root / "logs"),
                        contextlib.redirect_stderr(self.stderr)):
            patcher.__enter__()
            self.addCleanup(patcher.__exit__, None, None, None)
        (self.root / "logs").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_mkdtemp_failure_gives_no_redirect_and_one_notice(self):
        sink = verify._ProjectEventsSink()
        self.assertEqual(sink.env(), {}, "建目录失败时返回空字典，不抛异常")
        self.assertEqual(sink.env(), {}, "同一次 verify 的后续项目检查同样不重定向")
        self.assertEqual(self.mkdtemp.call_count, 1, "建目录失败后不再重复尝试")
        lines = [line for line in self.stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"向 stderr 提示恰好一行：{lines}")
        self.assertIn(REASON, lines[0], "提示写明失败原因")
        sink.close()  # 没有临时目录可删：不抛异常、不再提示
        self.assertEqual(len([line for line in self.stderr.getvalue().splitlines() if line.strip()]), 1)

    def test_project_check_still_runs_without_redirect(self):
        sink = verify._ProjectEventsSink()
        check = verify.Check("pilot-probe", ("default",), command=[sys.executable, "-c", PROBE], project=True)
        result = verify.run_check(check, sink)
        self.assertEqual((result.status, result.code), ("pass", 0), "项目检查照常运行，子进程环境里没有重定向变量")
        sink.close()


if __name__ == "__main__":
    unittest.main()
