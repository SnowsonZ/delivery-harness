"""T305 合并账本与 PR 锚点测试：假平台（假 gh + 本地 bare remote + 匿名临时 git 仓库）提供运行记录、
CI/route artifact、评审评论与合并事实，无既有本机库仍复原完整引用/决定/锚点账本；只写已合并 PR 且
按合并 head（非最新 head）取记录；writer 幂等/冲突不覆盖/竞争有限重试且禁强推；PR 锚点评论同标记
更新；ruleset 模板禁删/强推且工作流是最小权限的受信任 job；本机原始链与毒化内容不入账本。

夹具沿用 tests/test_trace_events_cli.py 与 tests/test_github_events.py 的模式：隔离 events_db.ROOT、
递增冻结时钟、假 gh 桩（记录全部调用，写操作单独记账）；CI 包由构建仓库里真实的 emit + ci_events
.export 生成，评审审计标记由 T105 真实生产者（dispatch_observation.review_audit / review_materials）
构造，运行记录由 T201 真实生产者（run_timeline.record_fields）组装——生产者/消费者任一方漂移形状
都会让解析失败、账本缺评审摘要而使本文件失败。不碰真实库/PR/工作流，不调用真 gh。
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
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli
from engine.agents import dispatch_observation, run_timeline
from engine.core import events, events_db
from engine.reports import ci_events, ledger

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
REPO = "owner/repo"
GITHUB_ORIGIN = "https://github.com/owner/repo.git"
BRANCH = "task/305-fixture"
TASKBOOK = "docs/plans/task-305-audit-ledger.md"
RECORD_PATH = "docs/runs/task-305-audit-ledger/1.json"
TS = "2026-01-02T03:04:05Z"
TS2 = "2026-03-04T05:06:07Z"
POISON = "localpoisonvalue305"


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Clock:
    """递增冻结时钟：从 2026-01-02T03:01 起每次调用前进一分钟，事件/锚点 ts 严格递增且可比。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        hour, minute = divmod(self.step, 60)
        return f"2026-01-02T{3 + hour // 24:02d}:{(hour % 24 + 1):02d}:{minute:02d}.000Z"


