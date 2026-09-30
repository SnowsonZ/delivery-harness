"""T205 告警测试：最后一轮 CI 预警在 wait 前、守卫拒绝阈值按被拒工具计数、远端标记去重、发布失败隔离。

夹具沿用 tests/test_events_agents.py 的模式：匿名临时 git 仓库（含匿名 bare 远端）、隔离 events_db.ROOT、
冻结时钟、假 gh 与假执行方宿主（流解析委托真实 PiHost），不碰真实库/PR/工作流。派发经真实产品入口
（Dispatcher.run）驱动；告警去重与发布直接经共用发布入口 alerts.publish 驱动。[alerts] 配置经打桩
alerts.load_rules 注入——真实 rules.toml 不含该节，既有派发场景因此不受预警影响（派发预警默认关闭，
模板中该节也是注释状态，显式启用后才生效）。
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch, dispatch_host
from engine.core import alerts, events, events_db

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


def denied(reasons: list[str]) -> str:
    """一次被守卫拒绝的工具调用结果：多个拒绝理由仍算一次调用（不按理由计数）。"""
    return dispatch_host.GUARD_DENIAL + "：\n" + "\n".join(f"- {reason}" for reason in reasons)


def stream(*calls: str) -> str:
    """夹具执行方流：一条 assistant 完成消息 + 若干工具完成事件（文本含守卫拒绝标记计为被拒）。"""
    lines = [json.dumps({"type": "message_end",
                         "message": {"role": "assistant", "model": "provider/model-x",
                                     "usage": {"input": 1, "output": 1, "cost": {"total": 0.25}}}})]
    for text in calls:
        lines.append(json.dumps({"type": "tool_execution_end", "isError": dispatch_host.GUARD_DENIAL in text,
                                 "result": {"content": [{"type": "text", "text": text}]}}))
    return "\n".join(lines) + "\n"


def executor_script(payload: str) -> str:
    """假执行方：写流到 stdout、落一个文件并提交（--allow-empty：重试轮没有新改动也能提交）。"""
    return ("import pathlib, subprocess, sys; "
            f"sys.stdout.write({payload!r}); "
            "pathlib.Path('note.txt').write_text('work\\n'); "
            "subprocess.run(['git', 'add', '-A'], check=True); "
            "subprocess.run(['git', 'commit', '-q', '-m', 'work', '--allow-empty'], check=True)")


class RecordingHost:
    """假执行方宿主：argv 启动写固定流的脚本；解析委托真实 PiHost（真实解析逻辑被覆盖）。"""

    name = "fake-pi"

    def __init__(self, script: str):
        self.script = script
        self.pi = dispatch_host.PiHost()

    def version(self):
        return "9.9.9-fake"

    def argv(self, prompt, guard):
        return [sys.executable, "-c", self.script]

    def parse(self, events_path):
        return self.pi.parse(events_path)

    def parse_observability(self, events_path):
        return self.pi.parse_observability(events_path)


class FakeGitHub:
    """派发用 gh 桩：记录调用序列与评论/议题内容；fail_publish 时发布类调用抛 RuntimeError。"""

    def __init__(self, *, branch_exists: bool = False, ci=(True, "", [7]), fail_publish: bool = False):
        self.calls: list = []
        self.branch_exists = branch_exists
        self.ci_result = ci
        self.fail_publish = fail_publish
        self.pushes: list[str] = []
        self.pr = None
        self.comments: list[tuple] = []  # (pr, body, label)，按创建顺序
        self.edited_comments: list[tuple] = []  # (comment_id, body)
        self.issues: list[dict] = []  # {"number", "title", "body", "labels"}
        self.edited_issues: list[tuple] = []  # (number, body)
        self.labels: list[tuple] = []  # (pr, label)

    def remote_branch_exists(self, branch):
        self.calls.append(("remote_branch_exists", branch))
        return self.branch_exists

    def push(self, slot, branch):
        self.calls.append(("push", branch))
        self.pushes.append(branch)

    def open_pr(self, slot, branch, title, body):
        self.calls.append(("open_pr", branch))
        self.pr = 14
        return self.pr

    def comment(self, pr, body, label=None):
        if self.fail_publish:
            raise RuntimeError("gh down")
        self.calls.append(("comment", pr, body, label))
        self.comments.append((pr, body, label))
        if label:
            self.add_label(pr, label)
        return f"https://example.invalid/pull/{pr}#issuecomment-{len(self.comments)}"

    def add_label(self, pr, label):
        self.calls.append(("add_label", pr, label))
        self.labels.append((pr, label))

    def create_issue(self, title, body, labels):
        if self.fail_publish:
            raise RuntimeError("gh down")
        self.calls.append(("create_issue", title, tuple(labels)))
        issue = {"number": 100 + len(self.issues), "title": title, "body": body, "labels": list(labels)}
        self.issues.append(issue)
        return f"https://example.invalid/issues/{issue['number']}"

    def list_comments(self, pr):
        self.calls.append(("list_comments", pr))
        return [{"id": index + 1, "body": item[1]} for index, item in enumerate(self.comments) if item[0] == pr]

    def edit_comment(self, comment_id, body):
        if self.fail_publish:
            raise RuntimeError("gh down")
        self.calls.append(("edit_comment", comment_id))
        self.edited_comments.append((comment_id, body))
        pr, _old, label = self.comments[comment_id - 1]
        self.comments[comment_id - 1] = (pr, body, label)

    def list_issues(self, label):
        self.calls.append(("list_issues", label))
        return [{"number": issue["number"], "body": issue["body"]}
                for issue in self.issues if label in issue["labels"]]

    def edit_issue(self, number, body):
        if self.fail_publish:
            raise RuntimeError("gh down")
        self.calls.append(("edit_issue", number))
        self.edited_issues.append((number, body))
        for issue in self.issues:
            if issue["number"] == number:
                issue["body"] = body

    def wait_ci(self, branch, sha, timeout, detail=None):
        self.calls.append(("wait_ci", branch))
        ok, summary, run_ids = self.ci_result
        if detail is not None:
            detail["run_ids"] = list(run_ids)
        return ok, summary


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-dispatch-alerts-"))
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
        self.reset_notice_flags()

    def reset_notice_flags(self):
        """复位每进程一次的提示标记，让各子场景的 stderr 断言确定。"""
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)
        alerts._hinted = False
        self.addCleanup(setattr, alerts, "_hinted", False)

    # ---------- 夹具 ----------

    def git(self, *args, check=True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_", "GITHUB_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def commit(self, files: dict[str, str], message: str) -> str:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def commit_guards(self):
        """给 origin/main 装上守卫夹具：必拒载荷直接退出 2，扩展文件存在即可。"""
        self.commit({"harness/command_guard.py": "import sys\nsys.exit(2)\n",
                     ".pi/extensions/harness-guard.ts": "// guard fixture\n"}, "guards")
        self.git("push", "-q", "origin", "main")

    def header(self, task: str, *, retries=0, ci_rounds=1) -> dict:
        return {"task": task, "class": "K7", "risk": "R3", "designer": "codex", "size": "small",
                "spec_refs": [],
                "budget": {"wall_clock_min": 5, "retries": retries, "ci_rounds": ci_rounds, "tokens": None}}

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
        from engine.checks import taskbook
        reports = [taskbook.Report(rel, header=header_dict)]
        for patcher in (mock.patch.object(taskbook, "check_all", lambda *args, **kwargs: reports),
                        mock.patch.object(taskbook, "on_main", lambda *args, **kwargs: None),
                        mock.patch.object(taskbook, "load_exempt", lambda *args, **kwargs: {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def with_alerts(self, section: dict | None):
        """注入 rules.toml 配置：section 为字典时配置 [alerts] 节，None 表示不配置该节（派发预警全关）。"""
        rules = {} if section is None else {"alerts": section}
        patcher = mock.patch.object(alerts, "load_rules", return_value=rules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_dispatch(self, rel: str, host, github, **kwargs) -> tuple[int, str, str]:
        config = dispatch.Config(slots=2, stall_seconds=120, poll_seconds=0.05, ci_timeout_seconds=5,
                                 verify=[sys.executable, "-c", VERIFY_PASS])
        dispatcher = dispatch.Dispatcher(self.repo, config, github, host, identity=dict(GIT_ENV))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dispatcher.run(rel)
        return code, out.getvalue(), err.getvalue()

    def all_events(self) -> list[dict]:
        path = events_db.db_path()
        if path is None or not path.exists():
            return []
        with contextlib.closing(sqlite3.connect(path)) as conn:
            rows = conn.execute(f"SELECT {','.join(EVENT_COLUMNS)} FROM events ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(zip(EVENT_COLUMNS, row))
            item["outputs"] = json.loads(item["outputs"] or "{}")
            result.append(item)
        return result

    def trace_events(self, trace: str, stage: str | None = None) -> list[dict]:
        return [row for row in self.all_events()
                if row["trace_id"] == trace and (stage is None or row["stage"] == stage)]

    @staticmethod
    def steps(rows: list[dict]) -> list[tuple]:
        return [(row["step"], row["status"]) for row in rows]

    def alert_comments(self, github: FakeGitHub, trace: str, reason: str) -> list[tuple]:
        mark = alerts.marker(trace, reason)
        return [item for item in github.comments if mark in item[1]]

    # ---------- 验收 1：末轮预警在最后 wait_ci 前（预算 3 与 1），等待/推送数不变 ----------

    def test_last_ci_round_warns_before_wait(self):
        self.commit_guards()
        # 预算 3、CI 每轮都失败：前两轮不预警，第三轮在该次 wait_ci 之前预警一次
        rel = "docs/plans/task-931-a.md"
        self.write_taskbook(rel, self.header("T931A", ci_rounds=3))
        self.stub_taskbook(rel, self.header("T931A", ci_rounds=3))
        self.with_alerts({})
        gh = FakeGitHub(ci=(False, "CI 未通过：见摘要", [5]))
        code, _, _ = self.run_dispatch(rel, RecordingHost(executor_script(stream("done"))), gh)
        self.assertEqual(code, 1)
        calls = gh.calls
        waits = [index for index, call in enumerate(calls) if call[0] == "wait_ci"]
        self.assertEqual(len(waits), 3)
        self.assertEqual(len(gh.pushes), 4)  # 认领 + 3 份运行记录，与无预警时一致
        alerts_idx = [index for index, call in enumerate(calls)
                      if call[0] == "comment" and alerts.marker("task/931-a", "ci_last_round") in call[2]]
        self.assertEqual(len(alerts_idx), 1)  # 只在最后一轮预警一次，不逐轮重发
        self.assertGreater(alerts_idx[0], waits[1])  # 前两轮已等待之后
        self.assertLess(alerts_idx[0], waits[2])  # 且在最后一次 wait_ci 之前——不是跑过预算才提醒
        self.assertGreater(calls.index(("add_label", 14, "budget-exceeded")), waits[2])
        escalation = gh.comments[-1][1]
        self.assertIn("task/931-a", escalation)  # 升级包补 trace
        self.assertIn("时间线入口", escalation)  # 有 PR：入口指向 PR/CI artifact
        self.assertIn("本 PR 的 checks", escalation)
        self.assertIn("dispatch/executor_round", escalation)  # 安全阶段摘要

        # 预算 1：首轮（即最后一轮）在 wait_ci 前预警
        rel = "docs/plans/task-931-b.md"
        self.write_taskbook(rel, self.header("T931B", ci_rounds=1))
        self.stub_taskbook(rel, self.header("T931B", ci_rounds=1))
        gh = FakeGitHub(ci=(False, "CI 未通过：见摘要", [5]))
        code, _, _ = self.run_dispatch(rel, RecordingHost(executor_script(stream("done"))), gh)
        self.assertEqual(code, 1)
        calls = gh.calls
        waits = [index for index, call in enumerate(calls) if call[0] == "wait_ci"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(len(gh.pushes), 2)
        alerts_idx = [index for index, call in enumerate(calls)
                      if call[0] == "comment" and alerts.marker("task/931-b", "ci_last_round") in call[2]]
        self.assertEqual(len(alerts_idx), 1)
        self.assertLess(alerts_idx[0], waits[0])
        self.assertGreater(alerts_idx[0], calls.index(("open_pr", "task/931-b")))

        # 负例：未配置 [alerts]（默认关闭）时没有任何预警评论/标签，派发行为与既往完全一致
        rel = "docs/plans/task-931-c.md"
        self.write_taskbook(rel, self.header("T931C", ci_rounds=1))
        self.stub_taskbook(rel, self.header("T931C", ci_rounds=1))
        self.with_alerts(None)
        gh = FakeGitHub(ci=(False, "CI 未通过：见摘要", [5]))
        code, _, _ = self.run_dispatch(rel, RecordingHost(executor_script(stream("done"))), gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.labels, [(14, "budget-exceeded"), (14, "escalation")])  # 与既往一致：判级标签 + 升级标签
        self.assertEqual(len(gh.comments), 1)  # 只有原有升级评论
        self.assertEqual(self.trace_events("task/931-c", "alert"), [])

    # ---------- 验收 2：守卫拒绝阈值按轮内被拒工具计数，理由数不冒充调用数 ----------

    def test_guard_threshold_uses_denied_tools(self):
        self.commit_guards()
        section = {"guard_denials_threshold": 2}

        # 阈值-1：1 次被拒调用（带双拒绝理由）不预警——理由条数不冒充调用数
        rel = "docs/plans/task-932-a.md"
        self.write_taskbook(rel, self.header("T932A"))
        self.stub_taskbook(rel, self.header("T932A"))
        self.with_alerts(section)
        gh = FakeGitHub()
        host = RecordingHost(executor_script(stream(denied(["理由甲", "理由乙"]))))
        code, _, _ = self.run_dispatch(rel, host, gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.issues, [])
        self.assertEqual(self.alert_comments(gh, "task/932-a", "guard_denials"), [])

        # 阈值：2 次被拒调用 → 无 PR 时建一条 escalation 议题；派发本体（开 PR、等 CI、退出码）不变
        rel = "docs/plans/task-932-b.md"
        self.write_taskbook(rel, self.header("T932B"))
        self.stub_taskbook(rel, self.header("T932B"))
        gh = FakeGitHub()
        host = RecordingHost(executor_script(stream(denied(["理由甲"]), denied(["理由乙"]))))
        code, _, _ = self.run_dispatch(rel, host, gh)
        self.assertEqual(code, 0)
        self.assertEqual(len(gh.issues), 1)
        issue = gh.issues[0]
        self.assertEqual(issue["labels"], ["escalation"])
        self.assertIn(alerts.marker("task/932-b", "guard_denials"), issue["body"])
        self.assertIn("bin/harness trace task/932-b", issue["body"])  # 无 PR：入口含 trace 命令
        self.assertIn("dispatch/executor_round", issue["body"])
        self.assertEqual(sum(1 for call in gh.calls if call[0] == "wait_ci"), 1)
        alert_steps = self.steps(self.trace_events("task/932-b", "alert"))
        self.assertEqual(alert_steps, [("guard_denials", "ok"), ("ci_last_round", "ok")])

        # 阈值+1：3 次被拒调用 → 预警
        rel = "docs/plans/task-932-c.md"
        self.write_taskbook(rel, self.header("T932C"))
        self.stub_taskbook(rel, self.header("T932C"))
        gh = FakeGitHub()
        host = RecordingHost(executor_script(stream(denied(["甲"]), denied(["乙"]), denied(["丙"]))))
        code, _, _ = self.run_dispatch(rel, host, gh)
        self.assertEqual(code, 0)
        self.assertEqual(len(gh.issues), 1)

        # 非正配置：禁用该预警并提示配置错误，不改执行（仍开 PR 等 CI、退出 0、CI 预警不受影响）
        self.reset_notice_flags()
        rel = "docs/plans/task-932-d.md"
        self.write_taskbook(rel, self.header("T932D"))
        self.stub_taskbook(rel, self.header("T932D"))
        self.with_alerts({"guard_denials_threshold": 0})
        gh = FakeGitHub()
        host = RecordingHost(executor_script(stream(denied(["理由甲"]), denied(["理由乙"]))))
        code, _, err = self.run_dispatch(rel, host, gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.issues, [])
        self.assertIn("配置非法", err)
        self.assertEqual(len(self.alert_comments(gh, "task/932-d", "ci_last_round")), 1)

        # 非法类型：同样禁用、不预警
        rel = "docs/plans/task-932-e.md"
        self.write_taskbook(rel, self.header("T932E"))
        self.stub_taskbook(rel, self.header("T932E"))
        self.with_alerts({"guard_denials_threshold": "two"})
        gh = FakeGitHub()
        host = RecordingHost(executor_script(stream(denied(["理由甲"]), denied(["理由乙"]))))
        code, _, _ = self.run_dispatch(rel, host, gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.issues, [])
        self.assertEqual(self.alert_comments(gh, "task/932-e", "guard_denials"), [])

    # ---------- 验收 3：远端标记去重（PR 评论与升级议题），都有 escalation 标签/时间线 ----------

    def test_alert_and_escalation_share_one_comment(self):
        gh = FakeGitHub()
        trace = "task/933-x"
        # PR 路径：首评带 escalation 标签与时间线，同 trace/reason 重跑更新同一评论
        first = alerts.publish(trace, "ci_last_round", pr=14, task="T933", gh=gh)
        self.assertEqual(first, {"ok": True, "target": "pr", "updated": False, "error_kind": None})
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(gh.comments[0][2], "escalation")
        self.assertIn(alerts.marker(trace, "ci_last_round"), gh.comments[0][1])
        self.assertIn("时间线入口", gh.comments[0][1])
        second = alerts.publish(trace, "ci_last_round", pr=14, task="T933", gh=gh)
        self.assertEqual(second, {"ok": True, "target": "pr", "updated": True, "error_kind": None})
        self.assertEqual(len(gh.comments), 1)  # 不重发
        self.assertEqual([cid for cid, _body in gh.edited_comments], [1])
        self.assertEqual(gh.list_comments(14)[0]["body"], gh.edited_comments[0][1])
        # 不同 reason、不同 trace 各自成键，互不合并
        alerts.publish(trace, "guard_denials", pr=14, gh=gh)
        alerts.publish("task/933-y", "ci_last_round", pr=14, gh=gh)
        self.assertEqual(len(gh.comments), 3)

        # 无 PR：查/建一条 escalation 议题，重跑复用并更新同一议题
        issue_first = alerts.publish(trace, "guard_denials", task="T933", gh=gh)
        self.assertEqual(issue_first, {"ok": True, "target": "issue", "updated": False, "error_kind": None})
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.issues[0]["labels"], ["escalation"])
        self.assertIn(f"bin/harness trace {trace}", gh.issues[0]["body"])
        issue_second = alerts.publish(trace, "guard_denials", task="T933", gh=gh)
        self.assertEqual(issue_second, {"ok": True, "target": "issue", "updated": True, "error_kind": None})
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual([number for number, _body in gh.edited_issues], [gh.issues[0]["number"]])

        # 发布异常不传播：ok=False 带错误类型；未知 reason 不产生任何远端调用
        failed = alerts.publish(trace, "ci_last_round", pr=14, gh=FakeGitHub(fail_publish=True))
        self.assertEqual(failed, {"ok": False, "target": None, "updated": False, "error_kind": "RuntimeError"})
        fresh = FakeGitHub()
        unknown = alerts.publish(trace, "made_up", pr=14, gh=fresh)
        self.assertEqual(unknown["ok"], False)
        self.assertEqual(fresh.calls, [])
        # 发布结果记入本机 alert 事件（C1：alert 自己记汇总或发布结果）
        alert_steps = self.steps(self.trace_events(trace, "alert"))
        self.assertEqual(alert_steps, [("ci_last_round", "ok"), ("ci_last_round", "ok"),
                                       ("guard_denials", "ok"), ("guard_denials", "ok"),
                                       ("guard_denials", "ok"), ("ci_last_round", "error"),
                                       ("made_up", "error")])

    # ---------- 验收 4：发布失败不改派发退出码、不放宽预算、不吞原升级 ----------

    def test_publish_failure_preserves_original_exit(self):
        self.commit_guards()
        self.with_alerts({})

        # CI 通过：预警发布失败被吞掉（留本机 alert.error），退出码仍 0，等待照常
        rel = "docs/plans/task-934-a.md"
        self.write_taskbook(rel, self.header("T934A"))
        self.stub_taskbook(rel, self.header("T934A"))
        gh = FakeGitHub(fail_publish=True)
        code, out, _ = self.run_dispatch(rel, RecordingHost(executor_script(stream("done"))), gh)
        self.assertEqual(code, 0)
        self.assertIn("CI 通过", out)
        self.assertEqual(sum(1 for call in gh.calls if call[0] == "wait_ci"), 1)
        self.assertEqual(gh.labels, [])  # 预算未被触发
        rows = self.trace_events("task/934-a", "alert")
        self.assertEqual([(row["step"], row["status"], row["error_kind"]) for row in rows],
                         [("ci_last_round", "error", "RuntimeError")])

        # CI 未通过：预警失败不影响预算判级，原升级照常尝试并按既有行为抛出
        rel = "docs/plans/task-934-b.md"
        self.write_taskbook(rel, self.header("T934B"))
        self.stub_taskbook(rel, self.header("T934B"))
        gh = FakeGitHub(ci=(False, "CI 未通过：见摘要", [5]), fail_publish=True)
        with self.assertRaises(RuntimeError):
            self.run_dispatch(rel, RecordingHost(executor_script(stream("done"))), gh)
        self.assertEqual(gh.labels, [(14, "budget-exceeded")])  # 预算未被放宽
        rows = self.trace_events("task/934-b")
        self.assertIn(("escalate", "fail"), self.steps(rows))  # 原升级照常尝试（失败也留事件）
        self.assertEqual(self.steps(self.trace_events("task/934-b", "alert")),
                         [("ci_last_round", "error")])


if __name__ == "__main__":
    unittest.main()
