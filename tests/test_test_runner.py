"""T713 并行测试运行器测试：汇总与退出码、并行与 --serial 隔离、空目录与环境继承。

运行器经真实入口（test_runner.main 与 cli 登记）调用，夹具全部用匿名临时目录，不碰真实仓库。
并行与 serial 的重叠检测用标记文件同步，不靠固定 sleep：两个并行文件互相等待对方启动；
并行文件在同步后再观察一个短窗口，--serial 的文件此刻绝不能启动（它必须等并行批次结束）。
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import cli
from engine.checks import test_runner

# n 个用例都通过的文件；fail_at 指定的用例故意失败。
SUITE = """import unittest


class T(unittest.TestCase):
{methods}


if __name__ == "__main__":
    unittest.main()
"""


def suite_module(count: int, fail_at: int | None = None) -> str:
    methods = []
    for index in range(count):
        body = "self.fail('故意失败')" if index == fail_at else "pass"
        methods.append(f"    def test_{index}(self):\n        {body}\n")
    return SUITE.format(methods="\n".join(methods))


# 并行文件：写自己的启动标记，等对方的启动标记（超时即失败，证明没有同时运行）；
# a 在会合后还观察一个短窗口，serial 文件不得在此期间启动（重叠即失败）。
_PAR_A = """import time
import unittest
from pathlib import Path

MARKS = Path({marks!r})


class T(unittest.TestCase):
    def test_par_a(self):
        (MARKS / "a-start").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 30
        while not (MARKS / "b-start").exists():
            if time.monotonic() > deadline:
                self.fail("未等到 test_par_b.py 启动：并行文件没有同时运行")
            time.sleep(0.02)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (MARKS / "x-start").exists():
                self.fail("test_ser_x.py 与并行文件重叠执行")
            time.sleep(0.02)
"""

_PAR_B = """import time
import unittest
from pathlib import Path

MARKS = Path({marks!r})


class T(unittest.TestCase):
    def test_par_b(self):
        (MARKS / "b-start").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 30
        while not (MARKS / "a-start").exists():
            if time.monotonic() > deadline:
                self.fail("未等到 test_par_a.py 启动：并行文件没有同时运行")
            time.sleep(0.02)
"""

_SER_X = """import unittest
from pathlib import Path

MARKS = Path({marks!r})


class T(unittest.TestCase):
    def test_ser_x(self):
        (MARKS / "x-start").write_text("1", encoding="utf-8")
"""


class TestRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-test-runner-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_dir(self, files: dict[str, str]) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="dh-runner-suite-", dir=self.tmp))
        for name, content in files.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)  # 名字可含子包路径
            target.write_text(content, encoding="utf-8")
        return directory

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = test_runner.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_aggregates_results_and_exit_code(self):
        """2 个通过、1 个有失败的三个文件：退出码 1，输出含失败文件完整输出与合计行，Ran 等于用例数之和；
        全部通过时退出码 0；run-tests 已登记为 cli 子命令。"""
        self.assertEqual(cli.COMMANDS["run-tests"], "engine.checks.test_runner")
        mixed = self.make_dir({
            "test_a.py": suite_module(2),
            "test_b.py": suite_module(2),
            "test_c.py": suite_module(3, fail_at=1),
        })
        code, out, _ = self.run_main(["-s", str(mixed)])
        self.assertEqual(code, 1)
        self.assertIn("──── test_c.py 的完整输出 ────", out)
        self.assertIn("FAIL: test_1", out)  # 失败文件的完整输出（unittest 结果区）
        self.assertIn("故意失败", out)
        self.assertIn(f"合计：3 个文件，Ran {2 + 2 + 3} 个用例，失败 1、出错 0，", out)
        self.assertIn("test_c.py", out)

        clean = self.make_dir({"test_a.py": suite_module(2), "test_b.py": suite_module(4)})
        code, out, _ = self.run_main(["-s", str(clean)])
        self.assertEqual(code, 0)
        self.assertIn(f"合计：2 个文件，Ran {2 + 4} 个用例，失败 0、出错 0，", out)
        self.assertNotIn("的完整输出", out)

    def test_parallel_and_serial(self):
        """-j 2 下两个互相等待对方启动的文件能同时运行；--serial 的文件不与并行文件重叠执行。"""
        marks = Path(tempfile.mkdtemp(prefix="dh-runner-marks-", dir=self.tmp))
        suite = self.make_dir({
            "test_par_a.py": _PAR_A.format(marks=str(marks)),
            "test_par_b.py": _PAR_B.format(marks=str(marks)),
            "test_ser_x.py": _SER_X.format(marks=str(marks)),
        })
        code, out, err = self.run_main(["-s", str(suite), "-j", "2", "--serial", "test_ser_x.py"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count("✓ test_"), 3)
        for marker in ("a-start", "b-start", "x-start"):
            self.assertTrue((marks / marker).exists(), marker)

    def test_empty_fails_and_env_inherited(self):
        """没有任何测试文件时退出码 1；子进程继承调用方环境（夹具里读调用方设置的变量）。"""
        empty = self.make_dir({})
        code, out, err = self.run_main(["-s", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("没有匹配", err)

        env_suite = self.make_dir({
            "test_env.py": (
                "import os\nimport unittest\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_env(self):\n"
                "        self.assertEqual(os.environ.get('DH_RUNNER_ENV_PROBE'), 'sentinel-713')\n"
            ),
        })
        with mock.patch.dict(os.environ, {"DH_RUNNER_ENV_PROBE": "sentinel-713"}):
            code, out, err = self.run_main(["-s", str(env_suite)])
        self.assertEqual(code, 0, out + err)  # 读不到继承的变量时该用例失败、退出码 1

    def test_discovery_matches_unittest(self):
        """修订 2：发现口径与 unittest discover 一致——子包（带 __init__.py）里的测试不漏跑、
        不同子包的同名文件各算一个、无 __init__.py 的目录不进；Ran 总数与 discover 相同。"""
        suite = self.make_dir({
            "test_ok.py": suite_module(1),
            "pkg/__init__.py": "",
            "pkg/test_bad.py": suite_module(2, fail_at=0),
            "other/__init__.py": "",
            "other/test_ok.py": suite_module(2),
            "plain/test_skip.py": suite_module(1),  # 无 __init__.py：discover 不进，run-tests 也不进
        })
        discover = subprocess.run(  # 真跑一次 discover，取它的口径作对照
            [sys.executable, "-m", "unittest", "discover", "-s", str(suite)],
            capture_output=True, text=True, check=False)
        ran = re.search(r"Ran (\d+) tests? in ", discover.stdout + discover.stderr)
        self.assertEqual(discover.returncode, 1, discover.stdout + discover.stderr)
        self.assertEqual(ran.group(1), "5")  # 1 + 2 + 2，plain/ 不计入

        code, out, err = self.run_main(["-s", str(suite)])
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"合计：3 个文件，Ran {ran.group(1)} 个用例，失败 1、出错 0，", out)
        self.assertIn("✓ test_ok.py", out)          # 顶层与子包里的同名文件都执行
        self.assertIn("✓ other/test_ok.py", out)
        self.assertIn("✗ pkg/test_bad.py", out)      # 子包里的失败不被漏掉
        self.assertIn("──── pkg/test_bad.py 的完整输出 ────", out)
        self.assertIn("FAIL: test_0", out)
        self.assertNotIn("plain", out)


if __name__ == "__main__":
    unittest.main()
