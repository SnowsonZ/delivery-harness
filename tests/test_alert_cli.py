"""T403 告警 CLI 与工作流末步测试：设计 5 七行触发的 reason/trace/目标/标签、远端标记跨机去重、
工作流承载（push main 与 workflow_run、PR 检查只读、受信任默认分支代码、最小写权限）与发布失败隔离。

夹具沿用既有模式：匿名临时 git 仓库、隔离 events_db.ROOT、冻结时钟、假 gh 与假评审方；工作流侧复用
tests/test_ci_events_workflows.py 的模板解析与步骤重放（告警命令委托安装布局里的真实 CLI，gh 由
PATH 桩记录）。路由与审计触发事件经真实产品入口（policy.main、audit 观察发射）产出，不碰真实库/PR。
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
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import review
from engine.checks import r1_checks
from engine.core import alerts, events, events_db
from engine.reports import audit
from engine.routing import policy
from tests.test_ci_events_workflows import _Replay, parse_workflow

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
FROZEN_TS = "2026-02-03T04:05:06.789Z"
TRACE = "task/938-a"  # 发布失败隔离（验收 4）的公共追踪 ID
PR = 14
CHECKS = [{"name": "harness", "state": "SUCCESS", "link": "https://ci.example.invalid/run/1"}]


def verdict_script(verdict: str, findings_json: str) -> str:
    body = json.dumps({"verdict": verdict, "summary": "结论摘要", "findings": "PLACEHOLDER"},
                      ensure_ascii=False)
    return "import json, sys; print(" + repr(body.replace('"PLACEHOLDER"', findings_json)) + ")"


class FakeReviewer:
    """假评审方：argv 启动固定脚本；read 按脚本返回展示模型与 model_basis（与 T105 夹具同形）。"""

    name = "opencode"
    env: ClassVar[dict[str, str]] = {}

    def __init__(self, script: str):
        self.script = script

    def argv(self, prompt, workspace, output):
        return [sys.executable, "-c", self.script]

    def read(self, stdout, output):
        return stdout, "reported/model-1", "reported"


class FakeAlertGitHub:
    """C4 发布接口桩：publish 所需方法全部实现并记录；fail_alerts 时发布类调用抛 RuntimeError。
    同时充当评审桩（ReviewGitHub）的发布接口基类：方法形状与 dispatch.GitHub 一致。"""

    def __init__(self, *, fail_alerts: bool = False):
        self.fail_alerts = fail_alerts
        self.calls: list[tuple] = []
        self.comments: list[tuple] = []  # (pr, body, label)
        self.edited_comments: list[tuple] = []
        self.issues: list[dict] = []
        self.edited_issues: list[tuple] = []
        self.labels: list[tuple] = []

    def _fail(self):
        if self.fail_alerts:
            raise RuntimeError("alert gh down")

    def list_comments(self, pr):
        self._fail()
        self.calls.append(("list_comments", pr))
        return [{"id": index + 1, "body": item[1]} for index, item in enumerate(self.comments)
                if item[0] == pr]

    def comment(self, pr, body, label=None):
        if label:
            self._fail()
        self.calls.append(("comment", pr, label))
        self.comments.append((pr, body, label))
        if label:
            self.add_label(pr, label)
        return f"https://example.invalid/pull/{pr}#issuecomment-{len(self.comments)}"

    def edit_comment(self, comment_id, body):
        self._fail()
        self.calls.append(("edit_comment", comment_id))
        self.edited_comments.append((comment_id, body))
        pr, _old, label = self.comments[comment_id - 1]
        self.comments[comment_id - 1] = (pr, body, label)

    def list_issues(self, label):
        self._fail()
        self.calls.append(("list_issues", label))
        return [{"number": issue["number"], "body": issue["body"]}
                for issue in self.issues if label in issue["labels"]]

    def edit_issue(self, number, body):
        self._fail()
        self.calls.append(("edit_issue", number))
        self.edited_issues.append((number, body))
        for issue in self.issues:
            if issue["number"] == number:
                issue["body"] = body

    def create_issue(self, title, body, labels):
        self._fail()
        self.calls.append(("create_issue", title, tuple(labels)))
        issue = {"number": 100 + len(self.issues), "title": title, "body": body, "labels": list(labels)}
        self.issues.append(issue)
        return f"https://example.invalid/issues/{issue['number']}"

    def add_label(self, pr, label):
        self._fail()
        self.calls.append(("add_label", pr, label))
        self.labels.append((pr, label))

    def disable_auto_merge(self, pr):
        self._fail()
        self.calls.append(("disable_auto_merge", pr))
        return True


class ReviewGitHub(FakeAlertGitHub):
    """评审用 gh 桩：pr view/checks/remove-label 走 _run 脚本应答；发布接口继承发布桩。
    fail_alerts 只让告警路径失败（评审评论 label=None 照常发布），验证原结论不被告警失败改变。"""

    def __init__(self, pr_json: dict, checks: list, *, fail_alerts: bool = False):
        super().__init__(fail_alerts=fail_alerts)
        self.pr_json = pr_json
        self.checks = checks
        self.removed_labels = 0

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        joined = " ".join(argv)
        self.calls.append((joined,))
        if joined.startswith("gh pr view"):
            return json.dumps(self.pr_json)
        if joined.startswith("gh pr checks") and "name,state,link" in joined:
            return json.dumps(self.checks)
        if joined.startswith("gh pr edit") and "--remove-label" in joined:
            self.removed_labels += 1
            return ""
        raise AssertionError(f"未预期的 gh 调用：{joined}")


class MinimalReviewGitHub(ReviewGitHub):
    """只评论的最小适配器（T105 夹具同形）：没有 C4 发布接口（list_comments 缺失），告警后补应跳过。"""

    list_comments = None  # 接口缺失的形态：getattr 为 None，评审后补告警跳过


class AlertReplay(_Replay):
    """步骤重放：补 steps.<id>.outcome 取值与步骤级 env 的代入（告警步骤用 env 传 PR/分支与令牌）。"""

    def lookup(self, expression):
        expression = expression.strip()
        if expression == "steps.integrity.outcome":
            step = "Verify engine integrity of the merged copy"
            return "failure" if step in self.failed or step in self.swallowed else "success"
        return super().lookup(expression)

    def run(self, steps: list[dict], *, stub: Path) -> None:
        for step in steps:
            if "uses" not in step and (step.get("env") or self.condition(step.get("if"))):
                env = {key: self.substitute(str(value)) for key, value in (step.get("env") or {}).items()}
                with mock.patch.object(self, "env", {**self.env, **env}):
                    super().run([step], stub=stub)  # 基类逐步骤重放；这里只补 env 的代入
                continue
            super().run([step], stub=stub)


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-alert-cli-"))
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

    def all_events(self) -> list[dict]:
        path = events_db.db_path()
        if path is None or not path.exists():
            return []
        with contextlib.closing(sqlite3.connect(path)) as conn:
            rows = conn.execute(
                "SELECT stage, step, status, trace_id, error_kind FROM events ORDER BY id").fetchall()
        return [dict(zip(("stage", "step", "status", "trace_id", "error_kind"), row)) for row in rows]

    def trace_events(self, trace: str, stage: str | None = None) -> list[dict]:
        return [row for row in self.all_events()
                if row["trace_id"] == trace and (stage is None or row["stage"] == stage)]

    def project_route_steps(self, project: Path, trace: str) -> list[tuple]:
        """项目自身事件库里的 route 事件（policy 经 events_db.ROOT 注入写在项目库里）。"""
        path = project / ".git" / "harness" / "harness.db"
        if not path.exists():
            return []
        with contextlib.closing(sqlite3.connect(path)) as conn:
            rows = conn.execute("SELECT stage, step, status, trace_id FROM events ORDER BY id").fetchall()
        return [(row[1], row[2]) for row in rows
                if row[3] == trace and row[0] == "route"]

    def alert(self, argv: list[str], gh) -> int:
        """真实产品入口：alert 子命令（注入发布适配器桩，其余全真）。"""
        return alerts.main(list(argv), gh=gh)

    def bundle_alert(self, reason: str, trace: str, bundle: Path, *, pr: int | None = None,
                     task: str | None = None) -> FakeAlertGitHub:
        """带事件包证据核对的告警发布（断言退出码 0），返回发布桩。"""
        gh = FakeAlertGitHub()
        argv = [reason, "--trace", trace, "--bundle", str(bundle), "--json"]
        argv += (["--pr", str(pr)] if pr else []) + (["--task", task] if task else [])
        self.assertEqual(self.alert(argv, gh), 0)
        return gh

    def fresh_project(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir()
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **GIT_ENV}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True, env=env)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, capture_output=True, env=env)
        return path

    def install_engine(self, project: Path) -> None:
        """真实产品入口：把当前引擎安装进临时项目（安装布局，含 .harness/engine 与锁文件）。"""
        # HARNESS_ 覆盖开关不混进测试；HARNESS_EVENTS_REDIRECT 除外——verify 跑项目检查时带着它，
        # 安装子进程运行的是本仓库引擎（公共目录相同），清洗掉它会把测试事件写回真实库（B92 修订 2）。
        env = {**{k: v for k, v in os.environ.items()
                  if not k.startswith("GIT_") and (not k.startswith("HARNESS_")
                                                   or k == "HARNESS_EVENTS_REDIRECT")},
               **GIT_ENV, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(ENGINE_REPO / "engine" / "cli.py"),
                                 "install", "--target", str(project), "--allow-dirty"],
                                capture_output=True, text=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_in_project(self, project: Path, argv: list[str]) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "GITHUB_", "HARNESS_")) and key != "CI"}
        env.update(GIT_ENV)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([sys.executable, *argv], cwd=project, capture_output=True, text=True,
                              env=env, check=False, timeout=120)

    def export_events(self, project: Path, name: str, target: Path | None = None) -> Path:
        """真实产品入口：ci_events 导出（安装布局）→ harness-events 包目录。"""
        target = target or self.tmp / f"export-{name}"
        result = self.run_in_project(project, [".harness/engine/reports/ci_events.py",
                                               "export", "--target", str(target)])
        self.assertEqual(result.returncode, 0, result.stderr)
        return target

    def tamper_engine(self, project: Path) -> None:
        page = project / ".harness" / "engine" / "core" / "common.py"
        page.write_text(page.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    def run_policy_budget(self, project: Path, trace: str, *, escapes: int) -> None:
        """真实产品入口：policy.main（命令行同一入口）产出 route 事件；escape 议题超预算即 预算 fail。"""
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True,
                              check=True).stdout.strip()
        autonomy = {"size": {"max_lines": 400, "exclude": []},
                    "classes": {"K1": {"name": "说明与记录", "level": "L4", "window": 20,
                                       "max_escapes": 0, "audit_every": 0}}}

        def gh(*args):
            joined = " ".join(args)
            if joined.startswith("pr view") and "headRefName" in joined:
                return json.dumps({"headRefName": trace})
            if joined.startswith("pr view") and "labels" in joined:
                return json.dumps({"labels": []})
            if joined.startswith("pr list"):
                return json.dumps([{"number": 101}])
            if joined.startswith("issue list"):
                return json.dumps([{"number": 7, "title": "escape", "body": "#101"}] * escapes)
            raise AssertionError(f"未预期的 gh 调用：{joined}")

        orig_gather = policy.gather

        def gather(*args, **kwargs):  # 只注入 cwd 与 gh 桩，不改判定逻辑（与 T103 夹具同口径）
            kwargs.setdefault("cwd", project)
            kwargs.setdefault("gh", gh)
            return orig_gather(*args, **kwargs)

        with mock.patch.object(events_db, "ROOT", project), \
                mock.patch.object(r1_checks, "load_autonomy", return_value=autonomy), \
                mock.patch.object(policy, "gather", gather), \
                mock.patch.object(policy, "_gh", gh), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = policy.main(["--base", head, "--head", head, "--pr", "9", "--branch", trace])
        self.assertEqual(code, 0)

    def audit_findings(self, project: Path, rules: list[str]) -> None:
        """真实产品入口：audit 的观察发射写出 ci/audit.summary 与 ci/audit.finding 事件。"""
        report = {"pr": 9, "repository": "example/app", "trace_id": None,
                  "head_sha": "a" * 40, "merge_sha": None, "merged_at": None, "stages": [],
                  "references": [], "chains": [], "anchors": [],
                  "coverage": {"events": {"local": 0, "ci": 0, "github": 0}, "chains": 0, "anchors": 0,
                               "references": {"total": 0}, "ledger": "absent"},
                  "ok": not rules,
                  "findings": [{"rule": rule, "severity": "error", "source": "local", "stage": "review",
                                "ref": "docs/x@ab12", "reason": "分类原因"} for rule in rules]}
        with mock.patch.object(events_db, "ROOT", project):
            audit._emit_observations(report)

    def run_pr_head(self, trace: str) -> tuple[str, dict]:
        """建评审夹具：feature 分支一个提交，推为 origin 的 pull/14/head；返回 (head, PR 视图 JSON)。"""
        self.git("checkout", "-q", "-b", trace)
        head = self.commit({"docs/note.md": "note\n"}, "feature")
        self.git("push", "-q", "origin", "HEAD:refs/pull/14/head")
        return head, {"title": "T938：评审夹具", "body": "描述正文", "headRefName": trace,
                      "headRefOid": head, "baseRefName": "main", "state": "OPEN", "mergeCommit": None}

    def run_review(self, pr_json: dict, fake, *, gh=None, fail_alerts: bool = False):
        """真实产品入口：review_pr（注入假评审方与 gh 桩，吞掉 stdout）；返回 (退出码, gh 桩)。"""
        gh = gh or ReviewGitHub(pr_json, CHECKS, fail_alerts=fail_alerts)
        with mock.patch.object(review, "make_reviewer", lambda name: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            code = review.review_pr(PR, "opencode", root=self.repo, github=gh)
        return code, gh

    def template(self, name: str) -> dict:
        return parse_workflow((ENGINE_REPO / "templates/.github/workflows" / name).read_text(encoding="utf-8"))

    # ---------- 验收 1：设计 5 七行触发分别断言 reason/trace/目标/标签；正常与无预算配置不误报 ----------

    def test_all_seven_trigger_families(self):
        # 第 1 行 完整性检查失败（push main 承载的证据链：真实 integrity 检查 → 真实导出 → 证据核对 → 发布）
        trace = "task/938-int"
        project = self.fresh_project("row1")
        self.install_engine(project)
        gh = self.bundle_alert("integrity_failure", trace, self.export_events(project, "row1-clean"),
                               task="T938")
        self.assertEqual(gh.calls, [])  # 正常（未篡改）：包内无证据，不发布
        self.assertEqual([(row["step"], row["status"]) for row in self.trace_events(trace, "alert")],
                         [("integrity_failure", "skip")])
        self.tamper_engine(project)
        self.assertEqual(self.run_in_project(project, [".harness/engine/cli.py", "integrity"]).returncode, 1)
        gh = self.bundle_alert("integrity_failure", trace, self.export_events(project, "row1-tampered"),
                               task="T938")
        self.assertEqual(len(gh.issues), 1)  # 无 PR：复用一条升级议题
        self.assertEqual(gh.issues[0]["labels"], ["escalation"])
        self.assertIn(alerts.marker(trace, "integrity_failure"), gh.issues[0]["body"])
        self.assertIn(trace, gh.issues[0]["body"])
        self.assertEqual([(row["step"], row["status"]) for row in self.trace_events(trace, "alert")],
                         [("integrity_failure", "skip"), ("integrity_failure", "ok")])

        # 第 6 行 路由因误差预算耗尽拒绝自动合并（真实 policy.main 产出 route/预算 fail → 包证据 → 发布）
        trace = "task/938-route"
        project = self.fresh_project("row6")
        self.install_engine(project)
        self.run_policy_budget(project, trace, escapes=0)  # 正常：预算未耗尽
        gh = self.bundle_alert("budget_exhausted", trace, self.export_events(project, "row6-clean"), pr=9)
        self.assertEqual(gh.calls, [])
        self.run_policy_budget(project, trace, escapes=1)  # 误差预算耗尽：逃逸 1 > 预算 0
        gh = self.bundle_alert("budget_exhausted", trace, self.export_events(project, "row6-exhausted"), pr=9)
        route = self.project_route_steps(project, trace)
        self.assertIn(("预算", "fail"), route)  # 真实路由产出的预算规则失败事件是发布依据
        self.assertIn(("result", "deny"), route)
        self.assertEqual([item[1] for item in gh.calls if item[0] == "comment"], [9])
        self.assertEqual(gh.comments[0][2], "escalation")
        self.assertIn(alerts.marker(trace, "budget_exhausted"), gh.comments[0][1])

        # 第 7 行 审计发现：环节缺失与链头/锚点不一致（audit 观察发射真实产出 → 两条告警族分别发布）
        trace = "task/938-audit"
        project = self.fresh_project("row7")
        self.install_engine(project)
        self.audit_findings(project, ["missing_route", "anchor_mismatch", "hash_mismatch"])
        bundle = self.export_events(project, "row7")
        gh = self.bundle_alert("audit_missing_stage", trace, bundle, pr=9)
        self.assertEqual(self.alert(["audit_anchor_mismatch", "--trace", trace, "--pr", "9",
                                     "--bundle", str(bundle), "--json"], gh), 0)
        self.assertEqual(len(gh.comments), 2)
        self.assertIn(alerts.marker(trace, "audit_missing_stage"), gh.comments[0][1])
        self.assertIn(alerts.marker(trace, "audit_anchor_mismatch"), gh.comments[1][1])
        for comment in gh.comments:
            self.assertEqual(comment[2], "escalation")
            self.assertIn(trace, comment[1])
        # 负例：只有未映射的引用级发现（hash_mismatch）→ 不自动告警（未知失败不触发）
        project = self.fresh_project("row7-neg")
        self.install_engine(project)
        self.audit_findings(project, ["hash_mismatch"])
        bundle = self.export_events(project, "row7-neg")
        gh = self.bundle_alert("audit_missing_stage", trace, bundle, pr=9)
        self.assertEqual(self.alert(["audit_anchor_mismatch", "--trace", trace, "--pr", "9",
                                     "--bundle", str(bundle), "--json"], gh), 0)
        self.assertEqual(gh.calls, [])

        # 第 2 行 评审不通过 / 评审方自身失败（review 在已有评论发布后补标签与告警，不重跑评审）
        trace = "task/938-review"
        _head, pr_json = self.run_pr_head(trace)
        code, gh = self.run_review(pr_json, FakeReviewer(verdict_script("通过", "[]")))
        self.assertEqual(code, 0)
        self.assertEqual([item[2] for item in gh.comments], [None])  # 正常通过：只有评审评论，无告警
        self.assertEqual(gh.labels, [])

        code, gh = self.run_review(pr_json, FakeReviewer(verdict_script("不通过", '[{"severity": "阻断"}]')))
        self.assertEqual(code, 0)
        self.assertEqual(len(gh.comments), 2)  # 评审评论 + 告警评论；评审只跑一次
        self.assertEqual(gh.comments[0][2], None)
        self.assertEqual(gh.comments[1][2], "escalation")
        self.assertIn(alerts.marker(trace, "review_rejected"), gh.comments[1][1])
        self.assertIn(trace, gh.comments[1][1])
        self.assertEqual(gh.labels, [(PR, "escalation")])
        self.assertEqual([(row["step"], row["status"]) for row in self.trace_events(trace, "review")],
                         [("review", "ok"), ("review", "fail")])
        self.assertEqual([(row["step"], row["status"]) for row in self.trace_events(trace, "alert")],
                         [("review_rejected", "ok")])

        code, gh = self.run_review(pr_json, FakeReviewer("import sys; sys.stderr.write('ERROR: 额度用尽\\n'); sys.exit(3)"))
        self.assertEqual(code, 1)
        self.assertEqual([item[2] for item in gh.comments], ["escalation"])  # 无评审评论，只有告警
        self.assertIn(alerts.marker(trace, "review_error"), gh.comments[0][1])
        # 无发布接口的最小适配器：后补跳过，评审本体与评论不变，也不新增告警事件
        alert_before = len(self.trace_events(trace, "alert"))
        code, gh = self.run_review(pr_json, FakeReviewer(verdict_script("不通过", "[]")),
                                   gh=MinimalReviewGitHub(pr_json, CHECKS))
        self.assertEqual(code, 0)
        self.assertEqual([item[2] for item in gh.comments], [None])
        self.assertEqual(len(self.trace_events(trace, "alert")), alert_before)

        # 第 3 行 派发卡死/超时/打转/预算：既有升级通道之上的稳定 reason 键，CLI 显式发布各自成键
        trace = "task/938-disp"
        for reason in ("dispatch_stall", "dispatch_timeout", "dispatch_loop", "dispatch_budget"):
            gh = FakeAlertGitHub()
            self.assertEqual(self.alert([reason, "--pr", str(PR), "--trace", trace, "--task", "T938",
                                         "--json"], gh), 0)
            self.assertEqual(len(gh.comments), 1)
            self.assertEqual(gh.comments[0][0], PR)
            self.assertEqual(gh.comments[0][2], "escalation")
            self.assertIn(alerts.marker(trace, reason), gh.comments[0][1])
            self.assertIn(trace, gh.comments[0][1])
        self.assertEqual([row["step"] for row in self.trace_events(trace, "alert")],
                         ["dispatch_stall", "dispatch_timeout", "dispatch_loop", "dispatch_budget"])

        # 第 4 行 CI 只剩最后一轮：真实预警入口（wait 前由派发调用），预算 1 首轮即预警
        trace = "task/938-warn"
        with mock.patch.object(alerts, "load_rules", return_value={"alerts": {}}):
            gh = FakeAlertGitHub()
            alerts.last_round_alert(trace, "T938", 1, 3, pr=PR, gh=gh)
            self.assertEqual(gh.calls, [])  # 非末轮不预警（正常推进不误报）
            alerts.last_round_alert(trace, "T938", 3, 3, pr=PR, gh=gh)
            self.assertEqual(len(gh.comments), 1)
            self.assertEqual(gh.comments[0][2], "escalation")
            self.assertIn(alerts.marker(trace, "ci_last_round"), gh.comments[0][1])
            alerts.last_round_alert(trace, "T938", 1, 1, pr=PR, gh=gh)  # 预算 1：首轮即最后一轮
            self.assertEqual(len(gh.comments), 1)  # 同键更新，不重发
            self.assertEqual(len(gh.edited_comments), 1)

        # 第 5 行 守卫拒绝达到阈值：被拒工具计数触发，无 PR 复用升级议题
        trace = "task/938-denied"
        with mock.patch.object(alerts, "load_rules",
                               return_value={"alerts": {"guard_denials_threshold": 2}}):
            gh = FakeAlertGitHub()
            alerts.guard_round_alert(trace, "T938", 1, pr=None, gh=gh)
            self.assertEqual(gh.calls, [])  # 阈值-1 不预警
            alerts.guard_round_alert(trace, "T938", 2, pr=None, gh=gh)
            self.assertEqual(len(gh.issues), 1)
            self.assertEqual(gh.issues[0]["labels"], ["escalation"])
            self.assertIn(alerts.marker(trace, "guard_denials"), gh.issues[0]["body"])

        # 无预算配置：两个派发预警入口都是空操作（不误报）；其余各行不依赖配置
        before = len(self.all_events())
        with mock.patch.object(alerts, "load_rules", return_value={}):
            gh = FakeAlertGitHub()
            alerts.last_round_alert(trace, "T938", 3, 3, pr=PR, gh=gh)
            alerts.guard_round_alert(trace, "T938", 9, pr=PR, gh=gh)
            self.assertEqual(gh.calls, [])
        self.assertEqual([row for row in self.all_events()[before:]
                          if row["stage"] == "alert" and row["status"] == "ok"], [])

    # ---------- 验收 2：删本机缓存后重跑仍更新同一条远端评论（两 reason 两条，无 PR 复用议题） ----------

    def test_remote_marker_dedup_survives_new_machine(self):
        gh = FakeAlertGitHub()
        argv = ["ci_last_round", "--pr", str(PR), "--trace", TRACE, "--task", "T939", "--json"]
        self.assertEqual(self.alert(argv, gh), 0)
        self.assertEqual(len(gh.comments), 1)
        self.assertFalse(gh.edited_comments)
        self.assertTrue(self.trace_events(TRACE, "alert"))  # 本机有记录，但它不是去重权威

        # 删除本机缓存（事件库）：同 trace/reason 重跑仍更新同一条远端评论，不新增
        for suffix in ("", "-wal", "-shm"):
            Path(str(events_db.db_path()) + suffix).unlink(missing_ok=True)
        self.assertEqual(self.all_events(), [])
        self.assertEqual(self.alert(argv, gh), 0)
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual([cid for cid, _body in gh.edited_comments], [1])
        self.assertEqual(gh.list_comments(PR)[0]["body"], gh.edited_comments[0][1])

        # 第二个 reason → 第二条评论；各自成键互不合并
        self.assertEqual(self.alert(["guard_denials", "--pr", str(PR), "--trace", TRACE, "--json"], gh), 0)
        self.assertEqual(len(gh.comments), 2)
        self.assertIn(alerts.marker(TRACE, "guard_denials"), gh.comments[1][1])

        # 无 PR：查/建一条升级议题；删缓存重跑复用并更新同一条议题
        issue_argv = ["guard_denials", "--trace", "task/939-b", "--task", "T939", "--json"]
        self.assertEqual(self.alert(issue_argv, gh), 0)
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.issues[0]["labels"], ["escalation"])
        for suffix in ("", "-wal", "-shm"):
            Path(str(events_db.db_path()) + suffix).unlink(missing_ok=True)
        self.assertEqual(self.alert(issue_argv, gh), 0)
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual([number for number, _body in gh.edited_issues], [gh.issues[0]["number"]])

    # ---------- 验收 3：模板结构与假步骤重放——失败后才告警、受信任代码、最小写权限 ----------

    def test_workflows_run_on_failure_and_use_trusted_code(self):
        harness = self.template("harness.yml")
        auto = self.template("auto-merge.yml")
        self.assertEqual(harness["permissions"], {"contents": "read"})  # 继承权限只读
        self.assertEqual(auto["permissions"], {"contents": "read"})
        self.assertEqual(set(auto["on"]["workflow_run"]["workflows"]), {"harness"})  # 不由 pull_request 触发

        # 所有 pull_request 会运行的 job 无写权限；写权限 job 全部被 push main 条件限定（fork/同仓库一视同仁）
        for name, job in harness["jobs"].items():
            gate = str(job.get("if") or "")
            if "write" in json.dumps(job.get("permissions") or {}):
                self.assertIn("push", gate, name)
                self.assertIn("refs/heads/main", gate, name)
                if job.get("needs"):  # 依赖 harness 的承载在 harness 失败后仍要运行
                    self.assertIn("cancelled()", gate, name)
            else:
                for value in (job.get("permissions") or {"contents": "read"}).values():
                    self.assertEqual(value, "read", name)

        # push main 承载（harness.yml）：needs harness + 只在 push main；最小写权限 issues:write
        job = harness["jobs"]["alert"]
        self.assertEqual(job["needs"], "harness")
        self.assertEqual(job["permissions"], {"contents": "read", "issues": "write"})
        steps = job["steps"]
        names = [step.get("name") or step.get("uses") for step in steps]
        check = steps[names.index("Verify engine integrity of the merged copy")]
        self.assertEqual(check["run"], "python .harness/engine/cli.py integrity")  # 受信任的 main 检出
        alert_step = steps[names.index("Alert integrity failure")]
        self.assertEqual(alert_step["if"], "${{ always() && steps.integrity.outcome == 'failure' }}")
        self.assertTrue(alert_step["continue-on-error"])  # 告警失败不影响 job 结论
        self.assertIn('alert integrity_failure --trace "main@${GITHUB_SHA::7}"', alert_step["run"])

        # workflow_run 承载（auto-merge.yml）：needs judge + if always() 且只在 workflow_run 事件
        # （push 触发的同步一路不告警，T715）；默认分支代码；head/事件包只是数据
        job = auto["jobs"]["alert"]
        self.assertEqual(job["needs"], "judge")
        self.assertEqual(job["if"], "${{ always() && github.event_name == 'workflow_run' }}")  # push 时跳过
        self.assertEqual(job["permissions"], {"actions": "read", "contents": "read",
                                              "issues": "write", "pull-requests": "write"})
        steps = job["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "${{ github.event.repository.default_branch }}")
        downloads = [step for step in steps if "download-artifact" in str(step.get("uses"))]
        self.assertEqual(len(downloads), 2)
        self.assertEqual(downloads[0]["with"]["run-id"], "${{ github.event.workflow_run.id }}")
        self.assertEqual(downloads[0]["with"]["path"], "${{ runner.temp }}/trigger-events")
        self.assertEqual(downloads[1]["with"]["path"], "${{ runner.temp }}/judge-events")
        for step in steps:
            self.assertFalse([value for value in (step.get("with") or {}).values()
                              if "head_sha" in str(value).lower()], step.get("name"))
            for line in str(step.get("run", "")).splitlines():
                if line.strip().startswith("python"):
                    self.assertTrue(line.strip().startswith("python .harness/engine/"), line)
        alert_step = steps[-1]
        self.assertEqual(alert_step["if"], "${{ always() }}")
        self.assertTrue(alert_step["continue-on-error"])
        self.assertIn('alert budget_exhausted --pr "$PR" --trace "$HEAD_BRANCH" '
                      '--bundle "$RUNNER_TEMP/judge-events"', alert_step["run"])
        self.assertIn('alert integrity_failure --pr "$PR" --trace "$HEAD_BRANCH" '
                      '--bundle "$RUNNER_TEMP/trigger-events"', alert_step["run"])
        self.assertIn('if [ -z "$PR" ]', alert_step["run"])  # fork PR 无关联编号：跳过

        # ---- 重放（假判定步骤 + 真实告警命令 + gh 桩）----

        shim, log = self.write_gh_shim("wf-harness")
        project = self.fresh_project("wf-harness")
        self.install_engine(project)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True,
                              text=True, check=True).stdout.strip()
        env = self.replay_env(project, shim, log)
        env["GITHUB_SHA"] = head
        context = {"github.event_name": "push", "github.ref": "refs/heads/main",
                   "github.token": "fake-token", "github.repository": "owner/repo"}
        stub = self.write_stub(project)

        # push main + 完整性失败：告警步骤执行，真实命令经 gh 桩发布；观察失败不改变 job 结论
        replay = AlertReplay(project, env, context)
        replay.env["FAKE_FAIL"] = "integrity"
        replay.run(self.template("harness.yml")["jobs"]["alert"]["steps"], stub=stub)
        self.assertIn("Verify engine integrity of the merged copy", replay.swallowed)
        self.assertIn("Alert integrity failure", replay.ran)
        self.assertEqual(replay.conclusion, "success", replay.trace)
        self.assertIn(alerts.marker(f"main@{head[:7]}", "integrity_failure"), log.read_text(encoding="utf-8"))

        # push main + 完整性通过：告警步骤不运行，gh 桩无发布调用（不误报）
        env.pop("FAKE_FAIL", None)  # 第一轮重放的失败注入随环境共享，这里要摘掉
        log.write_text("", encoding="utf-8")
        replay = AlertReplay(project, env, context)
        replay.run(self.template("harness.yml")["jobs"]["alert"]["steps"], stub=stub)
        self.assertNotIn("Alert integrity failure", replay.ran)
        self.assertEqual(log.read_text(encoding="utf-8"), "")

        # workflow_run（同仓库 PR）：judge 预算拒绝 + 触发运行完整性失败 → 两条告警都发布
        shim, log = self.write_gh_shim("wf-auto")
        project = self.fresh_project("wf-auto")
        self.install_engine(project)
        self.run_policy_budget(project, TRACE, escapes=1)
        self.export_events(project, "judge", target=project.parent / "judge-events")
        self.tamper_engine(project)
        self.assertEqual(self.run_in_project(project, [".harness/engine/cli.py", "integrity"]).returncode, 1)
        self.export_events(project, "trigger", target=project.parent / "trigger-events")
        env = self.replay_env(project, shim, log)
        env["RUNNER_TEMP"] = str(project.parent)  # 两份事件包预置在步骤引用的位置（数据，不是代码）
        env["GITHUB_EVENT_NAME"] = "workflow_run"  # 告警 job 的事件条件（T715）：只在 workflow_run 运行
        context = {"github.event.workflow_run.pull_requests[0].number": "9",
                   "github.event.workflow_run.head_branch": TRACE,
                   "github.event.workflow_run.id": "66",
                   "github.event.repository.default_branch": "main",
                   "github.token": "fake-token", "github.repository": "owner/repo"}
        replay = AlertReplay(project, env, context)
        replay.run(self.template("auto-merge.yml")["jobs"]["alert"]["steps"], stub=self.write_stub(project))
        self.assertIn("Alert route budget rejection and integrity failure of the evaluated head", replay.ran)
        text = log.read_text(encoding="utf-8")
        self.assertIn(alerts.marker(TRACE, "budget_exhausted"), text)
        self.assertIn(alerts.marker(TRACE, "integrity_failure"), text)

        # fork PR（无关联 PR 编号）：告警步骤跳过，不发布
        log.write_text("", encoding="utf-8")
        context = {"github.event.workflow_run.pull_requests[0].number": "",
                   "github.event.workflow_run.head_branch": TRACE,
                   "github.event.workflow_run.id": "66",
                   "github.event.repository.default_branch": "main",
                   "github.token": "fake-token", "github.repository": "owner/repo"}
        replay = AlertReplay(project, env, context)
        replay.run(self.template("auto-merge.yml")["jobs"]["alert"]["steps"], stub=self.write_stub(project))
        self.assertEqual(log.read_text(encoding="utf-8"), "")  # 无关联 PR：跳过，不发布

    # ---------- 验收 4：告警 API 出错时原结论不变，告警失败被报告 ----------

    def test_publish_error_preserves_original_result(self):
        # 评审否决 + 告警 gh 挂：评审评论照常发布、退出码仍 0，告警失败留本机事件
        _head, pr_json = self.run_pr_head(TRACE)
        code, gh = self.run_review(pr_json, FakeReviewer(verdict_script("不通过", '[{"severity": "一般"}]')),
                                   fail_alerts=True)
        self.assertEqual(code, 0)
        self.assertEqual([item[2] for item in gh.comments], [None])  # 只有评审评论
        self.assertEqual(gh.labels, [])  # escalation 标签没打上（发布失败被隔离）
        rows = self.trace_events(TRACE, "alert")
        self.assertEqual([(row["step"], row["status"], row["error_kind"]) for row in rows],
                         [("review_rejected", "error", "RuntimeError")])

        # 评审方失败 + 告警 gh 挂：退出码仍 1，告警失败留事件
        code, gh = self.run_review(pr_json, FakeReviewer("import sys; sys.stderr.write('ERROR: down\\n'); sys.exit(3)"),
                                   fail_alerts=True)
        self.assertEqual(code, 1)
        rows = [row for row in self.trace_events(TRACE, "alert") if row["step"] == "review_error"]
        self.assertEqual([(row["status"], row["error_kind"]) for row in rows], [("error", "RuntimeError")])

        # verify 失败：完整性检查结论逐字不变；包证据发布失败被报告（ok=False + 事件）
        project = self.fresh_project("verify-fail")
        self.install_engine(project)
        self.tamper_engine(project)
        first = self.run_in_project(project, [".harness/engine/cli.py", "integrity"])
        self.assertEqual(first.returncode, 1)
        gh = FakeAlertGitHub(fail_alerts=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.alert(["integrity_failure", "--trace", TRACE, "--pr", str(PR),
                                         "--bundle", str(self.export_events(project, "verify-fail")),
                                         "--json"], gh), 0)
        self.assertIn('"error_kind": "RuntimeError"', out.getvalue())
        self.assertEqual(gh.comments, [])
        self.assertEqual([(row["step"], row["status"], row["error_kind"])
                          for row in self.trace_events(TRACE, "alert")
                          if row["step"] == "integrity_failure"],
                         [("integrity_failure", "error", "RuntimeError")])
        second = self.run_in_project(project, [".harness/engine/cli.py", "integrity"])
        self.assertEqual((second.returncode, second.stdout), (first.returncode, first.stdout))

        # route 拒绝：policy 结论（deny 事件与退出码）不受告警失败影响
        project = self.fresh_project("route-deny")
        self.install_engine(project)
        self.run_policy_budget(project, TRACE, escapes=1)
        before = self.project_route_steps(project, TRACE)
        self.assertIn(("result", "deny"), before)
        gh = FakeAlertGitHub(fail_alerts=True)
        self.assertEqual(self.alert(["budget_exhausted", "--trace", TRACE, "--pr", "9",
                                     "--bundle", str(self.export_events(project, "route-deny-2")),
                                     "--json"], gh), 0)
        self.assertEqual(gh.comments, [])
        self.run_policy_budget(project, TRACE, escapes=1)
        after = self.project_route_steps(project, TRACE)
        self.assertEqual(after[len(before):], before)  # 重跑结论逐项相同，仍是 deny

        # alert 命令自身：未知 reason 与坏事件包是参数错误（退出 2），不做任何远端调用
        gh = FakeAlertGitHub()
        self.assertEqual(self.alert(["made_up", "--pr", str(PR)], gh), 2)
        self.assertEqual(gh.calls, [])
        bad = self.tmp / "bad-bundle"
        bad.write_text("不是 JSON", encoding="utf-8")
        self.assertEqual(self.alert(["integrity_failure", "--bundle", str(bad)], gh), 2)
        self.assertEqual(gh.calls, [])

    # ---------- 重放夹具 ----------

    def write_gh_shim(self, name: str) -> tuple[Path, Path]:
        """gh 桩：记录调用与正文（stdin），按 C4 发布接口应答；返回 (目录, 日志)。"""
        folder = self.tmp / f"gh-shim-{name}"
        folder.mkdir()
        log = folder / "calls.log"
        log.write_text("", encoding="utf-8")
        script = folder / "gh"
        script.write_text(
            "#!/bin/bash\n"
            'echo "CALL $*" >> "$GH_SHIM_LOG"\n'
            'case "$*" in\n'
            '  *"--paginate"*) echo "[]" ;;\n'  # 分页评论列表（去重查询）：空
            '  *"issue list"*) echo "[]" ;;\n'
            '  *"pr comment"*|*"issue create"*) cat >> "$GH_SHIM_LOG"; '
            'echo "https://example.invalid/ok" ;;\n'
            '  *"issue edit"*|*"-X PATCH"*) cat >> "$GH_SHIM_LOG" ;;\n'
            "esac\n", encoding="utf-8")
        script.chmod(0o755)
        return folder, log

    def write_stub(self, project: Path) -> Path:
        """重放桩：判定类命令按 FAKE_FAIL 退出；alert 委托安装布局里的真实 CLI（真实发布与证据核对）。"""
        folder = self.tmp / "fake"
        folder.mkdir(exist_ok=True)
        path = folder / f"fake_cli_{project.name}.py"
        path.write_text(
            "import os, sys\n"
            'name = sys.argv[1] if len(sys.argv) > 1 else ""\n'
            'if name == "alert":\n'
            "    os.execv(sys.executable, [sys.executable, "
            f"{str(project / '.harness/engine/cli.py')!r}, "
            '"alert", *sys.argv[2:]])\n'
            'fail = name in set(os.environ.get("FAKE_FAIL", "").split(",")) - {""}\n'
            "sys.exit(1 if fail else 0)\n", encoding="utf-8")
        return path

    def replay_env(self, project: Path, shim: Path, log: Path) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "GITHUB_", "HARNESS_")) and key != "CI"}
        env.update(GIT_ENV)
        env["GITHUB_WORKSPACE"] = str(project)
        env["RUNNER_TEMP"] = str(self.tmp / "runner-temp")
        env["GH_SHIM_LOG"] = str(log)
        env["PATH"] = f"{shim}:{env['PATH']}"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env


if __name__ == "__main__":
    unittest.main()
