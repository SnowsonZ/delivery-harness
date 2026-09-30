"""命令守卫的覆盖变量结构判定（B60/T112）：heredoc 正文与引号内文字一律当数据，只在真赋值位置拒绝。

验收（不挂规格：B46 T112）：
  - heredoc 正文与 here-string（<<<）正文含覆盖变量字样不触发拒绝，命令按其余规则正常判定；
  - 引号内文字当数据（现状行为锁定）；
  - 真赋值（前缀 VAR=…、env VAR=…、export VAR=…）含 HARNESS_* 与拼接形式仍然全部拦截。
"""

from __future__ import annotations

import unittest

from engine.core import shell_structure
from engine.guards import command_guard

# 覆盖变量的拒绝理由（engine/core/shell_structure.OVERRIDE）。
OVERRIDE = shell_structure.OVERRIDE


class CommandGuardStructureTest(unittest.TestCase):
    def test_heredoc_body_is_data(self):
        """heredoc 正文含 HARNESS_ALLOW_TAG=1 字样不触发覆盖变量拒绝，命令按其余规则正常判定。"""
        for command in (
            # 写文档：正文是数据（2026-09-29 文档修改被误拦的场景）。
            "cat > README.md <<'EOF'\n设置 HARNESS_ALLOW_TAG=1 可放开 tag 守卫\nEOF",
            # 正文交给解释器执行：正文同样当数据，逐字出现的字样不算赋值。
            "python3 - <<'EOF'\nHARNESS_ALLOW_TAG=1\nEOF",
            # 正文交给 shell 执行：里面的引号文字也是数据。
            "bash <<'EOF'\necho 'HARNESS_ALLOW_TAG=1'\nEOF",
        ):
            with self.subTest(command=command):
                self.assertNotIn(OVERRIDE, command_guard.check_command(command))

    def test_here_string_is_data(self):
        """here-string（<<<）正文同样当数据。"""
        self.assertNotIn(OVERRIDE, command_guard.check_command("bash <<< 'HARNESS_ALLOW_TAG=1'"))

    def test_quoted_text_is_data(self):
        """引号内文字当数据（现状行为锁定）：echo 的参数、普通参数与解释器代码里的字符串都不拒绝。"""
        for command in (
            "echo 'HARNESS_ALLOW_TAG=1'",
            "grep HARNESS_ docs/plans/task-112-guard-and-path-parsing.md",
            "python3 -c 'print(\"HARNESS_ALLOW_TAG=1\")'",
        ):
            with self.subTest(command=command):
                self.assertNotIn(OVERRIDE, command_guard.check_command(command))

    def test_real_assignment_still_denied(self):
        """真赋值仍拒绝且能力不回退：前缀 VAR=…、env VAR=…、export VAR=… 三种位置，HARNESS_* 与拼接形式全部拦截。"""
        for command in (
            "HARNESS_ALLOW_TAG=1 git status",  # 命令前缀赋值
            "${P}_ALLOW_TAG=1 git status",  # 拼接形式的前缀赋值
            "env HARNESS_ALLOW_MAIN=1 git status",  # env
            "env ${P}_ALLOW_TAG=1 git status",  # env + 拼接
            "export HARNESS_SKIP_VERIFY=1",  # export
            "export ${P}_ALLOW_REWRITE=1",  # export + 拼接
            "bash -c 'HARNESS_ALLOW_TAG=1 git status'",  # 递归展开后的真赋值
        ):
            with self.subTest(command=command):
                self.assertIn(OVERRIDE, command_guard.check_command(command))


if __name__ == "__main__":
    unittest.main()
