"""T502（B124）：head_mismatch 是 load_ci 的信息性发现；畸形运行记录改报非信息性的 api。

病灶（真实 PR #201 预演，2026-10-08）：PR 推送过新提交后，查询结果里仍留有旧 head 的可信运行；
load_ci 按设计不导入它们并汇总一条 head_mismatch（N 是不同 head 的个数），audit 却把它映射成
reference_unavailable error，trace --ci 同样因此退出 1。本文件断言：

1. 旧 head 运行不再是失败：audit 无发现（coverage 与没有旧 head 运行的世界逐字相同）、trace --ci
   退出 0；信息性发现仍打印到 stderr、仍进 --json 的 ci.findings；
2. 其余发现码保持失败：events_io/events_judge 现有 20 个非信息性码加未知码 something_new 做参数化，
   audit 各恰一条 error（artifact_expired→reference_expired、hash_mismatch→hash_mismatch、其余→
   reference_unavailable）；混合发现（信息性在前/在后两种顺序）只保留真实失败；
3. load_ci 区分「合法旧 head」与「畸形记录」：head_sha 缺失或非 40 位十六进制的运行不进
   head_mismatch，汇总成一条 api 发现（N 是运行数）；判定段身份校验照旧各报各的，不去重；
4. 两个调用方共用 events_io.INFORMATIONAL_FINDINGS：扩大常量两处同时放行、清空常量回到现状。

夹具沿用 tests/test_events_judge_runs.py 的真实 emit + ci_events.export 事件包与
tests/test_audit_events.py 的最小可审计世界（tests/gh_fakes.py 的 FakeGhBase 假平台），事件包在项目
仓库的分支 head 上导出（origin 与 API 记录逐项相符）；全部经真实产品入口 cli.main 驱动；
不碰真实库/PR/工作流，不调用真 gh。
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
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli
from engine.core import events, events_db, events_io, events_judge
from engine.reports import audit, ci_events
from tests.gh_fakes import FakeGhBase

REPO = "owner/repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
PR_NUMBER = 201
BRANCH = "task/502-head-mismatch"
HEAD_RUN_ID = 9100
OLD_HEADS = ("e" * 40, "f" * 40)  # PR 曾推送过的两个旧 head（合法形状）
THIRD_HEAD = "a" * 40  # 判定运行名指向的另一个合法旧评估 head
MERGED_AT = "2026-03-04T05:06:07Z"
PR_TITLE = "feat：旧 head 运行不是审计失败"
PR_BODY = "head_mismatch 是 load_ci 的信息性发现。"
TRUSTED = ".github/workflows/harness.yml"

# events_io/events_judge 现有的非信息性发现码（任务书 2026-10-08 扫描；实现前已复核一致）。
KNOWN_NON_INFORMATIONAL = ("anchor", "anchor_mismatch", "api", "artifact_expired", "artifact_hash",
                           "artifact_missing", "artifact_name", "artifact_unsafe", "chain_gap",
                           "chain_head_mismatch", "chain_link", "future_schema", "hash_mismatch",
                           "origin_mismatch", "package_corrupt", "privacy", "schema", "seq_conflict",
                           "source_mismatch", "storage")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
_SERIAL = itertools.count()


def non_informational_codes() -> list[str]:
    """源码里 _finding("...") 的全部发现码（head_mismatch 除外）：日后新增的码自动进入参数化。"""
    codes: set[str] = set()
    for name in ("events_io.py", "events_judge.py"):
        source = (Path(__file__).resolve().parents[1] / "engine" / "core" / name).read_text("utf-8")
        codes.update(re.findall(r'_finding\("([a-z_]+)"', source))
    codes.discard("head_mismatch")
    return sorted(codes)


def branch_run(run_id: int, head: str, *, path: str = TRUSTED, **extra) -> dict:
    """REST 形状的分支运行记录（head 为合法形状；旧 head 样本与当前 head 运行共用本形状）。"""
    record = {"id": run_id, "run_attempt": 1, "event": "pull_request", "head_branch": BRANCH,
              "head_sha": head, "path": path}
    record.update(extra)
    return record


def artifact_entry(run_id: int, attempt: int, job: str) -> dict:
    label = f"harness-events-{run_id}-{attempt}-{job}"
    return {"id": run_id * 10, "name": label, "expired": False,
            "archive_download_url": f"https://dl/{label}"}


class _Clock:
    """递增冻结时钟：从 2026-01-02T00:01Z 起每次调用前进一分钟，事件 ts 严格递增且可解析。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        moment = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=self.step)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WorldGh(FakeGhBase):
    """audit/load_ci 共用假平台：FakeGhBase 的全部只读路由（合并事实、评论、议题、分支运行、
    artifact 与下载），另配 auto-merge 工作流与其（不带筛选参数的）运行列表。"""

    missing_commit = "fixed"

    def __init__(self, *, workflows=None, workflow_runs=None, **kwargs):
        super().__init__(**kwargs)
        self.workflows_list = list(workflows or [])
        self.judge_runs = dict(workflow_runs or {})

    def api(self, route: str, *, method: str = "GET", payload=None):
        path, _, query = route.partition("?")
        if method == "GET" and re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows", path):
            return {"total_count": len(self.workflows_list), "workflows": self.workflows_list}
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows/(\d+)/runs", path):
            items = self.judge_runs.get(int(match[1]), [])
            params = urllib.parse.parse_qs(query)
            page, size = int(params.get("page", ["1"])[0]), int(params.get("per_page", ["100"])[0])
            return {"total_count": len(items), "workflow_runs": items[(page - 1) * size:page * size]}
        return super().api(route, method=method, payload=payload)


