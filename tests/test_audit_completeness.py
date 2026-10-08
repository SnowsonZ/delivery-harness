"""T402 审计完整性与锚点防篡改测试：在 T401 复原核对之上，audit 按风险与 PR 种类检查应有环节
（R2+ 独立评审且身份不同、自动合并有受信任 CI 的 route.result、任务 PR 有运行记录与阶段锚点、
app 模式 R0/R1 自动合并批准者 App 且绑定合并 head、none 模式不凭空要求批准、设计方 PR 豁免派发
记录），并校验各 (source,trace) 原始链、固定锚点与对应前缀链头（合法追加不误报、删尾/改中可发现）、
账本与运行层一致；[audit] 配置缺省执行设计规则、合法可选生效、非法报告 configuration_error/退出 2，
坏库/关库如实报告，原路由返回不受审计配置影响。

夹具沿用 tests/test_audit_reconstruction.py 的模式：隔离 events_db.ROOT 与 common.CONFIG_DIR、递增
冻结时钟、假 gh 桩与匿名临时 git 仓库；运行记录由 T201 真实生产者组装、CI 包由真实 emit +
ci_events.export 生成、评审审计标记由 T105 真实生产者构造、账本与锚点评论由 T305 真实 writer 发布
（篡改账本用同一 plumbing 推入）。不碰真实库/PR/工作流，不调用真 gh，验收全部走 inspect_pr 与
cli.main 产品入口。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli
from engine.agents import dispatch_observation, run_timeline
from engine.checks import r1_checks
from engine.core import common, events, events_db
from engine.core.common import _load_toml
from engine.reports import audit, ci_events, ledger
from engine.routing import policy
from tests.gh_fakes import FakeGhBase

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
REPO = "owner/repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
BRANCH = "task/402-fixture"
TASKBOOK = "docs/plans/task-402-audit-completeness.md"
RECORD_PATH = "docs/runs/task-402-audit-completeness/1.json"
PR_TITLE = "feat：审计完整性与锚点防篡改"
PR_BODY = "审计完整性正文（夹具）。"
MERGED_AT = "2026-03-04T05:06:07Z"
TS = "2026-01-02T00:01:00.000Z"
BOT = ("github-actions[bot]", "Bot")
APP = ("harness-merge-app", "Bot")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def zip_bytes(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class _Clock:
    """递增冻结时钟：从 2026-01-02T00:01Z 起每次调用前进一分钟，ts 严格递增且可解析。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        moment = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=self.step)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeGh(FakeGhBase):
    """audit/ledger/load_ci/sync 共用 gh 桩：只读 API、artifact zip 下载与锚点评论写操作。

    行为即基类默认：closed 批量已关闭 PR 列表、runs 按 branch 过滤且 runs 与 artifacts 不分页、
    评论 POST 记入 writes 并追加到 comments。
    """


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-audit-completeness-"))
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

    # ---- 基础夹具（沿用 test_audit_reconstruction 的模式） ----

    def git(self, *args: str, cwd: Path, check: bool = True) -> str:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
        if check and done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout.strip()

    def fresh_repo(self, name: str) -> Path:
        path = self.tmp / f"{name}-{next(self._seq)}"
        path.mkdir()
        self.git("init", "-q", "-b", "main", cwd=path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        (path / TASKBOOK).parent.mkdir(parents=True, exist_ok=True)
        (path / TASKBOOK).write_text("task：T402 审计完整性与锚点防篡改（夹具任务书）\n", encoding="utf-8")
        (path / ".harness" / "config").mkdir(parents=True, exist_ok=True)
        self.git("add", "-A", cwd=path)
        self.git("commit", "-q", "-m", "init", cwd=path)
        return path

    def write_config(self, project: Path, text: str) -> None:
        """写夹具仓库的 checks.toml 并清配置缓存：[audit]/[platform] 配置随夹具走，不碰真实仓库。"""
        (project / ".harness" / "config" / "checks.toml").write_text(text, encoding="utf-8")
        _load_toml.cache_clear()

    def bare_origin(self, project: Path) -> Path:
        origin = self.tmp / f"{project.name}-origin.git"
        self.git("init", "--bare", "-q", "--initial-branch=main", str(origin), cwd=project)
        self.git("remote", "add", "origin", str(origin), cwd=project)
        return origin

    def use_root(self, project: Path) -> None:
        patch = mock.patch.object(events_db, "ROOT", project)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(common, "CONFIG_DIR", project / ".harness" / "config")
        patch.start()
        self.addCleanup(patch.stop)

    def build_project(self, *, with_record=True, fix_anchor=True) -> SimpleNamespace:
        """项目仓库：main（含任务书）+ 任务分支（本机事件与运行记录）+ --no-ff 合并 + bare origin。

        with_record=False 时不写运行记录（删记录夹具）；fix_anchor=False 时记录不含 anchors、也不写
        锚点表（删锚点夹具）。
        """
        project = self.fresh_repo("app")
        origin = self.bare_origin(project)
        self.use_root(project)
        base = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "-b", BRANCH, cwd=project)
        task_raw = (project / TASKBOOK).read_bytes()
        self.assertIsNotNone(events.emit(
            "dispatch", "admit", "ok", trace_id=BRANCH, duration_ms=5,
            inputs=[events.ref("taskbook", f"{TASKBOOK}@{base}", sha256=digest(task_raw),
                               size=len(task_raw))],
            decision={"by": "taskbook", "rule": "admit", "reason": "ok"}))
        stored = events.store_artifact(b"verify log 402\n")
        self.assertIsNotNone(events.emit(
            "verify", "verify.tests", "ok", trace_id=BRANCH, duration_ms=10,
            outputs={"log.sha256": stored["sha256"], "log.size": stored["size"], "log.ref": stored["ref"]}))
        branch_head = self.git("rev-parse", "HEAD", cwd=project)
        if with_record:
            timeline, head_hash = run_timeline.record_fields(BRANCH)
            self.assertTrue(head_hash)
            anchors = timeline["anchors"] if fix_anchor else []
            if fix_anchor:
                run_timeline.fix_anchor(BRANCH, head_hash)
            record = {"task": "task-402-audit-completeness", "class": "K7", "attempt": 1, "branch": BRANCH,
                      "started_at": TS, "ended_at": TS, "exit": "ok", "trace_id": BRANCH,
                      "stages": timeline["stages"], "anchors": anchors}
            path = project / RECORD_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.git("add", "-A", cwd=project)
            self.git("commit", "-q", "-m", "run：task-402 派发记录", cwd=project)
            branch_head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m", f"Merge pull request #402 from {BRANCH}\n\nbody",
                 BRANCH, cwd=project)
        merge_sha = self.git("rev-parse", "HEAD", cwd=project)
        self.git("push", "-q", "origin", "main", cwd=project)
        return SimpleNamespace(project=project, origin=origin, base=base, branch_head=branch_head,
                               merge_sha=merge_sha, stored=stored)

    def build_ci_packages(self, project: Path, branch_head: str, *, risk="R2", declared="K7",
                          auto_route=True, auto_merge=False) -> dict[str, bytes]:
        """在项目分支 head 上以 CI 身份生成两个 run 的事件包（真实 emit + ci_events.export）。"""
        real_url = self.git("remote", "get-url", "origin", cwd=project)
        self.git("remote", "set-url", "origin", GITHUB_URL, cwd=project)
        self.git("checkout", "-q", "--detach", branch_head, cwd=project)
        try:
            packages: dict[str, bytes] = {}
            result_steps = (("route", "result", "ok"),) if auto_route else ()
            for run_id, job, steps in (
                    ("9001", "job_x", (("verify", "tests", "ok"),)),
                    ("9002", "job_y", (("route", "facts", "ok"), *result_steps))):
                for key, value in (("CI", "true"), ("GITHUB_HEAD_REF", BRANCH), ("GITHUB_RUN_ID", run_id),
                                   ("GITHUB_RUN_ATTEMPT", "1"), ("GITHUB_JOB", job)):
                    os.environ[key] = value
                for stage, step, status in steps:
                    if step == "facts":
                        outputs = {"risk": risk, "machine_class": "K7", "declared_class": declared,
                                   "changed_lines": 3, "escapes": 0, "window": 20}
                    elif step == "result":
                        outputs = {"auto_merge": auto_merge, "audit": False, "approval": "app"}
                    else:
                        outputs = None
                    self.assertIsNotNone(events.emit(stage, step, status, trace_id=BRANCH,
                                                     duration_ms=7, outputs=outputs))
                target = self.tmp / f"exp-{run_id}-{job}-{next(self._seq)}"
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(ci_events.export_main(["--target", str(target)]), 0, err.getvalue())
                name = f"harness-events-{run_id}-1-{job}"
                content = (target / "harness-events.json").read_text(encoding="utf-8")
                packages[name] = zip_bytes({f"{name}/harness-events.json": content})
        finally:
            for key in [name for name in os.environ if name.startswith(("GITHUB_", "CI"))]:
                os.environ.pop(key, None)
            self.git("checkout", "-q", "main", cwd=project)
            self.git("remote", "set-url", "origin", real_url, cwd=project)
        return packages

    def ci_platform(self, head: str, packages: dict[str, bytes]):
        runs = [{"id": 9001, "run_attempt": 1, "head_sha": head, "head_branch": BRANCH,
                 "path": ".github/workflows/harness.yml"},
                {"id": 9002, "run_attempt": 1, "head_sha": head, "head_branch": BRANCH,
                 "path": ".github/workflows/harness.yml"}]
        artifacts = {9001: [{"id": 1, "name": name, "expired": False,
                             "archive_download_url": f"https://dl/{name}"}
                            for name in packages if name.startswith("harness-events-9001")],
                     9002: [{"id": 2, "name": name, "expired": False,
                             "archive_download_url": f"https://dl/{name}"}
                            for name in packages if name.startswith("harness-events-9002")]}
        downloads = {f"https://dl/{name}": data for name, data in packages.items()}
        return runs, artifacts, downloads

    def review_files(self, fx: SimpleNamespace) -> Path:
        folder = fx.project / "build" / "review"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "task.md").write_bytes((fx.project / TASKBOOK).read_bytes())
        (folder / "diff.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")
        (folder / "ci.md").write_text("CI 摘要", encoding="utf-8")
        (folder / "pr.md").write_text(f"# {PR_TITLE}\n\n{PR_BODY}", encoding="utf-8")
        (folder / "pack.md").write_text("评审包", encoding="utf-8")
        return folder

    def review_comment(self, fx: SimpleNamespace, *, reviewer="opencode", designer="codex",
                       implementers=("pi",), independent=True) -> str:
        """T105 真实生产者构造的评审评论体（independent-review 标记旁列 harness-review-audit 摘要）。"""
        self.review_files(fx)
        mats = dispatch_observation.review_materials(fx.project, fx.base, fx.branch_head, TASKBOOK, 402)
        summary = dispatch_observation.review_audit(
            trace_id=BRANCH, head=fx.branch_head, base=fx.base, reviewer=reviewer, model="review-model/1",
            model_basis="reported", designer=designer, implementers=list(implementers),
            independent=independent, same_host=False, parsed=True, verdict="通过", duration_ms=60000,
            findings=[], materials=mats)
        legacy = json.dumps({"verdict": "通过", "reviewer": reviewer, "head": fx.branch_head},
                            ensure_ascii=False)
        return ("### 独立评审（试行）：通过\n\n"
                f"<!-- independent-review {legacy} -->\n"
                f"<!-- harness-review-audit {json.dumps(summary, ensure_ascii=False)} -->\n")

    @staticmethod
    def as_comment(comment_id: int, body: str) -> dict:
        return {"id": comment_id, "body": body, "created_at": TS,
                "html_url": f"{GITHUB_URL}/issues/comments/{comment_id}"}

    def platform(self, fx: SimpleNamespace, *, comments=(), reviews=None, merger=("alice", "User"),
                 risk="R2", declared="K7", auto_route=True, auto_merge=False) -> FakeGh:
        """PR 402 的整套假平台：合并事实（合并者可换 Bot/User）、批准评审、CI 事件包。"""
        packages = self.build_ci_packages(fx.project, fx.branch_head, risk=risk, declared=declared,
                                          auto_route=auto_route, auto_merge=auto_merge)
        runs, artifacts, downloads = self.ci_platform(fx.branch_head, packages)
        pull = {"number": 402, "state": "closed", "merged": True, "merged_at": MERGED_AT,
                "merge_commit_sha": fx.merge_sha, "title": PR_TITLE, "body": PR_BODY,
                "head": {"ref": BRANCH, "sha": fx.branch_head},
                "merged_by": {"login": merger[0], "type": merger[1]}}
        if reviews is None:
            reviews = [{"state": "APPROVED", "user": {"login": merger[0], "type": merger[1]},
                        "commit_id": fx.branch_head}] if merger == BOT else []
        return FakeGh(pulls={402: pull}, closed=[pull], reviews={402: reviews},
                      pr_commits={402: [{"sha": "x1"}]},
                      commits={fx.merge_sha: {"parents": [{"sha": "p1"}, {"sha": "p2"}]}},
                      comments=list(comments), runs=runs, artifacts=artifacts, downloads=downloads)

    def world(self, *, comments=None, reviews=None, merger=("alice", "User"), risk="R2",
              declared="K7", auto_route=True, auto_merge=False, with_record=True, fix_anchor=True,
              config="") -> tuple[SimpleNamespace, FakeGh]:
        """一个完整世界：项目 + 运行记录（可删）+ 评审评论（可换/可删）+ 平台桩 + 夹具配置。"""
        fx = self.build_project(with_record=with_record, fix_anchor=fix_anchor)
        if config:
            self.write_config(fx.project, config)
        extra = list(comments or ())
        bodies = [] if comments is not None or reviews is not None else [self.review_comment(fx)]
        wrapped = [item if isinstance(item, dict) else self.as_comment(500 + index, item)
                   for index, item in enumerate([*bodies, *extra])]
        gh = self.platform(fx, comments=wrapped, reviews=reviews, merger=merger,
                           risk=risk, declared=declared, auto_route=auto_route, auto_merge=auto_merge)
        return fx, gh

    def push_ledger(self, origin: Path, data: bytes, pr: int, path: str) -> dict:
        """把账本字节直接提交到 bare origin 的 harness-audit 分支（plumbing），返回锚点评论。"""
        work = self.tmp / f"ledger-push-{pr}-{next(self._seq)}"
        work.mkdir()
        self.git("init", "-q", "-b", "main", cwd=work)
        self.git("remote", "add", "origin", str(origin), cwd=work)
        parents = []
        if self.git("ls-remote", str(origin), "refs/heads/harness-audit", cwd=work, check=False):
            self.git("fetch", "-q", "origin", "harness-audit", cwd=work)
            self.git("read-tree", "FETCH_HEAD^{tree}", cwd=work)
            parents = ["-p", "FETCH_HEAD"]
        else:
            self.git("read-tree", "--empty", cwd=work)
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=work, input=data,
                              capture_output=True, check=True).stdout.decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", cwd=work)
        tree = self.git("write-tree", cwd=work)
        commit = self.git("commit-tree", tree, "-m", f"audit：PR #{pr} 夹具账本（{path}）", *parents, cwd=work)
        self.git("push", "-q", "origin", f"{commit}:refs/heads/harness-audit", cwd=work)
        body = "\n".join([
            f"<!-- {ledger.ANCHOR_PREFIX}{pr} -->",
            "合并账本已固定（合并账本与链头锚点）：",
            f"- 分支：`{ledger.AUDIT_BRANCH}`",
            f"- 文件：`{path}` @ {commit}",
            f"- 文件 sha256：`{digest(data)}`",
            "- 链头：（无事件链）",
        ]) + "\n"
        return {"id": 7000 + pr, "body": body, "created_at": TS,
                "html_url": f"{GITHUB_URL}/issues/comments/{7000 + pr}"}

    def db_exec(self, fx: SimpleNamespace, sql: str, params=()) -> None:
        path = events_db.db_path()
        assert path is not None
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute(sql, params)
            conn.commit()

    def rules_of(self, report: dict) -> set[str]:
        return {item["rule"] for item in report["findings"]}

    def finding(self, report: dict, rule: str) -> dict:
        return next(item for item in report["findings"] if item["rule"] == rule)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def cli_env(self, gh=None, root=None):
        with contextlib.ExitStack() as stack:
            if gh is not None:
                stack.enter_context(mock.patch.object(audit, "GhClient", lambda: gh))
            if root is not None:
                stack.enter_context(mock.patch.object(audit, "ROOT", root))
            yield

    # ---- 验收 1：删 R2 评审 / auto 路由 / task 记录及锚点各报对应 finding；设计方 PR 豁免 ----

    def test_required_stages_by_risk_and_pr_kind(self):
        # 基线：任务 PR（R2、声明 K7、合格独立评审、运行记录+锚点）人工合并 → 无完整性发现
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False, auto_merge=False)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertTrue(report["ok"])

        # 删 R2 评审：没有评审摘要 → missing_review（error/github/review）
        fx, gh = self.world(comments=[], risk="R2", merger=("alice", "User"))
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("missing_review", self.rules_of(report))
        finding = self.finding(report, "missing_review")
        self.assertEqual((finding["severity"], finding["source"], finding["stage"]),
                         ("error", "github", "review"))
        self.assertFalse(report["ok"])

        # 删 auto 路由：Bot 自动合并但没有受信任 CI 的 route.result（auto_merge=true）→ missing_route
        fx, gh = self.world(risk="R1", merger=BOT, auto_route=False, auto_merge=True)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("missing_route", self.rules_of(report))
        finding = self.finding(report, "missing_route")
        self.assertEqual((finding["source"], finding["stage"]), ("ci", "route"))
        self.assertFalse(report["ok"])

        # local 链上的 route.result 不算 main 来源：本机补一条 auto_merge=true 仍必须报
        self.assertIsNotNone(events.emit(
            "route", "result", "ok", trace_id=BRANCH, duration_ms=1,
            outputs={"auto_merge": True, "audit": False, "approval": "app"},
            decision={"by": "policy", "rule": "result", "reason": "本机重放"}))
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("missing_route", self.rules_of(report))

        # 删 task 运行记录：任务 PR（route.facts 声明类别）没有运行记录 → missing_run_record
        fx, gh = self.world(with_record=False, risk="R2", merger=("alice", "User"))
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("missing_run_record", self.rules_of(report))
        finding = self.finding(report, "missing_run_record")
        self.assertEqual((finding["source"], finding["stage"]), ("local", "dispatch"))

        # 删锚点：记录在但既无记录内锚点也无锚点表行 → missing_anchor（不重复报缺记录）
        fx, gh = self.world(fix_anchor=False, risk="R2", merger=("alice", "User"))
        record = json.loads(self.git("show", f"{fx.merge_sha}:{RECORD_PATH}", cwd=fx.project))
        self.assertEqual(record.get("anchors"), [])
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("missing_anchor", self.rules_of(report))
        self.assertNotIn("missing_run_record", self.rules_of(report))

        # 设计方 PR：无声明类别、评审列无执行方、无运行记录 → 不要求派发记录（不误报）
        fx = self.build_project(with_record=False)
        body = self.review_comment(fx, implementers=())
        gh = self.platform(fx, comments=[self.as_comment(501, body)], merger=("alice", "User"),
                           risk="R2", declared=None, auto_route=False)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertNotIn("missing_run_record", self.rules_of(report), report["findings"])
        self.assertNotIn("missing_anchor", self.rules_of(report))

        # 评审列了执行方即任务 PR：三证据之一成立、同样没有运行记录时仍要报
        fx = self.build_project(with_record=False)
        body = self.review_comment(fx, implementers=("pi",))
        gh = self.platform(fx, comments=[self.as_comment(501, body)], merger=("alice", "User"), risk="R2", declared=None,
                           auto_route=False)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("missing_run_record", self.rules_of(report))

    # ---- 验收 2：app 批准绑定合并 head / 人冒充 App；none 模式不凭空要求；评审同设计方 ----

    def test_app_approval_binds_merged_head_and_none_is_supported(self):
        # 基线：app 模式 R1 自动合并，批准者是 App 且绑定合并 head → 无 approval 发现、整体无发现
        fx, gh = self.world(risk="R1", merger=BOT, auto_route=True, auto_merge=True)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertNotIn("approval_actor", self.rules_of(report))
        self.assertNotIn("approval_head", self.rules_of(report))

        # app 批准旧 head：批准绑定的提交不是合并 head → approval_head（不误报 approval_actor）
        fx = self.build_project()
        body = self.review_comment(fx)
        gh = self.platform(fx, comments=[self.as_comment(501, body)], merger=BOT, risk="R1", auto_route=True, auto_merge=True,
                           reviews=[{"state": "APPROVED", "user": {"login": APP[0], "type": APP[1]},
                                     "commit_id": "1" * 40}])
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        finding = self.finding(report, "approval_head")
        self.assertEqual((finding["severity"], finding["source"], finding["stage"]),
                         ("error", "github", "merge"))
        self.assertNotIn("approval_actor", self.rules_of(report))

        # 人冒充 App：app 模式 R1 自动合并的批准者是 User → approval_actor（绑定 head 无 approval_head）
        fx = self.build_project()
        body = self.review_comment(fx)
        gh = self.platform(fx, comments=[self.as_comment(501, body)], merger=BOT, risk="R1", auto_route=True, auto_merge=True,
                           reviews=[{"state": "APPROVED", "user": {"login": "alice", "type": "User"},
                                     "commit_id": fx.branch_head}])
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        finding = self.finding(report, "approval_actor")
        self.assertEqual(finding["stage"], "merge")
        self.assertIn("不是 App", finding["reason"])
        self.assertNotIn("approval_head", self.rules_of(report))

        # none 模式：无批准（approver=none、合并者 Bot）也不凭空要求 App → 无 approval 发现
        fx = self.build_project()
        body = self.review_comment(fx)
        self.write_config(fx.project, '[platform]\napproval = "none"\n')
        gh = self.platform(fx, comments=[self.as_comment(501, body)], merger=BOT, risk="R1", auto_route=True, auto_merge=True,
                           reviews=[])
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertNotIn("approval_actor", self.rules_of(report), report["findings"])
        self.assertNotIn("approval_head", self.rules_of(report))

        # 评审者与设计方同身份：reviewer_not_independent，且无合格独立评审补 missing_review
        fx = self.build_project()
        body = self.review_comment(fx, reviewer="codex", designer="codex")
        gh = self.platform(fx, comments=[self.as_comment(501, body)], merger=("alice", "User"), risk="R2", auto_route=False)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("reviewer_not_independent", self.rules_of(report))
        self.assertIn("missing_review", self.rules_of(report))
        finding = self.finding(report, "reviewer_not_independent")
        self.assertEqual(finding["stage"], "review")
        self.assertTrue(finding["ref"].startswith(f"{REPO}#402/comments/"), finding["ref"])

    # ---- 验收 3：合法追加不误报；删尾/改中间/账本对运行层不同分别被发现 ----

    def test_prefix_anchor_tail_deletion_and_ledger_mismatch(self):
        # (a) 合法后续追加：锚点固定后再 emit 本机事件，前缀锚点不是最终链头 → 不报 anchor_mismatch
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False)
        self.assertIsNotNone(events.emit("dispatch", "push_pr", "ok", trace_id=BRANCH, duration_ms=3))
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertNotIn("chain_invalid", self.rules_of(report))
        self.assertNotIn("anchor_mismatch", self.rules_of(report))

        # (b) 删尾：删掉锚点链头及其后事件——内部链仍合法（无 chain_invalid），但固定锚点发现删尾
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False)
        record = json.loads(self.git("show", f"{fx.merge_sha}:{RECORD_PATH}", cwd=fx.project))
        anchor_head = record["anchors"][0]["head_hash"]
        self.db_exec(fx, "DELETE FROM events WHERE source='local' AND trace_id=? AND seq>=2", (BRANCH,))
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertNotIn("chain_invalid", self.rules_of(report), report["findings"])
        finding = self.finding(report, "anchor_mismatch")
        self.assertEqual(finding["stage"], "dispatch")
        self.assertIn(anchor_head[:12], finding["ref"])
        self.assertFalse(report["ok"])

        # (c) 改中间：改中间事件内容 → 原始链校验失败（chain_invalid）；锚点链头仍在链上不重复报
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False)
        self.db_exec(fx, "UPDATE events SET outputs='{\"tampered\":1}' WHERE source='local' AND seq=1")
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("chain_invalid", self.rules_of(report))
        finding = self.finding(report, "chain_invalid")
        self.assertEqual(finding["source"], "local")
        self.assertIn("不符", finding["reason"])
        self.assertNotIn("anchor_mismatch", self.rules_of(report))

        # (d) 真实 writer 账本：链头与运行层一致 → 无 ledger_mismatch，报告仍全通过
        fx = self.build_project()
        body = self.review_comment(fx)
        gh_body = self.platform(fx, comments=[self.as_comment(501, body)], merger=("alice", "User"), risk="R2", auto_route=False)
        built = ledger.build_ledger(402, gh=gh_body, cwd=fx.project)
        self.assertEqual(built["missing"], [])
        stub = FakeGh()
        published = ledger.publish_ledger(built, gh=stub)
        self.assertTrue(published["ok"], published["findings"])
        gh = self.platform(fx, comments=[self.as_comment(501, body), stub.comments[0]], merger=("alice", "User"), risk="R2",
                           auto_route=False)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        self.assertTrue(report["ok"], report["findings"])

        # (e) 账本对运行层不同：账本链头换成别的哈希 → ledger_mismatch（账本字节本身与锚点一致仍核对通过）
        fx, gh_default = self.world(risk="R2", merger=("alice", "User"), auto_route=False)
        mutated = {"schema_version": 1, "repository": REPO, "pr": 402, "trace_id": BRANCH,
                   "head_sha": fx.branch_head, "merged_at": MERGED_AT, "merge_sha": fx.merge_sha,
                   "chains": [{"source": "ci:9002:1:job_y", "trace_id": BRANCH, "head_hash": "b" * 64}],
                   "references": []}
        anchor = self.push_ledger(fx.origin, canonical(mutated).encode("utf-8") + b"\n", 402,
                                  f"{MERGED_AT[:4]}/402.json")
        gh = self.platform(fx, comments=[gh_default.comments[0], anchor], merger=("alice", "User"),
                           risk="R2", auto_route=False)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        finding = self.finding(report, "ledger_mismatch")
        self.assertEqual((finding["source"], finding["stage"]), ("github", "merge"))
        self.assertIn("ci:9002:1:job_y", finding["ref"])
        self.assertEqual(next(item for item in report["references"] if item["kind"] == "ledger")["status"],
                         "verified")
        self.assertFalse(report["ok"])

    # ---- 验收 4：缺省/合法可选/非法配置、库关闭/坏库如实报告；原路由返回不受审计配置影响 ----

    def test_config_defaults_invalid_and_event_independence(self):
        # 缺省（无 [audit] 节）：R2 无评审 → 按设计缺省阈值 2 报 missing_review
        fx, gh = self.world(comments=[], risk="R2", merger=("alice", "User"), auto_route=False)
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("missing_review", self.rules_of(report))

        # 合法可选：阈值调到 3 → 同一世界不再要求 R2 评审（配置生效，不是被忽略）
        fx, gh = self.world(comments=[], risk="R2", merger=("alice", "User"), auto_route=False,
                            config="[audit]\nrequire_review_risk = 3\n")
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertNotIn("missing_review", self.rules_of(report), report["findings"])

        # 合法可选：verify_anchors=false → 删尾世界不再做锚点核对（内部链合法，无任何发现）
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False,
                            config="[audit]\nverify_anchors = false\n")
        self.db_exec(fx, "DELETE FROM events WHERE source='local' AND trace_id=? AND seq>=2", (BRANCH,))
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertNotIn("anchor_mismatch", self.rules_of(report), report["findings"])

        # 合法可选：require_route_for_auto=false → 自动合并没有 route.result 也不报
        fx, gh = self.world(risk="R1", merger=BOT, auto_route=False, auto_merge=True,
                            config="[audit]\nrequire_route_for_auto = false\n")
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertNotIn("missing_route", self.rules_of(report), report["findings"])

        # 非法配置：类型错误与未知键 → configuration_error（error），CLI 退出 2，不静默放宽
        for text in ('[audit]\nrequire_review_risk = "2"\n', '[audit]\nrequire_roote = true\n',
                     '[audit]\nverify_anchors = "yes"\n'):
            fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False, config=text)
            with self.cli_env(gh=gh, root=fx.project):
                code, out, err = self.run_cli("audit", "402", "--json")
            self.assertEqual(code, 2, (text, err))
            report = json.loads(out)
            finding = self.finding(report, "configuration_error")
            self.assertEqual((finding["severity"], finding["source"]), ("error", "local"))

        # 非法 [platform] approval 同样是 configuration_error（审批核对未执行，不冒充 none）
        fx, gh = self.world(risk="R1", merger=BOT, auto_route=True, auto_merge=True,
                            config='[platform]\napproval = "sudo"\n')
        report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertIn("configuration_error", self.rules_of(report))
        self.assertNotIn("approval_actor", self.rules_of(report))

        # 库关闭（HARNESS_EVENTS=off）：审计照常复原与核对，观察事件跳过，结论不受影响
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False)
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}):
            report = audit.inspect_pr(402, gh=gh, cwd=fx.project)
        self.assertEqual(report["findings"], [], report["findings"])

        # 坏库：事件库损坏 → chain_invalid 如实报告（不当无事件、不崩溃），退出 1、无栈溢出
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False)
        db_path = events_db.db_path()
        assert db_path is not None
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        db_path.write_bytes(b"this is not a sqlite database" * 10)
        with self.cli_env(gh=gh, root=fx.project):
            code, out, err = self.run_cli("audit", "402", "--json")
        self.assertEqual(code, 1, err)
        self.assertNotIn("Traceback", err)
        report = json.loads(out)
        finding = self.finding(report, "chain_invalid")
        self.assertIn("无法读取", finding["reason"])
        self.assertFalse(report["ok"])

        # 范围（批量模式）：非法 [audit] 配置让整批退出 2（configuration_error 优先于普通发现）；
        # 同一世界换合法配置后批量退出码回到 0/1（由发现决定），配置错误不静默淹没批量结果
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False,
                            config='[audit]\nrequire_review_risk = "2"\n')
        with self.cli_env(gh=gh, root=fx.project):
            code, out, _err = self.run_cli("audit", "--all-merged", "--json")
        self.assertEqual(code, 2)
        batch = json.loads(out)
        self.assertEqual([item["pr"] for item in batch["prs"]], [402])
        self.assertIn("configuration_error", self.rules_of(batch["prs"][0]))
        fx, gh = self.world(risk="R2", merger=("alice", "User"), auto_route=False,
                            config="[audit]\nrequire_review_risk = 3\n")
        with self.cli_env(gh=gh, root=fx.project):
            code, out, _err = self.run_cli("audit", "--all-merged", "--json")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["prs"][0]["ok"])

        # 原路由返回不受审计配置影响：无 [audit]/合法 [audit]/非法 [audit] 三种配置下
        # policy.main（路由产品入口）的 stdout 逐字相同、返回 0（[audit] 只属于 audit 报告）
        repo = self.fresh_repo("route")
        self.use_root(repo)
        base = self.git("rev-parse", "HEAD", cwd=repo)
        (repo / "README.md").write_text("# app routed\n", encoding="utf-8")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "route fixture", cwd=repo)
        head = self.git("rev-parse", "HEAD", cwd=repo)

        def fake_gh(*args):
            joined = " ".join(args)
            if joined.startswith("pr view"):
                return json.dumps({"headRefName": "task/policy", "labels": []})
            if joined.startswith("pr list"):
                return json.dumps([])
            if joined.startswith("issue list"):
                return json.dumps([])
            raise AssertionError(f"未预期的 gh 调用：{joined}")

        def gather(*args, **kwargs):
            kwargs.setdefault("cwd", repo)
            kwargs.setdefault("gh", fake_gh)
            return orig_gather(*args, **kwargs)

        autonomy = {"size": {"max_lines": 400, "exclude": []},
                    "classes": {"K1": {"name": "说明与记录", "level": "L4", "window": 20,
                                       "max_escapes": 0, "audit_every": 0}}}
        orig_gather = policy.gather
        outputs: list[tuple[int, str]] = []
        for text in ("", "[audit]\nrequire_review_risk = 1\n", '[audit]\nrequire_review_risk = "2"\n'):
            self.write_config(repo, text)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(r1_checks, "load_autonomy", return_value=autonomy), \
                    mock.patch.object(policy, "gather", gather), \
                    mock.patch.object(policy, "_gh", fake_gh), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = policy.main(["--base", base, "--head", head, "--branch", "task/policy"])
            outputs.append((code, out.getvalue()))
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn("合并路由", out.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])


if __name__ == "__main__":
    unittest.main()
