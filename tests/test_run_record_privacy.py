"""T203 运行记录内容检查测试（B38）：新旧记录全文递归拒绝命令正文、本机路径与会话正文，
共用合同 C3 的 C01-C07 禁例与 A01-A05 放行例逐个成立，失败只报字段位置与规则名、不回显禁项，
policy 经既有 run_findings 路径拒绝坏记录且结论不依赖事件库。

验收调用真实产品入口（run_check.check / run_check.main / policy.gather / policy.decide / scan_record），
不 mock 判定与 emit。夹具与 T108 同型：匿名临时 git 仓库、隔离 events_db.ROOT 与配置路径、冻结时钟；
记录形状沿用 T201 已钉住的字段（旧字段 + 附加字段 + trace_id/stages/anchors），全部匿名值，
不碰真实库、PR 或工作流。本任务不读事件判定记录：四个用例在事件库缺失与存在两种状态下结论一致。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.checks import taskbook
from engine.core import common, events, events_db
from engine.guards import command_guard
from engine.routing import policy, run_check

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
FROZEN_TS = "2026-01-02T03:04:05.678Z"
# 夹具规则：匿名值，只覆盖 risk 与路由读取的节。
RULES_TOML = """\
[risk]
r3 = []
r2 = []
r0 = ["docs/**", "README*.md"]
tests = ["tests/**"]
contracts = []
taskbooks = ["docs/plans/task-*.md"]
shrink_only = []
golden = []

[dispatch]
ci_workflows = ["harness"]
"""
TASK = "T290-A"
BRANCH = "task/290-a"
TASKBOOK_REL = "docs/plans/task-290-a.md"
RECORD_DIR = "docs/runs/task-290-a"
PROMPT_REL = f"{RECORD_DIR}/1.prompt.md"
RECORD_REL = f"{RECORD_DIR}/1.json"
PROMPT_TEXT = "为 T290-A 准备的提示词正文\n"
TASKBOOK_TEXT = f"""---
task: {TASK}
class: K7
risk: R3
designer: codex
size: small
architecture: false
spec_refs: []
no_spec_reason: 测试夹具
budget:
  wall_clock_min: 10
  ci_rounds: 3
  retries: 1
  tokens: null
rollback: git revert
---

## 目标终态

