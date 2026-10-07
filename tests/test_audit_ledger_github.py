"""T719 账本 github: 快照核对测试（B118/B121）：内容寻址快照（github:<PR>:<事实摘要>，读取时间不进
摘要）在合并工作流与本机 audit 各记一份时间戳，链头必然不同——`_ledger` 对 github: 链改为同 source
事件九字段语义核对（排除 ts/duration_ms/engine_version 等环境字段，不再认「链头在前缀里」），合并
事实按稳定身份投影独立必检（不被同 source 通过短路，候选为空与投影不同分 reason），运行层缺失的
快照按整个事件集合判定种类（合并快照由合并事实一致性承担、抽审/逃逸旧快照可被取代、其余失败关闭）；
ci:/本机链沿用链头与前缀成员核对不变。

夹具沿用 tests/test_audit_completeness.py 的模式：隔离 events_db.ROOT、递增冻结时钟（跨环境两侧
ts 必然不同）、假 gh 桩与匿名临时 git 仓库；账本经真实写入路径（build_ledger 内部走 github_events.sync
采集事实快照 + publish_ledger 发布锚点评论），运行层快照经真实 github_events.sync（audit 内部
_sync_github 或测试显式调用）生成。篡改用 sqlite UPDATE/emit 追加（链哈希不再相符由 chain_invalid
如实报告，本文件只断言 ledger_mismatch 的有无）。不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import sqlite3
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

from engine.agents import dispatch_observation, run_timeline
from engine.core import events, events_db, events_io
from engine.reports import audit, audit_completeness, ci_events, github_events, ledger

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
REPO = "owner/repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
BRANCH = "task/719-fixture"
TASKBOOK = "docs/plans/task-719-audit-ledger-github-snapshot.md"
RECORD_PATH = "docs/runs/task-719-audit-ledger-github-snapshot/1.json"
PR_TITLE = "feat：账本 github 快照核对"
PR_BODY = "T719 夹具正文。"
MERGED_AT = "2026-03-04T05:06:07Z"
TS = "2026-01-02T00:01:00.000Z"
AUDIT_ISSUE = {"number": 88, "title": "audit：抽审 #402", "body": "抽审任务", "state": "open",
               "created_at": TS, "labels": [{"name": "audit"}]}
ESCAPE_ISSUE = {"number": 77, "title": "escape：事故 #402", "body": "登记", "state": "open",
                "created_at": TS, "labels": [{"name": "escape"}, {"name": "class:K5"}]}


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
    """递增冻结时钟：从 2026-01-02T00:01Z 起每次调用前进一分钟，两侧环境的 ts 严格不同。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        moment = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=self.step)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeGh:
    """audit/ledger/github_events 共用 gh 桩：只读 API、artifact zip 下载与锚点评论 POST。"""

    def __init__(self, *, repo=REPO, pulls=None, reviews=None, pr_commits=None, commits=None,
                 comments=None, issues=None, runs=None, artifacts=None, downloads=None, fail=()):
        self.calls: list[tuple] = []
        self.writes: list[tuple] = []
        self._repo = repo
        self.pulls = pulls or {}
        self.reviews = reviews or {}
        self.pr_commits = pr_commits or {}
        self.commits = commits or {}
        self.comments = list(comments or [])
        self.issues = issues or {}
        self.runs = runs or []
        self.artifacts = artifacts or {}
        self.downloads = downloads or {}
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
            return {"total_count": len(items), "workflow_runs": items}
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows", path):
            return {"total_count": 0, "workflows": []}  # T720：load_ci 的第二段查询（本文件无 auto-merge 工作流）
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/artifacts", path):
            items = self.artifacts.get(int(match[1]), [])
            return {"total_count": len(items), "artifacts": items}
        raise AssertionError(f"FakeGh 未配置的路由：{path}")

    def _page(self, items: list, query: str) -> list:
        params = urllib.parse.parse_qs(query)
        page = int(params.get("page", ["1"])[0])
        cap = int(params.get("per_page", ["30"])[0])
        return items[(page - 1) * cap:page * cap]

    def _write(self, route: str, payload) -> dict:
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


