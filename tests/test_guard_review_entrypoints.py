"""T706 守卫补漏：评审与复核的其他入口、gh 带值选项错位、管道交给 shell 的命令。

自治试验设计第 6 节；T704（PR #113）验收发现的入口缺口与 Codex 独立评审的两条严重发现（不挂规格：B81）：
  - bin/harness 与 python3 cli.py 的 dispatch review|review-calibrate|signoff、顶层 review 命令被拒；
  - gh 的带值全局选项（-R/--repo/--hostname，分开写或连写）不再把选项值当成子命令，原有规则照常生效；
  - 兜底正则去锚定：引号内、经管道交给 shell 执行的命令同样命中；
  - 自检与派发流程命令（verify、taskbook、review-pack、review-plan、dispatch status/run）不受影响，
    设计方角色全部放行。
"""

from __future__ import annotations

import unittest

from engine.core import shell_structure
from engine.guards import command_guard

REVIEW_SIGNAL = shell_structure.REVIEW_SIGNAL

# 病灶：T704 之后仍放行的评审与复核入口（harness / cli.py 的顶层子命令与解释器直调）。
ENTRYPOINT_COMMANDS = (
    "bin/harness dispatch review 12",
    "./bin/harness dispatch signoff 3",
    "bin/harness review pr 12",
    "bin/harness review calibrate",
    "python3 .harness/engine/cli.py dispatch review 12",
    "python3 .harness/engine/cli.py review pr 12",
    "python3 /x/.harness/engine/cli.py review calibrate",
    "python3 engine/cli.py dispatch review-calibrate",
)
# 同样的入口经 && 串联或 bash -c 包裹。
WRAPPED_COMMANDS = (
    "echo ok && bin/harness dispatch review 12",
    "bash -c 'bin/harness review pr 12'",
    'bash -c "python3 .harness/engine/cli.py dispatch signoff 12"',
)
# 引号不闭合：无法结构化解析，只能靠兜底正则。
UNPARSEABLE_COMMANDS = (
    'bin/harness dispatch review "12',
    "bin/harness review pr '3",
    './bin/harness review calibrate "3',
    'python3 .harness/engine/cli.py dispatch signoff "12',
    'bin/dispatch review "12',
)
# 引号不闭合、但不是评审与复核入口：兜底正则不误伤。
UNPARSEABLE_ALLOWED = (
    'bin/harness review-pack "3',
    'bin/harness review-plan "x.md',
    'bin/dispatch status "1',
)
# 自检与派发流程命令：执行方照常放行。
ALLOWED_COMMANDS = (
    "bin/harness verify",
    "bin/harness taskbook docs/plans/task-1-x.md --on-main",
    "bin/harness review-pack 3",
    "bin/harness review-plan docs/plans/x.md",
    "bin/dispatch status",
    "bin/dispatch run docs/plans/task-1-x.md",
    "python3 .harness/engine/cli.py taskbook",
    "python3 .harness/engine/cli.py review-pack 3",
    "python3 engine/cli.py verify",
)


def structured(command: str, role: str = "implementer") -> list[str]:
    """只走结构化路径：text_rules 给空表，命中即结构化规则本体的拒绝（兜底正则不参与）。"""
    return shell_structure.check(command, lambda _text: [], role)