夹具任务书，仅用于内容检查测试。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 夹具 | 夹具 | 人工 | 人工核对 | 断言失败 |
"""
# 可信内置规则理由从已批准代码的规则表静态取得（与 run_check 的例外集合同源）。
RESET_HARD_REASON = next(reason for _pattern, key, reason in command_guard.COMMAND_RULES if key == "reset_hard")
MERGE_REASON = command_guard.MERGE_REASON


def valid_stages() -> list[dict]:
    """C3 摘要形状的四段真实时间线（执行、本地判定、等 CI 失败、升级），值全部匿名。"""
    return [
        {
            "stage": "dispatch", "step": "executor_round", "status": "ok", "ts": FROZEN_TS,
            "duration_ms": 1500, "attempt": 1, "round": 1,
            "inputs": [{"kind": "prompt", "ref": "c" * 64}, {"kind": "rev", "ref": "d" * 40}],
            "outputs": {"model": "provider/model-x", "exit": "ok", "guard_denied": 1,
                        "stream.sha256": "e" * 64, "stream.size": 210},
            "decision": None, "actor": {"role": "engine", "host": "local"},
            "source": "local", "head_hash": "f" * 64,
        },
        {
            "stage": "dispatch", "step": "local_verify", "status": "ok", "ts": FROZEN_TS,
            "duration_ms": 3200, "attempt": 1, "round": 1,
            "inputs": [{"kind": "rev", "ref": "d" * 40}, {"kind": "rev", "ref": "f" * 40}],
            "outputs": {"ok": True, "signature": "lint:abc123456789", "repeat": False,
                        "log.sha256": "1" * 64, "log.size": 96},
            "decision": None, "actor": {"role": "engine", "host": "local"},
            "source": "local", "head_hash": "2" * 64,
        },
        {
            "stage": "dispatch", "step": "ci_wait", "status": "fail", "ts": FROZEN_TS,
            "duration_ms": 90000, "attempt": 1, "round": 0,
            "inputs": [{"kind": "rev", "ref": "4" * 40}],
            "outputs": {"pr": 14, "round": 1, "ok": False, "run_ids": "5,7",
                        "summary.sha256": "5" * 64, "summary.size": 64,
                        "decision.reason.sha256": "6" * 64},
            "decision": {"by": "dispatch", "rule": "ci", "reason": "fail"},
            "actor": {"role": "engine", "host": "local"},
            "source": "local", "head_hash": "7" * 64,
        },
        {
            "stage": "dispatch", "step": "escalate", "status": "ok", "ts": FROZEN_TS,
            "duration_ms": 120, "attempt": 2, "round": 0,
            "inputs": [{"kind": "pr", "ref": "14"}, {"kind": "summary", "ref": "8" * 64, "size": 480}],
            "outputs": {"reason": "CI 第 2 轮未通过，已达预算 2 轮", "target": "pr", "pr": 14,
                        "labels": "escalation", "notified": True},
            "decision": None, "actor": {"role": "engine", "host": "local"},
            "source": "local", "head_hash": "9" * 64,
        },
    ]


def valid_record() -> dict:
    """合法记录：旧字段与附加字段一个不少，新 C3 字段齐备，全部值可通过内容检查。"""
    return {
        "task": TASK, "class": "K7", "attempt": 1, "branch": BRANCH,
        "gen_ai.agent.name": "fake-pi", "host_version": "0.85.1",
        "gen_ai.request.model": "provider/model-x",
        "prompt_sha256": hashlib.sha256(PROMPT_TEXT.encode()).hexdigest(),
        "prompt_path": PROMPT_REL,
        "guard_ref": "a" * 40,
        "started_at": "2026-01-02T03:04:05Z", "ended_at": "2026-01-02T03:14:05Z",
        "executor_seconds": 12.5, "exit": "ok", "retries": 0,
        "failure_signatures": [], "ci_rounds_before": 0, "guard_denials": {},
        "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 50, "cost": 0.5,
        "escalation": None,
        "trace_id": BRANCH, "stages": valid_stages(),
        "anchors": [{"source": "local", "stage": "dispatch", "head_hash": "b" * 64,
                     "fixed_in": "run_record"}],
    }


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-record-privacy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        (self.repo / ".harness" / "config").mkdir(parents=True)
        (self.repo / ".harness" / "config" / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.commit({TASKBOOK_REL: TASKBOOK_TEXT}, "任务书")
        self.base = self.head_sha()
        for target, name, value in (
            (events_db, "ROOT", self.repo),
            (common, "RULES_PATH", self.repo / ".harness" / "config" / "rules.toml"),
            (common, "CONFIG_DIR", self.repo / ".harness" / "config"),
            (taskbook, "ROOT", self.repo),
            (run_check, "ROOT", self.repo),
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

    # ---------- 夹具 ----------

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, check=True):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "HARNESS_"))}
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

    def head_sha(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def make_pr(self, record: dict) -> tuple[str, str]:
        """任务书留在 base（main），记录与提示词提交到任务分支：返回 (base, head)。"""
        self.git("checkout", "-q", "-B", BRANCH, "main")
        head = self.commit(
            {PROMPT_REL: PROMPT_TEXT, RECORD_REL: json.dumps(record, ensure_ascii=False, indent=2) + "\n"},
            f"run：{TASK} 第 1 次派发记录（ok）\n\nTask: {TASK}\n")
        return self.base, head

    def run_findings(self, record: dict) -> list[run_check.Finding]:
        base, head = self.make_pr(record)
        findings = run_check.check(base, head, BRANCH, cwd=self.repo, with_ci=False)
        self.assertIsNotNone(findings)
        return findings

    def content_finding(self, record: dict) -> run_check.Finding | None:
        return next((item for item in self.run_findings(record) if item.name == "记录内容"), None)

    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_check.main(args)
        return code, out.getvalue(), err.getvalue()

    def assert_all_ok(self, record: dict) -> None:
        findings = self.run_findings(record)
        self.assertTrue(findings)
        self.assertEqual([f"{item.name}: {item.reason}" for item in findings if not item.ok], [])
        self.assertNotIn("记录内容", [item.name for item in findings])

    # ---------- 验收 1：C01-C07 禁例逐个拒绝，A01-A05 放行例逐个通过 ----------

    def test_each_forbidden_content_is_rejected(self):
        note = lambda text: (lambda record: record["stages"][0]["outputs"].update(note=text))
        forbidden_cases = [
            ("C01 git status 命令全文", note("git status"), "命令正文"),
            ("C01 python -m unittest", note("python -m unittest"), "命令正文"),
            ("C01 gh pr view 1", note("gh pr view 1"), "命令正文"),
            ("C02 rm 带参数", note("rm -rf build"), "命令正文"),
            ("C02 裸 cat 在摘要字段", note("cat"), "命令正文"),
            ("C03 分号连接", note("run this; rm -rf x"), "Shell 结构"),
            ("C03 管道", note("echo ok | tee x"), "Shell 结构"),
            ("C03 反引号命令替换", note("see `whoami` output"), "Shell 结构"),
            ("C03 重定向", note("append to log > file.txt"), "Shell 结构"),
            ("C04 User: 标记（嵌套）", note("User: hello"), "会话正文"),
            ("C04 assistant 小写标记", note("assistant: hi there"), "会话正文"),
            ("C04 中文助手标记", note("助手：好的，马上执行"), "会话正文"),
            ("C04 顶层已知字段中的会话", lambda r: r.update(escalation="System: shutdown"), "会话正文"),
            ("C04 键中的标记", lambda r: r["stages"][0]["outputs"].update({"User:": "hi"}), "会话正文"),
            ("C05 多行会话", note("User: hi\nAssistant: hello"), "换行"),
            ("C05 列表中的 Unix 路径",
             lambda r: r.update(failure_signatures=["cat /Users/someone/notes.txt"]), "本机路径"),
            ("C05 嵌套引用中的路径",
             lambda r: r["stages"][0]["inputs"][0].update(ref="/Users/someone/x.md"), "本机路径"),
            ("C05 顶层 Windows 路径",
             lambda r: r.update(prompt_path="C:\\Users\\someone\\1.prompt.md"), "本机路径"),
            ("C06 可信理由追加正文", lambda r: r.update(guard_denials={RESET_HARD_REASON + "；再跑一遍": 1}),
             "未知理由"),
            ("C06 看似理由但不在可信集合", lambda r: r.update(guard_denials={"执行者不能编辑判定器": 1}),
             "未知理由"),
            ("C07 无标记自然语言正文", note("the executor explained the failure at length"), "未知自由文本"),
            ("C07 未知顶层字段", lambda r: r.update(notes="自由正文一段"), "未知字段"),
        ]
        for name, mutate, rule in forbidden_cases:
            with self.subTest(name):
                record = valid_record()
                mutate(record)
                finding = self.content_finding(record)
                self.assertIsNotNone(finding, name)
                self.assertFalse(finding.ok, name)
                self.assertIn(rule, finding.reason, name)
        pass_cases = [
            ("A01 分支/模型/版本按短 ID 语法",
             lambda r: r.update({"branch": "task/201-run-timeline", "gen_ai.request.model": "provider/model",
                                 "host_version": "0.85.1"}), None),
            ("A02 相对提示词路径、哈希与 GitHub 引用",
             lambda r: (r.update(prompt_path="docs/runs/task-x/1.prompt.md"),
                        r["stages"][1]["inputs"].append(
                            {"kind": "ci", "ref": "https://github.com/example/repo/actions/runs/9"})), None),
            ("A02 绝对路径拒绝", lambda r: r.update(prompt_path="/abs/x.md"), "字段形态"),
            ("A02 路径穿越拒绝", lambda r: r.update(prompt_path="docs/../x.md"), "字段形态"),
            ("A03 pattern 引用",
             lambda r: r["stages"][1]["inputs"].append({"kind": "pattern", "ref": "engine/**"}), None),
            ("A04 提及命令与连接符的可信理由",
             lambda r: r.update(guard_denials={RESET_HARD_REASON: 2, MERGE_REASON: 1}), None),
            ("A05 missing_context 按规定类别/原因/工具 ID",
             lambda r: r.update(missing_context=[{"category": "tool", "summary": "required_tool_unavailable",
                                                  "ref": "python"}], missing_context_status="reported"), None),
        ]
        for name, mutate, rule in pass_cases:
            with self.subTest(name):
                record = valid_record()
                mutate(record)
                finding = self.content_finding(record)
                if rule is None:
                    self.assertIsNone(finding, name)
                else:
                    self.assertIsNotNone(finding, name)
                    self.assertIn(rule, finding.reason, name)

    # ---------- 验收 2：真实旧形状与新 C3 字段通过，可信理由通过、伪造追加不行 ----------

    def test_old_and_new_safe_records_pass(self):
        # 旧格式：没有新增字段，guard_denials 是提及命令的可信理由，签名含固定问题描述
        old = {key: value for key, value in valid_record().items()
               if key not in ("trace_id", "stages", "anchors")}
        old["failure_signatures"] = ["lint:abc123456789", "工作区有未提交的改动：所有改动都要提交", "没有新的提交"]
        old["guard_denials"] = {RESET_HARD_REASON: 1, MERGE_REASON: 2}
        self.assertEqual(run_check.scan_record(old), [])
        self.assert_all_ok(old)

        # 新 C3 字段：完整时间线（含中文摘要与决策哈希）与锚点全部通过
        new = valid_record()
        self.assertEqual(run_check.scan_record(new), [])
        self.assert_all_ok(new)
        # run-check CLI 原只报告行为保持：干净记录全绿、退出码不变
        base, head = self.make_pr(new)
        code, out, _err = self.run_main(["--base", base, "--head", head, "--branch", BRANCH])
        self.assertEqual(code, 0)
        self.assertIn("✅", out)
        self.assertNotIn("❌", out)

        # 可信理由追加正文不再是例外（fail-closed）
        finding = self.content_finding({**new, "guard_denials": {RESET_HARD_REASON + "，追加正文": 1}})
        self.assertIsNotNone(finding)
        self.assertIn("未知理由", finding.reason)

    # ---------- 验收 3：失败报告只有字段位置与规则名，不回显禁项 ----------

    def test_finding_does_not_echo_secret(self):
        record = valid_record()
        record["stages"][0]["outputs"]["note"] = "git push --force origin main"
        record["failure_signatures"] = ["see /Users/someone/secret.txt"]
        record["guard_denials"] = {"未知理由正文里写了 echo hi": 1}
        findings = self.run_findings(record)
        content = next(item for item in findings if item.name == "记录内容")
        self.assertFalse(content.ok)
        for secret in ("git push --force", "origin main", "/Users/someone/secret.txt",
                       "未知理由正文里写了", "echo hi"):
            self.assertNotIn(secret, content.reason)
        for rule in ("命令正文", "本机路径", "未知理由"):
            self.assertIn(rule, content.reason)
        for location in ("stages[0].outputs.note", "failure_signatures[0]", "guard_denials"):
            self.assertIn(location, content.reason)
        rendered = run_check.render(findings)
        self.assertNotIn("git push --force", rendered)
        self.assertNotIn("/Users/someone/secret.txt", rendered)
        self.assertIn("记录内容", rendered)

    # ---------- 验收 4：policy 经 run_findings 拒绝坏记录，结论不依赖事件库 ----------

    def test_policy_rejects_bad_record_without_event_dependency(self):
        record = valid_record()
        record["stages"][0]["outputs"]["note"] = "git status"

        def offline(*_args):
            raise RuntimeError("夹具：gh 不可用")

        autonomy = {"classes": {}, "size": {}}
        for events_on in (False, True):
            with self.subTest(events_on=events_on):
                if events_on:
                    os.environ.pop("HARNESS_EVENTS", None)
                    events.emit(stage="dispatch", step="claim", status="ok", trace_id=BRANCH,
                                outputs={"claimed": True})
                    self.assertTrue(self.db_path.exists())
                else:
                    os.environ["HARNESS_EVENTS"] = "off"
                    if self.db_path.exists():
                        self.db_path.unlink()
                    self.assertFalse(self.db_path.exists())
                base, head = self.make_pr(record)
                facts = policy.gather(base, head, None, cwd=self.repo, autonomy=autonomy, gh=offline,
                                      branch=BRANCH)
                rules = policy.decide(facts, autonomy)
                record_rule = next(rule for rule in rules if rule.name == "运行记录")
                self.assertFalse(record_rule.ok)
                self.assertIn("记录内容", record_rule.reason)
                content = next(item for item in facts.run_findings if item.name == "记录内容")
                self.assertFalse(content.ok)
                self.assertIn("命令正文", content.reason)


if __name__ == "__main__":
    unittest.main()
