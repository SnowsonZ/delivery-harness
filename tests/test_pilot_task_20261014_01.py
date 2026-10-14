"""B79 派发推送重试：git push 瞬断时退避重试，最多 3 次；仍失败时抛出的错误写明分支与本地 head。

经真实 GitHub.push 与真实 git 驱动：GitHub 的根目录是临时目录，其中 bin/as-agent 是替身——按计数让前 N 次
git push 以 TLS 瞬断的报错失败退出，之后照常执行命令；远端是本地裸仓库。只替换 time.sleep，不真等待。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agents.github import GitHub

BRANCH = "task/b79-push"
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
TLS_ERROR = ("fatal: unable to access 'https://github.com/o/r.git/': "
             "LibreSSL SSL_connect: SSL_ERROR_SYSCALL in connection to github.com:443")
AS_AGENT = """#!/bin/sh
if [ "$1" = git ] && [ "$2" = push ]; then
  n=$(( $(cat '{counter}' 2>/dev/null || echo 0) + 1 ))
  echo "$n" > '{counter}'
  if [ "$n" -le {fails} ]; then
    echo "{error}" >&2
    exit 128
  fi
fi
exec "$@"
"""


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True,
                          text=True, check=True).stdout.strip()


class PushRetryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name).resolve()
        self.remote, self.slot, self.root = tmp / "remote.git", tmp / "slot", tmp / "root"
        self.counter = tmp / "push-count"
        git("init", "--quiet", "--bare", str(self.remote), cwd=tmp)
        self.slot.mkdir()
        git("init", "--quiet", "-b", "main", cwd=self.slot)
        (self.slot / "work.txt").write_text("执行方的成果\n")
        git("add", "work.txt", cwd=self.slot)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "work", cwd=self.slot)
        git("remote", "add", "origin", str(self.remote), cwd=self.slot)
        git("checkout", "--quiet", "-b", BRANCH, cwd=self.slot)
        self.head = git("rev-parse", "HEAD", cwd=self.slot)
        (self.root / "bin").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def push(self, fails: int) -> None:
        """前 fails 次推送瞬断；等待时长记在 self.waits。push 抛出的异常照常传给调用方。"""
        script = self.root / "bin" / "as-agent"
        script.write_text(AS_AGENT.format(counter=self.counter, fails=fails, error=TLS_ERROR))
        script.chmod(0o755)
        self.waits: list[float] = []
        with mock.patch.dict(os.environ, GIT_ENV), mock.patch("time.sleep", side_effect=self.waits.append):
            GitHub(self.root).push(self.slot, BRANCH)

    def attempts(self) -> int:
        return int(self.counter.read_text()) if self.counter.exists() else 0

    def remote_head(self) -> str:
        return git("for-each-ref", "--format=%(objectname)", f"refs/heads/{BRANCH}", cwd=self.remote)

    def test_transient_failure_is_retried_with_backoff(self):
        self.push(fails=2)
        waits = self.waits
        self.assertEqual(self.attempts(), 3, "前两次瞬断后第三次应推送成功")
        self.assertEqual(self.remote_head(), self.head, "远端分支应指向本地 head")
        self.assertEqual(len(waits), 2, f"两次失败之后各等待一次：{waits}")
        self.assertTrue(0 < waits[0] < waits[1], f"等待应递增（退避）：{waits}")
        self.assertLessEqual(sum(waits), 60, f"等待总计不超过 60 秒：{waits}")

    def test_persistent_failure_reports_branch_and_head(self):
        with self.assertRaises(RuntimeError) as caught:
            self.push(fails=99)
        message = str(caught.exception)
        self.assertEqual(self.attempts(), 3, "最多尝试 3 次")
        self.assertEqual(self.remote_head(), "", "远端不应出现该分支")
        self.assertIn(BRANCH, message, "错误应写明分支")
        self.assertIn(self.head, message, "错误应写明未推送的本地 head（完整 SHA）")
        self.assertEqual(len(self.waits), 2, f"只在两次重试之前等待，最后一次失败后直接抛出：{self.waits}")

    def test_first_success_does_not_wait(self):
        self.push(fails=0)
        self.assertEqual((self.attempts(), self.waits), (1, []), "一次成功不重试、不等待")
        self.assertEqual(self.remote_head(), self.head)


if __name__ == "__main__":
    unittest.main()