class FakeGh:
    """ledger 用 gh 桩：PR/评审/提交/议题/评论/Actions 只读 API 与锚点评论 POST/PATCH，记录全部调用。

    page_size 模拟更小的服务端页容量（迫使真实翻页）；fail 里的子串命中即抛 RuntimeError；未配置的
    PR/路由按 404/断言失败处理；writes 单独记账评论写操作（断言只写锚点评论，不做合并/批准动作）。
    """

    def __init__(self, *, repo=REPO, pulls=None, reviews=None, pr_commits=None, commits=None,
                 audit=None, escape=None, comments=None, runs=None, artifacts=None, downloads=None,
                 page_size=None, fail=()):
        self.calls: list[tuple] = []
        self.writes: list[tuple] = []
        self._repo = repo
        self.pulls = pulls or {}
        self.reviews = reviews or {}
        self.pr_commits = pr_commits or {}
        self.commits = commits or {}
        self.issues = {"audit": list(audit or []), "escape": list(escape or [])}
        self.comments = list(comments or [])
        self.runs = runs or []
        self.artifacts = artifacts or {}
        self.downloads = downloads or {}
        self.page_size = page_size
        self.fail = tuple(fail)
        self._ids = itertools.count(9000)

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
        if path.endswith("/issues"):
            label = urllib.parse.parse_qs(query).get("labels", [""])[0]
            return self._page(self.issues.get(label, []), query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs", path):
            return {"total_count": len(self.runs), "workflow_runs": self._page(self.runs, query)}
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/artifacts", path):
            items = self.artifacts.get(int(match[1]), [])
            return {"total_count": len(items), "artifacts": self._page(items, query)}
        raise AssertionError(f"FakeGh 未配置的路由：{path}")

    def _write(self, route: str, payload) -> dict:
        """锚点评论写操作：只允许 POST issues/N/comments 与 PATCH issues/comments/id。"""
        path = route.partition("?")[0]
        if re.fullmatch(r"repos/[^/]+/[^/]+/issues/\d+/comments", path):
            self.writes.append(("POST", route, payload))
            comment = {"id": next(self._ids), "body": payload["body"], "created_at": TS,
                       "html_url": f"{GITHUB_ORIGIN}/issues/comments/{next(self._ids)}"}
            self.comments.append(comment)
            return comment
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/issues/comments/(\d+)", path):
            self.writes.append(("PATCH", route, payload))
            for comment in self.comments:
                if comment["id"] == int(match[1]):
                    comment["body"] = payload["body"]
                    return comment
            raise AssertionError(f"PATCH 不存在的评论 {match[1]}")
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
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-audit-ledger-"))
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

    # ---- 夹具 ----

    def git(self, *args: str, cwd: Path, check: bool = True) -> str:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
        if check and done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout.strip()

    def fresh_repo(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir()
        self.git("init", "-q", "-b", "main", cwd=path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
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

    def clear_db(self, project: Path) -> None:
        """删掉本机事件库与产物目录：build 不得依赖任何既有本机链（设计 4.1：无本机库也可完成）。"""
        shutil.rmtree(project / ".git" / "harness", ignore_errors=True)

    def build_project(self) -> tuple[Path, Path, str, str, str]:
        """项目仓库：main + 任务分支（运行记录经 T201 真实生产者组装）+ --no-ff 合并提交 + bare origin。"""
        project = self.fresh_repo("app")
        origin = self.bare_origin(project)
        self.use_root(project)
        base = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "-b", BRANCH, cwd=project)
        events.emit("dispatch", "admit", "ok", trace_id=BRANCH, duration_ms=5,
                    inputs=[events.ref("taskbook", f"{TASKBOOK}@{base}")],
                    decision={"by": "taskbook", "rule": "admit", "reason": "ok"})
        events.emit("dispatch", "executor_round", "ok", trace_id=BRANCH, duration_ms=1000,
                    outputs={"exit": "ok", "round": 1})
        timeline, head_hash = run_timeline.record_fields(BRANCH)
        self.assertTrue(head_hash)
        record = {"task": "task-305-audit-ledger", "class": "K7", "attempt": 1, "branch": BRANCH,
                  "started_at": TS, "ended_at": TS, "exit": "ok", **timeline}
        path = project / RECORD_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "run：task-305 第 1 次派发记录（ok）", cwd=project)
        branch_head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m", f"Merge pull request #305 from {BRANCH}\n\nbody",
                 BRANCH, cwd=project)
        merge_sha = self.git("rev-parse", "HEAD", cwd=project)
        self.git("push", "-q", "origin", "main", cwd=project)
        return project, origin, branch_head, merge_sha, base

    def build_ci_packages(self) -> tuple[str, dict[str, bytes]]:
        """构建仓库经真实 emit + ci_events.export 生成两个独立 run 的 CI 包（含 route facts），打成 zip。"""
        builder = self.fresh_repo("builder")
        self.git("remote", "add", "origin", GITHUB_ORIGIN, cwd=builder)
        self.git("checkout", "-q", "-b", BRANCH, cwd=builder)
        head = self.git("rev-parse", "HEAD", cwd=builder)
        packages: dict[str, bytes] = {}
        with mock.patch.object(events_db, "ROOT", builder):
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
                target = self.tmp / f"exp-{run_id}-{job}"
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(ci_events.export_main(["--target", str(target)]), 0,
                                     err.getvalue())
                name = f"harness-events-{run_id}-1-{job}"
                content = (target / "harness-events.json").read_text(encoding="utf-8")
                packages[name] = self.zip_bytes({f"{name}/harness-events.json": content})
        for key in [name for name in os.environ if name.startswith(("GITHUB_", "CI"))]:
            os.environ.pop(key, None)
        return head, packages

    @staticmethod
    def zip_bytes(members: dict[str, str]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    def review_comment(self, project: Path, base: str, head: str) -> str:
        """T105 真实生产者构造的评审评论体：independent-review 标记旁列 harness-review-audit 审计摘要。"""
        folder = project / "build" / "review"
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in (("task.md", "任务书材料"), ("diff.patch", "diff --git a/a b/a\n"),
                           ("ci.md", "CI 摘要"), ("pr.md", "PR 描述"), ("pack.md", "评审包")):
            (folder / name).write_text(text, encoding="utf-8")
        materials = dispatch_observation.review_materials(project, base, head, TASKBOOK, 305)
        audit = dispatch_observation.review_audit(
            trace_id=BRANCH, head=head, base=base, reviewer="opencode", model="review-model/1",
            model_basis="reported", designer="codex", implementers=["pi"], independent=True,
            same_host=False, parsed=True, verdict="通过", duration_ms=60000,
            findings=[{"severity": "一般", "location": "engine/x.py:12", "problem": "边界说明",
                       "fix": "补断言"}],
            materials=materials)
        legacy = json.dumps({"verdict": "通过", "reviewer": "opencode", "head": head},
                            ensure_ascii=False)
        return ("### 独立评审（试行）：通过\n\n"
                f"<!-- independent-review {legacy} -->\n"
                f"<!-- harness-review-audit {json.dumps(audit, ensure_ascii=False)} -->\n")

    @staticmethod
    def as_comment(comment_id: int, body: str) -> dict:
        return {"id": comment_id, "body": body, "created_at": TS,
                "html_url": f"{GITHUB_ORIGIN}/issues/comments/{comment_id}"}

    @staticmethod
    def merged_pr(number: int, *, head: str, merge_sha: str, merged: bool = True) -> dict:
        return {"number": number, "state": "closed" if merged else "open", "merged": merged,
                "merged_at": TS2 if merged else None,
                "merge_commit_sha": merge_sha if merged else None,
                "head": {"ref": BRANCH, "sha": head},
                "merged_by": {"login": "alice", "type": "User"},
                "labels": [{"name": "class:K7"}]}

    def platform_gh(self, *, head: str, merge_sha: str, merged: bool = True, comments=None,
                    runs=None, artifacts=None, downloads=None) -> FakeGh:
        """整套假平台：PR 305 合并事实 + 人批准 + 无议题；评论/运行/artifact/下载可配置。"""
        return FakeGh(
            pulls={305: self.merged_pr(305, head=head, merge_sha=merge_sha, merged=merged)},
            reviews={305: [{"state": "APPROVED", "user": {"login": "alice", "type": "User"},
                            "commit_id": head}]},
            commits={merge_sha: {"parents": [{"sha": "p1"}, {"sha": "p2"}]}} if merged else {},
            pr_commits={305: [{"sha": "x1"}, {"sha": "x2"}]},
            comments=list(comments or []),
            runs=list(runs or []),
            artifacts=dict(artifacts or {}),
            downloads=dict(downloads or {}),
        )

    def ci_platform(self, head: str, packages: dict[str, bytes], *, extra_runs=None,
                    extra_artifacts=None, extra_downloads=None) -> tuple[list, dict, dict]:
        """把 build_ci_packages 的产物接上假平台：两个独立 run 各带自己的事件包 artifact。

        基础 run 绑定合并 head；extra_runs 带自己的 headSha（用于其他 head/过期样本）。
        """
        runs = [{"id": 9001, "run_attempt": 1, "headSha": head, "path": ".github/workflows/harness.yml"},
                {"id": 9002, "run_attempt": 1, "headSha": head, "path": ".github/workflows/harness.yml"},
                *(extra_runs or [])]
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

    # ---- 验收 1：无本机库复原完整账本；评审标记按精确形状解析，坏样本各报 missing ----

    def test_ledger_reconstructs_without_local_database(self):
        project, _origin, _project_head, merge_sha, base = self.build_project()
        builder_head, packages = self.build_ci_packages()
        runs, artifacts, downloads = self.ci_platform(builder_head, packages)
        comment = self.review_comment(project, base, builder_head)
        gh = self.platform_gh(head=builder_head, merge_sha=merge_sha,
                              comments=[self.as_comment(500, comment)],
                              runs=runs, artifacts=artifacts, downloads=downloads)
        self.clear_db(project)  # 无既有本机库；账本只能来自 git + API
        self.assertIsNotNone(events.emit("verify", "tests", "ok", trace_id=BRANCH,
                                         outputs={"marker": POISON}))  # 本机链毒化样本：不得入账本
        built = ledger.build_ledger(305, gh=gh, cwd=project)
        self.assertEqual(gh.writes, [], "build 不得做任何写操作")
        self.assertEqual(events_db.verify(), [])

        # 顶层事实、机器判定与批准者来自已导入事件，而非夹具直填
        self.assertEqual(built["schema_version"], 1)
        self.assertEqual((built["repository"], built["pr"], built["trace_id"]), (REPO, 305, BRANCH))
        self.assertEqual((built["head_sha"], built["merge_sha"], built["merged_at"]),
                         (builder_head, merge_sha, TS2))
        self.assertEqual((built["class"], built["risk"]), ("K7", "R3"))
        self.assertEqual(built["approval"], {"approver": "alice", "approver_type": "User",
                                             "approval_commit": builder_head, "approval_bound": True})
        self.assertEqual(built["missing"], [])

        # stages 覆盖三类证据：运行记录摘要（C3）、CI/route 与 GitHub 原始事件（C5）、评审审计摘要
        summaries = [item for item in built["stages"] if item.get("evidence_kind") == "run_record_summary"]
        self.assertEqual({item["step"] for item in summaries}, {"admit", "executor_round"})
        self.assertTrue(all("head_hash" in item for item in summaries))
        raws = [item for item in built["stages"] if item.get("evidence_kind") == "event"]
        steps = {(item["stage"], item["step"]) for item in raws}
        self.assertIn(("route", "facts"), steps)
        self.assertIn(("merge", "github.merge"), steps)
        self.assertTrue(all(item["source"].startswith(("ci:", "github:")) for item in raws))
        review = next(item for item in built["stages"]
                      if item.get("evidence_kind") == "review_comment_summary")
        self.assertEqual((review["verdict"], review["reviewer"], review["model_basis"]),
                         ("通过", "opencode", "reported"))
        self.assertEqual(review["severity_counts"]["一般"], 1)
        self.assertEqual({item["kind"] for item in review["materials"]},
                         {"task", "diff", "ci", "pr", "pack"})  # T105/C6 精确形状（含 pack）
        pack = next(item for item in review["materials"] if item["kind"] == "pack")
        self.assertEqual(pack["encoding"], "raw_bytes")
        self.assertIn("artifact_sha256", pack["recipe"])

        # references：运行记录按合并 head 的原始字节哈希；评审评论按评论原始字节哈希
        refs = {item["kind"]: item for item in built["references"]}
        record_raw = subprocess.run(["git", "show", f"{merge_sha}:{RECORD_PATH}"], cwd=project,
                                    capture_output=True, check=True).stdout
        self.assertEqual(refs["run_record"], {"kind": "run_record", "ref": f"{RECORD_PATH}@{merge_sha}",
                                              "sha256": digest(record_raw),
                                              "size": len(record_raw), "encoding": "raw_bytes"})
        self.assertEqual(refs["review_comment"]["sha256"], digest(comment.encode("utf-8")))
        self.assertEqual(refs["review_comment"]["ref"], f"{REPO}#305/comments/500")

        # anchors 与 chains：记录锚点 + ci_artifact 锚点 + ci/github 链头；本机原始链不入 chains/sources
        self.assertEqual({item["fixed_in"] for item in built["anchors"]}, {"run_record", "ci_artifact"})
        sources = {item["source"]: item["class"] for item in built["sources"]}
        self.assertEqual({source for source, klass in sources.items() if klass == "ci"},
                         {"ci:9001:1:job_x", "ci:9002:1:job_y"})
        self.assertTrue(any(klass == "github" for klass in sources.values()))
        self.assertTrue(all(klass in ("ci", "github") for klass in sources.values()))
        chain_map = {chain["source"]: chain["head_hash"] for chain in built["chains"]}
        self.assertIn("ci:9002:1:job_y", chain_map)
        self.assertTrue(all(value for value in chain_map.values()))
        self.assertTrue(all(not source.startswith("local") for source in chain_map))

        # 隐私：毒化本机事件、本机路径与构建路径不出现在账本任何位置
        text = canonical(built)
        self.assertNotIn(POISON, text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn(str(project), text)

        # 坏评审样本各报 missing，且不产生评审 stage：缺标记/未来版本/缺字段/错误 head
        audit_body = re.search(r"<!-- harness-review-audit (.*?) -->", comment)[1]
        audit = json.loads(audit_body)
        variants = {
            "marker_missing": comment.split("<!-- harness-review-audit")[0] + "<!-- 无审计标记 -->\n",
            "version_unsupported": comment.replace(audit_body, canonical({**audit, "schema_version": 2})),
            "field_incomplete": comment.replace(audit_body, canonical(
                {key: value for key, value in audit.items() if key != "verdict"})),
            "head_mismatch": comment.replace(audit_body, canonical({**audit, "head": "f" * 40})),
        }
        for reason, body in variants.items():
            with self.subTest(reason=reason):
                stale_runs, stale_artifacts, stale_downloads = self.ci_platform(builder_head, packages)
                bad = self.platform_gh(head=builder_head, merge_sha=merge_sha,
                                       comments=[self.as_comment(500, body)],
                                       runs=stale_runs, artifacts=stale_artifacts,
                                       downloads=stale_downloads)
                self.clear_db(project)
                built = ledger.build_ledger(305, gh=bad, cwd=project)
                reasons = [item["reason"] for item in built["missing"] if item["item"] == "review"]
                self.assertIn(reason, reasons)
                self.assertFalse([item for item in built["stages"]
                                  if item.get("evidence_kind") == "review_comment_summary"])

    # ---- 验收 2：关闭未合并不写；只取合并 head 与它的全部尝试；缺材料如实列 missing ----

    def test_only_merged_and_exact_head_are_written(self):
        project, origin, _project_head, merge_sha, _base = self.build_project()
        builder_head, packages = self.build_ci_packages()

        # 关闭未合并：build 明确失败（B52 不在范围），远端 harness-audit 分支从未被创建
        closed = self.platform_gh(head=builder_head, merge_sha=merge_sha, merged=False)
        with self.assertRaises(ledger.LedgerError) as caught:
            ledger.build_ledger(305, gh=closed, cwd=project)
        self.assertIn("未合并", str(caught.exception))  # 原因如实：是 B52 边界，不是合并事实缺失
        self.assertEqual(self.git("ls-remote", str(origin), "refs/heads/harness-audit",
                                  cwd=project, check=False), "")

        # 已合并：除匹配 head 的两个 run 外还有一个其他 head 的运行 → 只导入匹配 head，缺额列 missing
        stale_name = "harness-events-9003-1-job_z"
        runs, artifacts, downloads = self.ci_platform(
            builder_head, packages,
            extra_runs=[{"id": 9003, "run_attempt": 1, "headSha": "e" * 40,
                         "path": ".github/workflows/harness.yml"}],
            extra_artifacts={9003: [{"id": 3, "name": stale_name, "expired": False,
                                     "archive_download_url": "https://dl/stale"}]},
            extra_downloads={"https://dl/stale": self.zip_bytes(
                {f"{stale_name}/harness-events.json": "{}"})})
        gh = self.platform_gh(head=builder_head, merge_sha=merge_sha, runs=runs,
                              artifacts=artifacts, downloads=downloads)
        self.clear_db(project)
        built = ledger.build_ledger(305, gh=gh, cwd=project)
        stale = next(item for item in built["missing"] if item["item"] == "ci_events")
        self.assertEqual(stale["reason"], "head_mismatch")
        self.assertTrue(all(row["source"] != "ci:9003:1:job_z" for row in built["stages"]))
        self.assertEqual([row["source"] for row in built["stages"]].count("ci:9001:1:job_x"), 1)

        # 运行记录只按合并 head：合并后 main 前进并改写同名记录，账本仍用合并 head 的那份
        record_path = project / RECORD_PATH
        mutated = json.loads(record_path.read_text(encoding="utf-8"))
        mutated["exit"] = "stopped"
        mutated["stages"].append({"stage": "dispatch", "step": "escalate", "status": "fail", "ts": TS})
        record_path.write_text(json.dumps(mutated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "篡改样本：main 上的同名记录", cwd=project)
        self.clear_db(project)
        built = ledger.build_ledger(305, gh=gh, cwd=project)
        record_raw = subprocess.run(["git", "show", f"{merge_sha}:{RECORD_PATH}"], cwd=project,
                                    capture_output=True, check=True).stdout
        refs = {item["kind"]: item for item in built["references"]}
        self.assertEqual(refs["run_record"]["ref"], f"{RECORD_PATH}@{merge_sha}")
        self.assertEqual(refs["run_record"]["sha256"], digest(record_raw))
        self.assertTrue(all(item.get("step") != "escalate" for item in built["stages"]
                            if item.get("evidence_kind") == "run_record_summary"))

        # 记录在 main 上被删除也不影响：枚举与内容都必须按合并 head（在最新 head 枚举会漏掉它）
        self.git("rm", "-q", RECORD_PATH, cwd=project)
        self.git("commit", "-q", "-m", "删除样本：main 上没有运行记录", cwd=project)
        self.clear_db(project)
        built = ledger.build_ledger(305, gh=gh, cwd=project)
        refs = {item["kind"]: item for item in built["references"]}
        self.assertIn("run_record", refs)
        self.assertEqual(refs["run_record"]["sha256"], digest(record_raw))
        self.assertFalse([item for item in built["missing"] if item["item"] == "run_record"])

        # 缺材料：没有评审评论、CI artifact 过期 → 各报 missing，账本照常复原其余环节
        runs, artifacts, downloads = self.ci_platform(
            builder_head, packages,
            extra_runs=[{"id": 9004, "run_attempt": 1, "headSha": builder_head,
                         "path": ".github/workflows/harness.yml"}],
            extra_artifacts={9004: [{"id": 4, "name": "harness-events-9004-1-job_e", "expired": True,
                                     "archive_download_url": "https://dl/expired"}]})
        gh = self.platform_gh(head=builder_head, merge_sha=merge_sha, runs=runs,
                              artifacts=artifacts, downloads=downloads)
        self.clear_db(project)
        built = ledger.build_ledger(305, gh=gh, cwd=project)
        reasons = {(item["item"], item["reason"]) for item in built["missing"]}
        self.assertIn(("review", "not_found"), reasons)
        self.assertIn(("ci_events", "artifact_expired"), reasons)
        self.assertFalse([item for item in built["stages"]
                          if item.get("evidence_kind") == "review_comment_summary"])
        self.assertTrue(built["chains"], "缺环节不剥夺其余环节的链与锚点")

    # ---- 验收 3：bare remote 上的追加语义：幂等、冲突不覆盖、竞争有限重试、禁强推 ----

    def test_append_conflict_and_concurrent_writers(self):
        _project, origin, *_ = self.build_project()
        ledger_a = {"schema_version": 1, "repository": REPO, "pr": 305, "trace_id": BRANCH,
                    "merged_at": "2026-03-04T05:06:07Z",
                    "chains": [{"source": "ci:1:1:job", "trace_id": BRANCH, "head_hash": "h1"}]}
        ledger_b = {**ledger_a, "pr": 306, "merged_at": "2026-04-04T05:06:07Z"}
        observed: list[list[str]] = []
        real_run = subprocess.run

        def recording_run(argv, *args, **kwargs):
            if "push" in [str(item) for item in argv[:6]]:
                observed.append([str(item) for item in argv])
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(ledger.subprocess, "run", recording_run):
            stub = FakeGh()
            first = ledger.publish_ledger(ledger_a, gh=stub)
            self.assertTrue(first["ok"], first["findings"])
            self.assertTrue(first["updated"])
            self.assertTrue(ledger.publish_ledger(ledger_b, gh=stub)["ok"])

            def branch_bytes(path: str) -> bytes:
                done = subprocess.run(["git", "-C", str(origin), "show", f"harness-audit:{path}"],
                                      capture_output=True, check=False)
                self.assertEqual(done.returncode, 0, done.stderr)
                return done.stdout

            self.assertEqual(branch_bytes("2026/305.json"), canonical(ledger_a).encode("utf-8") + b"\n")
            self.assertEqual(branch_bytes("2026/306.json"), canonical(ledger_b).encode("utf-8") + b"\n")
            log = self.git("log", "--format=%H", "harness-audit", cwd=origin).split()
            self.assertEqual(len(log), 2, "两次追加恰好两个提交")

            # 同 PR 相同字节幂等：不新增提交，commit 即既有链头
            again = ledger.publish_ledger(ledger_a, gh=stub)
            self.assertTrue(again["ok"] and not again["updated"], again["findings"])
            self.assertEqual(again["commit"], log[0], "幂等命中：commit 即当前分支头，不新增提交")
            self.assertEqual(self.git("log", "--format=%H", "harness-audit", cwd=origin).split(), log)

            # 同 PR 不同字节冲突：不覆盖旧文件、不新增提交、明确报告覆盖检测来自 writer 自查
            conflict = ledger.publish_ledger({**ledger_a, "risk": "R2"}, gh=stub)
            self.assertFalse(conflict["ok"])
            self.assertTrue(conflict["conflict"])
            finding = next(item for item in conflict["findings"] if item["code"] == "ledger_conflict")
            self.assertIn("不覆盖", finding["detail"])
            self.assertEqual(branch_bytes("2026/305.json"), canonical(ledger_a).encode("utf-8") + b"\n")
            self.assertEqual(self.git("log", "--format=%H", "harness-audit", cwd=origin).split(), log)

            # 推送竞争：第一次推送被拒 → fetch 后重试成功
            real_push = ledger._push

            def racing_push(work, commit):
                racing_push.calls += 1
                if racing_push.calls == 1:
                    return False, "! [rejected] harness-audit -> harness-audit (non-fast-forward)"
                return real_push(work, commit)

            racing_push.calls = 0
            with mock.patch.object(ledger, "_push", racing_push):
                raced = ledger.publish_ledger(
                    {**ledger_a, "pr": 307, "merged_at": "2026-05-04T05:06:07Z"}, gh=stub)
            self.assertTrue(raced["ok"], raced["findings"])
            self.assertEqual(racing_push.calls, 2, "被拒后恰好重试一次")

            # 重试耗尽：报告失败且远端不变（绝不强推）
            def failing_push(work, commit):
                return False, "! [rejected] harness-audit -> harness-audit (non-fast-forward)"

            with mock.patch.object(ledger, "_push", failing_push):
                exhausted = ledger.publish_ledger(
                    {**ledger_a, "pr": 308, "merged_at": "2026-06-04T05:06:07Z"}, gh=stub)
            self.assertFalse(exhausted["ok"])
            self.assertIn("push_exhausted", [item["code"] for item in exhausted["findings"]])

        # 禁强推：所有 push 调用都没有 --force/-f/--delete；历史提交与旧文件始终未变
        self.assertTrue(observed)
        for argv in observed:
            self.assertFalse({"--force", "-f", "--delete"} & set(argv), argv)
        final_log = self.git("log", "--format=%H", "harness-audit", cwd=origin).split()
        self.assertEqual(final_log[0], raced["commit"])
        self.assertEqual(final_log[1], log[0])
        self.assertEqual(final_log[2], log[1])
        self.assertEqual(len(final_log), 3)
        self.assertEqual(branch_bytes("2026/305.json"), canonical(ledger_a).encode("utf-8") + b"\n")

    # ---- 验收 4：锚点评论同标记更新而非新增；ruleset 禁删/强推；受信任 job 最小权限与失败隔离 ----

    def test_anchor_comment_ruleset_and_privacy(self):
        self.build_project()
        built = {"schema_version": 1, "repository": REPO, "pr": 305, "trace_id": BRANCH,
                 "merged_at": "2026-03-04T05:06:07Z",
                 "chains": [{"source": "ci:9:1:job", "trace_id": BRANCH, "head_hash": "c" * 64},
                            {"source": f"github:305:{'a' * 16}", "trace_id": BRANCH,
                             "head_hash": "d" * 64}]}
        stub = FakeGh()
        first = ledger.publish_ledger(built, gh=stub)
        self.assertTrue(first["ok"], first["findings"])
        self.assertEqual(first["comment"], "created")
        self.assertEqual(len(stub.comments), 1, "首次发布只新增一条锚点评论")
        body = stub.comments[0]["body"]
        self.assertIn(f"<!-- {ledger.ANCHOR_PREFIX}305 -->", body)
        self.assertIn(f"`{ledger.AUDIT_BRANCH}`", body)
        self.assertIn("`2026/305.json`", body)
        self.assertIn(first["commit"], body)
        self.assertIn(first["sha256"], body)
        for chain in built["chains"]:
            self.assertIn(chain["source"], body)
            self.assertIn(chain["head_hash"], body)

        # 第二次发布（同字节幂等命中）：仍是同一条评论被更新，而非新增
        second = ledger.publish_ledger(built, gh=stub)
        self.assertTrue(second["ok"] and not second["updated"])
        self.assertEqual(second["comment"], "updated")
        self.assertEqual(len(stub.comments), 1)
        self.assertEqual([method for method, _, _ in stub.writes], ["POST", "PATCH"])
        self.assertEqual(stub.writes[1][1].rpartition("/")[2], str(stub.comments[0]["id"]))

        # 评论 API 失败：只记发现，不影响账本写入结果（观察失败与业务分离）
        broken = FakeGh()
        with mock.patch.object(broken, "api", side_effect=RuntimeError("HTTP 403")):
            degraded = ledger.publish_ledger({**built, "pr": 306, "merged_at": "2026-04-04T05:06:07Z"},
                                             gh=broken)
        self.assertTrue(degraded["ok"])
        self.assertIsNone(degraded["comment"])
        self.assertIn("comment", [item["code"] for item in degraded["findings"]])

        # ruleset 模板：禁删除/强推两条规则；只有这两条，不宣称 ruleset 独自保证只追加
        ruleset = json.loads((ENGINE_REPO / "templates/.github/rulesets/harness-audit.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["conditions"]["ref_name"]["include"], [ledger.AUDIT_BRANCH])
        self.assertEqual([rule["type"] for rule in ruleset["rules"]], ["deletion", "non_fast_forward"])
        self.assertEqual(ruleset["bypass_actors"], [])

        # 工作流模板：受信任账本 job 只在 push 到 main 运行，默认分支引擎、最小写权限、失败隔离
        text = (ENGINE_REPO / "templates/.github/workflows/harness.yml").read_text(encoding="utf-8")
        lines = text.splitlines()
        start = next(index for index, line in enumerate(lines) if line.strip() == "audit-ledger:")
        end = next((index for index in range(start + 1, len(lines))
                    if re.match(r"^  \S", lines[index])), len(lines))
        job = "\n".join(lines[start:end])
        self.assertIn("github.event_name == 'push'", job)
        self.assertIn("github.ref == 'refs/heads/main'", job)
        permissions = job.split("permissions:", 1)[1].split("steps:", 1)[0]
        self.assertEqual(sorted(line.strip() for line in permissions.splitlines() if line.strip()),
                         ["contents: write", "pull-requests: write"])
        self.assertNotIn("issues: write", permissions)
        self.assertNotIn("administration", permissions)
        checkout = job.split("steps:", 1)[1].split("- name: Publish merge ledger", 1)[0]
        self.assertIn("actions/checkout@v7", checkout)
        self.assertNotIn("ref:", checkout)  # 检出不 pin 任何 PR ref：引擎来自默认分支
        self.assertIn("continue-on-error: true", job)
        self.assertIn("GH_TOKEN: ${{ github.token }}", job)
        self.assertIn("GH_REPO: ${{ github.repository }}", job)
        self.assertIn('python .harness/engine/reports/ledger.py --head "$GITHUB_SHA"', job)

    # ---- 步骤 2：失败隔离与范围——事件关闭如实降级、writer 与观察层解耦、账本不注册 CLI ----

    def test_events_disabled_degrades_honestly_and_writer_is_isolated(self):
        project, origin, _project_head, merge_sha, base = self.build_project()
        builder_head, packages = self.build_ci_packages()
        runs, artifacts, downloads = self.ci_platform(builder_head, packages)
        comment = self.review_comment(project, base, builder_head)
        gh = self.platform_gh(head=builder_head, merge_sha=merge_sha,
                              comments=[self.as_comment(500, comment)],
                              runs=runs, artifacts=artifacts, downloads=downloads)
        self.clear_db(project)
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}):
            built = ledger.build_ledger(305, gh=gh, cwd=project)
        # 观察层关闭：sync 采不到合并/批准事实，如实列 missing；CI 包导入不依赖 emit 开关，
        # 运行记录摘要与评审摘要来自 git/API，照常复原
        reasons = {(item["item"], item["reason"]) for item in built["missing"]}
        self.assertIn(("events", "disabled"), reasons)
        self.assertIn(("approval", "not_found"), reasons)
        self.assertTrue(all(not source.startswith("github:") for source in
                            (item["source"] for item in built["chains"])))
        self.assertEqual((built["class"], built["risk"]), ("K7", "R3"))
        self.assertEqual({item["step"] for item in built["stages"]
                          if item.get("evidence_kind") == "run_record_summary"},
                         {"admit", "executor_round"})
        self.assertTrue([item for item in built["stages"]
                         if item.get("evidence_kind") == "review_comment_summary"])

        # writer 与观察层解耦：事件关闭时账本照常追加、锚点评论照常写入
        stub = FakeGh()
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}):
            result = ledger.publish_ledger(built, gh=stub)
        self.assertTrue(result["ok"], result["findings"])
        self.assertEqual(result["comment"], "created")
        done = subprocess.run(["git", "-C", str(origin), "show", "harness-audit:2026/305.json"],
                              capture_output=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, canonical(built).encode("utf-8") + b"\n")

        # 范围：账本不注册 CLI（C6：只被受信任工作流调用）；隐私边界断言见验收 1（毒化样本/本机路径）
        self.assertNotIn("ledger", cli.COMMANDS)


if __name__ == "__main__":
    unittest.main()