class TraceGh:
    """trace --ci 的最小 gh 桩：PR 视图给出分支；load_ci 由测试直接替换，不触网。"""

    def pr(self, pr: int) -> dict:
        return {"headRefName": BRANCH, "headRefOid": "c" * 40, "repository": REPO}


class T502Fixture(unittest.TestCase):
    """隔离夹具基类：临时目录、ROOT 指向无库目录、递增冻结时钟、GIT_/GITHUB_/CI 环境清理。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-audit-head-mismatch-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.no_db = self.tmp / "no-db"
        self.no_db.mkdir()
        root_patch = mock.patch.object(events_db, "ROOT", self.no_db)
        root_patch.start()
        self.addCleanup(root_patch.stop)
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

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        """真实产品入口 cli.main：捕获 stdout/stderr，返回（退出码, stdout, stderr）。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(args))
            except SystemExit as exc:  # argparse 参数错误以 SystemExit(2) 抛出
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def export_package(self, repo: Path, checkout: str, run_id: int, job: str,
                       emits: list[dict]) -> bytes:
        """检出 checkout 后以给定 Actions 身份经真实 emit + ci_events.export 生成事件包 zip。"""
        env = {"CI": "true", "GITHUB_HEAD_REF": BRANCH, "GITHUB_REF_NAME": BRANCH,
               "GITHUB_RUN_ID": str(run_id), "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": job}
        self.git("checkout", "-q", "--detach", checkout, cwd=repo)
        label = f"harness-events-{run_id}-1-{job}"
        target = self.tmp / f"export-{next(_SERIAL)}"
        with mock.patch.object(events_db, "ROOT", repo):
            for key, value in env.items():
                os.environ[key] = value
            try:
                for item in emits:
                    assert events.emit(**item) is not None, item
                assert ci_events.export_main(["--target", str(target)]) == 0
            finally:
                for key in env:
                    os.environ.pop(key, None)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"{label}/harness-events.json",
                             (target / "harness-events.json").read_text(encoding="utf-8"))
        return buffer.getvalue()

    def build_world(self, *, with_head_run: bool = True, with_old_runs: bool = True,
                    runs: list | None = None) -> SimpleNamespace:
        """匿名临时仓库 + --no-ff 合并的最小可审计世界；runs 给定时用它整体替换分支运行列表。

        with_head_run 时当前 head 的 harness 运行带真实事件包（在分支 head 上 emit + export，
        origin 与 API 记录逐项相符）；with_old_runs 追加两个旧 head 的运行（无包：旧 head 运行
        不会被查询 artifact）。
        """
        project = self.tmp / f"app-{next(_SERIAL)}"
        project.mkdir()
        self.git("init", "-q", "-b", "main", cwd=project)
        (project / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "init", cwd=project)
        self.git("remote", "add", "origin", GITHUB_URL, cwd=project)
        self.use_root(project)
        self.git("checkout", "-q", "-b", BRANCH, cwd=project)
        (project / "change.txt").write_text("change\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "change", cwd=project)
        head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m",
                 f"Merge pull request #{PR_NUMBER} from {BRANCH}\n\n{PR_BODY}", BRANCH, cwd=project)
        world_runs = list(runs) if runs is not None else (
            [branch_run(HEAD_RUN_ID, head)] + ([branch_run(9101, OLD_HEADS[0]),
                                                branch_run(9102, OLD_HEADS[1])] if with_old_runs else []))
        artifacts, downloads = {}, {}
        if with_head_run:
            label = f"harness-events-{HEAD_RUN_ID}-1-verify"
            artifacts[HEAD_RUN_ID] = [artifact_entry(HEAD_RUN_ID, 1, "verify")]
            downloads[f"https://dl/{label}"] = self.export_package(
                project, head, HEAD_RUN_ID, "verify",
                [{"stage": "verify", "step": "tests", "status": "ok", "duration_ms": 7}])
        return SimpleNamespace(project=project, head=head, runs=world_runs,
                               merge_sha=self.git("rev-parse", "HEAD", cwd=project),
                               artifacts=artifacts, downloads=downloads)

    def platform(self, world: SimpleNamespace, **kwargs) -> WorldGh:
        """整套假平台：PR 201 合并事实（人批准）+ 空评论/评审/议题 + 世界里的 CI 运行与事件包。"""
        pull = {"number": PR_NUMBER, "state": "closed", "merged": True, "merged_at": MERGED_AT,
                "merge_commit_sha": world.merge_sha, "title": PR_TITLE, "body": PR_BODY,
                "head": {"ref": BRANCH, "sha": world.head},
                "merged_by": {"login": "alice", "type": "User"}}
        return WorldGh(pulls={PR_NUMBER: pull}, closed=[pull], runs=world.runs,
                       artifacts=world.artifacts, downloads=world.downloads, **kwargs)

    # ---- 产品入口 ----

    def run_audit(self, world: SimpleNamespace, canned: list[dict] | None = None):
        """audit CLI；canned 给定时替换 load_ci 的返回（直接给发现码，不必造出每种真实故障）。"""
        result = {"imported": 0, "skipped": 0, "findings": list(canned or [])}
        with contextlib.ExitStack() as stack:
            fake = self.platform(world)
            stack.enter_context(mock.patch.object(audit, "GhClient", lambda: fake))
            stack.enter_context(mock.patch.object(audit, "ROOT", world.project))
            if canned is not None:
                stack.enter_context(mock.patch.object(events_io, "load_ci", lambda *a, **k: result))
            code, out, err = self.run_cli("audit", str(PR_NUMBER), "--json")
        return code, json.loads(out), err

    def run_trace(self, findings: list[dict], *, constant=None):
        """trace --ci CLI：PR 视图给分支、load_ci 返回给定发现；constant 给定时替换信息性集合。"""
        result = {"imported": 0, "skipped": 0, "findings": list(findings)}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(events_io, "GhClient", TraceGh))
            stack.enter_context(mock.patch.object(events_io, "load_ci", lambda *a, **k: result))
            if constant is not None:
                stack.enter_context(
                    mock.patch.object(events_io, "INFORMATIONAL_FINDINGS", constant))
            code, out, err = self.run_cli("trace", str(PR_NUMBER), "--ci", "--json")
        return code, json.loads(out), err