class GitHubLedgerTest(unittest.TestCase):
    """共用夹具基类：项目 + 运行记录 + 事实齐备的平台桩；真实账本写入路径与真实事实同步。
    不直接携带测试方法；验收 1–6 各为一个具名子类（任务书验收表的覆盖列）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-t719-"))
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

    # ---- 基础夹具（沿用 test_audit_completeness 的模式） ----

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
        (path / TASKBOOK).write_text("task：T719 账本 github 快照核对（夹具任务书）\n", encoding="utf-8")
        (path / ".harness" / "config").mkdir(parents=True, exist_ok=True)
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

    def build_project(self) -> SimpleNamespace:
        """项目仓库：main（含任务书）+ 任务分支（本机事件与运行记录）+ --no-ff 合并 + bare origin。"""
        project = self.fresh_repo("app")
        origin = self.bare_origin(project)
        self.use_root(project)
        base = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "-b", BRANCH, cwd=project)
        self.git("commit", "-q", "--allow-empty", "-m", "branch work", cwd=project)
        stored = events.store_artifact(b"verify log 719\n")
        self.assertIsNotNone(events.emit(
            "dispatch", "admit", "ok", trace_id=BRANCH, duration_ms=5,
            decision={"by": "taskbook", "rule": "admit", "reason": "ok"}))
        self.assertIsNotNone(events.emit(
            "verify", "verify.tests", "ok", trace_id=BRANCH, duration_ms=10,
            outputs={"log.sha256": stored["sha256"], "log.size": stored["size"], "log.ref": stored["ref"]}))
        branch_head = self.git("rev-parse", "HEAD", cwd=project)
        timeline, head_hash = run_timeline.record_fields(BRANCH)
        self.assertTrue(head_hash)
        run_timeline.fix_anchor(BRANCH, head_hash)
        record = {"task": "task-719", "class": "K5", "attempt": 1, "branch": BRANCH,
                  "started_at": TS, "ended_at": TS, "exit": "ok", "trace_id": BRANCH,
                  "stages": timeline["stages"], "anchors": timeline["anchors"]}
        path = project / RECORD_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "run：task-719 派发记录", cwd=project)
        branch_head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m", f"Merge pull request #402 from {BRANCH}\n\n{PR_BODY}",
                 BRANCH, cwd=project)
        merge_sha = self.git("rev-parse", "HEAD", cwd=project)
        self.git("push", "-q", "origin", "main", cwd=project)
        return SimpleNamespace(project=project, origin=origin, base=base, branch_head=branch_head,
                               merge_sha=merge_sha, stored=stored)

    def build_ci_packages(self, project: Path, branch_head: str) -> dict[str, bytes]:
        """在项目分支 head 上以 CI 身份生成两个 run 的事件包（真实 emit + ci_events.export）。"""
        real_url = self.git("remote", "get-url", "origin", cwd=project)
        self.git("remote", "set-url", "origin", GITHUB_URL, cwd=project)
        self.git("checkout", "-q", "--detach", branch_head, cwd=project)
        try:
            packages: dict[str, bytes] = {}
            for run_id, job, steps in (
                    ("9001", "job_x", (("verify", "tests", "ok"),)),
                    ("9002", "job_y", (("route", "facts", "ok"), ("route", "result", "ok")))):
                for key, value in (("CI", "true"), ("GITHUB_HEAD_REF", BRANCH), ("GITHUB_RUN_ID", run_id),
                                   ("GITHUB_RUN_ATTEMPT", "1"), ("GITHUB_JOB", job)):
                    os.environ[key] = value
                for stage, step, status in steps:
                    outputs = {"risk": "R2", "machine_class": "K5", "declared_class": "K5",
                               "changed_lines": 3, "escapes": 0, "window": 20} if step == "facts" else \
                              {"auto_merge": False, "audit": False, "approval": "app"} if step == "result" else None
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

    def review_comment(self, fx: SimpleNamespace) -> str:
        """T105 真实生产者构造的评审评论体（独立评审摘要）。"""
        self.review_files(fx)
        mats = dispatch_observation.review_materials(fx.project, fx.base, fx.branch_head, TASKBOOK, 402)
        summary = dispatch_observation.review_audit(
            trace_id=BRANCH, head=fx.branch_head, base=fx.base, reviewer="opencode", model="review-model/1",
            model_basis="reported", designer="codex", implementers=["pi"], independent=True,
            same_host=False, parsed=True, verdict="通过", duration_ms=60000, findings=[], materials=mats)
        legacy = json.dumps({"verdict": "通过", "reviewer": "opencode", "head": fx.branch_head},
                            ensure_ascii=False)
        return ("### 独立评审（试行）：通过\n\n"
                f"<!-- independent-review {legacy} -->\n"
                f"<!-- harness-review-audit {json.dumps(summary, ensure_ascii=False)} -->\n")

    @staticmethod
    def as_comment(comment_id: int, body: str) -> dict:
        return {"id": comment_id, "body": body, "created_at": TS,
                "html_url": f"{GITHUB_URL}/issues/comments/{comment_id}"}

    def platform(self, fx: SimpleNamespace, *, comments=(), labels=(), audit_issues=None,
                 escape_issues=None, reviews=None, merger=("alice", "User"), title=PR_TITLE,
                 merged_at=MERGED_AT, parents=2, pr_commits=1, with_ci=False, pr=402,
                 head_ref=BRANCH, head_sha=None, merge_sha=None, fail=(), commit_title=None) -> FakeGh:
        """PR 的整套假平台：合并/抽审/逃逸事实路由（驱动真实 sync）+ 可选 CI 事件包与评论。"""
        head_sha, merge_sha = head_sha or fx.branch_head, merge_sha or fx.merge_sha
        packages = None
        if with_ci:
            packages = getattr(fx, "packages", None)
            if packages is None:
                packages = self.build_ci_packages(fx.project, fx.branch_head)
                fx.packages = packages
            runs, artifacts, downloads = self.ci_platform(fx.branch_head, packages)
        else:
            runs, artifacts, downloads = [], {}, {}
        pull = {"number": pr, "state": "closed", "merged": True, "merged_at": merged_at,
                "merge_commit_sha": merge_sha, "title": title, "body": PR_BODY,
                "labels": [{"name": name} for name in labels],
                "head": {"ref": head_ref, "sha": head_sha},
                "merged_by": {"login": merger[0], "type": merger[1]}}
        message = f"Merge pull request #{pr} from {head_ref}\n\n{PR_BODY}" if parents == 2 \
            else f"{commit_title or title} (#{pr})"
        commit = {"parents": [{"sha": "p1"}, {"sha": "p2"}] if parents == 2 else [{"sha": "p1"}],
                  "commit": {"message": message}}
        return FakeGh(pulls={pr: pull}, reviews={pr: list(reviews or [])},
                      pr_commits={pr: [{"sha": f"c{i}"} for i in range(pr_commits)]},
                      commits={merge_sha: commit}, comments=list(comments),
                      issues={"audit": list(audit_issues or []), "escape": list(escape_issues or [])},
                      runs=runs, artifacts=artifacts, downloads=downloads, fail=fail)

    def author_world(self, *, labels=(), audit_issues=None, escape_issues=None, reviews=None,
                     merger=("alice", "User"), title=PR_TITLE, merged_at=MERGED_AT, parents=2,
                     pr_commits=1, with_review=False, with_ci=False, mutate_ledger=None,
                     inject=None):
        """账本作者侧世界：项目 + 真实账本写入路径（build_ledger 内部经 github_events.sync 采集
        快照）+ publish_ledger 发布；返回 (fx, built, anchor)。inject 在账本构建前注入额外事件
        （先手动预同步拿到 source，再注入，build_ledger 内部的同步幂等不受影响）；
        mutate_ledger 在发布前改账本文档。"""
        fx = self.build_project()
        comments = [self.as_comment(501, self.review_comment(fx))] if with_review else []
        fx.review = comments[0] if comments else None
        gh_build = self.platform(fx, comments=comments, labels=labels, audit_issues=audit_issues,
                                 escape_issues=escape_issues, reviews=reviews, merger=merger,
                                 title=title, merged_at=merged_at, parents=parents,
                                 pr_commits=pr_commits, with_ci=with_ci)
        if inject is not None:
            github_events.sync(402, gh=gh_build)
            inject(self)
        built = ledger.build_ledger(402, gh=gh_build, cwd=fx.project)
        if mutate_ledger is not None:
            mutate_ledger(built)
        stub = FakeGh()
        published = ledger.publish_ledger(built, gh=stub)
        self.assertTrue(published["ok"], published["findings"])
        self.assertIsNotNone(stub.comments, published)
        return fx, built, stub.comments[0]  # publish_ledger 的 result["comment"] 是动作字符串

    def audit_gh(self, fx: SimpleNamespace, anchor, *, comments=(), **kwargs) -> FakeGh:
        """审计侧平台桩：事实路由驱动 inspect_pr 内部的 github_events.sync（运行层快照来源）。"""
        return self.platform(fx, comments=[*comments, anchor], **kwargs)

    # ---- 运行层事件库的观察与改动 ----

    def db_exec(self, sql: str, params=()) -> None:
        path = events_db.db_path()
        assert path is not None
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute(sql, params)
            conn.commit()

    def db_query(self, sql: str, params=()) -> list[tuple]:
        path = events_db.db_path()
        assert path is not None
        with contextlib.closing(sqlite3.connect(path)) as conn:
            return conn.execute(sql, params).fetchall()

    def wipe_events(self) -> None:
        """清空事件/引用/锚点（模拟全新环境的事件库）；产物文件与运行记录（在 git 里）不受影响。"""
        for table in ("refs", "anchors", "events"):
            self.db_exec(f"DELETE FROM {table}")

    def github_rows(self) -> list[tuple]:
        return self.db_query("SELECT id, source, seq, step FROM events WHERE source LIKE 'github:%'"
                             " ORDER BY source, seq")

    def row_by_step(self, step: str) -> tuple:
        return next(row for row in self.github_rows() if row[3] == step)

    def resync(self, fx: SimpleNamespace, **kwargs) -> None:
        """清库后用真实 sync 重建运行层快照（冻结时钟已前进：同 source、不同 ts）。"""
        github_events.sync(402, gh=self.platform(fx, **kwargs))

    # ---- 账本视图的小工具 ----

    def merge_event(self, built: dict) -> dict:
        return next(stage for stage in built["stages"] if stage.get("step") == "github.merge")

    def chain_head_of(self, built: dict, source: str) -> str:
        return next(chain["head_hash"] for chain in built["chains"] if chain["source"] == source)

    def inject(self, source: str, *, step="github.merge", stage="merge", status="ok", actor=None,
               inputs=None, outputs=None, decision=None, error=None) -> None:
        """运行层追加一条事件（emit 计算哈希链，链本身合法）。"""
        self.assertTrue(events.emit(stage, step, status, trace_id=BRANCH, source=source,
                                    actor=actor or {"role": "engine", "host": "github"},
                                    inputs=inputs, outputs=outputs, decision=decision, error=error))

    def snapshot_source(self, variant: str) -> str:
        return github_events._snapshot_source(402, {"kind": "merge", "variant": variant})

    def rules_of(self, report: dict) -> set[str]:
        return {item["rule"] for item in report["findings"]}

    def mismatches(self, report: dict) -> list[dict]:
        return [item for item in report["findings"] if item["rule"] == "ledger_mismatch"]

    def inspect(self, fx: SimpleNamespace, anchor, *, env_off=False, **kwargs) -> dict:
        gh = self.audit_gh(fx, anchor, **kwargs)
        if env_off:
            with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}):
                return audit.inspect_pr(402, gh=gh, cwd=fx.project)
        return audit.inspect_pr(402, gh=gh, cwd=fx.project)

class CrossEnvironmentTest(GitHubLedgerTest):
    """验收 1：跨环境同事实（B118 核心）——语义核对通过，链头不同不再是误报。"""

    def test_cross_environment_same_facts_pass(self):
        fx, built, anchor = self.author_world(labels=("alpha", "beta"))
        merge = self.merge_event(built)
        source = merge["source"]
        self.assertTrue(source.startswith("github:402:"), source)  # 内容寻址快照链
        self.wipe_events()
        self.resync(fx, labels=("alpha", "beta"))
        # 同 source、链头不同（ts 不同），且 duration_ms/engine_version 也不同：仍不得报
        self.assertNotEqual(self.chain_head_of(built, source), events_db.chain_head(source, BRANCH))
        self.db_exec("UPDATE events SET duration_ms=777, engine_version='9.9.9-test'"
                     " WHERE source LIKE 'github:%'")
        report = self.inspect(fx, anchor, labels=("alpha", "beta"))
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        # 抽审快照链同样跨环境通过
        sample_source = next(stage["source"] for stage in built["stages"]
                             if stage.get("step") == "github.audit_sample")
        self.assertNotEqual(self.chain_head_of(built, sample_source),
                            events_db.chain_head(sample_source, BRANCH))

    def test_merge_snapshot_events_present_in_both_environments(self):
        """跨环境用例的完备性：账本含合并（加两标签）与抽审快照链，运行层重新同步出同 source。"""
        fx, built, _anchor = self.author_world(labels=("alpha", "beta"))
        sources = {chain["source"] for chain in built["chains"] if chain["source"].startswith("github:")}
        self.assertEqual(len(sources), 2, sources)  # 合并快照（merge+2 标签）+ 抽审快照
        steps = sorted(stage["step"] for stage in built["stages"]
                       if stage.get("evidence_kind") == "event" and stage.get("source") in sources)
        self.assertEqual(steps, ["github.audit_sample", "github.merge",
                                 "github.merge_label", "github.merge_label"])
        self.wipe_events()
        self.resync(fx, labels=("alpha", "beta"))
        self.assertEqual(len({row[1] for row in self.github_rows()}), 2)

class EveryFieldTest(GitHubLedgerTest):
    """验收 2：逐字段反例（标签/抽审事件，只能由语义核对抓住）。"""

    def test_every_semantic_field_difference_reported(self):
        fx, _built, anchor = self.author_world(labels=("alpha",))
        label_sql = "UPDATE events SET {expr} WHERE id=?"
        cases = [
            ("stage", label_sql.format(expr="stage='merge2'"), ()),
            ("step", label_sql.format(expr="step='github.other'"), ()),
            ("status", label_sql.format(expr="status='fail'"), ()),
            ("actor", label_sql.format(expr="actor_role='mallory'"), ()),
            ("decision", label_sql.format(expr="decision_by='who'"), ()),
            ("error", label_sql.format(expr="error_kind='boom'"), ()),
            ("seq", label_sql.format(expr="seq=99"), ()),
        ]
        for field, sql, params in cases:
            with self.subTest(field=field):
                self.wipe_events()
                self.resync(fx, labels=("alpha",))
                row = self.row_by_step("github.merge_label")
                self.db_exec(sql, (*params, row[0]))
                report = self.inspect(fx, anchor, labels=("alpha",))
                mismatches = self.mismatches(report)
                self.assertTrue(mismatches, f"改动 {field} 未报 ledger_mismatch")
                reason = "；".join(item["reason"] for item in mismatches)
                self.assertIn(field, reason)

    def test_inputs_outputs_difference_reported_without_values(self):
        fx, _built, anchor = self.author_world(labels=("alpha",))
        for field, sql, params, secret, original in (
                ("inputs", "UPDATE refs SET ref='owner/repo#403' WHERE event_id=? AND kind='pr'", (), "403",
                 "owner/repo#402"),
                ("outputs", "UPDATE events SET outputs=? WHERE id=?",
                 ('{"label":"alpha","pr":402,"tampered":1}',), "tampered", "alpha")):
            with self.subTest(field=field):
                self.wipe_events()
                self.resync(fx, labels=("alpha",))
                row = self.row_by_step("github.merge_label")
                self.db_exec(sql, (*params, row[0]))
                report = self.inspect(fx, anchor, labels=("alpha",))
                mismatches = self.mismatches(report)
                self.assertTrue(mismatches, f"改动 {field} 未报 ledger_mismatch")
                reason = "；".join(item["reason"] for item in mismatches)
                self.assertIn(field, reason)
                self.assertNotIn(secret, reason)  # reason 只写字段名，不回显运行层的值
                self.assertNotIn(original, reason)  # 也不回显账本侧的值（评审 mutation P5）
                self.assertNotRegex(reason, r"[{}\[\]']")  # 没有字典或列表形状的原文

    def test_sample_event_difference_reported(self):
        fx, _built, anchor = self.author_world(labels=("alpha",))
        self.wipe_events()
        self.resync(fx, labels=("alpha",))
        row = self.row_by_step("github.audit_sample")
        self.db_exec("UPDATE events SET outputs='{\"issue\":null,\"pr\":402,\"sampled\":true}' WHERE id=?",
                     (row[0],))
        report = self.inspect(fx, anchor, labels=("alpha",))
        mismatches = self.mismatches(report)
        self.assertTrue(mismatches)
        self.assertIn("outputs", "；".join(item["reason"] for item in mismatches))

    def test_boolean_changed_to_number_reported(self):
        # 评审 193 第 4 轮：Python 里 True == 1，逐值比较若直接用 == 会放过 sampled 从 true 被改成 1
        fx, _built, anchor = self.author_world(labels=("alpha",), audit_issues=[AUDIT_ISSUE])
        self.wipe_events()
        self.resync(fx, labels=("alpha",), audit_issues=[AUDIT_ISSUE])
        row = self.row_by_step("github.audit_sample")
        self.db_exec("UPDATE events SET outputs='{\"issue\":88,\"pr\":402,\"sampled\":1}' WHERE id=?", (row[0],))
        report = self.inspect(fx, anchor, labels=("alpha",), audit_issues=[AUDIT_ISSUE])
        mismatches = self.mismatches(report)
        self.assertTrue(mismatches, "sampled: true → 1 未报 ledger_mismatch")
        self.assertIn("outputs", "；".join(item["reason"] for item in mismatches))

    def test_environment_only_fields_not_reported(self):
        fx, _built, anchor = self.author_world(labels=("alpha",))
        cases = [("ts", "UPDATE events SET ts='2030-01-01T00:00:00.000Z' WHERE id=?"),
                 ("duration_ms", "UPDATE events SET duration_ms=4321 WHERE id=?"),
                 ("engine_version", "UPDATE events SET engine_version='0.0.0-test' WHERE id=?")]
        for field, sql in cases:
            with self.subTest(field=field):
                self.wipe_events()
                self.resync(fx, labels=("alpha",))
                row = self.row_by_step("github.merge_label")
                self.db_exec(sql, (row[0],))
                report = self.inspect(fx, anchor, labels=("alpha",))
                self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])

class LaterEventTest(GitHubLedgerTest):
    """验收 3：后续事件的差异（防「只比较第一条」）。"""

    def test_later_event_difference_reported(self):
        fx, _built, anchor = self.author_world(labels=("alpha", "beta"))
        for which, step in (("最后一条", "github.merge_label"),):
            with self.subTest(position=which):
                self.wipe_events()
                self.resync(fx, labels=("alpha", "beta"))
                rows = [row for row in self.github_rows() if row[3] == step]
                target = rows[-1] if which == "最后一条" else rows[0]
                self.db_exec("UPDATE events SET outputs='{\"label\":\"x\",\"pr\":402}' WHERE id=?",
                             (target[0],))
                report = self.inspect(fx, anchor, labels=("alpha", "beta"))
                self.assertTrue(self.mismatches(report), f"只改{which}未报")
        # 第二条（第一个标签事件）单独改也必须报
        self.wipe_events()
        self.resync(fx, labels=("alpha", "beta"))
        rows = [row for row in self.github_rows() if row[3] == "github.merge_label"]
        self.db_exec("UPDATE events SET status='fail' WHERE id=?", (rows[0][0],))
        report = self.inspect(fx, anchor, labels=("alpha", "beta"))
        self.assertTrue(self.mismatches(report))

class CountAndAppendTest(GitHubLedgerTest):
    """验收 4：数量与追加（不再认「链头在前缀里」）。"""

    def test_count_and_append_differences_reported(self):
        # (a) 运行层比账本少一条标签事件
        fx, built, anchor = self.author_world(labels=("alpha", "beta"))
        self.wipe_events()
        self.resync(fx, labels=("alpha", "beta"))
        row = [item for item in self.github_rows() if item[3] == "github.merge_label"][-1]
        self.db_exec("DELETE FROM events WHERE id=?", (row[0],))
        report = self.inspect(fx, anchor, labels=("alpha", "beta"))
        self.assertTrue(self.mismatches(report))
        self.assertIn("事件数量不同", "；".join(item["reason"] for item in self.mismatches(report)))

        # (b) 在合法链尾追加一条改了合并者的事件：旧账本链头仍是运行层前缀成员，也必须报
        fx, built, anchor = self.author_world(labels=("alpha", "beta"))
        source = self.merge_event(built)["source"]
        head = self.chain_head_of(built, source)
        merge = self.merge_event(built)
        self.inject(source, inputs=merge["inputs"], outputs={"pr": 402, "merger": "mallory"})
        hashes = {row[0] for row in self.db_query(
            "SELECT hash FROM events WHERE source=? AND trace_id=?", (source, BRANCH))}
        self.assertIn(head, hashes)  # 前缀成员成立：旧实现会放过
        report = self.inspect(fx, anchor, labels=("alpha", "beta"))
        self.assertTrue(self.mismatches(report))

        # (c) 重新同步后的运行层追加一条事件（数量差）同样报
        self.wipe_events()
        self.resync(fx, labels=("alpha", "beta"))
        merge2 = self.merge_event(built)
        self.inject(source, inputs=merge2["inputs"], outputs=dict(merge2["outputs"]))
        report = self.inspect(fx, anchor, labels=("alpha", "beta"))
        self.assertTrue(self.mismatches(report))

        # (d) 单事件抽审链在运行层多出一条事件同样报
        fx, built, anchor = self.author_world(labels=("alpha",))
        sample_source = next(stage["source"] for stage in built["stages"]
                             if stage.get("step") == "github.audit_sample")
        self.inject(sample_source, step="github.audit_sample", stage="ci",
                    outputs={"issue": None, "pr": 402, "sampled": True})
        report = self.inspect(fx, anchor, labels=("alpha",))
        self.assertTrue(self.mismatches(report))

class SupersededSnapshotTest(GitHubLedgerTest):
    """验收 5：整条快照缺失按种类处理 + 合并事实独立必检（B121），①–⑬ 各一个子测试。"""

    def test_merge_snapshot_missing_label_added_not_reported(self):
        # ① 合并后加标签：事实合法变化另起新 source，旧快照缺失不报；label_count 不在投影里
        fx, _built, anchor = self.author_world(labels=("alpha",))
        self.wipe_events()
        report = self.inspect(fx, anchor, labels=("alpha", "beta"))
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])

    def test_title_change_and_unknown_merge_method_not_reported(self):
        # ② 合并后改 PR 标题：squash 的 merge_method 变 unknown 是可变字段，不报
        fx, built, anchor = self.author_world(parents=1, pr_commits=1)
        merge = self.merge_event(built)
        self.assertEqual(merge["outputs"]["merge_method"], "squash")
        self.wipe_events()
        # 合并提交主题保持原 squash 主题，只改 PR 标题：_merge_method 比对「当前标题 (#N)」落空，判 unknown
        report = self.inspect(fx, anchor, parents=1, pr_commits=1, title="feat：改过的标题",
                              commit_title=PR_TITLE)
        runtime = [row for row in events_io.query(trace_id=BRANCH) if row["step"] == "github.merge"]
        self.assertEqual([row["outputs"]["merge_method"] for row in runtime], ["unknown"])
        self.assertNotIn(merge["source"], {row["source"] for row in runtime})  # 事实变了，source 随之另起
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])

    def test_approval_drift_and_read_failures_not_reported(self):
        # ③ 批准列表变化 / reviews 读取失败 / 合并提交读取失败：批准与 merge_method 不在投影里
        approved = [{"state": "APPROVED", "user": {"login": "bob", "type": "User"}, "commit_id": "9" * 40}]
        fx, _built, anchor = self.author_world()
        self.wipe_events()
        report = self.inspect(fx, anchor, reviews=approved)
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        self.wipe_events()
        report = self.inspect(fx, anchor, fail=("pulls/402/reviews",))
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        self.wipe_events()
        report = self.inspect(fx, anchor, fail=("commits/",))
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])

    def test_stable_fact_differences_reported(self):
        # ④ 运行层合并事实与账本不同（merger/merger_type/merged_at/合并提交）→ 报，reason 写「与运行层不同」
        for field, kwargs, name in (("merger", {"merger": ("mallory", "User")}, "merger"),
                                    ("merger_type", {"merger": ("mallory", "Bot")}, "merger_type"),
                                    ("merged_at", {"merged_at": "2026-03-05T05:06:07Z"}, "merged_at"),
                                    ("合并提交", {"merge_sha": "f" * 40}, "inputs")):
            with self.subTest(field=field):
                fx, _built, anchor = self.author_world()
                self.wipe_events()
                report = self.inspect(fx, anchor, **kwargs)
                mismatches = self.mismatches(report)
                self.assertTrue(mismatches, f"运行层 {field} 不同未报")
                reason = "；".join(item["reason"] for item in mismatches)
                self.assertIn("与运行层不同", reason)
                self.assertIn(name, reason)

    def test_projection_compares_booleans_and_numbers_strictly(self):
        # 评审 193 第 4 轮：True == 1。合并投影里 pr 号为 1 的 PR，运行层把 pr 改成 true 也必须报
        def merge(source: str, pr_value) -> dict:
            return {"source": source, "trace_id": BRANCH, "step": "github.merge", "stage": "merge", "status": "ok",
                    "actor": {"role": "engine", "host": "github"}, "decision": {}, "error": {},
                    "inputs": [{"kind": "pr", "ref": "owner/repo#1"}],
                    "outputs": {"pr": pr_value, "merger": "alice", "merger_type": "User", "merged_at": MERGED_AT}}
        ledger_events = {"github:1:aaa": [merge("github:1:aaa", 1)]}
        same = audit_completeness._merge_fact_findings(ledger_events, {"github:1:bbb": [merge("github:1:bbb", 1)]})
        self.assertEqual(same, [])
        tampered = audit_completeness._merge_fact_findings(
            ledger_events, {"github:1:bbb": [merge("github:1:bbb", True)]})
        self.assertEqual(len(tampered), 1)
        self.assertIn("字段：pr", tampered[0]["reason"])

    def test_no_runtime_merge_facts_reported(self):
        # ⑤ 运行层没有任何合并事实（事件关闭，同步无产出）→ 报且 reason 区分「读不到」
        fx, _built, anchor = self.author_world(labels=("alpha",))
        self.wipe_events()
        report = self.inspect(fx, anchor, env_off=True, labels=("alpha",))
        mismatches = self.mismatches(report)
        self.assertTrue(mismatches)
        reason = "；".join(item["reason"] for item in mismatches)
        self.assertIn("运行层没有该 PR 的合并事实", reason)

    def test_masked_difference_reported(self):
        # ⑥ 账本 A 缺失，运行层有同投影的 B（加标签）与合并者不同的 C → 仍报（不被 B 掩盖）
        fx, _built, anchor = self.author_world(labels=("alpha",))
        self.wipe_events()
        github_events.sync(402, gh=self.platform(fx, labels=("alpha", "beta")))
        github_events.sync(402, gh=self.platform(fx, merger=("mallory", "User"), labels=("alpha",)))
        report = self.inspect(fx, anchor, labels=("alpha",))
        mismatches = self.mismatches(report)
        self.assertTrue(mismatches)
        self.assertIn("merger", "；".join(item["reason"] for item in mismatches))

    def test_masked_difference_reported_when_a_present(self):
        # ⑥a A 仍在且逐字节相同（快速通过），B（加标签）与 C（合并者不同）也必须被核对到
        fx, _built, anchor = self.author_world(labels=("alpha",))
        github_events.sync(402, gh=self.platform(fx, labels=("alpha", "beta")))
        github_events.sync(402, gh=self.platform(fx, merger=("mallory", "User"), labels=("alpha",)))
        report = self.inspect(fx, anchor, labels=("alpha",))
        mismatches = self.mismatches(report)
        self.assertTrue(mismatches)
        self.assertIn("merger", "；".join(item["reason"] for item in mismatches))

    def test_same_branch_two_prs(self):
        # ⑥b 同分支名被另一个 PR 复用：403 的合并快照不参与 402 的核对；402 自己有 C 仍报
        fx, _built, anchor = self.author_world()
        github_events.sync(403, gh=self.platform(fx, pr=403, head_sha="e1" * 20, merge_sha="e2" * 20))
        report = self.inspect(fx, anchor)
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        github_events.sync(402, gh=self.platform(fx, merger=("mallory", "User")))
        report = self.inspect(fx, anchor)
        self.assertTrue(self.mismatches(report))

    def test_superseded_sample_and_escape_not_reported(self):
        # ⑦ 抽审/逃逸旧快照可被取代：新议题、新状态或读取失败都不报
        fx, _built, anchor = self.author_world(audit_issues=[AUDIT_ISSUE], escape_issues=[ESCAPE_ISSUE])
        self.wipe_events()
        report = self.inspect(fx, anchor, audit_issues=[dict(AUDIT_ISSUE, number=89)])
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        self.wipe_events()
        report = self.inspect(fx, anchor, fail=("labels=audit",))
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        self.wipe_events()
        report = self.inspect(fx, anchor, escape_issues=[dict(ESCAPE_ISSUE, state="closed")])
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])

    def test_malformed_seq_in_ledger_fail_closed_without_crash(self):
        # 评审 193：账本是外部输入，同 source 内 seq 混有整数与字符串时排序不得抛 TypeError 把审计打崩
        def corrupt_seq(doc):
            merge = next(stage for stage in doc["stages"]
                         if stage.get("evidence_kind") == "event" and stage.get("step") == "github.merge")
            merge["seq"] = bad_seq
        for present, bad_seq in ((True, "1"), (False, "1"), (True, True), (False, True)):  # 布尔值不算整数
            with self.subTest(source_present_in_runtime=present, seq=repr(bad_seq)):
                fx, _built, anchor = self.author_world(labels=("alpha",), mutate_ledger=corrupt_seq)
                self.wipe_events()
                report = self.inspect(fx, anchor, labels=("alpha",) if present else ("alpha", "beta"))
                reasons = [item["reason"] for item in self.mismatches(report)]
                self.assertTrue(any("seq" in reason for reason in reasons), reasons)
                self.assertNotIn("'1'", "；".join(reasons))  # 不回显原始值

    def test_malformed_containers_in_ledger_fail_closed_without_crash(self):
        # 评审 193 第 3 轮：stages/chains 不是列表（整数、字符串、字典）时遍历不得抛 TypeError。
        # stages 走真实审计入口（账本写入路径容忍它）；chains 在发布路径上就会被写入方拒绝，
        # 只能直接调用 _ledger（审计侧的唯一遍历点）。
        for bad in (5, "x", {"a": 1}):
            with self.subTest(field="stages", value=repr(bad)):
                fx, _built, anchor = self.author_world(
                    labels=("alpha",), mutate_ledger=lambda doc, bad=bad: doc.update({"stages": bad}))
                self.wipe_events()
                report = self.inspect(fx, anchor, labels=("alpha",))
                reasons = [item["reason"] for item in self.mismatches(report)]
                self.assertTrue(any("账本 stages 字段形状不符" in reason for reason in reasons), reasons)
                self.assertNotIn(repr(bad), "；".join(reasons))  # 不回显内容
        for bad in (7, "y", {"a": 1}):
            with self.subTest(field="chains", value=repr(bad)):
                auditor = SimpleNamespace(ledger_doc={"chains": bad, "stages": []}, event_rows=[], stages=[],
                                          trace=BRANCH)
                reasons = [item["reason"] for item in audit_completeness._ledger(auditor)]
                self.assertEqual(reasons, ["账本 chains 字段形状不符（失败关闭）"])

    def test_duplicate_merge_event_in_ledger_fail_closed(self):
        # 评审 193：首条之后再出现 github.merge 不属于「一条合并事件加零到多条标签事件」，旧 source 缺失时失败关闭
        def duplicate_merge(doc):
            merge = next(stage for stage in doc["stages"]
                         if stage.get("evidence_kind") == "event" and stage.get("step") == "github.merge")
            doc["stages"].append({**merge, "seq": merge["seq"] + 100})
        fx, _built, anchor = self.author_world(labels=("alpha",), mutate_ledger=duplicate_merge)
        self.wipe_events()
        report = self.inspect(fx, anchor, labels=("alpha", "beta"))
        self.assertTrue(any("种类无法识别" in item["reason"] for item in self.mismatches(report)),
                        report["findings"])

    def test_unknown_kind_missing_fail_closed(self):
        # ⑧ 未知 step 的 github: 快照在运行层没有 → 失败关闭（注入必须在账本构建之前）
        fx, _built, anchor = self.author_world(
            labels=("alpha",),
            inject=lambda t: t.inject(t.row_by_step("github.merge")[1], step="github.mystery",
                                      stage="ci", outputs={"pr": 402}))
        self.wipe_events()
        report = self.inspect(fx, anchor, labels=("gamma",))
        self.assertTrue(self.mismatches(report))

    def test_missing_without_event_text_reported(self):
        # ⑨ 账本只固定链头、没有事件原文，运行层也没有 → 报（不放宽）
        fx, _built, anchor = self.author_world(labels=("alpha",), mutate_ledger=lambda doc: doc.update(
            stages=[stage for stage in doc["stages"]
                    if not (stage.get("evidence_kind") == "event"
                            and stage.get("source", "").startswith("github:"))]))
        self.wipe_events()
        report = self.inspect(fx, anchor, labels=("alpha", "beta"))
        reasons = [item["reason"] for item in self.mismatches(report)]
        # 合并快照（旧 source 在运行层缺失）：没有事件原文可核对；抽审快照（同事实同 source 仍在运行层，
        # 链头因 ts 不同）：只固定链头也不放宽。两条分支各自要有断言，不能互相顶替（变异 K5）。
        self.assertTrue(any("账本没有事件原文可核对" in reason for reason in reasons), reasons)
        self.assertTrue(any("没有事件原文可核对，且链头" in reason for reason in reasons), reasons)

    def test_ci_chain_missing_still_not_reported(self):
        # ⑩ ci: 链在运行层没有：维持现状（不报，由引用核对报告）
        fx, built, anchor = self.author_world(with_ci=True)
        self.assertTrue(any(chain["source"].startswith("ci:") for chain in built["chains"]))
        self.wipe_events()
        report = self.inspect(fx, anchor, with_ci=False)
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])

    def test_projection_fields_each_load_bearing(self):
        # ⑪ 旧 source 缺失、新 source 里逐项改变稳定投影字段：第 5 条自己保留这些字段
        scalar = {"stage": "merge2", "status": "fail", "decision": {"by": "who"},
                  "error": {"kind": "boom"}, "actor": {"role": "engine", "host": "ci"}}
        output_values = {"pr": 999, "merger": "mallory", "merger_type": "Bot",
                         "merged_at": "2030-01-01T00:00:00Z"}
        head = "e" * 40
        input_tweaks = {"head": lambda items: [item if item["kind"] != "head" else {**item, "ref": head}
                                               for item in items],
                        "合并提交": lambda items: [item if item["kind"] != "merge_commit"
                                                   else {**item, "ref": "f" * 40} for item in items],
                        "PR号": lambda items: [item if item["kind"] != "pr"
                                               else {**item, "ref": "owner/repo#999"} for item in items]}
        for name in list(scalar) + list(output_values) + list(input_tweaks):
            with self.subTest(field=name):
                fx, built, anchor = self.author_world()
                merge = self.merge_event(built)
                event = dict(merge)
                if name in scalar:
                    event[name] = scalar[name]
                elif name in output_values:
                    event["outputs"] = {**merge["outputs"], name: output_values[name]}
                else:
                    event["inputs"] = input_tweaks[name](merge["inputs"])
                source = self.snapshot_source(name)
                self.wipe_events()
                self.inject(source, inputs=event["inputs"], outputs=event["outputs"],
                            stage=event["stage"] if name != "stage" else "merge",
                            status=event["status"], actor=event["actor"],
                            decision=event.get("decision"), error=event.get("error"))
                if name == "stage":  # stage 不在 emit 的合法枚举里：落库后再改（运行层事实被改动）
                    self.db_exec("UPDATE events SET stage='merge2' WHERE source=?", (source,))
                report = self.inspect(fx, anchor)
                mismatches = self.mismatches(report)
                self.assertTrue(mismatches, f"投影字段 {name} 被改动未报")
                reason = "；".join(item["reason"] for item in mismatches)
                self.assertIn("与运行层不同", reason)
                self.assertIn("inputs" if name in input_tweaks else name, reason)

    def test_rename_and_repository_case(self):
        # ⑫ merger 是 login：改名报（人工确认）；仓库名只改大小写不报；改成别的仓库报
        fx, built, anchor = self.author_world()
        merge = self.merge_event(built)
        self.wipe_events()
        self.inject(self.snapshot_source("rename"), inputs=merge["inputs"],
                    outputs={**merge["outputs"], "merger": "alice2"})
        report = self.inspect(fx, anchor)
        self.assertIn("merger", "；".join(item["reason"] for item in self.mismatches(report)))
        self.wipe_events()
        self.inject(self.snapshot_source("case"), inputs=[{**item, "ref": "OWNER/REPO#402"}
                                                          if item["kind"] == "pr" else item
                                                          for item in merge["inputs"]],
                    outputs=merge["outputs"])
        report = self.inspect(fx, anchor)
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        self.wipe_events()
        self.inject(self.snapshot_source("repo"), inputs=[{**item, "ref": "other/repo#402"}
                                                          if item["kind"] == "pr" else item
                                                          for item in merge["inputs"]],
                    outputs=merge["outputs"])
        report = self.inspect(fx, anchor)
        mismatches = self.mismatches(report)
        self.assertTrue(mismatches)
        self.assertIn("inputs", "；".join(item["reason"] for item in mismatches))

    def test_kind_classification(self):
        # ⑬ 种类判定：合并快照（含标签）缺失不报；未知 step、只有标签、混合种类失败关闭
        fx, _built, anchor = self.author_world(labels=("alpha", "beta"))
        self.wipe_events()
        report = self.inspect(fx, anchor, labels=("gamma",))
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])

        # 未知 step 必须在账本事件原文里（注入在账本构建前），运行层只剩新议题的新 source：
        # 抽审快照种类混入未知 step → 失败关闭（与 ⑧ 合并快照混入互补，覆盖种类判定的另一支）
        fx, _built, anchor = self.author_world(
            audit_issues=[AUDIT_ISSUE],
            inject=lambda t: t.inject(t.row_by_step("github.audit_sample")[1], step="github.mystery",
                                      stage="ci", outputs={"pr": 402}))
        self.wipe_events()
        report = self.inspect(fx, anchor, audit_issues=[dict(AUDIT_ISSUE, number=89)])
        self.assertTrue(self.mismatches(report))

        fx, _built, anchor = self.author_world(inject=lambda t: [
            t.inject(t.snapshot_source("labels-only"), step="github.merge_label",
                     outputs={"pr": 402, "label": "x"}),
            t.inject(t.snapshot_source("labels-only"), step="github.merge_label",
                     outputs={"pr": 402, "label": "y"})])
        self.wipe_events()
        report = self.inspect(fx, anchor)
        self.assertTrue(self.mismatches(report))

        fx, _built, anchor = self.author_world(labels=("alpha",), inject=lambda t: t.inject(
            t.row_by_step("github.merge")[1], step="github.escape", stage="ci",
            outputs={"pr": 402, "issue": 1}))
        self.wipe_events()
        report = self.inspect(fx, anchor, labels=("gamma",))
        self.assertTrue(self.mismatches(report))

class PreservedBehaviorTest(GitHubLedgerTest):
    """验收 6：仍保留的行为（ci:/本机链核对、无事件原文不放宽）。"""

    def test_preserved_behaviors(self):
        # github: 链头等于运行层链头（同一环境、无差异）不报，且整份报告全绿
        fx, built, anchor = self.author_world(labels=("alpha",), with_review=True, with_ci=True)
        report = self.inspect(fx, anchor, labels=("alpha",), with_ci=True, comments=[fx.review])
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertTrue(report["ok"])

        # ci: 链头在运行层事件哈希里（合法追加）不报；换成别的哈希仍报
        fx, built, anchor = self.author_world(with_ci=True)
        ci_source = next(chain["source"] for chain in built["chains"] if chain["source"].startswith("ci:"))
        self.inject(ci_source, step="postscript", stage="ci", outputs={"note": 1})
        report = self.inspect(fx, anchor, with_ci=True)  # 同一环境：ci 链头仍在前缀里
        self.assertNotIn("ledger_mismatch", self.rules_of(report), report["findings"])
        fx, built, anchor = self.author_world(with_ci=True, mutate_ledger=lambda doc: doc.update(
            chains=[{**chain, "head_hash": "b" * 64} if chain["source"].startswith("ci:") else chain
                    for chain in doc["chains"]]))
        report = self.inspect(fx, anchor, with_ci=True)
        mismatches = self.mismatches(report)
        self.assertTrue(any("ci:" in item["ref"] for item in mismatches), mismatches)

        # github: 链没有事件原文且链头不等于运行层链头 → 仍报（目标终态 4）
        fx, built, anchor = self.author_world(labels=("alpha",), mutate_ledger=lambda doc: doc.update(
            stages=[stage for stage in doc["stages"]
                    if not (stage.get("evidence_kind") == "event"
                            and stage.get("source", "").startswith("github:"))]))
        self.wipe_events()
        self.resync(fx, labels=("alpha",))
        report = self.inspect(fx, anchor, labels=("alpha",))
        self.assertTrue(self.mismatches(report))


if __name__ == "__main__":
    unittest.main()
