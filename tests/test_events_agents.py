"""T105 埋点测试：派发全链与独立评审的观察事件、C6 审计摘要标记、工具计数与本机产物哈希、观察失败隔离。

夹具全部使用匿名临时 git 仓库（含匿名 bare 远端）、隔离 events_db.ROOT、冻结时钟、假 gh 与假执行方
宿主（流解析委托真实 PiHost），不碰真实库、PR、工作流。派发与评审经真实产品入口（Dispatcher.run、
review.review_pr）驱动；只隔离外部副作用（执行方与评审方二进制、gh、任务书检查器、verify 命令）。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from typing import ClassVar
from unittest import mock

from engine.agents import dispatch, dispatch_host, review
from engine.agents import dispatch_observation as observation
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
VERIFY_FAIL = "import sys; sys.exit(1)"
AUDIT_MARK = "<!-- harness-review-audit "
SEVERITY_ZEROS = {"阻断": 0, "严重": 0, "一般": 0, "建议": 0, "unknown": 0}


def mixed_stream() -> str:
    """夹具执行方流：成功、双理由拒绝、单理由拒绝、普通失败、损坏行、无结果各一。"""
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


def executor_script(stream: str, *, exit_code: int = 0, note: str = "", stderr: str = "",
                    git_failures: int = 0) -> str:
    """假执行方：写一段 JSONL 流到 stdout，做一次提交；note 非空时写澄清备注，stderr 非空时写死因，
    exit_code 非零即失败退出。git 操作（add/commit）对瞬时失败做 3 次退避重试（0.1/0.3/0.9 秒，B73），
    重试用尽把 git stderr 写入流后再退出非零（死因随流落盘）；git_failures 模拟前 N 次 git 子进程瞬断。"""
    script = f"""import pathlib, subprocess, sys, time
seen = 0


def git(argv):
    global seen
    err = ''
    for delay in (0.0, 0.1, 0.3, 0.9):
        if delay:
            time.sleep(delay)
        seen += 1
        if seen <= {git_failures}:
            err = 'fatal: 模拟 git 瞬断\\n'
            continue
        done = subprocess.run(['git', *argv], capture_output=True, text=True)
        if done.returncode == 0:
            return
        err = done.stderr
    sys.stdout.write('git ' + ' '.join(argv) + ' 重试 3 次后仍失败：\\n' + err)
    sys.exit(1)


