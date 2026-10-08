"""B119 任务书准入的白名单行数余量检查（T721 A 部分）。

覆盖：声明「净增不超过 / 不得超过 N 行」撞质量棘轮上限即报错（文字含文件、当前行数 C、N 与上限）；
无声明且距上限不足 100 行报错并钉住 LONG_FILE−100 边界；新增、测试与非 Python 文件不报；
LONG_FILE 与 quality.sources() 被 patch 时结果随之变化（证明复用质量棘轮口径而非写死目录）；
同一条目只取第一个反引号路径。接入点：dispatch.admit 抛 Stop、显式路径的 taskbook.main 退出码 1、
不带路径的 check_all 对同一份任务书不报余量错误、本仓库全部历史任务书不带路径检查仍合格；
验收表 ≥ 10 行的体量提示：main 打印「提示：」不改退出码，admit 打印到 stderr 不阻断。
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch
from engine.checks import quality, taskbook
from engine.core import events_db

REL = "docs/plans/task-901-headroom.md"
HEADER = """---
task: T901
class: K5
risk: R2
designer: claude-code
size: medium
architecture: false
spec_refs: []
no_spec_reason: 测试
budget:
  wall_clock_min: 60
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert
---
"""


def taskbook_text(entries: str, rows: int = 1) -> str:
    """夹具任务书：白名单条目与验收表行数可调；其余部分满足现行全部准入规则。"""
    table = "\n".join(f"| 不挂规格：测试{i} | 做 | 夹具 | `tests.test_x` | 无 |" for i in range(rows))
    return (HEADER + f"""
## 目标终态

做。

## 白名单

{entries}

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
{table}

## 非目标

无。

## 前置条件

无。

## 步骤与提交顺序

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 做 | `engine/x.py` | `bin/verify` | 不挂规格：测试0 |
""")


class HeadroomFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-headroom-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db = mock.patch.object(events_db, "ROOT", self.tmp)
        db.start()
        self.addCleanup(db.stop)

    def write(self, rel: str, text: str) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def lines(self, rel: str, count: int) -> Path:
        return self.write(rel, "x\n" * count)

    def whitelist(self, entries: str, rows: int = 1) -> str:
        self.write(REL, taskbook_text(entries, rows))
        return REL

    def errors(self, rel: str) -> list[str]:
        return taskbook.headroom_errors(rel, self.tmp)


class HeadroomTest(HeadroomFixture):
    ENTRY = "- `engine/x.py`（当前 {{current}} 行，净增{{word}} 20 行，最终必须 ≤ 800）"

    def declared_entry(self, current: int, word: str = "不超过") -> str:
        return self.ENTRY.replace("{{current}}", str(current)).replace("{{word}}", word)

    def test_declared_over_limit_mentions_file_current_added_limit(self):
        self.lines("engine/x.py", 797)
        rel = self.whitelist(self.declared_entry(797))
        errors = self.errors(rel)
        self.assertEqual(len(errors), 1)
        for fragment in ("engine/x.py", "797", "20", "800", "新模块", "降低净增"):
            self.assertIn(fragment, errors[0])

    def test_declared_must_not_exceed_wording_also_checked(self):
        self.lines("engine/x.py", 797)
        rel = self.whitelist(self.declared_entry(797, word="不得超过"))
        self.assertEqual(len(self.errors(rel)), 1)

    def test_both_declaration_wordings_are_parsed_not_just_flagged_by_the_margin_rule(self):
        # 评审：797 行时，即使声明没被解析，「距上限不足 100 行未声明」那条也会报错，掩盖写法漏认。
        # 用 650 行（未声明时不报）+ 声明净增 200（650+200>800）：只有声明路径能报错。
        for word in ("不超过", "不得超过"):
            with self.subTest(word=word):
                self.lines("engine/x.py", 650)
                entry = self.ENTRY.replace("{{current}}", "650").replace("{{word}}", word).replace("20 行", "200 行")
                errors = self.errors(self.whitelist(entry))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("声明净增不超过 200 行", errors[0])
                self.assertIn("650+200=850", errors[0])

    def test_declared_small_addition_passes(self):
        self.lines("engine/x.py", 797)  # 797+3=800，不超上限
        rel = self.whitelist(self.declared_entry(797).replace("20 行", "3 行"))
        self.assertEqual(self.errors(rel), [])

    def test_undeclared_near_limit_requires_declaration(self):
        self.lines("engine/x.py", 797)
        rel = self.whitelist("- `engine/x.py`（当前 797 行，最终必须 ≤ 800）")
        errors = self.errors(rel)
        self.assertEqual(len(errors), 1)
        self.assertIn("净增不超过 N 行", errors[0])

    def test_undeclared_boundary_at_long_file_minus_100(self):
        # 钉住 LONG_FILE−100：699 行不要求声明，700 行必须声明
        for current, expected in ((699, 0), (700, 1)):
            self.lines("engine/x.py", current)
            rel = self.whitelist(f"- `engine/x.py`（当前 {current} 行，最终必须 ≤ 800）")
            self.assertEqual(len(self.errors(rel)), expected, current)

    def test_missing_test_and_non_python_files_skipped(self):
        rel = self.whitelist("""
