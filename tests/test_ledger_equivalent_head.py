"""T705 回归：账本与审计接受「内容相同」的评审 head。

合同制路径允许分支在评审之后只同步 main（diff 字节相同）就沿用原评审（T701 的
signals.same_content）；这样合并的 head 和评审时的 head 一定不同。夹具 git 仓库复现该形状：
评审 head 是 R，main 前进后分支同步 main 得到 H，再以 --no-ff 合并（合并提交的第一个父提交就是
合并前的 main）。账本与审计用同一判定、同一 base（merge_sha^1）：内容相同时评审证据保留、
原 JSON 不改写（head 字段仍是 R）；R 之后内容又有变化（包括只改缩进）仍报 head_mismatch；
浅克隆与 git 出错退回严格比对；不传 same 时行为逐字不变；audit-ledger 的 checkout 检出全部历史。

夹具沿用 tests/test_audit_ledger.py 的模式：假 gh 桩（只读 API + 评论页）、匿名临时 git 仓库、
隔离 events_db.ROOT；评审审计标记由 T105 真实生产者（dispatch_observation.review_audit /
review_materials）构造。不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch_observation
from engine.core import events, events_db
from engine.reports import audit, ledger
from engine.routing import signals
from tests.gh_fakes import FakeGhBase

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
REPO = "owner/repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
BRANCH = "task/705-fixture"
TASKBOOK = "docs/plans/task-705-ledger-equivalent-head.md"
TS = "2026-01-02T03:04:05Z"
MERGED_AT = "2026-03-04T05:06:07Z"
PR_TITLE = "feat：内容相同的评审 head"
PR_BODY = "T705 夹具正文。"
PR = 705


class _Clock:
    """递增冻结时钟：从 2026-01-02T00:01Z 起每次调用前进一分钟，ts 严格递增且可解析。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        moment = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=self.step)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeGh(FakeGhBase):
    """账本/审计共用的只读 gh 桩：pulls/reviews/commits/评论/议题/运行足够走完 sync 与 load_ci。

    缺失提交抛 KeyError、缺失评论抛 StopIteration；runs 不按 branch 过滤；repo() 不记录调用。
    """

    runs_filter_by_branch = False
    paginate_runs = True
    paginate_artifacts = True
    missing_pr = "key"
    missing_commit = "key"
    missing_comment = "stop"

    def repo(self) -> str:
        return REPO


class LedgerEquivalentHeadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-ledger-equivalent-head-"))
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

    def fresh_repo(self) -> Path:
        path = self.tmp / "app"
        path.mkdir()
        self.git("init", "-q", "-b", "main", cwd=path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        (path / TASKBOOK).parent.mkdir(parents=True, exist_ok=True)
        (path / TASKBOOK).write_text("task：T705 内容相同的评审 head（夹具任务书）\n", encoding="utf-8")
        self.git("add", "-A", cwd=path)
        self.git("commit", "-q", "-m", "init", cwd=path)
        return path

    def build_world(self, *, content_shift_after_review: bool = False) -> SimpleNamespace:
        """评审 head R → main 前进（只动别的文件）→ 分支同步 main 得 H → --no-ff 合并。

        content_shift_after_review 时 R 与 H 之间还有一个只改缩进的内容提交（diff 字节必然不同）。
        """
        project = self.fresh_repo()
        self.git("remote", "add", "origin", GITHUB_URL, cwd=project)
        patch = mock.patch.object(events_db, "ROOT", project)
        patch.start()
        self.addCleanup(patch.stop)
        fork = self.git("rev-parse", "HEAD", cwd=project)
        # 任务分支的内容提交：评审时的 head R
        self.git("checkout", "-q", "-b", BRANCH, cwd=project)
        (project / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "feat：T705 内容提交（评审 head R）", cwd=project)
        reviewed = self.git("rev-parse", "HEAD", cwd=project)
        # main 前进：只动别的文件，保证两段 diff 的字节完全相同
        self.git("checkout", "-q", "main", cwd=project)
        (project / "other.txt").write_text("main advance\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "chore：main 前进（合并前的 main）", cwd=project)
        main_before = self.git("rev-parse", "HEAD", cwd=project)
        # 分支同步 main（内容不变）；「内容又有变化」时补一个只改缩进的提交
        self.git("checkout", "-q", BRANCH, cwd=project)
        self.git("merge", "--no-ff", "-q", "-m", "sync：同步 main（diff 字节不变）", "main", cwd=project)
        if content_shift_after_review:
            (project / "feature.py").write_text("def f():\n        return 1\n", encoding="utf-8")
            self.git("add", "-A", cwd=project)
            self.git("commit", "-q", "-m", "style：只改缩进（内容已变，必须重新评审）", cwd=project)
        head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m", f"Merge pull request #{PR} from {BRANCH}\n\nbody",
                 BRANCH, cwd=project)
        merge_sha = self.git("rev-parse", "HEAD", cwd=project)
        # 闭包用的 base 是合并提交的第一个父提交，即合并前的 main（变异用合并提交本身会被本文件抓住）
        self.assertEqual(self.git("rev-parse", f"{merge_sha}^1", cwd=project), main_before)
        return SimpleNamespace(project=project, fork=fork, reviewed=reviewed, head=head,
                               main_before=main_before, merge_sha=merge_sha)

    def review_comment(self, world: SimpleNamespace) -> str:
        """T105 真实生产者构造的评审评论体（评审指向 R，head 字段保持评审时的值）。"""
        folder = world.project / "build" / "review"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "task.md").write_bytes((world.project / TASKBOOK).read_bytes())
        (folder / "diff.patch").write_text("diff --git a/feature.py b/feature.py\n", encoding="utf-8")
        (folder / "ci.md").write_text("CI 摘要", encoding="utf-8")
        (folder / "pr.md").write_text(f"# {PR_TITLE}\n\n{PR_BODY}", encoding="utf-8")
        (folder / "pack.md").write_text("评审包", encoding="utf-8")
        materials = dispatch_observation.review_materials(
            world.project, world.fork, world.reviewed, TASKBOOK, PR)
        summary = dispatch_observation.review_audit(
            trace_id=BRANCH, head=world.reviewed, base=world.fork, reviewer="opencode",
            model="review-model/1", model_basis="reported", designer="codex", implementers=["pi"],
            independent=True, same_host=False, parsed=True, verdict="通过", duration_ms=60000,
            findings=[{"severity": "一般", "location": "engine/x.py:12", "problem": "边界说明",
                       "fix": "补断言"}],
            materials=materials)
        legacy = json.dumps({"verdict": "通过", "reviewer": "opencode", "head": world.reviewed},
                            ensure_ascii=False)
        return ("### 独立评审（试行）：通过\n\n"
                f"<!-- independent-review {legacy} -->\n"
                f"<!-- harness-review-audit {json.dumps(summary, ensure_ascii=False)} -->\n")

    def platform(self, world: SimpleNamespace, comments: list[dict]) -> FakeGh:
        """PR 705 的假平台：合并事实（head=H）+ 评审评论；无 CI 运行（缺额如实列 missing）。"""
        return FakeGh(
            pulls={PR: {"number": PR, "state": "closed", "merged": True, "merged_at": MERGED_AT,
                        "merge_commit_sha": world.merge_sha, "title": PR_TITLE, "body": PR_BODY,
                        "head": {"ref": BRANCH, "sha": world.head},
                        "merged_by": {"login": "alice", "type": "User"}}},
            reviews={PR: [{"state": "APPROVED", "user": {"login": "alice", "type": "User"},
                           "commit_id": world.head}]},
            commits={world.merge_sha: {"parents": [{"sha": world.main_before},
                                                   {"sha": world.head}]}},
            pr_commits={PR: []},
            comments=comments)

    @staticmethod
    def as_comment(comment_id: int, body: str) -> dict:
        return {"id": comment_id, "body": body, "created_at": TS,
                "html_url": f"{GITHUB_URL}/issues/comments/{comment_id}"}

    def shallow_origin(self, world: SimpleNamespace) -> Path:
        """浅克隆用的 bare origin：推送 main 后供 --depth 1 克隆。"""
        origin = self.tmp / "app-shallow-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(world.project), "push", "-q", str(origin), "main"],
                       check=True, capture_output=True)
        return origin

    @staticmethod
    def review_missing(built: dict) -> list[str]:
        return [item["reason"] for item in built["missing"] if item["item"] == "review"]

    # ---- 验收 1：同步 main 后的合并 head 仍保留评审证据，审计同样收录 ----

    def test_synced_head_keeps_review(self):
        world = self.build_world()
        # 前提自检：R 是 H 的祖先，且两段 diff（相对合并前 main）字节相同
        self.assertTrue(signals.same_content(world.reviewed, world.head,
                                             f"{world.merge_sha}^1", world.project))
        gh = self.platform(world, [self.as_comment(500, self.review_comment(world))])
        built = ledger.build_ledger(PR, gh=gh, cwd=world.project)
        self.assertEqual(built["head_sha"], world.head)
        self.assertNotIn("head_mismatch", self.review_missing(built))
        # 评审证据保留：原 JSON 不改写，head 字段仍是评审时的 R
        review = next(item for item in built["stages"]
                      if item.get("evidence_kind") == "review_comment_summary")
        self.assertEqual(review["head"], world.reviewed)
        self.assertEqual(review["base"], world.fork)
        self.assertEqual(review["verdict"], "通过")
        self.assertIn("review_comment", {item["kind"] for item in built["references"]})
        # audit.py 的评审阶段同样收录（同一判定、同一 base）
        report = audit.inspect_pr(PR, gh=self.platform(
            world, [self.as_comment(500, self.review_comment(world))]), cwd=world.project)
        stage = next(item for item in report["stages"] if item.get("stage") == "review")
        self.assertEqual(stage["head"], world.reviewed)

    # ---- 验收 2：R 之后内容又有变化（包括只改缩进）仍报 head_mismatch ----

    def test_changed_content_still_mismatch(self):
        world = self.build_world(content_shift_after_review=True)
        # 前提自检：缩进变化让两段 diff 字节不同（base 用合并提交本身时两边 diff 都为空，本用例抓住）
        self.assertFalse(signals.same_content(world.reviewed, world.head,
                                              f"{world.merge_sha}^1", world.project))
        gh = self.platform(world, [self.as_comment(500, self.review_comment(world))])
        built = ledger.build_ledger(PR, gh=gh, cwd=world.project)
        self.assertIn("head_mismatch", self.review_missing(built))
        self.assertFalse([item for item in built["stages"]
                          if item.get("evidence_kind") == "review_comment_summary"])
        report = audit.inspect_pr(PR, gh=self.platform(
            world, [self.as_comment(500, self.review_comment(world))]), cwd=world.project)
        self.assertFalse([item for item in report["stages"] if item.get("stage") == "review"])

    # ---- 验收 3：浅克隆/git 出错退回严格比对；不传 same 时行为逐字不变 ----

    def test_strict_fallback(self):
        world = self.build_world()
        comment = self.review_comment(world)
        gh = self.platform(world, [self.as_comment(500, comment)])

        # 缺省（same=None）：与原来逐字相同——head 不同报 head_mismatch，head 相同直接采信
        parsed, problem = ledger._parse_review_audit(comment, world.head)
        self.assertIsNone(parsed)
        self.assertEqual((problem["item"], problem["reason"]), ("review", "head_mismatch"))
        parsed_ok, problem_ok = ledger._parse_review_audit(comment, world.reviewed)
        self.assertIsNone(problem_ok)
        self.assertEqual(parsed_ok["head"], world.reviewed)
        missing: list[dict] = []
        self.assertEqual(ledger._review_entries(gh, REPO, PR, world.head, missing), [])
        self.assertEqual([item["reason"] for item in missing if item["item"] == "review"],
                         ["head_mismatch"])

        # 浅克隆（CI 上 fetch-depth 缺省就是它）：历史截断拿不到祖先，退回严格比对，不外泄异常
        shallow = self.tmp / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.shallow_origin(world)}",
                        str(shallow)], capture_output=True, check=True)
        built = ledger.build_ledger(PR, gh=self.platform(
            world, [self.as_comment(500, comment)]), cwd=shallow)
        self.assertIn("head_mismatch", self.review_missing(built))
        self.assertFalse([item for item in built["stages"]
                          if item.get("evidence_kind") == "review_comment_summary"])

        # git 出错（不是仓库）：same_content 返回假，同样退回严格比对
        broken = self.tmp / "not-a-repo"
        broken.mkdir()
        built = ledger.build_ledger(PR, gh=self.platform(
            world, [self.as_comment(500, comment)]), cwd=broken)
        self.assertIn("head_mismatch", self.review_missing(built))

    # ---- 验收 4：audit-ledger 的 checkout 检出全部历史 ----

    def test_ledger_checkout_full_history(self):
        text = (ENGINE_REPO / "templates/.github/workflows/harness.yml").read_text(encoding="utf-8")
        lines = text.splitlines()
        start = next(index for index, line in enumerate(lines) if line.strip() == "audit-ledger:")
        end = next((index for index in range(start + 1, len(lines))
                    if re.match(r"^  \S", lines[index])), len(lines))
        job = "\n".join(lines[start:end])
        checkout = job.split("steps:", 1)[1].split("- name: Publish merge ledger", 1)[0]
        self.assertIn("actions/checkout@v7", checkout)
        self.assertIn("fetch-depth: 0", checkout)  # 浅克隆会让上面的判定在 CI 上永远退回严格比对
        self.assertNotIn("fetch-depth: 0", job.split("- name: Publish merge ledger", 1)[1])


if __name__ == "__main__":
    unittest.main()
