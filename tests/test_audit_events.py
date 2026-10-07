"""T404 audit 观察事件测试：每次运行恰一条 ci/audit.summary、每条 finding 一条 ci/audit.finding
（stage 固定 ci、不新增阶段枚举；批量与无法审计的失败报告同样各一条）；事件 outputs 只含安全字段
（pr/head_sha/规则与 severity 计数/coverage 摊平/ok，finding 事件含 rule/severity/source/stage/ref），
reason 原文与本机路径不入事件；观察写入失败（T106 同款真实失败点：库目录换成普通文件）时退出码与
stdout 和禁用观察逐字一致、stderr 只差 T101 固定的一次提示（C0 三态等价）——三态在同一世界内依次
对比，报告内嵌 head_sha 等世界特定的提交哈希，跨世界本就无从逐字一致。

夹具沿用 tests/test_audit_reconstruction.py 的模式：隔离 events_db.ROOT、递增冻结时钟、假 gh 桩，
最小可审计世界（合并事实 + 空评论/CI/议题；评论列表故障产生一条 reference_unavailable 发现），
全部经 cli.main 真实产品入口驱动；不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli
from engine.core import events, events_db, events_io
from engine.reports import audit

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
REPO = "owner/repo"
BRANCH = "task/404-fixture"
PR_TITLE = "feat：audit 观察事件"
PR_BODY = "audit 观察事件夹具正文。"
MERGED_AT = "2026-03-04T05:06:07Z"
WARN = "harness：事件写入失败，已跳过（不影响本次运行）"


class _Clock:
    """递增冻结时钟：从 2026-01-02T00:01Z 起每次调用前进一分钟，ts 严格递增且可解析。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        moment = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=self.step)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeGh:
    """最小 gh 桩：合并事实、空评审/提交/议题/CI 运行列表；fail_comments 时 PR 评论列表故障。"""

    def __init__(self, *, pulls=None, closed=None, fail_comments=False):
        self.calls: list[tuple] = []
        self.pulls = pulls or {}
        self.closed = list(closed or [])
        self.fail_comments = fail_comments

    def repo(self) -> str:
        self.calls.append(("repo",))
        return REPO

    def pr(self, pr: int) -> dict:
        self.calls.append(("pr", pr))
        pull = self.pulls.get(pr)
        if pull is None:
            raise RuntimeError(f"HTTP 404: Not Found（夹具无 PR {pr}）")
        return {"headRefName": pull["head"]["ref"], "headRefOid": pull["head"]["sha"],
                "repository": REPO}

    def api(self, route: str, *, method: str = "GET", payload=None):
        self.calls.append(("GET", route))
        path, _, query = route.partition("?")
        if re.fullmatch(r"repos/[^/]+/[^/]+/pulls", path):
            return self._page(self.closed, query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path):
            pull = self.pulls.get(int(match[1]))
            if pull is None:
                raise RuntimeError(f"HTTP 404: Not Found（夹具无 PR {match[1]}）")
            return pull
        if re.fullmatch(r"repos/[^/]+/[^/]+/pulls/\d+/(reviews|commits)", path):
            return []
        if re.fullmatch(r"repos/[^/]+/[^/]+/commits/[0-9a-f]+", path):
            return {"parents": [{"sha": "p1"}, {"sha": "p2"}]}
        if re.fullmatch(r"repos/[^/]+/[^/]+/issues", path):
            return []
        if re.fullmatch(r"repos/[^/]+/[^/]+/issues/\d+/comments", path):
            if self.fail_comments:
                raise RuntimeError("HTTP 403: 权限不足（夹具）")
            return []
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs", path):
            return {"total_count": 0, "workflow_runs": []}
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows", path):
            return {"total_count": 0, "workflows": []}  # T720：load_ci 的第二段查询（本文件无 auto-merge 工作流）
        raise AssertionError(f"FakeGh 未配置的路由：{path}")

    def _page(self, items: list, query: str) -> list:
        page = int(urllib.parse.parse_qs(query).get("page", ["1"])[0])
        cap = int(urllib.parse.parse_qs(query).get("per_page", ["30"])[0])
        return items[(page - 1) * cap:page * cap]


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-audit-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        clock = mock.patch.object(events_db, "_now", new=_Clock())
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [name for name in os.environ if name.startswith(CLEAN_PREFIXES)]:
            os.environ.pop(key, None)
        for key, value in GIT_ENV.items():
            os.environ[key] = value
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)
        self._seq = itertools.count()

    # ---- 基础夹具 ----

    def git(self, *args: str, cwd: Path, check: bool = True) -> str:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
        if check and done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout.strip()

    def use_root(self, project: Path) -> None:
        patch = mock.patch.object(events_db, "ROOT", project)
        patch.start()
        self.addCleanup(patch.stop)

    def build_world(self) -> SimpleNamespace:
        """匿名临时仓库：main + 任务分支 --no-ff 合并（无评论/CI 包/运行记录的最小可审计世界）。"""
        project = self.tmp / f"app-{next(self._seq)}"
        project.mkdir()
        self.git("init", "-q", "-b", "main", cwd=project)
        (project / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "init", cwd=project)
        self.use_root(project)
        self.git("checkout", "-q", "-b", BRANCH, cwd=project)
        (project / "change.txt").write_text("change\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "change", cwd=project)
        head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m",
                 f"Merge pull request #401 from {BRANCH}\n\n{PR_BODY}", BRANCH, cwd=project)
        return SimpleNamespace(project=project, head=head,
                               merge_sha=self.git("rev-parse", "HEAD", cwd=project))

    @staticmethod
    def merged_pull(world: SimpleNamespace, number: int = 401, *, missing_merge=False) -> dict:
        """合并事实；missing_merge 模拟合并事实缺失（inspect_pr 抛 AuditError → 批量失败报告）。"""
        return {"number": number, "state": "closed", "merged": True, "merged_at": MERGED_AT,
                "merge_commit_sha": None if missing_merge else world.merge_sha,
                "title": PR_TITLE, "body": PR_BODY, "head": {"ref": BRANCH, "sha": world.head},
                "merged_by": {"login": "alice", "type": "User"}}

    def platform(self, world: SimpleNamespace, *, fail_comments=False, extra_pull=None) -> FakeGh:
        pulls = {401: self.merged_pull(world)}
        closed = [pulls[401]]
        if extra_pull is not None:
            pulls[extra_pull["number"]] = extra_pull
            closed.append(extra_pull)
        return FakeGh(pulls=pulls, closed=closed, fail_comments=fail_comments)

    @contextmanager
    def cli_env(self, gh=None, root=None):
        """audit CLI 的测试环境：注入假 gh 与夹具根目录（ROOT 在调用时解析）。"""
        with contextlib.ExitStack() as stack:
            if gh is not None:
                stack.enter_context(mock.patch.object(audit, "GhClient", lambda: gh))
            if root is not None:
                stack.enter_context(mock.patch.object(audit, "ROOT", root))
            yield

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        """真实产品入口 cli.main：捕获 stdout/stderr，返回（退出码, stdout, stderr）。"""
        events._warned = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(args))
            except SystemExit as exc:  # argparse 参数错误以 SystemExit(2) 抛出
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def audit_events(self, step: str) -> list[dict]:
        return [row for row in events_io.query() if row["step"] == step]

    def sink_break(self, project: Path) -> None:
        """T106 同款真实失败写入点：库目录换成普通文件，SQLite 建目录即抛 OSError。"""
        harness = project / ".git" / "harness"
        if harness.is_dir():
            shutil.rmtree(harness)
        harness.write_text("harness sink broken", encoding="utf-8")

    # ---- 验收 1：每次运行恰一条 audit.summary、每条 finding 一条 audit.finding，stage=ci ----

    def test_summary_and_finding_events_per_run(self):
        # 干净世界（无评论、无 CI 运行、无运行记录）：恰一条 audit.summary（ok），无 finding 事件
        world = self.build_world()
        with self.cli_env(gh=self.platform(world), root=world.project):
            code, out, err = self.run_cli("audit", "401", "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["ok"])
        summaries = self.audit_events("audit.summary")
        self.assertEqual(len(summaries), 1)
        self.assertEqual((summaries[0]["stage"], summaries[0]["status"]), ("ci", "ok"))
        self.assertEqual(self.audit_events("audit.finding"), [])

        # 有发现的世界（评论列表故障 → 一条 reference_unavailable）：恰一条 summary（fail）+ 逐 finding
        world2 = self.build_world()
        with self.cli_env(gh=self.platform(world2, fail_comments=True), root=world2.project):
            code2, out2, err2 = self.run_cli("audit", "401", "--json")
        self.assertEqual(code2, 1, err2)
        report = json.loads(out2)
        self.assertEqual(len(report["findings"]), 1)
        summaries2 = self.audit_events("audit.summary")
        findings2 = self.audit_events("audit.finding")
        self.assertEqual(len(summaries2), 1)
        self.assertEqual((summaries2[0]["stage"], summaries2[0]["status"]), ("ci", "fail"))
        self.assertEqual(len(findings2), len(report["findings"]))
        self.assertEqual((findings2[0]["stage"], findings2[0]["status"]), ("ci", "fail"))

        # 同一世界再跑一次：再各写一条——每次运行恰一条，不因同 PR/head 重复审计而略写
        with self.cli_env(gh=self.platform(world2, fail_comments=True), root=world2.project):
            code3, _out3, err3 = self.run_cli("audit", "401")
        self.assertEqual(code3, 1, err3)
        self.assertEqual(len(self.audit_events("audit.summary")), 2)
        self.assertEqual(len(self.audit_events("audit.finding")), 2)

        # 批量模式：每个被审计 PR 各一条 summary；无法审计的 PR 走失败报告同样各一条（含其 finding）
        world3 = self.build_world()
        bad = self.merged_pull(world3, number=402, missing_merge=True)
        with self.cli_env(gh=self.platform(world3, fail_comments=True, extra_pull=bad),
                          root=world3.project):
            code4, out4, err4 = self.run_cli("audit", "--all-merged", "--json")
        self.assertEqual(code4, 1, err4)
        self.assertEqual([item["pr"] for item in json.loads(out4)["prs"]], [401, 402])
        summaries3 = self.audit_events("audit.summary")
        self.assertEqual(len(summaries3), 2)
        self.assertEqual(sorted(row["outputs"]["pr"] for row in summaries3), [401, 402])
        self.assertIs(next(row for row in summaries3 if row["outputs"]["pr"] == 402)
                      ["outputs"]["head_sha"], None)
        self.assertEqual(len(self.audit_events("audit.finding")), 2)  # 401 一条 + 402 失败报告一条

        # 阶段枚举未扩大：stage 固定 ci，STAGES 仍是 C0 的八阶段、不含 audit
        self.assertNotIn("audit", events.STAGES)
        self.assertEqual(events.STAGES,
                         ("guard", "verify", "dispatch", "ci", "route", "review", "merge", "alert"))

    # ---- 验收 2：事件 outputs 仅含安全字段，reason 原文与私密内容不入事件 ----

    def test_event_payload_safe_fields_only(self):
        world = self.build_world()
        with self.cli_env(gh=self.platform(world, fail_comments=True), root=world.project):
            code, out, err = self.run_cli("audit", "401", "--json")
        self.assertEqual(code, 1, err)
        report = json.loads(out)
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        summary = self.audit_events("audit.summary")[0]
        finding_event = self.audit_events("audit.finding")[0]

        # summary outputs 恰好是安全字段全集：pr/head_sha/ok + severity 计数 + 规则计数 + coverage 摊平
        self.assertEqual(set(summary["outputs"]),
                         {"pr", "head_sha", "ok", "severity.error", "rule.reference_unavailable",
                          "coverage.events.local", "coverage.events.ci", "coverage.events.github",
                          "coverage.chains", "coverage.anchors", "coverage.ledger",
                          "coverage.references.total", "coverage.references.verified",
                          "coverage.references.unchecked", "coverage.references.hash_mismatch",
                          "coverage.references.snapshot_changed", "coverage.references.unavailable",
                          "coverage.references.expired"})
        outputs = summary["outputs"]
        self.assertEqual(outputs["pr"], 401)
        self.assertEqual(outputs["head_sha"], world.head)
        self.assertIs(outputs["ok"], False)
        self.assertEqual(outputs["rule.reference_unavailable"], 1)
        self.assertEqual(outputs["severity.error"], 1)
        self.assertEqual(outputs["coverage.ledger"], "absent")
        self.assertEqual(outputs["coverage.references.total"], 0)

        # finding outputs 恰好是周报去重所需安全字段：rule/severity/source/stage/ref + pr/head_sha
        self.assertEqual(set(finding_event["outputs"]),
                         {"pr", "head_sha", "rule", "severity", "source", "stage", "ref"})
        finding_outputs = finding_event["outputs"]
        self.assertEqual(finding_outputs["pr"], 401)
        self.assertEqual(finding_outputs["head_sha"], world.head)
        for key in ("rule", "severity", "source", "stage", "ref"):
            self.assertEqual(finding_outputs[key], finding[key])

        # reason 原文、报告正文与本机路径都不入事件；decision/error 列为空；输出值无路径形状
        for row in (summary, finding_event):
            blob = json.dumps({"outputs": row["outputs"], "decision": row["decision"],
                               "error": row["error"], "step": row["step"]}, ensure_ascii=False)
            self.assertNotIn(finding["reason"], blob)
            self.assertNotIn(str(world.project), blob)
            self.assertNotIn(PR_BODY, blob)
            self.assertEqual(row["decision"], {})
            self.assertEqual(row["error"], {})
            self.assertTrue(all(not str(value).startswith("/") and "\n" not in str(value)
                                for value in row["outputs"].values()),
                            row["outputs"])

    # ---- 验收 3：观察写入失败时退出码与输出和禁用观察逐字一致（C0） ----

    def test_observation_failure_isolated(self):
        # 三态共用同一世界：报告内嵌 head_sha/merge_sha 等世界特定的提交哈希，跨世界无从逐字一致
        world = self.build_world()
        # 禁用观察：HARNESS_EVENTS=off，审计照常（退出 1、报告照出），零事件、零写入尝试
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}), \
                self.cli_env(gh=self.platform(world, fail_comments=True), root=world.project):
            off = self.run_cli("audit", "401", "--json")
        self.assertEqual(off[0], 1, off[2])
        self.assertTrue(json.loads(off[1])["findings"])
        self.assertEqual(self.audit_events("audit.summary") + self.audit_events("audit.finding"), [])
        self.assertNotIn(WARN, off[2])

        # 写入失败：同一世界把库目录换成普通文件，固定提示恰一次、失败不留半条事件（目录未重建）
        self.sink_break(world.project)
        with self.cli_env(gh=self.platform(world, fail_comments=True), root=world.project):
            broken = self.run_cli("audit", "401", "--json")
        self.assertEqual(broken[2].count(WARN), 1, broken[2])
        self.assertTrue((world.project / ".git" / "harness").is_file())
        # 退出码与 stdout 和禁用观察逐字一致；stderr 只差 T101 固定的一次提示
        self.assertEqual(broken[0], off[0])
        self.assertEqual(broken[1], off[1])
        self.assertEqual([line for line in broken[2].splitlines() if line != WARN],
                         off[2].splitlines())

        # 开启态对照：恢复库目录后事件照写（1 条 summary + 1 条 finding），退出码与发现与禁用观察
        # 一致（报告多出的阶段来自 sync 写入的 GitHub 事实事件，不是观察旁路改了判定）
        (world.project / ".git" / "harness").unlink()
        with self.cli_env(gh=self.platform(world, fail_comments=True), root=world.project):
            on = self.run_cli("audit", "401", "--json")
        self.assertEqual(on[0], off[0])
        self.assertEqual(json.loads(on[1])["findings"], json.loads(off[1])["findings"])
        self.assertNotIn(WARN, on[2])
        self.assertEqual(len(self.audit_events("audit.summary")), 1)
        self.assertEqual(len(self.audit_events("audit.finding")), 1)


if __name__ == "__main__":
    unittest.main()