class AuditHeadMismatchTest(T502Fixture):
    """验收 1、2：旧 head 运行不再是 audit 失败；其余发现码（含未知码）的映射原样保持。"""

    def test_older_head_runs_are_not_a_finding(self):
        # 世界 A：当前 head 运行与事件包齐全 + 两个旧 head 的运行（真实 load_ci 路径）
        world = self.build_world(with_old_runs=True)
        loaded = events_io.load_ci(PR_NUMBER, gh=self.platform(world))
        self.assertEqual([finding["code"] for finding in loaded["findings"]], ["head_mismatch"])
        self.assertIn("2 个其他 head", loaded["findings"][0]["detail"])
        code, report, err = self.run_audit(world)
        self.assertEqual((code, report["findings"], report["ok"]), (0, [], True), err)
        self.assertGreater(report["coverage"]["events"]["ci"], 0)
        # 同一世界去掉旧 head 运行：coverage 逐字相同（信息性发现不影响任何汇总字段）
        clean = self.build_world(with_old_runs=False)
        code, clean_report, err = self.run_audit(clean)
        self.assertEqual(code, 0, err)
        self.assertEqual(clean_report["coverage"], report["coverage"])

    def test_other_ci_findings_are_still_errors(self):
        world = self.build_world(with_head_run=False, runs=[])
        codes = non_informational_codes()
        self.assertTrue(set(KNOWN_NON_INFORMATIONAL) <= set(codes),
                        f"源码扫描应覆盖已知 20 个非信息性码：{codes}")
        expected = {"artifact_expired": "reference_expired", "hash_mismatch": "hash_mismatch"}
        for code_name in (*codes, "something_new"):
            with self.subTest(code=code_name):
                finding = {"code": code_name, "detail": f"夹具发现 {code_name}"}
                exit_code, report, err = self.run_audit(world, canned=[finding])
                self.assertEqual(exit_code, 1, (code_name, err))
                self.assertEqual([item["rule"] for item in report["findings"]],
                                 [expected.get(code_name, "reference_unavailable")], code_name)
                self.assertTrue(all(item["severity"] == "error" for item in report["findings"]),
                                code_name)
                self.assertFalse(report["ok"], code_name)

    def test_mixed_findings_keep_only_the_real_ones(self):
        world = self.build_world(with_head_run=False, runs=[])
        head = {"code": "head_mismatch", "detail": "2 个其他 head 的运行未导入"}
        expired = {"code": "artifact_expired", "detail": "artifact 已过保留期"}
        for label, findings in (("信息性在前", [head, expired]), ("信息性在后", [expired, head])):
            with self.subTest(case=label):
                exit_code, report, _err = self.run_audit(world, canned=findings)
                self.assertEqual((exit_code, [item["rule"] for item in report["findings"]]),
                                 (1, ["reference_expired"]), label)


