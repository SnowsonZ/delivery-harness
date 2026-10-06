"""T713 并行测试运行器测试：汇总与退出码、并行与 --serial 隔离、空目录与环境继承、
发现口径与 unittest discover 完全一致（含包级测试、load_tests、导入失败）、
不切换工作目录、--serial 逐个校验、id 集合核对。

运行器经真实入口（test_runner.main 与 cli 登记）调用；父进程发现会把夹具模块导入本进程，
夹具模块名因此全部带唯一序号，避免多次调用之间 sys.modules 命中旧夹具。夹具目录全部用
匿名临时目录，不碰真实仓库。并行与 serial 的重叠检测用标记文件同步，不靠固定 sleep：
两个并行模块互相等待对方启动；并行模块在会合后再观察一个短窗口，--serial 的模块此刻
绝不能启动（它必须等并行批次结束）。
"""

from __future__ import annotations

import contextlib
import io
import itertools
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

# n 个用例都通过的模块；fail_at 指定的用例故意失败。
SUITE = """import unittest


class T(unittest.TestCase):
{methods}


if __name__ == "__main__":
    unittest.main()
"""

_UNIQUE = itertools.count()


def unique(name: str) -> str:
    """带唯一序号的模块名：父进程发现会把夹具导入本进程，重名会在多次调用间命中 sys.modules。"""
    return f"{name}_{next(_UNIQUE)}"


def suite_module(count: int, fail_at: int | None = None) -> str:
    methods = []
    for index in range(count):
        body = "self.fail('故意失败')" if index == fail_at else "pass"
        methods.append(f"    def test_{index}(self):\n        {body}\n")
    return SUITE.format(methods="\n".join(methods))