sys.stdout.write({stream!r})
pathlib.Path('note.txt').write_text('work\\n')
git(['add', '-A'])
# --allow-empty：重试轮没有新改动时提交仍成功，verify 才能每轮都跑
git(['commit', '-q', '-m', 'work', '--allow-empty'])
"""
    if note:
        script += (f"pathlib.Path('build/dispatch').mkdir(parents=True, exist_ok=True)\n"
                   f"pathlib.Path('build/dispatch/escalation.md').write_text({note!r})\n")
    if stderr:
        script += f"sys.stdout.flush()\nsys.stderr.write({stderr!r})\n"
    if exit_code:
        script += f"sys.exit({exit_code})\n"
    return script


def verdict_script(verdict: str, findings_json: str) -> str:
    body = json.dumps({"verdict": verdict, "summary": "结论摘要", "findings": "PLACEHOLDER"},
                      ensure_ascii=False)
    body = body.replace('"PLACEHOLDER"', findings_json)
    return "import json, sys; print(" + repr(body) + ")"


class RecordingHost:
    """假执行方宿主：argv 启动写固定流的脚本；解析委托真实 PiHost（真实解析逻辑被覆盖）。"""

    name = "fake-pi"

    def __init__(self, calls: list, script: str):
        self.calls = calls
        self.script = script
        self.pi = dispatch_host.PiHost()

    def version(self):
        self.calls.append(("version",))
        return "9.9.9-fake"

    def argv(self, prompt, guard):
        self.calls.append(("argv", prompt))
        return [sys.executable, "-c", self.script]

    def parse(self, events_path):
        self.calls.append(("parse",))
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
        self.pr_bodies: list[str] = []
        self.comments: list[str] = []
        self.labels: list[str] = []
        self.issues: list[tuple] = []

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
        self.pr_bodies.append(body)
        return self.pr

    def comment(self, pr, body, label=None):
        self.calls.append(("comment", pr))
        self.comments.append(body)
        if label:
            self.add_label(pr, label)
        return f"https://example.invalid/repo/pull/{pr}#issuecomment-1"

    def add_label(self, pr, label):
        self.calls.append(("add_label", pr, label))
        self.labels.append(label)

    def disable_auto_merge(self, pr):
        self.calls.append(("disable_auto_merge", pr))
        return True

    def create_issue(self, title, body, labels):
        self.calls.append(("create_issue", title, tuple(labels)))
        self.issues.append((title, body))
        return "https://example.invalid/repo/issues/1"

    def wait_ci(self, branch, sha, timeout, detail=None):
        self.calls.append(("wait_ci", branch))
        ok, summary, run_ids = self.ci_result
        if detail is not None:
            detail["run_ids"] = list(run_ids)
        return ok, summary

    def existing_pr(self, branch):
        """分支已有开放 PR 的桩（B70/T121）：测试设 `existing` 属性即视为该编号的开放 PR，缺省无；
        不记入 calls，既有调用序列断言保持不变。"""
        return getattr(self, "existing", None)


class FakeReviewer:
    """假评审方：argv 启动固定脚本；read 按脚本返回展示模型与 model_basis（C6）。"""

    name = "opencode"
    env: ClassVar[dict] = {}

    def __init__(self, script: str, *, model_name="cfg/model-9", reported="reported/model-1",
                 basis="reported"):
        self.script = script
        self.model_name = model_name
        self.reported = reported
        self.basis = basis

    def argv(self, prompt, workspace, output):
        return [sys.executable, "-c", self.script]

    def read(self, stdout, output):
        return stdout, self.reported, self.basis


class FakeReviewGitHub:
    """评审用 gh 桩：记录 gh 调用形状；评论捕获正文并返回固定 URL。"""

    def __init__(self, pr_json: dict, checks: list):
        self.pr_json = pr_json
        self.checks = checks
        self.calls: list[str] = []
        self.comments: list[str] = []
        self.removed_labels = 0

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        joined = " ".join(argv)
        self.calls.append(joined)
        if joined.startswith("gh pr view"):
            return json.dumps(self.pr_json)
        if joined.startswith("gh pr checks") and "name,state,link" in joined:
            return json.dumps(self.checks)
        if joined.startswith("gh pr edit") and "--remove-label" in joined:
            self.removed_labels += 1
            return ""
        raise AssertionError(f"未预期的 gh 调用：{joined}")

    def comment(self, pr, body, label=None):
        self.calls.append(f"gh pr comment {pr} --body-file -")
        self.comments.append(body)
        return f"https://example.invalid/repo/pull/{pr}#issuecomment-9"

    def disable_auto_merge(self, pr):
        self.calls.append(f"gh pr merge {pr} --disable-auto")
        return True


def old_marker_head(body: str) -> str:
    return json.loads(body.split(review.REVIEW_MARK, 1)[1].split(" -->", 1)[0])["head"]


class RecordingTitleGitHub(FakeGitHub):
    """在 FakeGitHub 之上另存 open_pr 的标题与正文（calls 调用序列保持不变）。"""

    def __init__(self, repo, **kwargs):
        super().__init__(repo, **kwargs)
        self.titles: list[str] = []

    def open_pr(self, slot, branch, title, body):
        self.titles.append(title)
        return super().open_pr(slot, branch, title, body)


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-agents-"))
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

    def commit(self, files: dict[str, str], message: str) -> str:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def origin_main(self) -> str:
        return self.git("rev-parse", "origin/main").stdout.strip()

    def header(self, task: str, *, retries=0) -> dict:
        return {"task": task, "class": "K7", "risk": "R3", "designer": "codex", "size": "small",
                "spec_refs": [],
                "budget": {"wall_clock_min": 5, "retries": retries, "ci_rounds": 1, "tokens": None}}

    def write_taskbook(self, rel: str, header_dict: dict, *, title: str = "# 任务：夹具"):
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
            f"rollback: git revert\n---\n\n{title}\n",
            encoding="utf-8")

    def stub_taskbook(self, rel: str, header_dict: dict, *, exists=True):
        """隔离任务书检查器（admit 的下游）：返回固定 Report，on_main 无问题、豁免表为空。"""
        reports = [taskbook.Report(rel, header=header_dict)] if exists else []
        for patcher in (mock.patch.object(taskbook, "check_all", lambda *args, **kwargs: reports),
                        mock.patch.object(taskbook, "on_main", lambda *args, **kwargs: None),
                        mock.patch.object(taskbook, "load_exempt", lambda *args, **kwargs: {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_dispatcher(self, host, github, *, retries=0, slots=2,
                        verify_pass=True) -> dispatch.Dispatcher:
        script = VERIFY_PASS if verify_pass else VERIFY_FAIL
        config = dispatch.Config(slots=slots, stall_seconds=120, poll_seconds=0.05, ci_timeout_seconds=5,
                                 verify=[sys.executable, "-c", script])
        return dispatch.Dispatcher(self.repo, config, github, host, identity=dict(GIT_ENV))

    def run_dispatch(self, rel: str, host, github, *, expect: int | None = None, label: str = "",
                     **kwargs) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.make_dispatcher(host, github, **kwargs).run(rel)
        code_out, code_err = out.getvalue(), err.getvalue()
        if expect is not None:
            self.assert_dispatch_exit(code, expect, code_out, code_err, label)
        return code, code_out, code_err

    def executor_stream_tail(self, lines: int = 30) -> str:
        """最近一份执行方事件流的最后 N 行（B73）：run_monitored 已把子进程 stderr 并进流文件，尾部即死因。"""
        runs = self.repo / ".git" / "dispatch" / "runs"
        if not runs.is_dir():
            return ""
        streams = sorted(runs.glob("*/*/round-*.jsonl"), key=lambda path: path.stat().st_mtime)
        if not streams:
            return ""
        text = streams[-1].read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def assert_dispatch_exit(self, code: int, expected: int, out: str = "", err: str = "",
                             label: str = "") -> None:
        """dispatch 退出码断言统一入口（B73）：失败消息附 dispatch stdout/stderr 与执行方流尾部死因，
        失败现场不再沉默（沿用 #59 在 dispatch_once 的先例并补流尾部）；只增失败信息，不改判定。"""
        if code == expected:
            return
        detail = f"[{label}] " if label else ""
        detail += f"dispatch 退出码 {code} != {expected}"
        if out or err:
            detail += f"\n--- dispatch stdout ---\n{out}\n--- dispatch stderr ---\n{err}"
        tail = self.executor_stream_tail()
        if tail:
            detail += f"\n--- 执行方流尾部（最后 30 行）---\n{tail}"
        self.fail(detail)

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

    def input_refs(self, event_id: int) -> list[tuple]:
        path = events_db.db_path()
        with contextlib.closing(sqlite3.connect(path)) as conn:
            return conn.execute(
                "SELECT kind, ref, sha256, size FROM refs WHERE event_id=? AND direction='in' ORDER BY rowid",
                (event_id,)).fetchall()

    def steps(self, rows: list[dict]) -> list[tuple]:
        return [(row["step"], row["status"]) for row in rows]

    def artifact(self, sha: str) -> bytes | None:
        path = events_db.artifacts_dir() / sha
        return path.read_bytes() if path.exists() else None

    @staticmethod
    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def commit_guards(self):
        """给 origin/main 装上守卫夹具：必拒载荷直接退出 2，扩展文件存在即可。"""
        self.commit({"harness/command_guard.py": "import sys\nsys.exit(2)\n",
                     ".pi/extensions/harness-guard.ts": "// guard fixture\n"}, "guards")
        self.git("push", "-q", "origin", "main")

    def run_pr_head(self) -> dict:
        """建评审夹具：feature 分支一个提交，推为 origin 的 pull/14/head；返回 PR 视图 JSON。"""
        self.git("checkout", "-q", "-b", "task/905-obs")
        head = self.commit({"docs/note.md": "note\n"}, "feature")
        self.git("push", "-q", "origin", "HEAD:refs/pull/14/head")
        return {"title": "T905：评审夹具", "body": "描述正文", "headRefName": "task/905-obs",
                "headRefOid": head, "baseRefName": "main", "state": "OPEN", "mergeCommit": None}

    def audit_of(self, body: str) -> dict:
        self.assertIn(AUDIT_MARK, body)
        return json.loads(body.split(AUDIT_MARK, 1)[1].split(" -->", 1)[0])

    def break_observation(self):
        """让观察 API 与读取引用全部失败：events API 抛错，引用准备与流读取抛错，库文件损坏。"""

        def boom(*args, **kwargs):
            raise OSError("observation down")

        for target, name in ((events, "emit"), (events, "store_artifact"), (events, "file_ref"),
                             (observation, "_admit_inputs"), (observation, "_stream_refs")):
            patcher = mock.patch.object(target, name, boom)
            patcher.start()
            self.addCleanup(patcher.stop)
        db = events_db.db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"this is not a sqlite database at all")

    # ---------- 验收 1：成功全链与各失败分支的设计 3.2 事件 ----------

    def test_dispatch_success_and_failure_steps(self):
        rel_a = "docs/plans/task-905-a.md"
        self.stub_taskbook(rel_a, self.header("T905A"), exists=False)
        with self.assertRaises(dispatch.Stop):
            self.run_dispatch(rel_a, RecordingHost([], ""), FakeGitHub(self.repo))
        # 准入失败时还没有分支：事件落在派发机的当前 trace 上，按步骤与状态定位
        rows = [row for row in self.all_events() if (row["step"], row["status"]) == ("admit", "fail")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision_by"], "taskbook")
        self.assertEqual(rows[0]["error_kind"], "admit")
        self.assertIn("不是 docs/plans/task-*.md 下的任务书", rows[0]["decision_reason"])

        # claim：远端分支已存在 → admit ok、claim fail（已被认领），之后没有其他事件与 gh 调用
        rel_b = "docs/plans/task-905-b.md"
        self.write_taskbook(rel_b, self.header("T905B"))
        self.stub_taskbook(rel_b, self.header("T905B"))
        gh = FakeGitHub(self.repo, branch_exists=True)
        with self.assertRaises(dispatch.Stop):
            self.run_dispatch(rel_b, RecordingHost([], ""), gh)
        rows = self.trace_events("task/905-b")
        self.assertEqual(self.steps(rows), [("admit", "ok"), ("claim", "fail")])
        self.assertEqual(rows[1]["outputs"], {"claimed": False})
        self.assertEqual((rows[1]["decision_by"], rows[1]["decision_rule"], rows[1]["decision_reason"]),
                         ("dispatch", "claim", "已被认领"))
        self.assertEqual(gh.calls, [("remote_branch_exists", "task/905-b")])

        # guard_preflight：origin/main 没有守卫 → 导出失败，preflight fail 后停止
        rel_c = "docs/plans/task-905-c.md"
        self.write_taskbook(rel_c, self.header("T905C"))
        self.stub_taskbook(rel_c, self.header("T905C"))
        with self.assertRaises(dispatch.Stop):
            self.run_dispatch(rel_c, RecordingHost([], ""), FakeGitHub(self.repo))
        rows = self.trace_events("task/905-c")
        self.assertEqual(self.steps(rows), [("admit", "ok"), ("guard_preflight", "fail")])
        self.assertEqual(rows[1]["outputs"], {"denied": False})
        self.assertEqual(self.input_refs(rows[1]["id"]), [("rev", self.origin_main(), None, None)])

        # 装上守卫后：slot 用尽 → preflight ok、slot fail
        self.commit_guards()
        rel_d = "docs/plans/task-905-d.md"
        self.write_taskbook(rel_d, self.header("T905D"))
        self.stub_taskbook(rel_d, self.header("T905D"))
        lock = events_db.common_dir() / "dispatch" / "slots" / "1.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid()}))
        try:
            with self.assertRaises(dispatch.Stop):
                self.run_dispatch(rel_d, RecordingHost([], ""), FakeGitHub(self.repo), slots=1)
        finally:
            lock.unlink()
        rows = self.trace_events("task/905-d")
        self.assertEqual(self.steps(rows),
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "fail")])
        self.assertIn("槽位都在使用中", rows[2]["outputs"]["problem"])

        # executor：执行方异常退出 → executor_round fail（退出原因 error）→ escalate 开议题
        rel_e = "docs/plans/task-905-e.md"
        self.write_taskbook(rel_e, self.header("T905E"))
        self.stub_taskbook(rel_e, self.header("T905E"))
        self.run_dispatch(rel_e, RecordingHost([], executor_script(mixed_stream(), exit_code=3)),
                          FakeGitHub(self.repo), expect=1)
        rows = self.trace_events("task/905-e")
        self.assertEqual(self.steps(rows),
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "fail"), ("escalate", "ok")])
        self.assertEqual(rows[4]["outputs"]["exit"], "error")
        self.assertEqual(rows[4]["outputs"]["model"], "provider/model-x")
        self.assertEqual(rows[5]["outputs"]["target"], "issue")
        self.assertEqual(rows[5]["outputs"]["labels"], "escalation")
        self.assertEqual(rows[5]["outputs"]["notified"], True)

        # local_verify：verify 未通过且重试用尽 → local_verify fail → escalate；摘要只留哈希
        rel_f = "docs/plans/task-905-f.md"
        self.write_taskbook(rel_f, self.header("T905F"))
        self.stub_taskbook(rel_f, self.header("T905F"))
        gh = FakeGitHub(self.repo)
        self.run_dispatch(rel_f, RecordingHost([], executor_script(mixed_stream())), gh,
                          verify_pass=False, expect=1)
        rows = self.trace_events("task/905-f")
        self.assertEqual(self.steps(rows),
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "fail"), ("escalate", "ok")])
        verify_row = rows[5]
        self.assertEqual((verify_row["outputs"]["ok"], verify_row["outputs"]["repeat"]), (False, False))
        self.assertTrue(verify_row["outputs"]["signature"])
        refs = self.input_refs(verify_row["id"])
        self.assertEqual(refs[0], ("rev", self.origin_main(), None, None))
        self.assertEqual(refs[1][0], "rev")
        self.assertNotEqual(refs[1][1], self.origin_main())
        escalated = rows[6]
        self.assertIn("重试次数用尽", escalated["outputs"]["reason"])
        self.assertEqual(escalated["outputs"]["summary.sha256"],
                         self.sha(gh.issues[0][1].encode("utf-8")))
        self.assertEqual(self.artifact(escalated["outputs"]["summary.sha256"]),
                         gh.issues[0][1].encode("utf-8"))

        # 打转：连续两轮同一失败签名 → 第二轮 repeat=True，exit=loop → escalate
        rel_g = "docs/plans/task-905-g.md"
        self.write_taskbook(rel_g, self.header("T905G", retries=1))
        self.stub_taskbook(rel_g, self.header("T905G", retries=1))
        self.run_dispatch(rel_g, RecordingHost([], executor_script(mixed_stream())),
                          FakeGitHub(self.repo), retries=1, verify_pass=False, expect=1)
        rows = self.trace_events("task/905-g")
        self.assertEqual([row["step"] for row in rows],
                         ["admit", "guard_preflight", "slot", "claim", "executor_round", "local_verify",
                          "executor_round", "local_verify", "escalate"])
        self.assertEqual((rows[5]["outputs"]["repeat"], rows[7]["outputs"]["repeat"]), (False, True))
        self.assertEqual(rows[5]["outputs"]["signature"], rows[7]["outputs"]["signature"])
        self.assertIn("打转", rows[8]["outputs"]["reason"])

        # clarify：执行方写备注 → clarify ok（missing_context 恒 0，由 T202 填充）→ escalate
        rel_h = "docs/plans/task-905-h.md"
        self.write_taskbook(rel_h, self.header("T905H"))
        self.stub_taskbook(rel_h, self.header("T905H"))
        self.run_dispatch(rel_h, RecordingHost([], executor_script(mixed_stream(), note="缺上下文")),
                          FakeGitHub(self.repo), expect=1)
        rows = self.trace_events("task/905-h")
        self.assertEqual([row["step"] for row in rows],
                         ["admit", "guard_preflight", "slot", "claim", "executor_round", "clarify", "escalate"])
        self.assertEqual(rows[5]["outputs"], {"requested": True, "missing_context": 0})
        self.assertEqual(self.input_refs(rows[5]["id"])[0][0], "note")
        self.assertEqual(rows[6]["outputs"]["reason"], "本地未完成（执行方请求澄清）")

        # push_pr 与 ci_wait：CI 用尽预算 → push_pr ok、ci_wait fail、escalate 评论 PR 并打标签
        rel_i = "docs/plans/task-905-i.md"
        self.write_taskbook(rel_i, self.header("T905I"))
        self.stub_taskbook(rel_i, self.header("T905I"))
        gh = FakeGitHub(self.repo, ci=(False, "CI 未通过：见摘要", [5]))
        self.run_dispatch(rel_i, RecordingHost([], executor_script(mixed_stream())), gh, expect=1)
        rows = self.trace_events("task/905-i")
        self.assertEqual(self.steps(rows),
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "ok"), ("push_pr", "ok"),
                          ("ci_wait", "fail"), ("escalate", "ok")])
        self.assertEqual(rows[6]["outputs"]["pr"], 14)
        self.assertEqual(rows[6]["outputs"]["body.sha256"], self.sha(gh.pr_bodies[0].encode("utf-8")))
        self.assertEqual(self.artifact(rows[6]["outputs"]["body.sha256"]), gh.pr_bodies[0].encode("utf-8"))
        summary = "CI 未通过：见摘要".encode()
        self.assertEqual(rows[7]["outputs"], {"pr": 14, "round": 1, "ok": False, "run_ids": "5",
                                              "summary.sha256": self.sha(summary), "summary.size": len(summary),
                                              "summary.ref": self.sha(summary)})
        self.assertIn("budget-exceeded", gh.labels)
        self.assertEqual(len(gh.comments), 1)

        # 升级评论发布失败：escalate 事件仍留（notified=False、status fail），原异常原样传播
        rel_l = "docs/plans/task-905-l.md"
        self.write_taskbook(rel_l, self.header("T905L"))
        self.stub_taskbook(rel_l, self.header("T905L"))
        gh = FakeGitHub(self.repo, ci=(False, "CI 未通过：见摘要", [5]))

        def broken_comment(pr, body, label=None):
            raise RuntimeError("gh down")

        gh.comment = broken_comment
        with self.assertRaises(RuntimeError):
            self.run_dispatch(rel_l, RecordingHost([], executor_script(mixed_stream())), gh)
        rows = self.trace_events("task/905-l")
        self.assertEqual(self.steps(rows),
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "ok"), ("push_pr", "ok"),
                          ("ci_wait", "fail"), ("escalate", "fail")])
        self.assertEqual((rows[-1]["outputs"]["target"], rows[-1]["outputs"]["notified"]), ("pr", False))
        self.assertEqual(gh.comments, [])
        # 状态摘要哈希与产物仍随失败事件保存
        self.assertIsNotNone(self.artifact(rows[-1]["outputs"]["summary.sha256"]))

        # 成功全链：admit→preflight→slot→claim→executor→local_verify→push_pr→ci_wait 全部 ok，退出码 0
        rel_k = "docs/plans/task-905-k.md"
        self.write_taskbook(rel_k, self.header("T905K"))
        self.stub_taskbook(rel_k, self.header("T905K"))
        gh = FakeGitHub(self.repo, ci=(True, "", [7]))
        _, out, _ = self.run_dispatch(rel_k, RecordingHost([], executor_script(mixed_stream())), gh, expect=0)
        self.assertIn("CI 通过", out)
        rows = self.trace_events("task/905-k")
        self.assertEqual(self.steps(rows),
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "ok"), ("push_pr", "ok"), ("ci_wait", "ok")])
        self.assertEqual(self.input_refs(rows[0]["id"]),
                         [("taskbook", f"{rel_k}@{self.origin_main()}",
                           self.sha((self.repo / rel_k).read_bytes()),
                           (self.repo / rel_k).stat().st_size),
                          ("rev", self.origin_main(), None, None)])
        self.assertEqual(rows[0]["outputs"],
                         {"task": "T905K", "class": "K7", "wall_clock_min": 5, "retries": 0, "ci_rounds": 1})
        self.assertEqual(rows[1]["outputs"], {"denied": True})
        self.assertEqual(rows[2]["outputs"], {"slot": 1, "start": self.origin_main()})
        self.assertEqual(rows[3]["outputs"], {"claimed": True})
        executor = rows[4]
        stream = (self.repo / ".git" / "dispatch" / "runs" / "T905K" / "1" / "round-1.jsonl").read_bytes()
        self.assertEqual(executor["outputs"]["host"], "fake-pi")
        self.assertEqual(executor["outputs"]["host_version"], "9.9.9-fake")
        self.assertEqual(executor["outputs"]["model"], "provider/model-x")
        self.assertEqual(executor["outputs"]["exit"], "ok")
        self.assertEqual(executor["outputs"]["gen_ai.usage.input_tokens"], 10)
        self.assertEqual(executor["outputs"]["gen_ai.usage.output_tokens"], 5)
        self.assertEqual(executor["outputs"]["cost"], 0.5)
        self.assertEqual(executor["outputs"]["guard_allowed"], 3)
        self.assertEqual(executor["outputs"]["guard_denied"], 2)
        self.assertEqual(executor["outputs"]["stream.sha256"], self.sha(stream))
        self.assertEqual(executor["outputs"]["stream.size"], len(stream))
        self.assertEqual(self.artifact(executor["outputs"]["stream.ref"]), stream)
        self.assertEqual(self.input_refs(executor["id"])[0][0], "prompt")
        self.assertEqual(len(self.input_refs(executor["id"])[0][1]), 64)
        verified = rows[5]
        self.assertEqual((verified["outputs"]["ok"], verified["outputs"]["signature"],
                          verified["outputs"]["repeat"]), (True, None, False))
        self.assertEqual(self.artifact(verified["outputs"]["log.sha256"]), b"local verify ok\n")
        self.assertEqual(rows[6]["outputs"]["pr"], 14)
        self.assertEqual(rows[7]["outputs"], {"pr": 14, "round": 1, "ok": True, "run_ids": "7"})
        self.assertEqual(gh.calls, [("remote_branch_exists", "task/905-k"), ("push", "task/905-k"),
                                    ("push", "task/905-k"), ("open_pr", "task/905-k"),
                                    ("wait_ci", "task/905-k")])  # 两次 push：认领与运行记录
        for row in rows:
            self.assertEqual(row["stage"], "dispatch")
            self.assertEqual(row["source"], "local")
        self.assertEqual(events_db.verify(), [])

    # ---------- 验收 2：工具计数（理由数不冒充调用数）与原始字节产物哈希 ----------

    def test_tool_counts_and_artifact_hash(self):
        host = dispatch_host.PiHost()
        stream_path = self.tmp / "streams" / "round-1.jsonl"
        stream_path.parent.mkdir(parents=True)
        stream_path.write_text(mixed_stream(), encoding="utf-8")
        observed = host.parse_observability(stream_path)
        self.assertEqual(observed, {"guard_allowed": 3, "guard_denied": 2, "missing_context": []})
        model, usage, denials = host.parse(stream_path)  # 旧三元接口不变：denials 按理由计数
        self.assertEqual(model, "provider/model-x")
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 5, "cost": 0.5})
        self.assertEqual(denials, {"理由甲": 2, "理由乙": 1})
        self.assertEqual(sum(denials.values()), 3)  # 理由条数（3）≠ 被拒工具数（2）
        missing = self.tmp / "streams" / "absent.jsonl"
        self.assertEqual(host.parse_observability(missing),
                         {"guard_allowed": 0, "guard_denied": 0, "missing_context": []})
        self.assertEqual(host.parse(missing), ("", {}, {}))

        # RunResult / Attempt 的 guard_allowed 是末尾带默认值的 int 字段
        self.assertEqual(fields(dispatch_host.RunResult)[-1].name, "guard_allowed")
        self.assertEqual(fields(dispatch.Attempt)[-1].name, "guard_allowed")
        self.assertEqual(dispatch_host.RunResult("ok", 0, 1.0).guard_allowed, 0)
        self.assertEqual(dispatch.Attempt(ok=False, exit="ok").guard_allowed, 0)

        # 产品路径：run_executor 经真实解析产出计数，executor_round 事件带原始字节流哈希
        dispatcher = self.make_dispatcher(RecordingHost([], executor_script(mixed_stream())),
                                          FakeGitHub(self.repo))
        task = dispatch.Task("docs/plans/task-905-t.md", "T905T", "K7", "R3",
                             {"wall_clock_min": 5, "retries": 0, "ci_rounds": 1}, [], "task/905-t")
        result = dispatcher.run_executor(task, self.repo, Path("guard-unused"), "提示词", stream_path)
        self.assertEqual(result.exit, "ok")
        self.assertEqual(result.guard_allowed, 3)
        rows = self.trace_events("task/905-t")
        self.assertEqual([row["step"] for row in rows], ["executor_round"])
        outputs = rows[0]["outputs"]
        raw = stream_path.read_bytes()
        self.assertEqual(outputs["guard_allowed"], 3)
        self.assertEqual(outputs["guard_denied"], 2)  # 不是理由条数 3
        self.assertEqual(outputs["stream.sha256"], self.sha(raw))
        self.assertEqual(outputs["stream.size"], len(raw))
        self.assertEqual(outputs["stream.ref"], self.sha(raw))
        self.assertEqual(self.artifact(outputs["stream.ref"]), raw)  # 产物保存原始字节

    # ---------- 验收 3：评审元数据、C6 审计摘要标记与评审失败 ----------

    def test_review_metadata_and_failure(self):
        pr_json = self.run_pr_head()
        head = pr_json["headRefOid"]
        base = self.git("merge-base", "origin/main", head).stdout.strip()
        checks = [{"name": "harness", "state": "SUCCESS", "link": "https://ci.example.invalid/run/1"}]
        workspace = self.tmp / "app-review"
        folder = workspace / "build" / "review"

        # 通过：事件、评论 URL、C6 标记逐字段、材料哈希与旧标记读回
        gh = FakeReviewGitHub(pr_json, checks)
        with mock.patch.object(review, "make_reviewer", lambda name: FakeReviewer(verdict_script("通过", "[]"))), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review.review_pr(14, "opencode", root=self.repo, github=gh), 0)
        self.assertEqual(len(gh.comments), 1)
        body = gh.comments[0]
        self.assertEqual(old_marker_head(body), head)  # 旧标记仍在且可读回
        self.assertEqual(review.reviewed_heads([{"body": body}]), {head})
        audit = self.audit_of(body)
        self.assertEqual(audit["schema_version"], 1)
        self.assertEqual((audit["trace_id"], audit["head"], audit["base"]), ("task/905-obs", head, base))
        self.assertEqual((audit["reviewer"], audit["model"], audit["model_basis"]),
                         ("opencode", "reported/model-1", "reported"))
        self.assertEqual((audit["designer"], audit["implementers"], audit["independent"]), (None, [], None))
        self.assertEqual((audit["same_host"], audit["parsed"], audit["verdict"]), (False, True, "通过"))
        self.assertIsInstance(audit["duration_ms"], int)
        self.assertGreaterEqual(audit["duration_ms"], 0)
        self.assertEqual(audit["severity_counts"], SEVERITY_ZEROS)
        self.assertEqual([item["kind"] for item in audit["materials"]], ["task", "diff", "ci", "pr", "pack"])
        by_kind = {item["kind"]: item for item in audit["materials"]}
        self.assertEqual(by_kind["task"], {"kind": "task", "ref": "none",
                                           "sha256": self.sha("无".encode()),
                                           "size": len("无".encode()), "encoding": "utf8",
                                           "recipe": {"id": "task_v1"}})
        self.assertEqual(by_kind["diff"]["ref"], f"{base}...{head}")
        self.assertEqual(by_kind["ci"]["ref"], "pull/14/checks")
        self.assertEqual(by_kind["pr"]["ref"], "pull/14")
        for kind, name in (("diff", "diff.patch"), ("ci", "ci.md"), ("pr", "pr.md")):
            data = (folder / name).read_bytes()
            self.assertEqual(by_kind[kind]["sha256"], self.sha(data))
            self.assertEqual(by_kind[kind]["size"], len(data))
            self.assertEqual(by_kind[kind]["encoding"], "utf8")
            self.assertEqual(by_kind[kind]["recipe"], {"id": f"{kind}_v1"})
        pack = (folder / "pack.md").read_bytes()
        self.assertEqual(by_kind["pack"]["sha256"], self.sha(pack))
        self.assertEqual(by_kind["pack"]["encoding"], "raw_bytes")
        self.assertEqual(by_kind["pack"]["ref"], self.sha(pack))
        self.assertEqual(by_kind["pack"]["recipe"],
                         {"id": "pack_v1", "engine_ref": head, "artifact_sha256": self.sha(pack),
                          "retention_days": 30})
        self.assertEqual(self.artifact(self.sha(pack)), pack)  # pack 原始字节转存本机产物

        rows = self.trace_events("task/905-obs")
        self.assertEqual(self.steps(rows), [("review", "ok")])
        event = rows[0]
        self.assertEqual(event["stage"], "review")
        self.assertEqual(event["outputs"]["verdict"], "通过")
        self.assertEqual(event["outputs"]["model"], "reported/model-1")
        self.assertEqual(event["outputs"]["comment.url"], "https://example.invalid/repo/pull/14#issuecomment-9")
        self.assertEqual(event["outputs"]["comment.sha256"], self.sha(body.encode("utf-8")))
        self.assertEqual(event["outputs"]["comment.size"], len(body.encode("utf-8")))
        self.assertEqual(event["outputs"]["same_host"], False)
        self.assertEqual(event["outputs"]["severity.unknown"], 0)
        self.assertEqual(self.input_refs(event["id"])[0], ("rev", head, None, None))
        self.assertEqual([item[0] for item in self.input_refs(event["id"])[1:]],
                         ["task", "diff", "ci", "pr", "pack"])
        self.assertEqual(gh.removed_labels, 1)

        # 不通过：结论 fail、按严重度计数、评论仍带 C6 标记；模型未报告时记显式请求值
        gh = FakeReviewGitHub(pr_json, checks)
        findings = [{"severity": "阻断"}, {"severity": "严重"}, {"severity": "一般"},
                    {"severity": "建议"}, {"severity": "奇怪"}]
        fake = FakeReviewer(verdict_script("不通过", json.dumps(findings, ensure_ascii=False)),
                            reported="", basis="explicit_request", model_name="cfg/model-9")
        with mock.patch.object(review, "make_reviewer", lambda name: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review.review_pr(14, "opencode", root=self.repo, github=gh), 0)
        rows = [row for row in self.trace_events("task/905-obs") if row["id"] > event["id"]]
        self.assertEqual(self.steps(rows), [("review", "fail")])
        audit = self.audit_of(gh.comments[0])
        self.assertEqual(audit["model_basis"], "explicit_request")
        self.assertEqual(audit["model"], "cfg/model-9")  # 显式请求可记录，不冒充已报告
        self.assertEqual(audit["severity_counts"], {"阻断": 1, "严重": 1, "一般": 1, "建议": 1, "unknown": 1})
        outputs = rows[0]["outputs"]
        self.assertEqual((outputs["severity.blocker"], outputs["severity.critical"], outputs["severity.major"],
                          outputs["severity.minor"], outputs["severity.unknown"]), (1, 1, 1, 1, 1))
        self.assertEqual(outputs["comment.url"], "https://example.invalid/repo/pull/14#issuecomment-9")

        # 评审方退出非零：没有评论、事件 error 带 error.kind，材料仍记录
        gh = FakeReviewGitHub(pr_json, checks)
        fake = FakeReviewer("import sys; sys.stderr.write('ERROR: 额度已用尽\\n'); sys.exit(3)",
                            reported="", basis="unknown")  # 崩溃前未报告模型
        with mock.patch.object(review, "make_reviewer", lambda name: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review.review_pr(14, "opencode", root=self.repo, github=gh), 1)
        self.assertEqual(gh.comments, [])
        self.assertEqual(gh.removed_labels, 0)
        rows = [row for row in self.trace_events("task/905-obs") if row["step"] == "review"][-1]
        self.assertEqual((rows["status"], rows["error_kind"]), ("error", "reviewer_exit"))
        self.assertEqual(rows["outputs"]["verdict"], "评审失败")
        self.assertIn("额度已用尽", rows["outputs"]["failure"])
        self.assertIsNone(rows["outputs"]["model"])
        self.assertNotIn("comment.url", rows["outputs"])
        self.assertEqual([item[0] for item in self.input_refs(rows["id"])[1:]],
                         ["task", "diff", "ci", "pr", "pack"])
        self.assertEqual(events_db.verify(), [])

    # ---------- 验收 4：观察 API 与读取引用失败不改变派发与评审 ----------

    def test_observation_does_not_change_dispatch_or_review(self):
        rel = "docs/plans/task-905-j.md"
        self.write_taskbook(rel, self.header("T905J"))
        self.commit_guards()

        def dispatch_state(label: str, *, off: bool, broken: bool):
            self.stub_taskbook(rel, self.header("T905J"))
            if off:
                os.environ["HARNESS_EVENTS"] = "off"
            if broken:
                self.break_observation()
            host = RecordingHost([], executor_script(mixed_stream()))
            gh = FakeGitHub(self.repo, ci=(True, "", [7]))
            try:
                _, out, err = self.run_dispatch(rel, host, gh, expect=0, label=label)
            finally:
                os.environ.pop("HARNESS_EVENTS", None)
            return host.calls, gh.calls, out, err

        off = dispatch_state("off", off=True, broken=False)
        healthy = dispatch_state("on", off=False, broken=False)
        broken = dispatch_state("broken", off=False, broken=True)
        for label, index in (("host", 0), ("gh", 1), ("stdout", 2), ("stderr", 3)):
            self.assertEqual(off[index], healthy[index], label)
            self.assertEqual(off[index], broken[index], label)

        # 评审：同一 PR 在关闭、健康、观察失败三种状态下评论与调用序列一致（duration 是唯一计时差异）
        pr_json = self.run_pr_head()
        checks = [{"name": "harness", "state": "SUCCESS", "link": "https://ci.example.invalid/run/1"}]

        def review_state(label: str, *, off: bool, broken: bool):
            if off:
                os.environ["HARNESS_EVENTS"] = "off"
            if broken:
                self.break_observation()
            gh = FakeReviewGitHub(pr_json, checks)
            fake = FakeReviewer(verdict_script("通过", "[]"))
            try:
                with mock.patch.object(review, "make_reviewer", lambda name: fake), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    code = review.review_pr(14, "opencode", root=self.repo, github=gh)
            finally:
                os.environ.pop("HARNESS_EVENTS", None)
            self.assertEqual(code, 0, label)
            body = re.sub(r'"duration_ms": \d+', '"duration_ms": 0', gh.comments[0])
            return gh.calls, body, out.getvalue()

        off = review_state("off", off=True, broken=False)
        healthy = review_state("on", off=False, broken=False)
        broken = review_state("broken", off=False, broken=True)
        for label, index in (("gh", 0), ("comment", 1), ("stdout", 2)):
            self.assertEqual(off[index], healthy[index], label)
            self.assertEqual(off[index], broken[index], label)

    # ---------- 验收 5：PR 标题不重复任务 id 前缀、人工验收一节按验收表条件化 ----------

    def test_pr_title_has_no_duplicate_prefix(self):
        """任务书标题已带「TXXX：」前缀时 PR 标题不重复拼接（B65）；无前缀时照常补上。"""
        self.commit_guards()

        def dispatch_once(rel: str, task_id: str) -> RecordingTitleGitHub:
            self.stub_taskbook(rel, self.header(task_id))
            gh = RecordingTitleGitHub(self.repo, ci=(True, "", [7]))
            self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream())), gh, expect=0)
            return gh

        # 标题已带前缀：# T905P：夹具标题 → PR 标题恰为「T905P：夹具标题」，不出现两次前缀
        rel = "docs/plans/task-905-p.md"
        self.write_taskbook(rel, self.header("T905P"), title="# T905P：夹具标题")
        self.assertEqual(dispatch_once(rel, "T905P").titles, ["T905P：夹具标题"])

        # 标题无前缀：# 任务：夹具 → PR 标题「T905Q：夹具」
        rel = "docs/plans/task-905-q.md"
        self.write_taskbook(rel, self.header("T905Q"))
        self.assertEqual(dispatch_once(rel, "T905Q").titles, ["T905Q：夹具"])

    def test_pr_body_manual_section_conditional(self):
        """验收表存在「人工」证据行时 pr_body 写指引，否则写「无」（B57）。"""
        self.commit_guards()
        manual_table = ("\n| 编号 | 验收内容 | 证据类型 | 覆盖 |\n|---|---|---|---|\n"
                        "| M1 | 真机人工验收 | 人工 | 无 |\n")
        auto_table = ("\n| 编号 | 验收内容 | 证据类型 | 覆盖 |\n|---|---|---|---|\n"
                      "| A1 | 夹具验收 | 夹具 | `tests.test_fixture.SomeTest` |\n")
        guidance = "见任务书验收表中的人工条目（`bin/harness acceptance --manual`）。"

        def dispatch_once(rel: str, task_id: str, title: str) -> RecordingTitleGitHub:
            self.write_taskbook(rel, self.header(task_id), title=title)
            self.stub_taskbook(rel, self.header(task_id))
            gh = RecordingTitleGitHub(self.repo, ci=(True, "", [7]))
            self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream())), gh, expect=0)
            return gh

        # 有「人工」行：写指引
        gh = dispatch_once("docs/plans/task-905-r.md", "T905R", "# 任务：夹具" + manual_table)
        self.assertIn(f"## 需要人工验收的部分\n\n{guidance}\n", gh.pr_bodies[0])

        # 表内只有可自动化行：写「无」，不写指引
        gh = dispatch_once("docs/plans/task-905-s.md", "T905S", "# 任务：夹具" + auto_table)
        self.assertIn("## 需要人工验收的部分\n\n无\n", gh.pr_bodies[0])
        self.assertNotIn("bin/harness acceptance --manual", gh.pr_bodies[0])

        # 没有验收表：同样写「无」（直接调用 pr_body，钉住 root 参数）
        rel = "docs/plans/task-905-t.md"
        self.write_taskbook(rel, self.header("T905T"))
        task = dispatch.Task(rel, "T905T", "K7", "R3", {}, [], "task/905-t")
        body = dispatch.pr_body(task, dispatch.Attempt(ok=True, exit="ok"), 1, "a" * 64, root=self.repo)
        self.assertIn("## 需要人工验收的部分\n\n无\n", body)

    # ---------- 验收 6：分支已有开放 PR 时复用编号，不重复开（B70/T121） ----------

    def test_resume_reuses_existing_pr(self):
        """分支已有开放 PR（resume）：open_pr 不被调用，既有编号复用进 CI 反馈与升级（B70）。"""
        self.commit_guards()
        rel = "docs/plans/task-905-u.md"
        self.write_taskbook(rel, self.header("T905U"))
        self.stub_taskbook(rel, self.header("T905U"))
        self.git("push", "-q", "origin", "main:refs/heads/task/905-u")  # resume 从远端已有分支续跑
        gh = FakeGitHub(self.repo, branch_exists=True, ci=(False, "CI 未通过：见摘要", [5]))
        gh.existing = 77
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.make_dispatcher(RecordingHost([], executor_script(mixed_stream())), gh).run(rel, resume=True)
        self.assert_dispatch_exit(code, 1, out.getvalue(), err.getvalue())
        self.assertNotIn(("open_pr", "task/905-u"), gh.calls)  # 不新建 PR（B70 崩溃点）
        self.assertEqual(gh.pr_bodies, [])
        rows = self.trace_events("task/905-u")
        self.assertNotIn("push_pr", [row["step"] for row in rows])
        # 既有编号复用进后续反馈：CI 轮次与升级都落在 #77
        self.assertEqual(next(row for row in rows if row["step"] == "ci_wait")["outputs"]["pr"], 77)
        self.assertEqual(next(row for row in rows if row["step"] == "escalate")["outputs"]["pr"], 77)
        self.assertIn(("comment", 77), gh.calls)
        self.assertIn("budget-exceeded", gh.labels)

    def test_no_existing_pr_unchanged(self):
        """无既有 PR：新建路径与编号逐字不变（B70）。"""
        self.commit_guards()
        rel = "docs/plans/task-905-v.md"
        self.write_taskbook(rel, self.header("T905V"))
        self.stub_taskbook(rel, self.header("T905V"))
        gh = FakeGitHub(self.repo, ci=(True, "", [7]))
        _, out, _ = self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream())), gh, expect=0)
        self.assertIn("CI 通过", out)
        rows = self.trace_events("task/905-v")
        self.assertEqual([row["step"] for row in rows],
                         ["admit", "guard_preflight", "slot", "claim", "executor_round", "local_verify",
                          "push_pr", "ci_wait"])
        self.assertEqual(gh.calls, [("remote_branch_exists", "task/905-v"), ("push", "task/905-v"),
                                    ("push", "task/905-v"), ("open_pr", "task/905-v"),
                                    ("wait_ci", "task/905-v")])  # 查询为空不改变既有调用序列与新建路径
        self.assertEqual(rows[6]["outputs"]["pr"], 14)
        self.assertEqual(rows[7]["outputs"]["pr"], 14)

    # ---------- 验收（B73）：dispatch 断言失败现场携带死因；执行方 git 瞬断被重试吞掉 ----------

    def test_dispatch_failure_diagnostics_visible(self):
        """执行方写 stderr 后退出 1：退出码断言的失败消息含流尾部，stderr 死因在最后 30 行内（B73）。"""
        self.commit_guards()
        rel = "docs/plans/task-936-a.md"
        self.write_taskbook(rel, self.header("T936A"))
        self.stub_taskbook(rel, self.header("T936A"))
        dying = executor_script(mixed_stream(), exit_code=1, stderr="fatal: index.lock 已被占用\n")
        with self.assertRaises(AssertionError) as caught:
            self.run_dispatch(rel, RecordingHost([], dying), FakeGitHub(self.repo), expect=0)
        message = str(caught.exception)
        self.assertIn("执行方流尾部", message)
        tail = message.split("最后 30 行）---", 1)[1].strip()
        self.assertIn("fatal: index.lock 已被占用", tail)  # 死因随流落盘并被断言消息携带
        self.assertLessEqual(len(tail.splitlines()), 30)

    def test_executor_script_retries_transient_git(self):
        """git 前两次瞬断第三次成功被重试吞掉：dispatch 正常完成（无重试时执行方死在 add 上，B73）。"""
        self.commit_guards()
        rel = "docs/plans/task-935-a.md"
        self.write_taskbook(rel, self.header("T935A"))
        self.stub_taskbook(rel, self.header("T935A"))
        gh = FakeGitHub(self.repo, ci=(True, "", [7]))
        _, out, _ = self.run_dispatch(rel, RecordingHost([], executor_script(mixed_stream(), git_failures=2)),
                                      gh, expect=0)
        self.assertIn("CI 通过", out)
        rows = self.trace_events("task/935-a")
        self.assertEqual(self.steps(rows),
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "ok"), ("push_pr", "ok"), ("ci_wait", "ok")])


if __name__ == "__main__":
    unittest.main()
