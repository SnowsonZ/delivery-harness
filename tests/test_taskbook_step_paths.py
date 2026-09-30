"""任务书步骤路径解析（B61/T112）：根目录多点文件名（README.zh-CN.md）完整识别并参与交叉核对。"""

from __future__ import annotations

import unittest

from engine.checks import taskbook

STEPS = (
    "| # | 改动 | 涉及文件 |\n"
    "|---|---|---|\n"
    "| 1 | 说明与索引 | `README.zh-CN.md`、`README.md`、`docs/plans/index.md` |"
)
HEADER = {"class": "K1", "risk": "R0", "architecture": False}
RULES = {
    "taskbook": {"guard_paths": [], "architecture_paths": [], "modules": {}},
    "risk": {"r3": ["README.zh-CN.md"]},
}


class TaskbookStepPathsTest(unittest.TestCase):
    def test_root_multi_dot_step_paths(self):
        """PATH_TOKEN 完整识别根目录多点文件名并参与交叉核对；单点与多级路径行为不变。"""
        # 完整识别：多点根文件名不再截成 zh-CN.md；单点 README.md 与多级路径照旧。
        self.assertEqual(taskbook.step_files(STEPS),
                         ["README.md", "README.zh-CN.md", "docs/plans/index.md"])
        # 参与类别/风险交叉核对：整名命中 r3 规则时报错点名完整路径；截断时核对不到、断言失败。
        errors = taskbook.check_steps(STEPS, HEADER, RULES)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("步骤触及 R3 路径（README.zh-CN.md）", errors[0])


if __name__ == "__main__":
    unittest.main()