# 并行模块：写自己的启动标记，等对方的启动标记（超时即失败，证明没有同时运行）；
# a 在会合后还观察一个短窗口，serial 模块此刻绝不能启动（重叠即失败）。
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
                self.fail("未等到并行对手启动：两个模块没有同时运行")
            time.sleep(0.02)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (MARKS / "x-start").exists():
                self.fail("serial 模块与并行模块重叠执行")
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
                self.fail("未等到并行对手启动：两个模块没有同时运行")
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
        """2 个通过、1 个有失败的三个模块：退出码 1，输出含失败模块完整输出与合计行，Ran 等于
        用例数之和；全部通过时退出码 0；run-tests 已登记为 cli 子命令。"""
        self.assertEqual(cli.COMMANDS["run-tests"], "engine.checks.test_runner")
        names = [unique(name) for name in ("test_a", "test_b", "test_c")]
        mixed = self.make_dir({
            f"{names[0]}.py": suite_module(2),
            f"{names[1]}.py": suite_module(2),
            f"{names[2]}.py": suite_module(3, fail_at=1),
        })
        code, out, _ = self.run_main(["-s", str(mixed)])
        self.assertEqual(code, 1)
        self.assertIn(f"──── {names[2]} 的完整输出 ────", out)
        self.assertIn("FAIL: test_1", out)  # 失败模块的完整输出（unittest 结果区）
        self.assertIn("故意失败", out)
        self.assertIn(f"合计：3 个模块，Ran {2 + 2 + 3} 个用例，失败 1、出错 0，", out)
        self.assertIn("一致", out)  # 分片 id 与发现集合核对通过

        other = [unique(name) for name in ("test_a", "test_b")]
        clean = self.make_dir({f"{other[0]}.py": suite_module(2), f"{other[1]}.py": suite_module(4)})
        code, out, _ = self.run_main(["-s", str(clean)])
        self.assertEqual(code, 0)
        self.assertIn(f"合计：2 个模块，Ran {2 + 4} 个用例，失败 0、出错 0，", out)
        self.assertNotIn("的完整输出", out)

    def test_parallel_and_serial(self):
        """-j 2 下两个互相等待对方启动的模块能同时运行；--serial 的模块不与并行模块重叠执行。"""
        marks = Path(tempfile.mkdtemp(prefix="dh-runner-marks-", dir=self.tmp))
        par_a, par_b, ser_x = unique("test_par_a"), unique("test_par_b"), unique("test_ser_x")
        suite = self.make_dir({
            f"{par_a}.py": _PAR_A.format(marks=str(marks)),
            f"{par_b}.py": _PAR_B.format(marks=str(marks)),
            f"{ser_x}.py": _SER_X.format(marks=str(marks)),
        })
        code, out, err = self.run_main(["-s", str(suite), "-j", "2", "--serial", ser_x])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count("✓ "), 3)
        for marker in ("a-start", "b-start", "x-start"):
            self.assertTrue((marks / marker).exists(), marker)

    def test_empty_fails_and_env_inherited(self):
        """没有任何测试时退出码 1；子进程继承调用方环境（夹具里读调用方设置的变量）。"""
        empty = self.make_dir({})
        code, out, err = self.run_main(["-s", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("没有匹配", err)

        env_name = unique("test_env")
        env_suite = self.make_dir({
            f"{env_name}.py": (
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
        """修订 3：发现交给 unittest——包级 __init__.py 里的失败 TestCase、模块级与包级
        load_tests（附加的用例只在 load_tests 里构造）、依赖 pattern 的 load_tests、不同子包的
        同名文件、导入失败的 _FailedTest：run-tests 的退出码、Ran 总数、模块数都与真跑一次
        `unittest discover` 完全相同，失败与出错也一一对应。"""
        top, pkg, loader_pkg, load_pkg, twin_pkg, broken_pkg = (
            unique(name) for name in ("test_ok", "pkg", "loader", "pkgload", "twin", "broken"))
        suite = self.make_dir({
            f"{top}.py": suite_module(1),
            # 包级 __init__.py 里定义一个失败的 TestCase：discover 会跑，run-tests 不能漏。
            f"{pkg}/__init__.py": (
                "import unittest\n\n\n"
                "class InitFails(unittest.TestCase):\n"
                "    def test_init_fail(self):\n"
                "        self.fail('包级 __init__ 故意失败')\n"
            ),
            f"{pkg}/test_ok.py": suite_module(1),   # 与顶层 test_ok 同名，模块名不同
            f"{twin_pkg}/__init__.py": "",
            f"{twin_pkg}/test_ok.py": suite_module(1),  # 第二个子包里的同名文件
            # 模块级 load_tests，且依赖 pattern：只有 pattern 匹配默认值时才附加用例。
            f"{loader_pkg}/__init__.py": "",
            f"{loader_pkg}/test_extra.py": (
                "import unittest\n\n\n"
                "class Extra(unittest.TestCase):\n"
                "    def test_extra(self):\n"
                "        pass\n\n\n"
                "def load_tests(loader, tests, pattern):\n"
                "    if pattern == 'test*.py':\n"
                "        class OnlyViaLoadTests(unittest.TestCase):\n"
                "            def test_pattern_only(self):\n"
                "                pass\n"
                "        tests.addTest(OnlyViaLoadTests('test_pattern_only'))\n"
                "    return tests\n"
            ),
            # 包级 load_tests：__init__.py 里附加（函数内局部类不会被自动发现）。
            f"{load_pkg}/__init__.py": (
                "import unittest\n\n\n"
                "class PkgFound(unittest.TestCase):\n"
                "    def test_pkgfound(self):\n"
                "        pass\n\n\n"
                "def load_tests(loader, tests, pattern):\n"
                "    class OnlyViaLoadTests(unittest.TestCase):\n"
                "        def test_pkg_only(self):\n"
                "            pass\n"
                "    tests.addTest(OnlyViaLoadTests('test_pkg_only'))\n"
                "    return tests\n"
            ),
            # 导入失败：discover 按 _FailedTest 记一个出错用例，run-tests 同样对待。
            f"{broken_pkg}/__init__.py": "",
            f"{broken_pkg}/test_imp.py": "import module_that_does_not_exist_713\n",
        })
        discover = subprocess.run(  # 真跑一次 discover，取它的口径作对照
            [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest",
             "discover", "-s", str(suite)],
            capture_output=True, text=True, check=False)
        ran = re.search(r"Ran (\d+) tests? in ", discover.stdout + discover.stderr)
        self.assertEqual(discover.returncode, 1, discover.stdout + discover.stderr)
        self.assertEqual(ran.group(1), "9")  # 顶层1 + 包级失败1 + pkg同名1 + twin同名1
        #       + loader 模块2（含 pattern 附加）+ pkgload 包2（含 load_tests 附加）+ 导入失败1

        code, out, err = self.run_main(["-s", str(suite)])
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"合计：7 个模块，Ran {ran.group(1)} 个用例，失败 1、出错 1，", out)
        self.assertIn("一致", out)  # 实际运行的测试 id 与发现集合相等
        for passing in (top, f"{pkg}.test_ok", f"{twin_pkg}.test_ok",
                        f"{loader_pkg}.test_extra", load_pkg):
            self.assertIn(f"✓ {passing}", out)  # 同名文件与 load_tests 附加的用例都执行
        self.assertIn(f"✗ {pkg}", out)  # 包级失败被当成该模块的失败
        self.assertIn(f"──── {pkg} 的完整输出 ────", out)
        self.assertIn("FAIL: test_init_fail", out)
        self.assertIn("✗ unittest.loader", out)  # 导入失败同样按出错用例跑出失败分片
        self.assertIn("module_that_does_not_exist_713", out)

    def test_cwd_preserved(self):
        """子进程在调用方的工作目录里运行：夹具按相对路径读项目根下的文件，discover 与
        run-tests 都通过（切到测试目录运行的实现会读不到文件而失败）。"""
        project = Path(tempfile.mkdtemp(prefix="dh-runner-project-", dir=self.tmp))
        (project / "fixture-713.txt").write_text("root-713", encoding="utf-8")
        reader = unique("test_reads")
        suite = project / "testsuites"
        suite.mkdir()
        (suite / f"{reader}.py").write_text(
            "import unittest\nfrom pathlib import Path\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_read(self):\n"
            "        self.assertEqual(Path('fixture-713.txt').read_text(encoding='utf-8'), 'root-713')\n",
            encoding="utf-8")
        previous = os.getcwd()
        os.chdir(project)  # run_main 在本进程内调用，子进程继承本进程的工作目录
        self.addCleanup(os.chdir, previous)

        discover = subprocess.run(  # 对照：unittest discover 从项目根目录启动同样通过
            [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest",
             "discover", "-s", "testsuites"],
            capture_output=True, text=True, check=False)
        self.assertEqual(discover.returncode, 0, discover.stdout + discover.stderr)

        code, out, err = self.run_main(["-s", "testsuites"])  # 相对目录按调用方 cwd 解析
        self.assertEqual(code, 0, out + err)
        self.assertIn("合计：1 个模块，Ran 1 个用例，失败 0、出错 0，", out)

    def test_serial_specs_validated_individually(self):
        """--serial 里有一个拼错的模块名：退出码 2，不执行任何测试（并行批次也不启动）。"""
        alpha, beta = unique("test_alpha"), unique("test_beta")
        marks = Path(tempfile.mkdtemp(prefix="dh-runner-marks-", dir=self.tmp))
        suite = self.make_dir({
            f"{alpha}.py": suite_module(1),
            f"{beta}.py": _SER_X.format(marks=str(marks)),  # 一旦执行就写标记
        })
        code, out, err = self.run_main(["-s", str(suite), "--serial", alpha,
                                        "--serial", alpha + "_typo"])
        self.assertEqual(code, 2)
        self.assertIn("不在", err)
        self.assertIn(alpha + "_typo", err)
        self.assertNotIn("✓", out)
        self.assertNotIn("✗", out)
        self.assertNotIn("合计", out)
        self.assertFalse((marks / "x-start").exists())  # 没有任何测试被执行

    def test_id_mismatch_fails(self):
        """分片实际运行的测试 id 与发现集合不相等（这里删掉一个分片报告的 id）：
        即使所有用例都通过也退出码 1，并列出缺少的差异。"""
        names = [unique(name) for name in ("test_a", "test_b")]
        suite = self.make_dir({f"{names[0]}.py": suite_module(1), f"{names[1]}.py": suite_module(1)})
        original = test_runner._load_result

        def drop_one_id(path):
            payload = original(path)
            if payload and payload.get("ids"):
                payload["ids"] = payload["ids"][:-1]
            return payload

        with mock.patch.object(test_runner, "_load_result", side_effect=drop_one_id):
            code, _, err = self.run_main(["-s", str(suite)])
        self.assertEqual(code, 1)
        self.assertIn("不一致", err)
        self.assertIn("缺少（2）", err)


if __name__ == "__main__":
    unittest.main()