class LoadCiStaleTest(T502Fixture):
    """验收 3：畸形运行记录是 api 发现，不是「旧 head 的历史」；判定段照旧各报各的。"""

    def test_malformed_runs_are_api_findings_not_history(self):
        # 分支查询三种运行混在一起：head_sha 缺失、非 40 位十六进制、合法旧 SHA
        world = self.build_world(with_head_run=False, runs=[
            {"id": 9101, "run_attempt": 1, "event": "pull_request", "head_branch": BRANCH,
             "path": TRUSTED},                                                       # head_sha 缺失
            branch_run(9102, "not-a-sha"),                                           # 形状不符
            branch_run(9103, OLD_HEADS[0]),
            branch_run(9104, OLD_HEADS[1]),
        ])
        fake = self.platform(world)
        result = events_io.load_ci(PR_NUMBER, gh=fake)
        codes = [finding["code"] for finding in result["findings"]]
        self.assertEqual(codes.count("api"), 1, result["findings"])
        self.assertEqual(codes.count("head_mismatch"), 1, result["findings"])
        api = next(finding for finding in result["findings"] if finding["code"] == "api")
        self.assertIn("2 个运行的 head_sha 缺失或形状不符", api["detail"])
        stale = next(finding for finding in result["findings"] if finding["code"] == "head_mismatch")
        self.assertIn("2 个其他 head", stale["detail"])  # 只统计两个合法旧 head
        # 三种运行都不导入：没有 artifact 查询、库里没有 ci 事件
        self.assertEqual(result["imported"], 0)
        self.assertEqual([call for call in fake.calls if "/artifacts" in str(call)], [])
        self.assertEqual(events_io.query(source="ci"), [])
        # audit 对此世界仍有一条 reference_unavailable error（来自 api；head_mismatch 不产生）
        exit_code, report, err = self.run_audit(world)
        self.assertEqual(exit_code, 1, err)
        self.assertEqual([item["rule"] for item in report["findings"]],
                         ["reference_unavailable"], err)
        self.assertIn("CI 事件包不可用（api）", report["findings"][0]["reason"])

        # 同一个畸形运行同时出现在判定查询里：两段各报一条 api，不去重；运行名指向合法旧评估
        # head 的畸形运行只计入 head_mismatch，不进身份校验
        judged = self.platform(
            world,
            workflows=[{"id": 501, "name": "auto-merge", "path": ".github/workflows/auto-merge.yml"}],
            workflow_runs={501: [
                {"id": 9101, "run_attempt": 1, "event": "workflow_run", "head_branch": "main",
                 "head_sha": "not-a-sha", "path": ".github/workflows/auto-merge.yml",
                 "display_title": events_judge.judge_run_name(PR_NUMBER, world.head)},
                {"id": 9105, "run_attempt": 1, "event": "workflow_run", "head_branch": "main",
                 "head_sha": "also-bad", "path": ".github/workflows/auto-merge.yml",
                 "display_title": events_judge.judge_run_name(PR_NUMBER, THIRD_HEAD)},
            ]})
        result = events_io.load_ci(PR_NUMBER, gh=judged)
        codes = [finding["code"] for finding in result["findings"]]
        self.assertEqual((codes.count("api"), codes.count("head_mismatch")), (2, 1), result["findings"])
        self.assertTrue(any("判定运行 9101" in finding["detail"] for finding in result["findings"]
                            if finding["code"] == "api"))
        stale = next(finding for finding in result["findings"] if finding["code"] == "head_mismatch")
        self.assertIn("3 个其他 head", stale["detail"])  # 两个旧 head + 判定名指向的第三个
        self.assertEqual(result["imported"], 0)
        self.assertEqual(events_io.query(source="ci"), [])


