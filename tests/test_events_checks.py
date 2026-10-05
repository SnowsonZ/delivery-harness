"""T108 埋点测试：taskbook.admit 与 hygiene、base_tests/mutation/evidence、run_check/quality/metrics
的逐项结构化事件。

验收调用真实产品入口（各检查的 main），只隔离环境（匿名临时 git 仓库、事件库 ROOT、配置路径、
基线文件位置与冻结时钟），不 mock 判定与 emit。夹具全部匿名，不碰真实库、PR 或工作流。
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

from engine.checks import base_tests, evidence, hygiene, mutate, quality, replay, taskbook
from engine.core import cases, common, events, events_db
from engine.reports import metrics
from engine.routing import run_check

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
EVENT_COLUMNS = ["id", "ts", "source", "trace_id", "seq", "prev_hash", "hash", "stage", "step", "status",
                 "duration_ms", "actor_role", "actor_host", "model", "decision_by", "decision_rule",
                 "decision_reason", "error_kind", "error_signature", "outputs", "engine_version", "redacted"]
FROZEN_TS = "2026-01-02T03:04:05.678Z"
# 夹具规则：匿名值。home_path_pattern 刻意与真实规则不同（不设占位白名单），但夹具行用的
# /Users/test/ 在真实卫生规则里属于占位用户名，不会让本仓库自己的检查失败。
RULES_TOML = """\
[hygiene]
forbidden = ["build/**", "**/.DS_Store"]
allowed = []
max_file_kb = 1024
secret_patterns = ['XSECRETKEY-[A-Za-z0-9]{20,}']
home_path_pattern = '/Users/(?!placeholder/)'

[risk]
r3 = []
r2 = ["src/**", "app.py"]
r0 = ["docs/**", "README*.md"]
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
CHECKS_TOML = """\
[sources]
python_dirs = ["src"]
python_glob = "*.py"
code = ["src/**", "app.py"]
ui = []

[[mutation.targets]]
name = "sign"
file = "src/calc.py"
functions = ["sign"]
tests = ["test_calc.CalcTest"]

[[mutation.targets]]
name = "broken"
file = "src/calc.py"
functions = ["sign"]
tests = ["test_calc.MissingTest"]
"""
REPLAY_CASES = """\
BASELINE = []
CASES = []
GUARDED = {"T108-X1": ("test_app.AppTest.test_flag",)}
DEFERRED = {}
"""
CALC_SRC = "def sign(v):\n    return 1 if v > 0 else -1\n"
TEST_APP_BASE = """\
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class AppTest(unittest.TestCase):
    def test_base(self):
        self.assertFalse(app.flag())
"""
TEST_APP_MODIFIED = TEST_APP_BASE.replace("self.assertFalse(app.flag())",
                                          "self.assertFalse(app.flag())  # 已有测试被删改一行")
