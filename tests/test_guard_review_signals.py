"""T704 守卫禁止执行方写评审与复核结论：结构化与兜底两条路径都拦评论、评审与派发评审子命令。

自治试验设计第 6 节：合同制自动合并读取 Agent 账号在 PR 上留下的评审与复核标记，而执行方用的
是同一个账号，必须挡住执行方写这类结论的常规路径。验收（不挂规格：B81）：
  - 执行方角色下 gh pr/issue comment、gh pr review（不论带什么参数）、bin/dispatch
    review|review-calibrate|signoff 被拒绝，理由是 REVIEW_SIGNAL；&& 串联、bash -c 包裹同样拒绝；
  - 无法结构化解析的命令（引号不闭合）退回字符串规则兜底，同样拒绝；
  - 设计方角色以上命令都不触发 REVIEW_SIGNAL（行为与现在相同）；执行方的只读命令放行；
  - 执行方调用 MCP 评论类工具被拒，事件规则键为 tool_review_signal；命令拒绝规则键为 review_signal；
  - guard_denials 以 REVIEW_SIGNAL 为键的运行记录通过 run_check 的记录内容校验。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.core import common, events, events_db, shell_structure
from engine.guards import command_guard
from engine.routing import run_check

REVIEW_SIGNAL = shell_structure.REVIEW_SIGNAL
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
RULES_TOML = """\
[guard]
implementer_protected = []
"""
# 执行方不能写的评审与复核结论：结构化可解析的命令（含串联与 bash -c 包裹）。
DENIED_COMMANDS = (
    "gh pr comment 1 -b x",
    "gh issue comment 1 -b x",
    "gh pr review 1 --comment -b x",
    "gh pr review 1",
    "gh pr review 1 --approve",
    "bin/dispatch review 1",
    "./bin/dispatch signoff 1",
    "bin/dispatch review-calibrate",
    "git status && gh pr comment 1 -b x",
    "bash -c 'gh pr review 1 --comment -b x'",
    "bash -c 'bin/dispatch review 12'",
)
# 引号不闭合：无法结构化解析，退回字符串规则（宁可误报）。
UNPARSEABLE_COMMANDS = (
    'gh pr comment 1 -b "x',
    "gh issue comment 1 -b 'x",
    'gh pr review 1 --comment -b "x',
    'bin/dispatch review "1',
    './bin/dispatch signoff "1',
)
# 执行方的只读命令：不因新规则误伤。
READ_ONLY_COMMANDS = ("gh pr view 1", "gh pr list", "gh issue view 1", "bin/dispatch status")


def structured(command: str, role: str = "implementer") -> list[str]:
    """只走结构化路径：text_rules 给空表，命中即结构化规则本体的拒绝（兜底正则不参与）。"""
    return shell_structure.check(command, lambda _text: [], role)


class GuardReviewSignalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-review-signals-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        (self.repo / ".harness" / "config").mkdir(parents=True)
        (self.repo / ".harness" / "config" / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "HARNESS_"))}
        for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "init")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True,
                           env={**env, **GIT_ENV})
        for target, name, value in (
            (events_db, "ROOT", self.repo),
            (common, "RULES_PATH", self.repo / ".harness" / "config" / "rules.toml"),
            (common, "CONFIG_DIR", self.repo / ".harness" / "config"),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    # ---------- 结构化路径 ----------

    def test_implementer_denied_structured(self):
        """执行方角色下评论、评审与派发评审子命令都被拒，理由是 REVIEW_SIGNAL；串联与包裹同样生效。"""
        for command in DENIED_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(REVIEW_SIGNAL, structured(command), command)
                self.assertIn(REVIEW_SIGNAL, command_guard.check_command(command, role="implementer"), command)

    # ---------- 兜底路径 ----------

    def test_implementer_denied_fallback(self):
        """无法结构化解析的命令（引号不闭合）退回字符串规则，同样被拒绝。"""
        for command in UNPARSEABLE_COMMANDS:
            with self.subTest(command=command):
                # 夹具确实是无法结构化解析的命令：确认测到的是兜底路径，不是结构化路径又过一遍。
                with self.assertRaises(ValueError):
                    shell_structure.check(command, lambda _text: [], "implementer")
                self.assertIn(REVIEW_SIGNAL, command_guard.check_command(command, role="implementer"), command)

    # ---------- 设计方与只读命令 ----------

    def test_designer_and_read_only_allowed(self):
        """设计方角色不触发 REVIEW_SIGNAL（行为与现在相同）；执行方的只读命令全部放行。"""
        for command in DENIED_COMMANDS + UNPARSEABLE_COMMANDS:
            with self.subTest(command=command, role="designer"):
                self.assertNotIn(REVIEW_SIGNAL, command_guard.check_command(command), command)
        for command in READ_ONLY_COMMANDS:
            with self.subTest(command=command, role="implementer"):
                self.assertEqual(structured(command), [], command)
                self.assertEqual(command_guard.check_command(command, role="implementer"), [], command)

    # ---------- 工具规则与事件规则键 ----------

    def test_tool_rules_and_event_keys(self):
        """执行方的 MCP 评论类工具被拒；经真实入口写的事件里，工具与命令的规则键分别为
        tool_review_signal 与 review_signal。"""
        for tool in ("add_issue_comment", "create_issue_comment", "add_pull_request_comment",
                     "add_comment_to_pending_review"):
            with self.subTest(tool=tool):
                self.assertEqual(command_guard.check_tool(tool, role="implementer"), [REVIEW_SIGNAL])
        self.assertEqual(command_guard.check_tool("add_issue_comment"), [])  # 设计方放行
        self.assertEqual(command_guard.rule_key(REVIEW_SIGNAL, "tool"), "tool_review_signal")
        self.assertEqual(command_guard.rule_key(REVIEW_SIGNAL), "review_signal")
        for payload in ({"tool_name": "add_issue_comment"}, {"command": "gh pr comment 1 -b x"}):
            err = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                    contextlib.redirect_stderr(err):
                code = command_guard.main(["--format", "json", "--role", "implementer"])
            self.assertEqual(code, 2, err.getvalue())
            self.assertIn(REVIEW_SIGNAL, err.getvalue())
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT stage, step, status, decision_by, decision_rule, actor_role FROM events ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual([(row[0], row[1], row[2], row[3], row[5]) for row in rows],
                         [("guard", "command", "deny", "guard", "implementer")] * 2)
        self.assertEqual([row[4] for row in rows], ["tool_review_signal", "review_signal"])

    # ---------- 运行记录的可信理由 ----------

    def test_run_record_accepts_review_signal_denial(self):
        """guard_denials 以 REVIEW_SIGNAL 为键、计数为 1 的运行记录通过记录内容校验；
        清单外的相近理由仍判「未知理由」，证明空结果来自 REVIEW_SIGNAL 在可信清单内。"""
        self.assertEqual(run_check.scan_record({"guard_denials": {REVIEW_SIGNAL: 1}}), [])
        problems = run_check.scan_record({"guard_denials": {REVIEW_SIGNAL + "x": 1}})
        self.assertIn(("guard_denials", "未知理由"), problems)


if __name__ == "__main__":
    unittest.main()