class TraceHeadMismatchTest(T502Fixture):
    """验收 4：trace --ci 全部打印、全部进 --json，退出码只由非信息性发现决定。"""

    def test_exit_code_ignores_only_the_informational_finding(self):
        head = {"code": "head_mismatch", "detail": "2 个其他 head 的运行未导入"}
        code, view, err = self.run_trace([head])
        self.assertEqual(code, 0, err)
        self.assertIn("其他 head", err)  # 信息性发现仍打印，让人看见旧 head 的运行
        self.assertEqual(view["ci"]["findings"], [head])  # --json 的 ci.findings 仍含该条
        for code_name in (*non_informational_codes(), "something_new"):
            with self.subTest(code=code_name):
                finding = {"code": code_name, "detail": f"夹具发现 {code_name}"}
                exit_code, _view, err = self.run_trace([finding])
                self.assertEqual(exit_code, 1, (code_name, err))
                self.assertIn(code_name, err)  # 非信息性发现仍打印
        for label, findings in (("信息性在前", [head, {"code": "artifact_expired", "detail": "已过期"}]),
                                ("信息性在后", [{"code": "artifact_expired", "detail": "已过期"}, head])):
            with self.subTest(case=label):
                exit_code, view, err = self.run_trace(findings)
                self.assertEqual(exit_code, 1, label)
                self.assertEqual(len(view["ci"]["findings"]), 2, label)  # 输出不被吞掉
                self.assertIn("其他 head", err, label)


class SharedConstantTest(T502Fixture):
    """验收 5：两个调用方共用 events_io.INFORMATIONAL_FINDINGS，改常量两处同时变化。"""

    def test_both_callers_share_the_events_io_constant(self):
        self.assertEqual(events_io.INFORMATIONAL_FINDINGS, frozenset({"head_mismatch"}))
        world = self.build_world(with_head_run=False, runs=[])
        widened = frozenset({"head_mismatch", "artifact_missing"})
        finding = {"code": "artifact_missing", "detail": "夹具：运行没有事件包"}
        # 扩大常量：audit 不再产生 finding、trace 退出 0——两处行为都跟着常量变
        with mock.patch.object(events_io, "INFORMATIONAL_FINDINGS", widened):
            exit_code, report, err = self.run_audit(world, canned=[finding])
            self.assertEqual((exit_code, report["findings"]), (0, []), err)
            code, _view, err = self.run_trace([finding])
            self.assertEqual(code, 0, err)
        # 清空常量：回到现状（audit 有 error、trace 退出 1）
        head = {"code": "head_mismatch", "detail": "1 个其他 head 的运行未导入"}
        with mock.patch.object(events_io, "INFORMATIONAL_FINDINGS", frozenset()):
            exit_code, report, err = self.run_audit(world, canned=[head])
            self.assertEqual((exit_code, [item["rule"] for item in report["findings"]]),
                             (1, ["reference_unavailable"]), err)
            code, _view, err = self.run_trace([head])
            self.assertEqual(code, 1, err)


if __name__ == "__main__":
    unittest.main()