TEST_APP_FLAG = "\n    def test_flag(self):\n        self.assertTrue(app.flag())  # T108-X1\n"
TEST_CALC = """\
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from calc import sign


class CalcTest(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(sign(5), 1)

    def test_one(self):
        self.assertEqual(sign(1), 1)

    def test_zero(self):
        self.assertEqual(sign(0), -1)

    def test_negative(self):
        self.assertEqual(sign(-3), -1)


class MissingTest(unittest.TestCase):
    def test_broken(self):
        self.fail("恒失败，覆盖变异目标的报错路径")
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

夹具任务书，仅用于埋点测试。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| {acceptance_ref} | 夹具 | 人工 | 人工核对 | 断言失败 |
"""


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-checks-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        (self.repo / ".harness" / "config").mkdir(parents=True)
        (self.repo / ".harness" / "project").mkdir(parents=True)
        (self.repo / ".harness" / "state").mkdir(parents=True)
        (self.repo / "docs" / "plans").mkdir(parents=True)
        (self.repo / ".harness" / "config" / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (self.repo / ".harness" / "config" / "checks.toml").write_text(CHECKS_TOML, encoding="utf-8")
        (self.repo / ".harness" / "project" / "replay_cases.py").write_text(REPLAY_CASES, encoding="utf-8")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.base = self.head_sha()
        for target, name, value in (
            (events_db, "ROOT", self.repo),
            (cases, "PROJECT_DIR", self.repo / ".harness" / "project"),
            (common, "RULES_PATH", self.repo / ".harness" / "config" / "rules.toml"),
            (common, "CONFIG_DIR", self.repo / ".harness" / "config"),
            (taskbook, "ROOT", self.repo),
            (hygiene, "ROOT", self.repo),
            (base_tests, "ROOT", self.repo),
            (evidence, "ROOT", self.repo),
            (quality, "ROOT", self.repo),
            (run_check, "ROOT", self.repo),
            (metrics, "ROOT", self.repo),
        ):
            self.patch(target, name, value)
        clock = mock.patch.object(events_db, "_now", return_value=FROZEN_TS)
        clock.start()
        self.addCleanup(clock.stop)
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

    def commit(self, files: dict[str, str], message: str) -> str:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.head_sha()

    def reset_hard(self, rev: str) -> None:
        self.git("reset", "-q", "--hard", rev)

    def head_sha(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def short(self, rev: str) -> str:
        return self.git("rev-parse", "--short", rev).stdout.strip()

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
        return [dict(zip(EVENT_COLUMNS, row)) for row in
                self.rows("SELECT * FROM events WHERE id>? ORDER BY id", (min_id,))]

    def steps_after(self, min_id: int) -> list[str]:
        return [row["step"] for row in self.events_after(min_id)]

    def input_refs(self, event_id: int) -> list[tuple]:
        return self.rows("SELECT kind,ref,sha256,size FROM refs WHERE event_id=? AND direction='in' ORDER BY rowid",
                         (event_id,))

    def outputs_of(self, row: dict) -> dict:
        return json.loads(row["outputs"])

    def install_mutation_baseline(self, baseline: dict) -> Path:
        path = self.repo / ".harness" / "state" / "mutation-baseline.json"
        path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.patch(mutate, "BASELINE", path)
        return path

    def install_quality_baseline(self, baseline: dict) -> Path:
        path = self.repo / ".harness" / "state" / "quality-baseline.json"
        path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.patch(quality, "BASELINE", path)
        return path

    def isolate_mutate_copy(self) -> None:
        """copy_worktree 以真实 common.ROOT 的 git 与路径为基准；改为夹具仓库（隔离环境，不改判定）。"""
        real_git = replay.git
        self.patch(replay, "git", lambda *args, **kwargs: real_git(*args, **{**kwargs, "cwd": self.repo}))
        self.patch(replay, "ROOT", self.repo)

    # ---------- 验收 1：taskbook.admit 引用/规则/计数 与 hygiene 规则/条数，不含命中内容 ----------

    def test_taskbook_and_hygiene_metadata(self):
        self.commit({
            "docs/plans/task-901-legal.md": taskbook_text("T901"),
            "docs/plans/task-902-bad.md": taskbook_text("T902", ci_rounds=9, acceptance_ref="ZZ99"),
        }, "任务书夹具")
        rev = self.head_sha()

        # 只有不合格的任务书发 admit 事件（路径@提交+内容哈希、问题规则计数），另加一条汇总
        before = self.max_event_id()
        code, out, err = self.run_main(taskbook)
        self.assertEqual(code, 1)
        self.assertIn("不合格 1", out)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["taskbook.admit", "taskbook.summary"])
        bad, summary = rows
        self.assertEqual((bad["stage"], bad["status"], bad["redacted"]), ("ci", "fail", 0))
        self.assertEqual(self.outputs_of(bad),
                         {"problems": 3, "header.budget": 1, "acceptance.row": 1, "acceptance.link": 1})
        self.assertEqual(bad["decision_by"], "taskbook")
        self.assertEqual(bad["decision_rule"], "admit")
        self.assertTrue(bad["decision_reason"].startswith("budget.ci_rounds = 9"), bad["decision_reason"])
        bad_body = (self.repo / "docs/plans/task-902-bad.md").read_bytes()
        self.assertEqual(self.input_refs(bad["id"]),
                         [("taskbook", f"docs/plans/task-902-bad.md@{rev}",
                           hashlib.sha256(bad_body).hexdigest(), len(bad_body))])
        self.assertEqual((summary["stage"], summary["status"], summary["redacted"]), ("ci", "fail", 0))
        self.assertEqual(self.outputs_of(summary), {"total": 2, "failed": 1})
        self.assertEqual((summary["decision_by"], summary["decision_rule"], summary["decision_reason"]),
                         ("taskbook", "admit", "1 份不合格"))
        self.assertEqual(self.input_refs(summary["id"]), [])

        # 干净的 tracked 扫描：无违规，只有汇总事件
        before = self.max_event_id()
        code, _out, _err = self.run_main(hygiene, "--tracked")
        self.assertEqual(code, 0)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["hygiene.summary"])
        summary = rows[0]
        self.assertEqual((summary["stage"], summary["status"]), ("ci", "ok"))
        self.assertEqual(self.outputs_of(summary), {"scope": "tracked", "violations": 0})
        self.assertEqual(self.input_refs(summary["id"]), [])

        # range 扫描：禁止路径 + 新增行本机路径两类违规；路径与规则名入事件，命中内容不保存
        self.commit({
            "build/out.txt": "产物\n",
            "notes.md": "记录\nlog /Users/test/data\n",
        }, "违规夹具")
        before = self.max_event_id()
        code, _out, err = self.run_main(hygiene, "--range", self.base)
        self.assertEqual(code, 1)
        self.assertIn("仓库卫生：2 处问题", err)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows],
                         ["hygiene.violation", "hygiene.violation", "hygiene.summary"])
        first, second, summary = rows
        self.assertEqual((first["stage"], first["status"]), ("ci", "fail"))
        self.assertEqual(self.outputs_of(first), {"path": "build/out.txt", "rule": "禁止路径"})
        self.assertEqual(self.outputs_of(second), {"path": "notes.md:2", "rule": "本机路径"})
        self.assertEqual((summary["stage"], summary["status"]), ("ci", "fail"))
        self.assertEqual(self.outputs_of(summary), {"scope": f"range:{self.base}", "violations": 2})
        for row in rows:
            self.assertNotIn("/Users/test", row["outputs"])  # 命中内容不入事件
            self.assertEqual(self.input_refs(row["id"]), [])

    # ---------- 验收 2：base_tests 强制与摘要、mutation 目标/得分/基线、evidence 每个缺陷 ----------

    def test_base_mutation_and_evidence_details(self):
        base = self.commit({
            "app.py": "def flag():\n    return False\n",
            "tests/test_app.py": TEST_APP_BASE,
        }, "基线")

        # 通过：base 测试在 head 上全部通过，强制为真
        head_ok = self.commit({"docs/note.md": "说明\n"}, "文档")
        before = self.max_event_id()
        code, _out, _err = self.run_main(base_tests, "--base", base, "--head", head_ok)
        self.assertEqual(code, 0)
        rows = [row for row in self.events_after(before) if row["step"] == "base_tests"]
        self.assertEqual(len(rows), 1)
        passed = rows[0]
        self.assertEqual((passed["stage"], passed["status"]), ("ci", "ok"))
        self.assertEqual(self.outputs_of(passed), {"ok": True, "enforced": True, "summary": "OK"})
        self.assertEqual(self.input_refs(passed["id"]),
                         [("rev", base, None, None), ("rev", head_ok, None, None)])

        # 强制且失败：head 改坏产品代码，base 测试失败，摘要行为 unittest 的 FAILED 行
        head_break = self.commit({"app.py": "def flag():\n    return True\n"}, "破坏行为")
        before = self.max_event_id()
        code, _out, _err = self.run_main(base_tests, "--base", base, "--head", head_break)
        self.assertEqual(code, 1)
        rows = [row for row in self.events_after(before) if row["step"] == "base_tests"]
        broken = rows[0]
        self.assertEqual((broken["stage"], broken["status"]), ("ci", "fail"))
        self.assertEqual(self.outputs_of(broken),
                         {"ok": False, "enforced": True, "summary": "FAILED (failures=1)"})

        # 不强制：head 删改了已有测试，只报告，退出码 0，enforced 为假
        head_tweak = self.commit({"tests/test_app.py": TEST_APP_MODIFIED}, "删改已有测试")
        before = self.max_event_id()
        code, _out, _err = self.run_main(base_tests, "--base", base, "--head", head_tweak)
        self.assertEqual(code, 0)
        rows = [row for row in self.events_after(before) if row["step"] == "base_tests"]
        unenforced = rows[0]
        self.assertEqual((unenforced["stage"], unenforced["status"]), ("ci", "fail"))
        self.assertEqual(self.outputs_of(unenforced),
                         {"ok": False, "enforced": False, "summary": "FAILED (failures=1)"})

        # 修复证据：完整证据（提交、改代码、前败后过）与缺失证据（无提交）分别核对所有字段
        self.reset_hard(base)
        fixed = self.commit({
            "app.py": "def flag():\n    return True\n",
            "tests/test_app.py": TEST_APP_BASE + TEST_APP_FLAG,
        }, "修复\n\nDefect: T108-X1")
        before = self.max_event_id()
        code, _out, _err = self.run_main(evidence, "--base", base, "--head", fixed)
        self.assertEqual(code, 0)
        rows = [row for row in self.events_after(before) if row["step"] == "evidence"]
        self.assertEqual(len(rows), 1)
        complete = rows[0]
        self.assertEqual((complete["stage"], complete["status"]), ("ci", "ok"))
        self.assertEqual(self.outputs_of(complete),
                         {"defect": "T108-X1", "commits": 1, "doc_only": False, "code_files": 1,
                          "tests": 1, "before": "fail", "after": "pass", "withdrawn": False,
                          "problems": 0, "warnings": 0})

        unrelated = self.commit({"docs/other.md": "其他\n"}, "无关提交")
        before = self.max_event_id()
        code, _out, _err = self.run_main(evidence, "--base", base, "--head", unrelated,
                                   "--ids", "T108-Z9", "--no-run")
        self.assertEqual(code, 1)
        rows = [row for row in self.events_after(before) if row["step"] == "evidence"]
        missing = rows[0]
        self.assertEqual((missing["stage"], missing["status"]), ("ci", "fail"))
        self.assertEqual(self.outputs_of(missing),
                         {"defect": "T108-Z9", "commits": 0, "doc_only": False, "code_files": 0,
                          "tests": 0, "before": None, "after": None, "withdrawn": False,
                          "problems": 1, "warnings": 0})

        # 变异：sign 目标全部杀死（得分与基线并列），broken 目标未变异就失败（error 与缺基线）
        self.commit({"src/calc.py": CALC_SRC, "tests/test_calc.py": TEST_CALC}, "变异目标")
        self.install_mutation_baseline({"sign": 1.0})
        self.isolate_mutate_copy()
        before = self.max_event_id()
        code, _out, _err = self.run_main(mutate, "--check")
        self.assertEqual(code, 1)
        rows = [row for row in self.events_after(before) if row["step"] == "mutation"]
        self.assertEqual(len(rows), 2)
        by_target = {self.outputs_of(row)["target"]: row for row in rows}
        sign, broken_target = by_target["sign"], by_target["broken"]
        self.assertEqual((sign["stage"], sign["status"]), ("ci", "ok"))
        self.assertEqual(self.outputs_of(sign),
                         {"target": "sign", "total": 4, "killed": 4, "score": 1.0,
                          "baseline": 1.0, "error": None})
        self.assertEqual((broken_target["stage"], broken_target["status"]), ("ci", "fail"))
        self.assertEqual(self.outputs_of(broken_target),
                         {"target": "broken", "total": 4, "killed": 0, "score": 0.0,
                          "baseline": None, "error": "未变异时测试就失败"})


    # ---------- 验收 3：run_check 每个 Finding、quality 每个指标、metrics 每个数值度量 ----------

    def test_run_quality_and_metrics_details(self):
        prompt_rel = "docs/runs/task-777-fixture/1.prompt.md"
        prompt_body = "为 T777 准备的提示词正文\n"
        taskbook_path = "docs/plans/task-777-fixture.md"
        base = self.commit({taskbook_path: taskbook_text("T777"), prompt_rel: prompt_body}, "任务书")
        record = {
            "task": "T777", "class": "K1", "attempt": 1, "branch": "task/777-fixture",
            "gen_ai.agent.name": "pi", "host_version": "0.85.1", "gen_ai.request.model": "test/model",
            "prompt_sha256": hashlib.sha256(prompt_body.encode()).hexdigest(),
            "prompt_path": prompt_rel, "guard_ref": f"main@{self.short(self.base)}",
            "started_at": "2026-01-02T03:04:05Z", "ended_at": "2026-01-02T03:14:05Z",
            "exit": "ok", "retries": 0, "failure_signatures": [], "guard_denials": {},
        }
        head = self.commit({"docs/runs/task-777-fixture/1.json": json.dumps(record, ensure_ascii=False)},
                           "实现\n\nTask: T777")

        # 适用性事件 + 每个 Finding 一条事件（规则、通过与原因引用）
        before = self.max_event_id()
        code, _out, _err = self.run_main(run_check, "--base", base, "--head", head, "--branch", "task/777-fixture")
        self.assertEqual(code, 0)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows],
                         ["run_check", "run_check.finding", "run_check.finding"])
        applicable, trailers, record_finding = rows
        self.assertEqual((applicable["stage"], applicable["status"]), ("ci", "ok"))
        self.assertEqual(self.outputs_of(applicable),
                         {"applicable": True, "taskbook": taskbook_path, "task": "T777", "in_pr": False})
        self.assertEqual((trailers["status"], trailers["decision_by"], trailers["decision_rule"]),
                         ("ok", "run_check", "归属"))
        self.assertEqual(self.outputs_of(trailers), {"finding": "归属", "ok": True})
        self.assertIn("Task: T777", trailers["decision_reason"])
        self.assertEqual((record_finding["status"], record_finding["decision_rule"]), ("ok", "运行记录"))
        self.assertEqual(self.outputs_of(record_finding), {"finding": "运行记录", "ok": True})
        self.assertIn("重试 0 次", record_finding["decision_reason"])

        # 退出方式不是 ok：运行记录 Finding 失败，原因引用保留
        self.reset_hard(base)
        bad_record = {**record, "exit": "timeout"}
        self.commit({"docs/runs/task-777-fixture/1.json": json.dumps(bad_record, ensure_ascii=False)},
                    "超时\n\nTask: T777")
        before = self.max_event_id()
        code, _out, _err = self.run_main(run_check, "--base", base, "--branch", "task/777-fixture")
        self.assertEqual(code, 0)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["run_check", "run_check.finding", "run_check.finding"])
        self.assertEqual(rows[2]["status"], "fail")
        self.assertEqual(self.outputs_of(rows[2]), {"finding": "运行记录", "ok": False})
        self.assertIn("timeout", rows[2]["decision_reason"])

        # 归属 Finding 失败：缺 Task trailer 的提交被点名
        self.commit({"docs/extra.md": "补充\n"}, "漏 trailer")
        before = self.max_event_id()
        code, _out, _err = self.run_main(run_check, "--base", base, "--branch", "task/777-fixture")
        self.assertEqual(code, 0)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["run_check", "run_check.finding", "run_check.finding"])
        trailers = rows[1]
        self.assertEqual((trailers["status"], trailers["decision_rule"]), ("fail", "归属"))
        self.assertEqual(self.outputs_of(trailers), {"finding": "归属", "ok": False})
        self.assertTrue(trailers["decision_reason"].endswith("没有 `Task: T777`"), trailers["decision_reason"])

        # 不适用：单一 skip 事件
        before = self.max_event_id()
        code, _out, _err = self.run_main(run_check, "--base", base, "--head", base, "--branch", "feature/none")
        self.assertEqual(code, 0)
        rows = self.events_after(before)
        self.assertEqual([row["step"] for row in rows], ["run_check"])
        self.assertEqual((rows[0]["stage"], rows[0]["status"]), ("ci", "skip"))
        self.assertEqual(self.outputs_of(rows[0]), {"applicable": False})

        # quality：六个指标各一条事件（当前/基线/棘轮），回归为 fail，基线引用带路径@提交+哈希
        baseline_path = self.install_quality_baseline({
            "complex_functions": 0, "files_over_800": 0, "largest_file_lines": 20,
            "swift_files_over_500": 0, "swift_long_functions": 0, "swift_deep_functions": 0,
        })
        self.commit({"src/small.py": "a = 1\n", "src/big.py": "x = 1\n" * 801}, "体量夹具")
        rev = self.head_sha()
        baseline_bytes = baseline_path.read_bytes()
        before = self.max_event_id()
        code, _out, _err = self.run_main(quality)
        self.assertEqual(code, 1)
        rows = [row for row in self.events_after(before) if row["step"] == "quality"]
        self.assertEqual(len(rows), 6)
        by_metric = {self.outputs_of(row)["metric"]: row for row in rows}
        expected = {
            "complex_functions": (0, 0, True, "ok"),
            "largest_file_lines": (801, 20, False, "ok"),
            "files_over_800": (1, 0, True, "fail"),
            "swift_files_over_500": (0, 0, True, "ok"),
            "swift_long_functions": (0, 0, True, "ok"),
            "swift_deep_functions": (0, 0, True, "ok"),
        }
        for metric, (value, baseline, ratcheted, status) in expected.items():
            row = by_metric[metric]
            self.assertEqual((row["stage"], row["status"]), ("ci", status), metric)
            self.assertEqual(self.outputs_of(row),
                             {"metric": metric, "value": value, "baseline": baseline, "ratcheted": ratcheted})
            self.assertEqual(self.input_refs(row["id"]),
                             [("baseline", f".harness/state/quality-baseline.json@{rev}",
                               hashlib.sha256(baseline_bytes).hexdigest(), len(baseline_bytes))])

        # metrics：每个数值度量一条事件；列表度量拆成计数与逐条目，不把嵌套结构交给过滤器
        base_m = self.head_sha()
        head_m = self.commit({"src/app2.py": "def value():\n    return 2\n"}, "修复\n\nDefect: T108-M1")
        before = self.max_event_id()
        code, _out, _err = self.run_main(metrics, "--base", base_m, "--head", head_m)
        self.assertEqual(code, 0)
        rows = [row for row in self.events_after(before) if row["step"] == "metrics"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual((row["stage"], row["status"], row["redacted"]), ("ci", "ok", 0))
            self.assertEqual(self.input_refs(row["id"]),
                             [("rev", base_m, None, None), ("rev", head_m, None, None)])
        seen: dict[str, list[dict]] = {}
        for row in rows:
            outputs = self.outputs_of(row)
            seen.setdefault(outputs["metric"], []).append(outputs)
        self.assertEqual(seen["提交数"], [{"metric": "提交数", "value": 1}])
        self.assertEqual(seen["风险等级"], [{"metric": "风险等级", "value": "R2"}])
        self.assertEqual(seen["Defect 修复"], [{"metric": "Defect 修复", "value": 1}])
        self.assertEqual(seen["声称已修但代码未变"], [{"metric": "声称已修但代码未变", "value": 0}])
        self.assertEqual(seen["修复带回放用例"], [{"metric": "修复带回放用例", "value": "0/1"}])
        self.assertEqual(seen["新增测试函数"], [{"metric": "新增测试函数", "value": 0}])
        self.assertEqual(seen["缺回放的修复"],
                         [{"metric": "缺回放的修复", "count": 1}, {"metric": "缺回放的修复", "item": "T108-M1"}])
        self.assertEqual(seen["base"], [{"metric": "base", "value": self.short(base_m)}])
        self.assertEqual(seen["head"], [{"metric": "head", "value": self.short(head_m)}])
        self.assertEqual(set(seen), {"base", "head", "提交数", "风险等级", "Defect 修复",
                                     "声称已修但代码未变", "修复带回放用例", "缺回放的修复", "新增测试函数"})
    # ---------- 验收 4：八个模块的失败结果在事件关闭、写入失败时保持原输出/退出码 ----------

    def test_disabled_and_failed_sink_preserve_check_results(self):
        warn = "harness：事件写入失败，已跳过（不影响本次运行）"
        # 失败夹具：每个模块至少一项检查结果为失败（run_check 与 metrics 本身只报告）
        self.commit({
            "docs/plans/task-902-bad.md": taskbook_text("T902", ci_rounds=9, acceptance_ref="ZZ99"),
            "build/out.txt": "产物\n",
            "notes.md": "记录\nlog /Users/test/data\n",
        }, "违规夹具")
        base_tests_rev = self.commit({
            "app.py": "def flag():\n    return False\n",
            "tests/test_app.py": TEST_APP_BASE,
        }, "基线")
        self.commit({"app.py": "def flag():\n    return True\n"}, "破坏行为")
        self.commit({"src/calc.py": CALC_SRC, "tests/test_calc.py": TEST_CALC}, "变异目标")
        self.install_mutation_baseline({"sign": 1.0})
        self.install_quality_baseline({
            "complex_functions": 0, "files_over_800": 0, "largest_file_lines": 20,
            "swift_files_over_500": 0, "swift_long_functions": 0, "swift_deep_functions": 0,
        })
        self.commit({"src/small.py": "a = 1\n", "src/big.py": "x = 1\n" * 801,
                     "docs/plans/task-888-iso.md": taskbook_text("T888")}, "体量夹具")
        record_base = self.head_sha()
        prompt_body = "为 T888 准备的提示词\n"
        record = {
            "task": "T888", "class": "K1", "attempt": 1, "branch": "task/888-iso",
            "gen_ai.agent.name": "pi", "host_version": "0.85.1", "gen_ai.request.model": "test/model",
            "prompt_sha256": hashlib.sha256(prompt_body.encode()).hexdigest(),
            "prompt_path": "docs/runs/task-888-iso/1.prompt.md",
            "guard_ref": f"main@{self.short(self.base)}",
            "started_at": "2026-01-02T03:04:05Z", "ended_at": "2026-01-02T03:14:05Z",
            "exit": "timeout", "retries": 0, "failure_signatures": [], "guard_denials": {},
        }
        self.commit({
            "docs/runs/task-888-iso/1.prompt.md": prompt_body,
            "docs/runs/task-888-iso/1.json": json.dumps(record, ensure_ascii=False),
        }, "超时记录\n\nTask: T888")
        self.isolate_mutate_copy()
        # 期望退出码与失败标记：确保等价比较覆盖的是真实的失败结果，不是全绿的空洞等价
        expected = {
            "taskbook": (1, "不合格 1"),
            "hygiene": (1, "仓库卫生："),
            "base_tests": (1, "FAILED"),
            "mutate": (1, "未变异时测试就失败"),
            "evidence": (1, "没有带 `Defect: T108-Z9` 的提交"),
            "quality": (1, "从 0 升到 1"),
            "run_check": (0, "❌"),  # 只报告不失败：发现为失败、退出码不变
            "metrics": (0, "缺回放的修复"),
        }
        cases = {
            "taskbook": (taskbook, []),
            "hygiene": (hygiene, ["--range", self.base]),
            "base_tests": (base_tests, ["--base", base_tests_rev]),
            "mutate": (mutate, ["--only", "broken"]),
            "evidence": (evidence, ["--base", self.base, "--ids", "T108-Z9", "--no-run"]),
            "quality": (quality, []),
            "run_check": (run_check, ["--base", record_base, "--branch", "task/888-iso"]),
            "metrics": (metrics, ["--base", self.base]),
        }
        for name, (module, argv) in cases.items():
            with self.subTest(module=name):
                code_marker = expected[name]
                os.environ["HARNESS_EVENTS"] = "off"
                off = self.run_main(module, *argv)
                self.assertEqual(off[0], code_marker[0], name)
                self.assertIn(code_marker[1], off[1] + off[2], name)
                os.environ.pop("HARNESS_EVENTS", None)
                events._warned = False
                on = self.run_main(module, *argv)
                self.db_path.unlink()
                self.db_path.mkdir()  # 库位置变成目录：写入必然失败
                events._warned = False
                try:
                    broken = self.run_main(module, *argv)
                finally:
                    self.db_path.rmdir()
                self.assertEqual(off, on)  # 关闭与开启：stdout、业务 stderr 与退出码逐字一致
                self.assertEqual(on[0], broken[0])
                self.assertEqual(on[1], broken[1])
                # 唯一差异是 T101 固定的一次写入失败提示；其余 stderr 逐字一致
                self.assertEqual([line for line in broken[2].splitlines() if line != warn],
                                 on[2].splitlines())
                self.assertEqual(broken[2].count(warn), 1)


if __name__ == "__main__":
    unittest.main()
