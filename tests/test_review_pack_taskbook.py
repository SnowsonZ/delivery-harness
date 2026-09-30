"""T114 回归：评审材料包的任务书探测回退（待办 B68）。

夹具在匿名临时 git 仓库（含匿名 bare 远端）里经真实产品入口 review.review_pr 组装评审材料，
只隔离外部副作用（评审方二进制与 gh）；断言实际写盘的 build/review/task.md 与评论里的
C6 材料清单。既有探测成功路径（run_check.scope 命中）与「无」现状在此一并锁定。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from engine.agents import review
from engine.core import events_db

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
AUDIT_MARK = "<!-- harness-review-audit "


class FakeReviewer:
    """假评审方：固定输出可解析的「通过」结论，不启动真实评审方二进制。"""

    name = "opencode"
    env: ClassVar[dict] = {}

    def __init__(self):
        self.model_name = None

    def argv(self, prompt, workspace, output):
        script = ("import json, sys; sys.stdout.write(json.dumps("
                  "{'verdict': '通过', 'summary': '夹具结论', 'findings': []}, ensure_ascii=False))")
        return [sys.executable, "-c", script]

    def read(self, stdout, output):
        return stdout, "", "unknown"


class FakeReviewGitHub:
    """评审用 gh 桩：PR 视图按夹具应答，评论捕获正文；CI 检查为空列表。"""

    def __init__(self, pr_json: dict):
        self.pr_json = pr_json
        self.comments: list[str] = []

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        joined = " ".join(argv)
        if joined.startswith("gh pr view"):
            return json.dumps(self.pr_json)
        if joined.startswith("gh pr checks") and "name,state,link" in joined:
            return json.dumps([])
        if joined.startswith("gh pr edit") and "--remove-label" in joined:
            return ""
        raise AssertionError(f"未预期的 gh 调用：{joined}")

    def comment(self, pr, body, label=None):
        self.comments.append(body)
        return f"https://example.invalid/repo/pull/{pr}#issuecomment-9"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReviewPackTaskbookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-review-pack-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        origin = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", ".", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("fetch", "-q", "origin")
        # 事件写进夹具仓库的状态目录，不碰真实库（review_pr 的观察旁路按真实路径走）
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("HARNESS_EVENTS", None)

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
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def taskbook_text(self, task: str) -> str:
        return ("---\n"
                f"task: {task}\nclass: K7\nrisk: R3\ndesigner: codex\nsize: small\n"
                "architecture: false\nspec_refs: []\nno_spec_reason: 测试夹具\n"
                "budget:\n  wall_clock_min: 5\n  retries: 0\n  ci_rounds: 1\n  tokens: null\n"
                "rollback: git revert\n---\n\n# 任务：夹具\n\n验收要求写在这里。\n")

    def run_review(self, title: str, body: str, branch: str) -> tuple[int, str, Path, str]:
        """把当前 HEAD 推为 origin 的 pull/14/head，经真实 review_pr 组装材料。
        返回 (退出码, 评论正文, 材料目录, 评审的提交)。"""
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("push", "-q", "-f", "origin", "HEAD:refs/pull/14/head")
        pr_json = {"title": title, "body": body, "headRefName": branch, "headRefOid": head,
                   "baseRefName": "main", "state": "OPEN", "mergeCommit": None}
        gh = FakeReviewGitHub(pr_json)
        with mock.patch.object(review, "make_reviewer", lambda name: FakeReviewer()), \
                contextlib.redirect_stdout(io.StringIO()):
            code = review.review_pr(14, "opencode", root=self.repo, github=gh)
        folder = review.review_workspace(self.repo) / "build" / "review"
        return code, (gh.comments[0] if gh.comments else ""), folder, head

    def task_material(self, body: str) -> dict:
        audit = json.loads(body.split(AUDIT_MARK, 1)[1].split(" -->", 1)[0])
        return next(item for item in audit["materials"] if item["kind"] == "task")

    # ---------- 验收：正文引用且探测未命中 → 材料包含任务书全文 ----------

    def test_body_reference_included(self):
        rel = "docs/plans/task-907-events-docs.md"
        text = self.taskbook_text("T907")
        self.commit({rel: text}, "taskbook on main")
        self.git("push", "-q", "origin", "main")
        # 探测未命中的形状：分支名不带 task/ 前缀，提交没有 Task trailer，改动不含任务书
        self.git("checkout", "-q", "-b", "docs/note-fix")
        self.commit({"docs/note.md": "整理后的说明\n"}, "docs only")

        code, body, folder, head = self.run_review("文档整理", "按 `docs/plans/task-907-events-docs.md` 执行。",
                                                   "docs/note-fix")
        self.assertEqual(code, 0)
        self.assertEqual((folder / "task.md").read_text(encoding="utf-8"), text)  # 全文，与探测成功同格式
        data = (folder / "task.md").read_bytes()
        self.assertEqual(self.task_material(body),
                         {"kind": "task", "ref": f"{rel}@{head}", "sha256": sha(data),
                          "size": len(data), "encoding": "utf8", "recipe": {"id": "task_v1"}})

    # ---------- 验收：无引用/文件不存在维持「无」；探测成功路径逐字不变 ----------

    def test_no_reference_and_existing_paths_unchanged(self):
        rel = "docs/plans/task-907-events-docs.md"
        text = self.taskbook_text("T907")
        self.commit({rel: text}, "taskbook on main")
        self.git("push", "-q", "origin", "main")

        # 正文没有任务书引用：维持「无」
        self.git("checkout", "-q", "-b", "docs/no-ref")
        self.commit({"docs/note.md": "甲\n"}, "no reference")
        code, body, folder, _ = self.run_review("无引用的改动", "只是整理文字。", "docs/no-ref")
        self.assertEqual(code, 0)
        self.assertEqual((folder / "task.md").read_text(encoding="utf-8"), "无")
        self.assertEqual(self.task_material(body)["ref"], "none")

        # 引用的文件在 base 上不存在：维持「无」
        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", "docs/missing-ref")
        self.commit({"docs/note.md": "乙\n"}, "missing reference")
        code, body, folder, _ = self.run_review("引用不存在的任务书", "见 docs/plans/task-999-absent.md。",
                                                "docs/missing-ref")
        self.assertEqual(code, 0)
        self.assertEqual((folder / "task.md").read_text(encoding="utf-8"), "无")
        self.assertEqual(self.task_material(body)["ref"], "none")

        # 既有探测成功（分支名命中 + 任务书随 PR 提交）：task.md 仍是 PR 里那份全文，正文再多引用也不回退
        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", "task/908-fix")
        in_pr_rel = "docs/plans/task-908-fix.md"
        in_pr_text = self.taskbook_text("T908")
        head = self.commit({"docs/note.md": "丙\n", in_pr_rel: in_pr_text}, "in-PR taskbook")
        code, body, folder, _ = self.run_review(
            "探测命中的改动", "正文还引用了 docs/plans/task-907-events-docs.md，不改变既有探测。", "task/908-fix")
        self.assertEqual(code, 0)
        self.assertEqual((folder / "task.md").read_text(encoding="utf-8"), in_pr_text)
        self.assertEqual(self.task_material(body)["ref"], f"{in_pr_rel}@{head}")


if __name__ == "__main__":
    unittest.main()