class GuardReviewEntrypointsTest(unittest.TestCase):
    def test_implementer_denied_all_entrypoints(self):
        """执行方角色下，病灶入口全部以 REVIEW_SIGNAL 拒绝；&& 串联与 bash -c 包裹同样拒绝。"""
        for command in ENTRYPOINT_COMMANDS + WRAPPED_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(REVIEW_SIGNAL, structured(command), command)
                self.assertIn(REVIEW_SIGNAL, command_guard.check_command(command, role="implementer"), command)

    def test_fallback_regex(self):
        """无法结构化解析的命令（引号不闭合）退回字符串规则，兜底正则同样拒绝。"""
        for command in UNPARSEABLE_COMMANDS:
            with self.subTest(command=command):
                # 夹具确实是无法结构化解析的命令：确认测到的是兜底路径，不是结构化路径又过一遍。
                with self.assertRaises(ValueError):
                    shell_structure.check(command, lambda _text: [], "implementer")
                self.assertIn(REVIEW_SIGNAL, command_guard.check_command(command, role="implementer"), command)

    def test_gh_value_options_do_not_shift_action(self):
        """带值选项不再把选项值当成子命令：-R/--repo/--hostname 分开写与连写都按真实子命令判定，
        原有 gh 规则（merge、approve、release、issue 与 label、api）对带 -R 的写法同样生效。"""
        # 不分角色的拒绝：选项值归位后，原有规则照常生效。
        for command, reason in (
            ("gh pr -R o/r merge 3", shell_structure.MERGE),
            ("gh issue --repo o/r delete 3", shell_structure.ISSUE_DELETE),
            ("gh label -R o/r delete escape", shell_structure.LABEL_ERASE),
            ("gh release -R o/r create v1", shell_structure.RELEASE),
            ("gh api --hostname api.example.com repos -X POST", shell_structure.API_WRITE),
        ):
            for role in ("implementer", "designer"):
                with self.subTest(command=command, role=role):
                    self.assertIn(reason, command_guard.check_command(command, role=role), command)
        # 执行方专属：评论、评审与 close 被拒，设计方不受这些规则约束。
        for command, reason in (
            ("gh pr -R o/r comment 1 -b x", REVIEW_SIGNAL),
            ("gh issue --repo=o/r comment 1 -b x", REVIEW_SIGNAL),
            ("gh pr -Ro/r review 1 --comment -b x", REVIEW_SIGNAL),
            ("gh issue -R o/r close 3", shell_structure.ISSUE_IMPLEMENTER),
        ):
            with self.subTest(command=command):
                self.assertIn(reason, command_guard.check_command(command, role="implementer"), command)
                self.assertEqual(command_guard.check_command(command), [], command)
        # pr review --approve：设计方是 APPROVE；执行方先被 REVIEW_SIGNAL 拦下（T704 既有顺序）。
        approve = command_guard.check_command("gh pr -R o/r review 1 --approve")
        self.assertIn(shell_structure.APPROVE, approve)
        self.assertIn(REVIEW_SIGNAL,
                      command_guard.check_command("gh pr -R o/r review 1 --approve", role="implementer"))
        # 只读与无关子命令不受影响：选项值归位后照样放行。
        for command in ("gh pr -R o/r view 3", "gh issue --repo o/r view 3", "gh pr -Ro/r list"):
            with self.subTest(command=command):
                self.assertEqual(structured(command), [], command)
                self.assertEqual(command_guard.check_command(command, role="implementer"), [], command)

    def test_piped_to_shell_denied(self):
        """引号内、经管道交给 shell 执行的命令同样命中兜底正则；设计方角色放行。"""
        for command in (
            'printf "bin/dispatch review 12" | sh',
            'printf "./bin/dispatch signoff 12" | bash',
            'echo "bin/harness review pr 3" | sh',
            "printf 'bin/harness dispatch review 12' | bash -s",
        ):
            with self.subTest(command=command):
                self.assertIn(REVIEW_SIGNAL, command_guard.check_command(command, role="implementer"), command)
                self.assertNotIn(REVIEW_SIGNAL, command_guard.check_command(command), command)

    def test_allowed_commands_untouched(self):
        """执行方的自检与派发流程命令（含引号不闭合的无关写法）照常放行；设计方角色下病灶命令全部放行。"""
        for command in ALLOWED_COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(structured(command), [], command)
                self.assertEqual(command_guard.check_command(command, role="implementer"), [], command)
        for command in UNPARSEABLE_ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(command_guard.check_command(command, role="implementer"), [], command)
        for command in ENTRYPOINT_COMMANDS + WRAPPED_COMMANDS + UNPARSEABLE_COMMANDS:
            with self.subTest(command=command, role="designer"):
                self.assertNotIn(REVIEW_SIGNAL, command_guard.check_command(command), command)

    def test_python_options_with_values_do_not_hide_script(self):
        """解释器带值选项（-W ignore、-X dev）的值不是脚本，-m engine.cli 同 cli.py 入口（T706 二轮评审严重项 1）。"""
        denied = (
            "python3 -W ignore .harness/engine/cli.py review pr 12",
            "python3 -X dev .harness/engine/cli.py dispatch review 12",
            "python3 -u -W ignore .harness/engine/cli.py dispatch signoff 3",
            "python3 -Wignore .harness/engine/cli.py review pr 12",
            "python3 -m engine.cli review pr 12",
        )
        for command in denied:
            with self.subTest(command=command):
                self.assertIn(REVIEW_SIGNAL, structured(command), command)
                self.assertNotIn(REVIEW_SIGNAL, command_guard.check_command(command), command)  # 设计方放行
        for command in ("python3 -W ignore .harness/engine/cli.py verify", "python3 -m unittest tests.test_x"):
            with self.subTest(command=command):
                self.assertEqual(structured(command), [], command)

    def test_gh_option_stripping_never_loosens(self):
        """剥离 -R 只为识别被挤偏的子命令，不能放宽原解析已拒绝的写法：--description 的值恰好是 -R 时，
        后面的登记标签仍是被编辑的目标（T706 二轮评审严重项 2）。真实的 -R o/r 照常识别。"""
        label = "esc" + "ape"
        for command in (f"gh label edit --description -R {label} --name renamed",
                        f"gh label edit {label} -R o/r --name renamed"):
            with self.subTest(command=command):
                self.assertIn(shell_structure.LABEL_ERASE, command_guard.check_command(command), command)
        self.assertIn(shell_structure.MERGE, command_guard.check_command("gh pr -R o/r merge 3"))
        self.assertEqual(structured("gh pr -R o/r view 3"), [])


if __name__ == "__main__":
    unittest.main()
