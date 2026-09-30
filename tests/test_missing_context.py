"""T202 缺失上下文摘要测试：提示词要求尾报告（标记、类别、空数组规则、C3 短 ID 清单）、
只从最后一条 assistant 完成消息抽取且工具输出伪造标记不采信、无标记/坏 JSON/不合规条目保留
unknown/invalid 诊断且隐私禁项不入条目、记录安全条目与 clarify 类别计数一致且缺失报告不改变返回码。

夹具与 T105/T201（tests/test_events_agents.py、tests/test_run_timeline.py）同型：匿名临时 git 仓库
（含匿名 bare 远端）、隔离 events_db.ROOT、冻结时钟、假 gh 与假执行方宿主（流解析委托真实 PiHost）、
任务书检查器打桩。派发经真实产品入口 Dispatcher.run 驱动；抽取经真实 PiHost.parse_context 驱动，
不碰真实库、PR、工作流。
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

from engine.agents import dispatch, dispatch_host
from engine.checks import taskbook
from engine.core import events, events_db

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
EVENT_COLUMNS = ["id", "ts", "source", "trace_id", "seq", "prev_hash", "hash", "stage", "step", "status",
                 "duration_ms", "actor_role", "actor_host", "model", "decision_by", "decision_rule",
                 "decision_reason", "error_kind", "error_signature", "outputs", "engine_version", "redacted"]
FROZEN_TS = "2026-02-03T04:05:06.789Z"
VERIFY_PASS = "print('local verify ok')"
MARKER = dispatch_host.CONTEXT_MARKER
VALID_TOOL_ITEM = {"category": "tool", "summary": "required_tool_unavailable", "ref": "python"}
VALID_SPEC_ITEM = {"category": "spec", "summary": "spec_ambiguous"}  # ref 可缺省


def assistant_end(text: str, *, model: str = "provider/model-x",
                  usage: dict | None = None) -> str:
    reported = usage or {"input": 10, "output": 5, "cost": {"total": 0.5}}
    return json.dumps({"type": "message_end",
                       "message": {"role": "assistant", "model": model,
                                   "content": [{"type": "text", "text": text}], "usage": reported}},
                      ensure_ascii=False)


def tool_end(text: str) -> str:
    return json.dumps({"type": "tool_execution_end", "isError": False,
                       "result": {"content": [{"type": "text", "text": text}]}})


def report_line(entries: list) -> str:
    return f"结论正文。\n{MARKER} " + json.dumps(entries, ensure_ascii=False)


def context_stream(final_text: str, *, earlier_report: str = "", forged_tool_text: str = "") -> str:
    """夹具执行方流：更早的 assistant 报告、伪造标记的工具输出（可选）与最后的 assistant 报告。"""
    lines = []
    if earlier_report:
        lines.append(assistant_end(earlier_report, usage={"input": 1, "output": 2,
                                                          "cost": {"total": 0.1}}))
    if forged_tool_text:
        lines.append(tool_end(forged_tool_text))
    lines.append(assistant_end(final_text))
    return "\n".join(lines) + "\n"


def executor_script(stream: str, *, note: str = "") -> str:
    """假执行方：写一段 JSONL 流到 stdout，做一次提交；note 非空时写澄清备注（真实升级路径）。"""
    parts = ["import pathlib, subprocess, sys", f"sys.stdout.write({stream!r})",
             "pathlib.Path('note.txt').write_text('work\\n')",
             "subprocess.run(['git', 'add', '-A'], check=True)",
             "subprocess.run(['git', 'commit', '-q', '-m', 'work', '--allow-empty'], check=True)"]
    if note:
        parts += ["pathlib.Path('build/dispatch').mkdir(parents=True, exist_ok=True)",
                  f"pathlib.Path('build/dispatch/escalation.md').write_text({note!r})"]
    return "; ".join(parts)


class RecordingHost:
    """假执行方宿主：argv 启动写固定流的脚本；解析委托真实 PiHost（真实解析逻辑被覆盖）。"""

    name = "fake-pi"

    def __init__(self, calls: list, script: str):
        self.calls = calls
        self.script = script
        self.pi = dispatch_host.PiHost()

    def version(self):
        return "9.9.9-fake"

    def argv(self, prompt, guard):
        self.calls.append(("argv", prompt))
        return [sys.executable, "-c", self.script]

    def parse(self, events_path):
        return self.pi.parse(events_path)

    def parse_observability(self, events_path):
        return self.pi.parse_observability(events_path)

    def parse_context(self, events_path):
        return self.pi.parse_context(events_path)


class FakeGitHub:
    """派发用 gh 桩：记录调用序列；push 走真实 git（本地 bare 远端），其余按脚本应答。"""

    def __init__(self, repo: Path, *, ci=(True, "", [7])):
        self.repo = repo
        self.calls: list = []
        self.ci_result = ci

    def remote_branch_exists(self, branch):
        self.calls.append(("remote_branch_exists", branch))
        return False

    def push(self, slot, branch):
        self.calls.append(("push", branch))
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        subprocess.run(["git", "push", "-q", "-f", "-u", "origin", branch], cwd=slot, check=True,
                       capture_output=True, env={**env, **GIT_ENV})

    def open_pr(self, slot, branch, title, body):
        self.calls.append(("open_pr", branch))
        return 14

    def comment(self, pr, body, label=None):
        self.calls.append(("comment", pr))
        if label:
            self.add_label(pr, label)
        return f"https://example.invalid/repo/pull/{pr}#issuecomment-1"

    def add_label(self, pr, label):
        self.calls.append(("add_label", pr, label))

    def create_issue(self, title, body, labels):
        self.calls.append(("create_issue", title, tuple(labels)))
        return "https://example.invalid/repo/issues/1"

    def wait_ci(self, branch, sha, timeout, detail=None):
        self.calls.append(("wait_ci", branch))
        ok, summary, run_ids = self.ci_result
        if detail is not None:
            detail["run_ids"] = list(run_ids)
        return ok, summary


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-missing-context-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        origin = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", ".", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("fetch", "-q", "origin")
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", return_value=FROZEN_TS)
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)
        self.stream_index = 0
        self.last_stream: Path | None = None
        self.assertIsNotNone(events_db.db_path())

    # ---------- 夹具 ----------

    def git(self, *args, check=True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_", "GITHUB_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def commit_guards(self):
        """给 origin/main 装上守卫夹具：必拒载荷直接退出 2，扩展文件存在即可。"""
        files = {"harness/command_guard.py": "import sys\nsys.exit(2)\n",
                 ".pi/extensions/harness-guard.ts": "// guard fixture\n"}
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.git("push", "-q", "origin", "main")

    def header(self, task: str) -> dict:
        return {"task": task, "class": "K7", "risk": "R3", "designer": "codex", "size": "small",
                "spec_refs": [],
                "budget": {"wall_clock_min": 5, "retries": 0, "ci_rounds": 1, "tokens": None}}

    def write_taskbook(self, rel: str, header_dict: dict):
        budget = header_dict["budget"]
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(
            "---\n"
            f"task: {header_dict['task']}\nclass: {header_dict['class']}\nrisk: {header_dict['risk']}\n"
            "designer: codex\nsize: small\narchitecture: false\nspec_refs: []\n"
            "no_spec_reason: 测试夹具\n"
            "budget:\n"
            f"  wall_clock_min: {budget['wall_clock_min']}\n  retries: {budget['retries']}\n"
            f"  ci_rounds: {budget['ci_rounds']}\n  tokens: null\n"
            "rollback: git revert\n---\n\n# 任务：夹具\n",
            encoding="utf-8")

    def stub_taskbook(self, rel: str, header_dict: dict):
        """隔离任务书检查器（admit 的下游）：返回固定 Report，on_main 无问题、豁免表为空。"""
        reports = [taskbook.Report(rel, header=header_dict)]
        for patcher in (mock.patch.object(taskbook, "check_all", lambda *args, **kwargs: reports),
                        mock.patch.object(taskbook, "on_main", lambda *args, **kwargs: None),
                        mock.patch.object(taskbook, "load_exempt", lambda *args, **kwargs: {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_dispatch(self, rel: str, host, github) -> int:
        out, err = io.StringIO(), io.StringIO()
        config = dispatch.Config(slots=2, stall_seconds=120, poll_seconds=0.05, ci_timeout_seconds=5,
                                 verify=[sys.executable, "-c", VERIFY_PASS])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return dispatch.Dispatcher(self.repo, config, github, host, identity=dict(GIT_ENV)).run(rel)

    def record(self, stem: str, number: int) -> dict:
        path = self.tmp / "app-slot-1" / "docs" / "runs" / stem / f"{number}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def all_events(self) -> list[dict]:
        path = events_db.db_path()
        if path is None or not path.exists():
            return []
        with contextlib.closing(sqlite3.connect(path)) as conn:
            try:
                rows = conn.execute(f"SELECT {','.join(EVENT_COLUMNS)} FROM events ORDER BY id").fetchall()
            except sqlite3.DatabaseError:
                return []
        result = []
        for row in rows:
            item = dict(zip(EVENT_COLUMNS, row))
            item["outputs"] = json.loads(item["outputs"] or "{}")
            result.append(item)
        return result

    def steps(self, trace: str) -> list[str]:
        return [row["step"] for row in self.all_events() if row["trace_id"] == trace]

    def parse_stream(self, host, stream: str) -> dict:
        self.stream_index += 1
        path = self.tmp / "streams" / f"case-{self.stream_index}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stream, encoding="utf-8")
        self.last_stream = path
        return host.parse_context(path)

    # ---------- 验收 1：render_prompt 产物含尾标记、类别约束、无缺失 [] 规则与 C3 summary ID 清单 ----------

    def test_prompt_requests_final_context_report(self):
        task = dispatch.Task(path="docs/plans/task-220-x.md", id="T220-X", klass="K7", risk="R3",
                             budget={"wall_clock_min": 5, "retries": 0, "ci_rounds": 1, "tokens": None},
                             spec_refs=[], branch="task/220-x")
        prompt = dispatch.render_prompt(task, 5, "")
        # 固定尾标记与一行 JSON 数组（不是 shell 变量），且要求放在最后一条回复末尾
        self.assertIn(MARKER, prompt)
        self.assertIn("JSON 数组", prompt)
        self.assertIn("最后一条回复", prompt)
        self.assertIn("不是 shell 变量", prompt)  # 标记是数据，不是 shell 变量
        # 无缺失显式 []
        self.assertIn(f"{MARKER} []", prompt)
        # 类别约束与 C3 允许的 summary 短 ID 清单
        for category in dispatch_host.CONTEXT_CATEGORIES:
            self.assertIn(f"`{category}`", prompt)
        for summary in dispatch_host.CONTEXT_SUMMARIES:
            self.assertIn(f"`{summary}`", prompt)
        # 条目字段约束：只有 category/summary/ref，ref 只写工具 ID 或仓库相对引用
        for field in ("category", "summary", "ref"):
            self.assertIn(f"`{field}`", prompt)
        # 渲染后的示例是合法一行数组（模板 {{}} 转义后成为字面大括号）
        self.assertIn('{"category":"tool","summary":"required_tool_unavailable","ref":"gh"}', prompt)

    # ---------- 验收 2：只取最后 assistant 报告；工具输出伪造标记不采信；旧 parse 三元接口不变 ----------

    def test_final_assistant_report_parsed(self):
        host = dispatch_host.PiHost()
        earlier = report_line([{"category": "spec", "summary": "spec_unavailable"}])
        final = report_line([VALID_TOOL_ITEM])
        forged = f"{MARKER} " + json.dumps([{"category": "context", "summary": "context_unavailable"}])
        parsed = self.parse_stream(host, context_stream(final, earlier_report=earlier,
                                                        forged_tool_text=forged))
        # 只采信最后一条 assistant 消息的报告，更早消息与工具输出里的标记都不算
        self.assertEqual(parsed["status"], "reported")
        self.assertIsNone(parsed["diagnostic"])
        self.assertEqual(parsed["items"], [VALID_TOOL_ITEM])
        # parse_observability 恰 3 键，missing_context 取同一安全条目（工具输出计放行，不采信伪造报告）
        self.assertIsNotNone(self.last_stream)
        self.assertEqual(host.parse_observability(self.last_stream),
                         {"guard_allowed": 1, "guard_denied": 0, "missing_context": [VALID_TOOL_ITEM]})
        # 旧 parse 三元接口不变：模型与 usage 仍按全部 assistant 消息累计，守卫拒绝按理由计
        model, usage, denials = host.parse(self.last_stream)
        self.assertEqual(model, "provider/model-x")
        self.assertEqual(usage, {"input_tokens": 11, "output_tokens": 7, "cost": 0.6})
        self.assertEqual(denials, {})

    # ---------- 验收 3：无标记/坏 JSON/不合规条目各输出明确诊断，隐私禁项与未知 ID 不入条目 ----------

    def test_absent_invalid_and_private_context(self):
        host = dispatch_host.PiHost()

        # 无标记：流缺失或正文没有尾标记，都是 unknown，不静默当无缺失
        self.assertEqual(host.parse_context(self.tmp / "absent.jsonl"),
                         {"status": "unknown", "items": [], "diagnostic": "no_marker"})
        self.assertEqual(self.parse_stream(host, context_stream("一切正常，无缺失。")),
                         {"status": "unknown", "items": [], "diagnostic": "no_marker"})

        # 坏 JSON 与非数组：标记在但格式错误，保留 invalid 诊断
        self.assertEqual(self.parse_stream(host, context_stream(f"{MARKER} [broken,")),
                         {"status": "invalid", "items": [], "diagnostic": "bad_json"})
        self.assertEqual(self.parse_stream(host, context_stream(MARKER)),
                         {"status": "invalid", "items": [], "diagnostic": "bad_json"})
        self.assertEqual(self.parse_stream(
            host, context_stream(f'{MARKER} {{"category": "tool"}}')),
            {"status": "invalid", "items": [], "diagnostic": "not_array"})

        # 条目不合规：未知 ID、自由正文、类别不对应、本机路径/穿越/超长 ref、多余字段、非 dict
        bad_entries = [
            {"category": "tool", "summary": "need_more_info"},  # 未知 summary ID
            {"category": "spec", "summary": "缺少设计文档第三章"},  # 任意自然语言正文
            {"category": "context", "summary": "required_tool_unavailable"},  # 短 ID 与类别不对应
            {"category": "tool", "summary": "required_tool_unavailable", "ref": "/Users/x/notes.md"},
            {"category": "tool", "summary": "required_tool_unavailable", "ref": "C:\\tmp\\x"},
            {"category": "tool", "summary": "required_tool_unavailable", "ref": "../outside.md"},
            {"category": "tool", "summary": "required_tool_unavailable", "ref": "a" * 200},
            {"category": "tool", "summary": "required_tool_unavailable", "note": "extra"},
            "plain string entry",
        ]
        parsed = self.parse_stream(host, context_stream(report_line(bad_entries)))
        self.assertEqual(parsed["status"], "invalid")
        self.assertEqual(parsed["diagnostic"], "invalid_entry")
        self.assertEqual(parsed["items"], [])  # 禁项一条都不入
        items_text = json.dumps(parsed["items"], ensure_ascii=False)
        for forbidden in ("/Users/", "C:\\", "缺少设计文档", "need_more_info", "outside.md"):
            self.assertNotIn(forbidden, items_text)

        # 混合报告：不合规条目标 invalid 丢弃，合规条目保留，状态如实标 invalid（不冒充空报告）
        parsed = self.parse_stream(host, context_stream(report_line([VALID_TOOL_ITEM, bad_entries[1]])))
        self.assertEqual(parsed["status"], "invalid")
        self.assertEqual(parsed["diagnostic"], "invalid_entry")
        self.assertEqual(parsed["items"], [VALID_TOOL_ITEM])

        # 显式 [] 与缺 ref 的合规条目：reported
        self.assertEqual(self.parse_stream(host, context_stream(f"{MARKER} []")),
                         {"status": "reported", "items": [], "diagnostic": None})
        parsed = self.parse_stream(host, context_stream(report_line([VALID_SPEC_ITEM])))
        self.assertEqual(parsed["status"], "reported")
        self.assertEqual(parsed["items"], [VALID_SPEC_ITEM])

    # ---------- 验收 4：记录安全条目与 clarify 类别计数一致；成功并报告缺失仍沿原返回码 ----------

    def test_record_and_clarify_event_counts(self):
        # 场景 A：报告两条缺失 + 澄清备注 → 记录与 clarify 事件计数同源一致，退出码沿原规则（1）
        rel = "docs/plans/task-220-a.md"
        self.write_taskbook(rel, self.header("T220-A"))
        self.stub_taskbook(rel, self.header("T220-A"))
        self.commit_guards()
        stream = context_stream(report_line([VALID_TOOL_ITEM, VALID_SPEC_ITEM]))
        host = RecordingHost([], executor_script(stream, note="缺上下文，请补充规格"))
        code = self.run_dispatch(rel, host, FakeGitHub(self.repo))
        self.assertEqual(code, 1)  # 澄清沿原升级路径，缺失报告不改变退出码
        record = self.record("task-220-a", 1)
        self.assertEqual(record["missing_context"], [VALID_TOOL_ITEM, VALID_SPEC_ITEM])
        self.assertEqual(record["missing_context_status"], "reported")
        self.assertIn("clarify", self.steps("task/220-a"))
        clarify_outputs = next(row["outputs"] for row in self.all_events()
                               if row["trace_id"] == "task/220-a" and row["step"] == "clarify")
        self.assertEqual(clarify_outputs,
                         {"requested": True, "missing_context": 2,
                          "missing_context.spec": 1, "missing_context.tool": 1})
        # 类别计数与记录条目一致
        per_category: dict = {}
        for item in record["missing_context"]:
            per_category[item["category"]] = per_category.get(item["category"], 0) + 1
        self.assertEqual({key: value for key, value in clarify_outputs.items()
                          if key.startswith("missing_context.")},
                         {f"missing_context.{category}": count
                          for category, count in sorted(per_category.items())})

        # 场景 B：成功完成并报告缺失 → 沿原返回码 0、正常开 PR，记录有安全条目、无 clarify 事件
        rel = "docs/plans/task-220-b.md"
        self.write_taskbook(rel, self.header("T220-B"))
        self.stub_taskbook(rel, self.header("T220-B"))
        gh = FakeGitHub(self.repo)
        code = self.run_dispatch(rel, RecordingHost([], executor_script(stream)), gh)
        self.assertEqual(code, 0)
        self.assertNotIn("clarify", self.steps("task/220-b"))
        self.assertIn(("open_pr", "task/220-b"), gh.calls)
        record = self.record("task-220-b", 1)
        self.assertEqual(record["exit"], "ok")
        self.assertEqual(record["missing_context"], [VALID_TOOL_ITEM, VALID_SPEC_ITEM])
        self.assertEqual(record["missing_context_status"], "reported")

        # 场景 C：成功但报告不合规 → 仍 0，记录为空条目且状态如实标 invalid（不冒充 unknown/无缺失）
        rel = "docs/plans/task-220-c.md"
        self.write_taskbook(rel, self.header("T220-C"))
        self.stub_taskbook(rel, self.header("T220-C"))
        code = self.run_dispatch(rel, RecordingHost(
            [], executor_script(context_stream(report_line(
                [{"category": "tool", "summary": "缺 gh 工具，跑不了 CI"}])))), FakeGitHub(self.repo))
        self.assertEqual(code, 0)
        record = self.record("task-220-c", 1)
        self.assertEqual(record["missing_context"], [])
        self.assertEqual(record["missing_context_status"], "invalid")

        # 场景 D：无标记 → 记录恒写两键且状态 unknown（C3 unknown 可见性）
        rel = "docs/plans/task-220-d.md"
        self.write_taskbook(rel, self.header("T220-D"))
        self.stub_taskbook(rel, self.header("T220-D"))
        code = self.run_dispatch(rel, RecordingHost(
            [], executor_script(context_stream("完成，无缺失说明。"))), FakeGitHub(self.repo))
        self.assertEqual(code, 0)
        record = self.record("task-220-d", 1)
        self.assertEqual(record["missing_context"], [])
        self.assertEqual(record["missing_context_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
