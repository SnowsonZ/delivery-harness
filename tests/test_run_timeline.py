"""T201 运行记录时间线测试：记录里的安全 stages/anchors 摘要、落盘前链头锚点、多轮多尝试不混时间线、
旧字段与现有 run-check 兼容、不为补时间线追加推送且 sink 不可用时锚点为空、原任务继续。

夹具与 T105（tests/test_events_agents.py）同型：匿名临时 git 仓库（含匿名 bare 远端）、隔离
events_db.ROOT、冻结时钟、假 gh 与假执行方宿主（流解析委托真实 PiHost）、任务书检查器打桩。
派发经真实产品入口 Dispatcher.run 驱动，只隔离外部副作用（执行方二进制、gh、任务书检查器、verify
命令），不碰真实库、PR、工作流。
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
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from engine.agents import dispatch, dispatch_host, run_timeline
from engine.checks import taskbook
from engine.core import events, events_db
from engine.routing import run_check

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
# 状态机 verify：第 2 次运行失败、其余通过——制造「尝试 2 有两轮本地判定」的时间线（第 1 次属于尝试 1）。
VERIFY_STATEFUL = (
    "import pathlib, sys\n"
    "state = pathlib.Path('build/verify-round')\n"
    "n = int(state.read_text()) if state.exists() else 0\n"
    "state.parent.mkdir(parents=True, exist_ok=True)\n"
    "state.write_text(str(n + 1))\n"
    "sys.exit(1 if n == 1 else 0)\n"
)
STAGE_KEYS = {"stage", "step", "status", "ts", "duration_ms", "attempt", "round"}  # B77 瘦身后的单条字段
POINTER_KEYS = {"stage", "step", "status", "ts", "duration_ms", "attempt", "round"}  # 终评修复配套：指针行七键  # B77 更早 attempt 的 push_pr/ci_wait 指针行
ANCHOR_KEYS = {"source", "stage", "head_hash", "fixed_in"}


def mixed_stream() -> str:
    """夹具执行方流：成功、双理由拒绝、单理由拒绝、普通失败、损坏行、无结果各一（与 T105 同型）。"""
    lines = [
        json.dumps({"type": "message_end", "message": {"role": "assistant", "model": "provider/model-x",
                                                       "usage": {"input": 10, "output": 5,
                                                                 "cost": {"total": 0.5}}}}),
        json.dumps({"type": "tool_execution_end", "isError": False,
                    "result": {"content": [{"type": "text", "text": "done"}]}}),
        json.dumps({"type": "tool_execution_end", "isError": True,
                    "result": {"content": [{"type": "text", "text": "harness 守卫拒绝了这次操作：\n- 理由甲\n- 理由乙"}]}}),
        json.dumps({"type": "tool_execution_end", "isError": True,
                    "result": {"content": [{"type": "text", "text": "harness 守卫拒绝了这次操作：\n- 理由甲"}]}}),
        json.dumps({"type": "tool_execution_end", "isError": True,
                    "result": {"content": [{"type": "text", "text": "命令以退出码 1 失败"}]}}),
        "not-json-corrupted-line",
        json.dumps({"type": "tool_execution_end", "isError": False}),
        "",
    ]
    return "\n".join(lines) + "\n"


def executor_script(stream: str) -> str:
    """假执行方：写一段 JSONL 流到 stdout，做一次提交（--allow-empty 让无改动的重试轮也能过）。"""
    parts = ["import pathlib, subprocess, sys", f"sys.stdout.write({stream!r})",
             "pathlib.Path('note.txt').write_text('work\\n')",
             "subprocess.run(['git', 'add', '-A'], check=True)",
             "subprocess.run(['git', 'commit', '-q', '-m', 'work', '--allow-empty'], check=True)"]
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


class FakeGitHub:
    """派发用 gh 桩：记录调用序列；push 走真实 git（本地 bare 远端），其余按脚本应答。"""

    def __init__(self, repo: Path, *, branch_exists: bool = False, ci=(True, "", [7])):
        self.repo = repo
        self.calls: list = []
        self.branch_exists = branch_exists
        self.ci_result = ci
        self.pr = None

    def remote_branch_exists(self, branch):
        self.calls.append(("remote_branch_exists", branch))
        return self.branch_exists

    def push(self, slot, branch):
        self.calls.append(("push", branch))
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        # 夹具远端上同名分支可能是上一状态留下的：强推覆盖，保持各状态调用序列一致
        subprocess.run(["git", "push", "-q", "-f", "-u", "origin", branch], cwd=slot, check=True,
                       capture_output=True, env={**env, **GIT_ENV})

    def open_pr(self, slot, branch, title, body):
        self.calls.append(("open_pr", branch))
        self.pr = 14
        return self.pr

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

    def existing_pr(self, branch):
        """分支已有开放 PR 的桩（B70/T121）：缺省无既有 PR；不记入 calls，既有调用序列断言不变。"""
        return getattr(self, "existing", None)


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-run-timeline-"))
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

    def origin_main(self) -> str:
        return self.git("rev-parse", "origin/main").stdout.strip()

    def slot_head(self, slot: Path) -> str:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=slot, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=True).stdout.strip()

    def header(self, task: str, *, retries=0) -> dict:
        return {"task": task, "class": "K7", "risk": "R3", "designer": "codex", "size": "small",
                "spec_refs": [],
                "budget": {"wall_clock_min": 5, "retries": retries, "ci_rounds": 1, "tokens": None}}

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

    def make_dispatcher(self, host, github, *, retries=0, slots=2, verify_script: str = VERIFY_PASS):
        config = dispatch.Config(slots=slots, stall_seconds=120, poll_seconds=0.05, ci_timeout_seconds=5,
                                 verify=[sys.executable, "-c", verify_script])
        return dispatch.Dispatcher(self.repo, config, github, host, identity=dict(GIT_ENV))

    def run_dispatch(self, rel: str, host, github, **kwargs) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.make_dispatcher(host, github, **kwargs).run(rel)
        return code, out.getvalue(), err.getvalue()

    def record(self, stem: str, number: int) -> tuple[Path, dict]:
        path = self.tmp / "app-slot-1" / "docs" / "runs" / stem / f"{number}.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

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

    def trace_events(self, trace: str) -> list[dict]:
        return [row for row in self.all_events() if row["trace_id"] == trace]

    def anchors_table(self) -> list[tuple]:
        with contextlib.closing(sqlite3.connect(events_db.db_path())) as conn:
            return conn.execute(
                "SELECT source,trace_id,stage,head_hash,fixed_in FROM anchors ORDER BY id").fetchall()

    @staticmethod
    def sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # ---------- 验收 1：一次本地执行后记录具备安全 stages、trace，锚点精确等于落盘前链头 ----------

    def test_record_contains_timeline_and_exact_anchor(self):
        rel = "docs/plans/task-210-a.md"
        self.write_taskbook(rel, self.header("T210-A"))
        self.stub_taskbook(rel, self.header("T210-A"))
        self.commit_guards()
        gh = FakeGitHub(self.repo, ci=(True, "", [7]))
        code, _, _ = self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream())), gh)
        self.assertEqual(code, 0)
        branch = "task/210-a"
        record_path, record = self.record("task-210-a", 1)

        # trace_id 与 stages：准入至本地检查按真实链序，没有尚未发生的 push_pr/ci_wait
        self.assertEqual(record["trace_id"], branch)
        stages = record["stages"]
        self.assertEqual([(item["step"], item["status"]) for item in stages],
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "ok")])
        rows = self.trace_events(branch)
        self.assertEqual([row["step"] for row in rows[6:]], ["push_pr", "ci_wait"])  # 记录之后才发生

        # 每项都是 B77 瘦身后的索引形状：七个标量字段，inputs/outputs/decision 等细节留在事件库
        for item in stages:
            self.assertEqual(set(item), STAGE_KEYS)
            self.assertEqual(item["ts"], FROZEN_TS)
            self.assertTrue(item["duration_ms"] is None or item["duration_ms"] >= 0)
            self.assertTrue(all(isinstance(item[key], (str, int, float)) or item[key] is None
                                for key in STAGE_KEYS))
        by_step = {item["step"]: item for item in stages}
        self.assertIsInstance(by_step["executor_round"]["duration_ms"], int)  # 执行环节计时长
        self.assertEqual([item["attempt"] for item in stages], [1] * 6)
        self.assertEqual([item["round"] for item in stages], [0, 0, 0, 0, 1, 1])

        # 锚点精确等于落盘前链头（不是跑完后的最终链头），且 anchors 表有同值一行
        head = rows[5]["hash"]
        self.assertEqual(len(record["anchors"]), 1)
        self.assertEqual(set(record["anchors"][0]), ANCHOR_KEYS)
        self.assertEqual(record["anchors"][0], {"source": "local", "stage": "dispatch",
                                                "head_hash": head, "fixed_in": "run_record"})
        self.assertNotEqual(head, events_db.chain_head("local", branch))  # 落盘后链又合法追加了两个事件
        self.assertEqual(self.anchors_table(), [("local", branch, "dispatch", head, "run_record")])
        self.assertEqual(events_db.verify(), [])  # 后续合法追加不断链、早期锚点不误报

        # 新增摘要字段是安全的：不含原始事件流、守卫理由正文、完整日志或本机路径
        # （旧字段 guard_denials 的安全规则摘要按 C3 允许保留，不在本断言范围）
        summary_text = json.dumps({"stages": record["stages"], "anchors": record["anchors"]},
                                  ensure_ascii=False)
        self.assertNotIn("理由甲", summary_text)
        self.assertNotIn("tool_execution_end", summary_text)
        self.assertNotIn("local verify ok", summary_text)
        self.assertNotIn("/Users/", summary_text)

        # 隐私口径（对 run_timeline 产品入口直查）：原始理由正文不因新事件进入摘要，
        # 理由细节留在事件库（B77 后 stages 不再携带 decision/outputs）
        events.emit(stage="dispatch", step="claim", status="fail", trace_id=branch,
                    outputs={"claimed": False},
                    decision={"by": "dispatch", "rule": "claim", "reason": "已被认领"})
        events.emit(stage="dispatch", step="executor_round", status="fail", trace_id=branch,
                    outputs={"exit": "timeout"},
                    decision={"by": "dispatch", "rule": "round", "reason": "超出时长预算"})
        before = record_path.read_bytes()
        timeline, latest = run_timeline.record_fields(branch)
        claim_item, round_item = timeline["stages"][-2], timeline["stages"][-1]
        self.assertEqual((claim_item["step"], claim_item["status"]), ("claim", "fail"))
        self.assertEqual((round_item["step"], round_item["status"]), ("executor_round", "fail"))
        self.assertNotIn("已被认领", json.dumps(timeline, ensure_ascii=False))
        self.assertNotIn("超出时长预算", json.dumps(timeline, ensure_ascii=False))
        self.assertEqual(latest, self.trace_events(branch)[-1]["hash"])
        self.assertEqual(record_path.read_bytes(), before)  # 摘要查询不改写已落盘的记录

    # ---------- 验收 2：两轮执行/两次尝试不混成一次时间线，失败事件与真实顺序可见 ----------

    def test_retry_stages_keep_attempt_and_round(self):
        rel = "docs/plans/task-210-b.md"
        header = self.header("T210-B", retries=1)
        header["budget"]["ci_rounds"] = 2
        self.write_taskbook(rel, header)
        self.stub_taskbook(rel, header)
        self.commit_guards()
        gh = FakeGitHub(self.repo, ci=(False, "CI 未通过：见摘要", [5]))
        code, _, _ = self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream())), gh,
                                       retries=1, verify_script=VERIFY_STATEFUL)
        self.assertEqual(code, 1)  # 两轮 CI 都失败：预算用尽升级，但两次尝试的记录都已落盘
        self.assertIn(("add_label", 14, "budget-exceeded"), gh.calls)

        _, record1 = self.record("task-210-b", 1)
        _, record2 = self.record("task-210-b", 2)
        self.assertEqual([record["attempt"] for record in (record1, record2)], [1, 2])
        self.assertEqual([record["exit"] for record in (record1, record2)], ["ok", "ok"])
        self.assertEqual([record["retries"] for record in (record1, record2)], [0, 1])
        self.assertEqual([record["ci_rounds_before"] for record in (record1, record2)], [0, 1])

        # 记录 1 落盘时只有第 1 次尝试的第 1 轮：没有 push_pr/ci_wait
        self.assertEqual([(item["step"], item["status"], item["attempt"], item["round"])
                          for item in record1["stages"]],
                         [("admit", "ok", 1, 0), ("guard_preflight", "ok", 1, 0), ("slot", "ok", 1, 0),
                          ("claim", "ok", 1, 0), ("executor_round", "ok", 1, 1), ("local_verify", "ok", 1, 1)])

        # 记录 2 的时间线（B77 收敛）：更早 attempt 只留 push_pr/ci_wait 指针行，本次 attempt 事件完整、
        # 尝试与轮次计数不混（失败事件在真实位置）
        self.assertEqual([(item["step"], item["status"], item["attempt"])
                          for item in record2["stages"][:2]],
                         [("push_pr", "prior", 1), ("ci_wait", "prior", 1)])
        for pointer in record2["stages"][:2]:
            self.assertEqual(set(pointer), POINTER_KEYS)
        self.assertEqual([(item["step"], item["status"], item["attempt"], item["round"])
                          for item in record2["stages"][2:]],
                         [("executor_round", "ok", 2, 1), ("local_verify", "fail", 2, 1),
                          ("executor_round", "ok", 2, 2), ("local_verify", "ok", 2, 2)])
        rows = self.trace_events("task/210-b")
        # 各自锚点指向各自落盘前的链头
        self.assertEqual(record1["anchors"][0]["head_hash"], rows[5]["hash"])
        self.assertEqual(record2["anchors"][0]["head_hash"], rows[11]["hash"])
        self.assertEqual(events_db.verify(), [])

    # ---------- 验收 3：新增字段以外与原记录夹具一致，现有 run-check 能验证原字段与提示词哈希 ----------

    def test_legacy_record_fields_preserved(self):
        rel = "docs/plans/task-210-c.md"
        self.write_taskbook(rel, self.header("T210-C"))
        self.stub_taskbook(rel, self.header("T210-C"))
        self.commit_guards()
        gh = FakeGitHub(self.repo, ci=(True, "", [7]))
        code, _, _ = self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream())), gh)
        self.assertEqual(code, 0)
        slot = self.tmp / "app-slot-1"
        _, record = self.record("task-210-c", 1)
        # 键集合：原记录字段一个不少、一个不多，新增只有 trace_id/stages/anchors 与
        # missing_context/missing_context_status（T202，A1 裁决：恒写两键，C3 unknown 可见性）
        legacy_extra = {"executor_seconds", "ci_rounds_before", "gen_ai.usage.input_tokens",
                        "gen_ai.usage.output_tokens", "cost", "escalation"}
        self.assertEqual(set(record),
                         set(run_check.RECORD_FIELDS) | legacy_extra
                         | {"trace_id", "stages", "anchors", "missing_context", "missing_context_status"})
        # 旧字段语义逐项钉住
        self.assertEqual((record["task"], record["class"], record["attempt"], record["branch"]),
                         ("T210-C", "K7", 1, "task/210-c"))
        self.assertEqual((record["gen_ai.agent.name"], record["host_version"]), ("fake-pi", "9.9.9-fake"))
        self.assertEqual(record["gen_ai.request.model"], "provider/model-x")
        self.assertEqual((record["gen_ai.usage.input_tokens"], record["gen_ai.usage.output_tokens"],
                          record["cost"]), (10, 5, 0.5))
        self.assertEqual((record["exit"], record["retries"], record["failure_signatures"]), ("ok", 0, []))
        self.assertEqual(record["guard_denials"], {"理由甲": 2, "理由乙": 1})  # 旧字段语义：按理由计数
        self.assertEqual(record["guard_ref"], self.origin_main())
        self.assertEqual(record["prompt_path"], "docs/runs/task-210-c/1.prompt.md")
        self.assertTrue(record["started_at"] and record["ended_at"])
        self.assertLessEqual(record["started_at"], record["ended_at"])
        self.assertEqual(record["escalation"], None)
        self.assertEqual(record["ci_rounds_before"], 0)
        self.assertGreaterEqual(record["executor_seconds"], 0.0)
        # 提示词字节未变：记录里的 sha256 与落盘文件原始字节一致
        prompt = slot / record["prompt_path"]
        self.assertEqual(hashlib.sha256(prompt.read_bytes()).hexdigest(), record["prompt_sha256"])
        # 现有 run-check（真实产品入口）仍能验证原字段与提示词哈希（按 git 原始字节比对）
        scope = run_check.Scope(rel, {"task": "T210-C", "class": "K7", "budget": {"ci_rounds": 1}})
        finding = run_check.check_record(self.origin_main(), self.slot_head(slot), scope, "task/210-c",
                                         cwd=slot)
        self.assertTrue(finding.ok, finding.reason)

    # ---------- 验收 4：原推送次数与 CI 轮次不变；sink 不可用时锚点为空且原任务继续 ----------

    def test_no_extra_push_for_post_record_stages(self):
        rel = "docs/plans/task-210-d.md"
        self.write_taskbook(rel, self.header("T210-D"))
        self.stub_taskbook(rel, self.header("T210-D"))
        self.commit_guards()
        gh = FakeGitHub(self.repo, ci=(True, "", [7]))
        code, _, _ = self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream())), gh)
        self.assertEqual(code, 0)
        # 真实调用序列：认领推送 + 记录推送共两次，等一次 CI；不为补 push_pr/ci_wait 锚点再写记录推送
        self.assertEqual(gh.calls, [("remote_branch_exists", "task/210-d"), ("push", "task/210-d"),
                                    ("push", "task/210-d"), ("open_pr", "task/210-d"),
                                    ("wait_ci", "task/210-d")])
        _, record = self.record("task-210-d", 1)
        self.assertEqual(record["ci_rounds_before"], 0)
        self.assertNotIn("push_pr", [item["step"] for item in record["stages"]])
        self.assertNotIn("ci_wait", [item["step"] for item in record["stages"]])
        self.assertEqual(record["anchors"][0]["fixed_in"], "run_record")
        self.assertTrue(record["anchors"][0]["head_hash"])

        # sink 不可用（库损坏）：读不到链，记录 stages/anchors 为空，原任务照常完成、调用序列不变
        events_db.db_path().write_bytes(b"this is not a sqlite database at all")
        rel_b = "docs/plans/task-210-e.md"
        self.write_taskbook(rel_b, self.header("T210-E"))
        self.stub_taskbook(rel_b, self.header("T210-E"))
        gh2 = FakeGitHub(self.repo, ci=(True, "", [7]))
        code, _, _ = self.run_dispatch(rel_b, RecordingHost([], executor_script(mixed_stream())), gh2)
        self.assertEqual(code, 0)
        self.assertEqual(gh2.calls, [("remote_branch_exists", "task/210-e"), ("push", "task/210-e"),
                                     ("push", "task/210-e"), ("open_pr", "task/210-e"),
                                     ("wait_ci", "task/210-e")])
        _, record2 = self.record("task-210-e", 1)
        self.assertEqual(record2["trace_id"], "task/210-e")
        self.assertEqual(record2["stages"], [])
        self.assertEqual(record2["anchors"], [])  # 锚点为空
        self.assertEqual(record2["exit"], "ok")

    # ---------- 验收 5（B69）：库锁竞争窗口内读取等待而非立即失败退化为空 ----------

    def test_read_events_waits_for_lock(self):
        rel = "docs/plans/task-210-f.md"
        self.write_taskbook(rel, self.header("T210-F"))
        self.stub_taskbook(rel, self.header("T210-F"))
        self.commit_guards()
        gh = FakeGitHub(self.repo, ci=(True, "", [7]))
        code, _, _ = self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream())), gh)
        self.assertEqual(code, 0)
        branch = "task/210-f"
        rows = self.trace_events(branch)
        self.assertEqual([row["step"] for row in rows[6:]], ["push_pr", "ci_wait"])

        # WAL 下写事务不阻塞读者；把库切回回滚日志模式，另一连接的独占写事务才能确定性复现
        # 锁竞争：读取在持锁窗口内等待（连接的 busy_timeout/timeout 生效）而非立即抛
        # database is locked 退化为空摘要。注意 sqlite3.connect 的 Python 默认 timeout 恰为
        # 5 秒，与本仓库 busy_timeout=5000 行为等价——本用例防的是「完全无等待」的退化，
        # 不区分两种 5 秒来源（连接参数单点收口的价值见 run_timeline._read_events 注释）。
        path = events_db.db_path()
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")

        acquired = threading.Event()

        def hold_write_transaction():
            with contextlib.closing(events_db._connect(path)) as conn:
                conn.execute("BEGIN EXCLUSIVE")
                acquired.set()  # 先拿到锁，再放读者进来（消除先后竞态）
                time.sleep(1.0)  # 持锁窗口自限时提交：读者在窗口内等待后读到完整链
                conn.execute("COMMIT")

        holder = threading.Thread(target=hold_write_transaction)
        holder.start()
        self.assertTrue(acquired.wait(10), "持锁线程未能获得独占事务")
        # 读者此时必然撞上独占锁：能返回完整时间线说明读取等待了持锁窗口
        # （无任何等待超时的连接在此立即抛 database is locked 并退化为空摘要）
        timeline, head = run_timeline.record_fields(branch)
        holder.join()

        # 等待后成功：时间线与锚点完整（准入至本地检查六阶段，外加记录后已发生的 push_pr/ci_wait）
        self.assertEqual([(item["step"], item["status"]) for item in timeline["stages"]],
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "ok"),
                          ("push_pr", "ok"), ("ci_wait", "ok")])
        self.assertEqual(timeline["anchors"], [{"source": "local", "stage": "dispatch",
                                                "head_hash": rows[-1]["hash"], "fixed_in": "run_record"}])
        self.assertEqual(head, rows[-1]["hash"])

if __name__ == "__main__":
    unittest.main()