- `engine/new.py`（新增，A 部分的测试）
- `tests/test_new.py`（新增，不在棘轮统计范围）
- `docs/note.md`（说明文档，非 Python）
- 没有反引号路径的条目
""")
        self.lines("engine/other.py", 797)  # 别的文件再紧张也不影响未检条目
        self.assertEqual(self.errors(rel), [])

    def test_long_file_patch_changes_result(self):
        self.lines("engine/x.py", 699)
        rel = self.whitelist("- `engine/x.py`（当前 699 行，最终必须 ≤ 800）")
        self.assertEqual(self.errors(rel), [])  # 699 < 800−100，不报
        with mock.patch.object(quality, "LONG_FILE", 799):
            self.assertEqual(len(self.errors(rel)), 1)  # 上限一变（余量变 100）即报
        with mock.patch.object(quality, "LONG_FILE", 900):
            self.assertEqual(self.errors(rel), [])

    def test_sources_patch_proves_reuse_of_ratchet_scope(self):
        self.lines("engine/x.py", 797)
        self.lines("lib/x.py", 797)
        rel = self.whitelist("""
- `engine/x.py`（当前 797 行，净增不超过 20 行）
- `lib/x.py`（当前 797 行，净增不超过 20 行）
""")
        with mock.patch.object(quality, "sources", return_value=[("lib", "*.py")]):
            errors = self.errors(rel)
        self.assertEqual(len(errors), 1)
        self.assertIn("lib/x.py", errors[0])
        self.assertNotIn("engine/x.py", errors[0])

    def test_only_first_backtick_path_of_an_entry_is_checked(self):
        self.lines("engine/x.py", 5)
        self.lines("engine/y.py", 797)
        rel = self.whitelist("- `engine/x.py`、`engine/y.py`（当前 5 行，净增不超过 20 行）")
        self.assertEqual(self.errors(rel), [])  # 若取了后面的路径，y.py 会报错

    def test_read_failure_returns_no_errors(self):
        self.assertEqual(taskbook.headroom_errors("docs/plans/task-902-absent.md", self.tmp), [])


class AdmissionWiringTest(HeadroomFixture):
    def test_admit_rejects_insufficient_headroom(self):
        self.lines("engine/x.py", 797)
        rel = self.whitelist("- `engine/x.py`（当前 797 行，净增不超过 20 行）")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(dispatch.Stop) as caught:
            dispatch.admit(rel, self.tmp, require_on_main=False)
        self.assertIn("准入未通过", str(caught.exception))
        self.assertIn("超过质量棘轮上限", str(caught.exception))

    def test_admit_passes_with_enough_headroom(self):
        self.lines("engine/x.py", 797)
        rel = self.whitelist("- `engine/x.py`（当前 797 行，净增不超过 3 行）")
        with contextlib.redirect_stderr(io.StringIO()):
            task = dispatch.admit(rel, self.tmp, require_on_main=False)
        self.assertEqual(task.id, "T901")

    def test_explicit_path_fails_but_check_all_ignores_headroom(self):
        self.lines("engine/x.py", 797)
        rel = self.whitelist("- `engine/x.py`（当前 797 行，净增不超过 20 行）")
        with mock.patch.object(taskbook, "ROOT", self.tmp):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = taskbook.main([rel])
            self.assertEqual(code, 1)
            self.assertIn("超过质量棘轮上限", out.getvalue())
            # 同一份任务书，不带路径（check_all 路径）不报余量错误
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                self.assertEqual(taskbook.main([]), 0)
            self.assertNotIn("✗", out2.getvalue())

    def test_all_history_taskbooks_still_pass_without_paths(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(taskbook.main([]), 0)
        self.assertNotIn("✗", out.getvalue())


class WarningTest(HeadroomFixture):
    def test_report_warning_at_ten_rows_and_none_below(self):
        for rows, expected in ((10, 1), (9, 0)):
            self.lines("engine/x.py", 10)
            rel = self.whitelist("- `engine/x.py`（当前 10 行）", rows=rows)
            report = taskbook.check_text(taskbook_text("- `engine/x.py`（当前 10 行）", rows), rel, {}, self.tmp)
            self.assertEqual(len(report.warnings), expected, rows)
            self.assertEqual(len(report.errors), 0, rows)
            if expected:
                self.assertIn("验收表 10 行", report.warnings[0])
                self.assertIn("考虑拆分", report.warnings[0])

    def test_main_prints_hint_without_changing_exit_code(self):
        rel = self.whitelist("- `engine/x.py`（当前 10 行）", rows=10)
        with mock.patch.object(taskbook, "ROOT", self.tmp):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(taskbook.main([rel]), 0)
            self.assertIn("提示：", out.getvalue())
            self.assertIn("考虑拆分", out.getvalue())
        # 不足 10 行没有提示
        rel9 = "docs/plans/task-901-fewer.md"
        self.write(rel9, taskbook_text("- `engine/x.py`（当前 10 行）", rows=9))
        with mock.patch.object(taskbook, "ROOT", self.tmp):
            out9 = io.StringIO()
            with contextlib.redirect_stdout(out9):
                self.assertEqual(taskbook.main([rel9]), 0)
            self.assertNotIn("提示：", out9.getvalue())

    def test_admit_prints_hint_to_stderr_without_blocking(self):
        rel = self.whitelist("- `engine/x.py`（当前 10 行）", rows=10)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            task = dispatch.admit(rel, self.tmp, require_on_main=False)
        self.assertEqual(task.id, "T901")  # 不阻断
        self.assertIn("提示：", err.getvalue())
        self.assertIn("考虑拆分", err.getvalue())


if __name__ == "__main__":
    unittest.main()
