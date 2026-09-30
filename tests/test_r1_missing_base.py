"""T113 r1 判定对无 main 提交仓库的既定行为：base 引用不存在（空仓库首个 PR、fresh clone 场景）时
r1 判定返回「无法核验」的明确结果而不是抛 git 错误，risk 层按既有降级口径取 R2 且理由含「无法核验」；
base 引用存在时既有四项判定（签名/依赖/迁移/规模）结果与现状逐字一致。

夹具使用匿名临时 git 仓库，事件关闭（观察旁路不在本任务范围），不碰真实仓库与事件库。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.checks import r1_checks
from engine.routing import risk

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}

BASE_SAMPLE = "def keep(a, b):\n    return a + b\n\n\ndef gone(a):\n    return a\n"

HEAD_SAMPLE = 'import requests\n\nSQL = "CREATE TABLE thing (id INTEGER)"\n\n\ndef keep(a, b, c=0):\n    return a + b\n'

# base 存在时的既有判定结果（逐字对照现状）：签名两处、依赖、迁移、规模；行数来自 numstat 5+4。
EXPECTED_REASONS = [
    "`engine/core/sample.py` 改了函数 `keep` 的签名",
    "`engine/core/sample.py` 删除了函数 `gone`",
    "`engine/core/sample.py` 新增了对 `requests` 的依赖",
    "`engine/core/sample.py` 新增了建表、改表或删表语句（数据迁移）",
    "增删 9 行，超过规模阈值 3",
]


class R1MissingBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-r1-missing-base-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "feature")
        # 事件关闭：本任务只核对判定结果本身，观察旁路不参与
        patcher = mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def commit(self, files: dict[str, str], message: str) -> str:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_missing_base_degrades_to_r2(self):
        head = self.commit({"engine/core/sample.py": HEAD_SAMPLE}, "change\n\nRisk: R1")
        # r1 层：不抛 git 错误，返回明确的「无法核验」理由
        reasons = r1_checks.violations("origin/main", head, cwd=self.repo)
        self.assertEqual(reasons, ["无法核验：base 引用 `origin/main` 不存在"])
        # risk 层：接到该结果按既有降级口径处理，降级 R2 且理由含「无法核验」
        report = risk.classify("origin/main", head, cwd=self.repo)
        self.assertEqual(report.label, "R2")
        self.assertFalse(report.claimed_r1)
        self.assertEqual(report.r1_violations, reasons)
        self.assertTrue(any("无法核验" in note for note in report.notes))

    def test_existing_base_behaviour_unchanged(self):
        base = self.commit({"engine/core/sample.py": BASE_SAMPLE}, "base")
        head = self.commit({"engine/core/sample.py": HEAD_SAMPLE}, "head")
        autonomy = {"size": {"max_lines": 3, "exclude": []}}
        self.assertEqual(r1_checks.violations(base, head, cwd=self.repo, autonomy=autonomy), EXPECTED_REASONS)

        # 平凡重构（只加行内注释）：四项判定全部通过，既有通过路径同样不变
        base2 = self.commit({"engine/core/other.py": "def ok():\n    return 1\n"}, "base2")
        head2 = self.commit({"engine/core/other.py": "def ok():\n    return 1  # 注释\n"}, "head2")
        self.assertEqual(r1_checks.violations(base2, head2, cwd=self.repo, autonomy=autonomy), [])


if __name__ == "__main__":
    unittest.main()
