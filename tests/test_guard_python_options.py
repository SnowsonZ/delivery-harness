"""守卫按 Python 的参数语法解析解释器参数（#123，PR #116 的逃逸）。

T706 的解释器分支有两处缺口：`_option_value` 把 `-Wonce` 里的 c 误认成 `-c`；`_python_target` 不消费
组合短选项（`-uX dev`、`-uW ignore`）的值，把值当成脚本。统一由 `shell_structure._python_args` 解析：
-c 代码、-m 模块、脚本三者择一，-W/-X 的值与无值开关一律跳过。
"""

from __future__ import annotations

import unittest

from engine.core import shell_structure
from engine.guards import command_guard

REVIEW_SIGNAL = shell_structure.REVIEW_SIGNAL


def implementer(command: str) -> list[str]:
    return command_guard.check_command(command, role="implementer")


class GuardPythonOptionsTest(unittest.TestCase):
    def test_escape_123_bypasses_denied(self):
        """Defect E123-R1（#123）：复现的三种写法，以及同族的连写、组合形式，执行方角色下都被拒绝；设计方放行。"""
        for command in (
            "python3 -Wonce .harness/engine/cli.py review pr 12",
            "python3 -uX dev .harness/engine/cli.py dispatch signoff 12",
            "python3 -uW ignore .harness/engine/cli.py review pr 12",
            "python3 -uWignore .harness/engine/cli.py review pr 12",
            "python3 -BX dev -W ignore .harness/engine/cli.py dispatch review 3",
            "python3 -I -m engine.cli review pr 12",
            "python3 -uXdev -m engine.cli dispatch signoff 3",
            "python3 -- .harness/engine/cli.py review pr 12",
            "python3 --check-hash-based-pycs never .harness/engine/cli.py review pr 12",
        ):
            with self.subTest(command=command):
                self.assertIn(REVIEW_SIGNAL, implementer(command), command)
                self.assertNotIn(REVIEW_SIGNAL, command_guard.check_command(command), command)

    def test_code_option_still_checked(self):
        """-c 的代码照旧按字符串规则检查，组合写法（-Bc、-uc）同样识别为代码而不是脚本。"""
        for command in (
            "python3 -c \"import os; os.system('git push --force origin main')\"",
            "python3 -Bc \"import os; os.system('git push --force origin main')\"",
            "python3 -u -c \"import os; os.system('git push --force origin main')\"",
        ):
            with self.subTest(command=command):
                self.assertTrue(implementer(command), command)
        self.assertEqual(implementer("python3 -c 'print(1)'"), [])

    def test_parse_shapes(self):
        """解析结果的形状：值不会被当成脚本，-m 之后的参数属于模块。"""
        cases = {
            ("-Wonce", "x.py", "a"): ("script", "x.py", ["a"]),
            ("-uX", "dev", "x.py"): ("script", "x.py", []),
            ("-Bc", "print(1)"): ("code", "print(1)", []),
            ("-mengine.cli", "review"): ("module", "engine.cli", ["review"]),
            ("-I", "-m", "unittest", "t"): ("module", "unittest", ["t"]),
            ("-u",): (None, "", []),
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                self.assertEqual(shell_structure._python_args(list(args)), expected)

    def test_unrelated_commands_untouched(self):
        for command in ("python3 -W ignore .harness/engine/cli.py verify", "python3 -m unittest tests.test_x",
                        "python3 -X dev -m pytest -q", "python3 tests/x.py review"):
            with self.subTest(command=command):
                self.assertNotIn(REVIEW_SIGNAL, implementer(command), command)


if __name__ == "__main__":
    unittest.main()
