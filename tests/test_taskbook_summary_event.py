"""T711 回归：任务书准入事件改为一条汇总加不合格逐份（B94）。

一次检查只发一条 ci/taskbook.summary（total/failed 计数与合格结论）；只有不合格的任务书
才各发一条 ci/taskbook.admit（字段与逐份时代的失败事件一致）。观察旁路验收：事件写入抛异常时，
检查的退出码与输出和不发事件时完全一致。

夹具沿用 tests/test_events_checks.py 的隔离方式（匿名临时 git 仓库、事件库 ROOT、配置路径），
调用真实 taskbook.main，不 mock 判定与 emit。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.checks import taskbook
from engine.core import common, events, events_db

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
RULES_TOML = """\
[risk]
r3 = []
r2 = ["src/**"]
r0 = ["docs/**"]
tests = ["tests/**"]
contracts = []
taskbooks = ["docs/plans/task-*.md"]
shrink_only = []
golden = []

[taskbook]
guard_paths = ["engine/**"]
architecture_paths = []

[taskbook.modules]
"""


def taskbook_text(task: str, *, ci_rounds: int = 1, acceptance_ref: str = "不挂规格：B46 夹具") -> str:
    """夹具任务书：合法为缺省；ci_rounds=9 与编号 ZZ99 分别触发头部与验收两类问题。"""
    return f"""---
task: {task}
class: K1
risk: R0
designer: codex
size: small
architecture: false
spec_refs: []
no_spec_reason: 测试夹具
budget:
  wall_clock_min: 10
  ci_rounds: {ci_rounds}
  retries: 1
  tokens: null
rollback: git revert
---

## 目标终态

夹具任务书，仅用于事件汇总回归测试。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| {acceptance_ref} | 夹具 | 人工 | 人工核对 | 断言失败 |
"""


class TaskbookSummaryEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-taskbook-summary-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        (self.repo / ".harness" / "config").mkdir(parents=True)
        (self.repo / ".harness" / "state").mkdir(parents=True)
        (self.repo / "docs" / "plans").mkdir(parents=True)
        (self.repo / ".harness" / "config" / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (self.repo / ".harness" / "config" / "checks.toml").write_text("", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        for target, name, value in (
            (events_db, "ROOT", self.repo),
            (common, "RULES_PATH", self.repo / ".harness" / "config" / "rules.toml"),
            (common, "CONFIG_DIR", self.repo / ".harness" / "config"),
            (taskbook, "ROOT", self.repo),
        ):
            self.patch(target, name, value)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    # ---------- 夹具与查询 ----------

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def commit(self, files: dict[str, str], message: str) -> None:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def head_sha(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def run_main(self, module, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def rows(self, sql, params=()):
        if not self.db_path.exists():
            return []
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def max_event_id(self) -> int:
        return self.rows("SELECT COALESCE(MAX(id), 0) FROM events")[0][0] if self.db_path.exists() else 0

    def events_after(self, min_id: int) -> list[dict]:
        keys = ("id", "stage", "step", "status", "decision_by", "decision_rule", "decision_reason", "outputs",
                "redacted")
        return [dict(zip(keys, row)) for row in self.rows(
            "SELECT id, stage, step, status, decision_by, decision_rule, decision_reason, outputs, redacted"
            " FROM events WHERE id>? ORDER BY id", (min_id,))]

    def input_refs(self, event_id: int) -> list[tuple]:
        return self.rows("SELECT kind,ref,sha256,size FROM refs WHERE event_id=? AND direction='in' ORDER BY rowid",
                         (event_id,))

    def outputs_of(self, row: dict) -> dict:
        return json.loads(row["outputs"])

    # ---------- 验收 ----------

    def test_one_summary_and_failures_only(self):
        files = {f"docs/plans/task-{901 + index}-legal.md": taskbook_text(f"T{901 + index}") for index in range(5)}
        bad_rel = "docs/plans/task-906-bad.md"
        files[bad_rel] = taskbook_text("T906", ci_rounds=9, acceptance_ref="ZZ99")
        self.commit(files, "任务书夹具")
        rev = self.head_sha()

        before = self.max_event_id()
        code, out, _err = self.run_main(taskbook)
        self.assertEqual(code, 1)
        self.assertIn("任务书 6 份：合格 5，不合格 1", out)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["taskbook.admit", "taskbook.summary"])
        admit, summary = rows
        # 不合格的那份逐份事件：字段与改动前的失败事件完全一致
        self.assertEqual((admit["stage"], admit["status"], admit["redacted"]), ("ci", "fail", 0))
        self.assertEqual(self.outputs_of(admit),
                         {"problems": 3, "header.budget": 1, "acceptance.row": 1, "acceptance.link": 1})
        self.assertEqual((admit["decision_by"], admit["decision_rule"]), ("taskbook", "admit"))
        self.assertTrue(admit["decision_reason"].startswith("budget.ci_rounds = 9"), admit["decision_reason"])
        bad_body = (self.repo / bad_rel).read_bytes()
        self.assertEqual(self.input_refs(admit["id"]),
                         [("taskbook", f"{bad_rel}@{rev}", hashlib.sha256(bad_body).hexdigest(), len(bad_body))])
        # 汇总：fail、total 6、failed 1、不合格份数的 reason，不带 inputs
        self.assertEqual((summary["stage"], summary["status"], summary["redacted"]), ("ci", "fail", 0))
        self.assertEqual(self.outputs_of(summary), {"total": 6, "failed": 1})
        self.assertEqual((summary["decision_by"], summary["decision_rule"], summary["decision_reason"]),
                         ("taskbook", "admit", "1 份不合格"))
        self.assertEqual(self.input_refs(summary["id"]), [])

    def test_all_pass_single_summary(self):
        files = {f"docs/plans/task-{901 + index}-legal.md": taskbook_text(f"T{901 + index}") for index in range(3)}
        self.commit(files, "任务书夹具")

        before = self.max_event_id()
        code, out, _err = self.run_main(taskbook)
        self.assertEqual(code, 0)
        self.assertIn("任务书 3 份：合格 3，不合格 0", out)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["taskbook.summary"])
        summary = rows[0]
        self.assertEqual((summary["stage"], summary["status"], summary["redacted"]), ("ci", "ok", 0))
        self.assertEqual(self.outputs_of(summary), {"total": 3, "failed": 0})
        self.assertEqual((summary["decision_by"], summary["decision_rule"], summary["decision_reason"]),
                         ("taskbook", "admit", "合格"))
        self.assertEqual(self.input_refs(summary["id"]), [])

    def test_event_failure_does_not_change_result(self):
        self.commit({
            "docs/plans/task-901-legal.md": taskbook_text("T901"),
            "docs/plans/task-902-bad.md": taskbook_text("T902", ci_rounds=9, acceptance_ref="ZZ99"),
        }, "任务书夹具")

        with mock.patch.object(events, "emit", side_effect=OSError("夹具：事件库写入失败")):
            broken = self.run_main(taskbook)
        with mock.patch.object(events, "emit", return_value=None):
            silent = self.run_main(taskbook)
        self.assertEqual(broken, silent)  # 事件写入抛异常：退出码、stdout 与 stderr 和不发事件时逐字一致
        self.assertEqual(broken[0], 1)
        self.assertIn("不合格 1", broken[1])


if __name__ == "__main__":
    unittest.main()
