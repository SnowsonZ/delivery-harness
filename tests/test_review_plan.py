"""T120/B54 任务拆分评审：材料收集与缺失清单、评审执行与产物落盘、review-plan 子命令接线。

评审方一律用假评审方（脚本化输出）走真实的 run_reviewer → parse_output 通道，在隔离的最小 git 项目里跑，
不碰网络也不碰真实 PR；材料收集以夹具文档为中心：链接的 docs 材料、未被链接的任务书与缺失链接都要核对。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli
from engine.agents import dispatch, plan_review, review


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.rstrip("\n")


def verdict_line(verdict: str, findings: list[dict] | None = None) -> str:
    return json.dumps({"verdict": verdict, "summary": "拆分评审假评审", "findings": findings or []},
                      ensure_ascii=False)


class ScriptedReviewer(review.Reviewer):
    """假评审方：不跑模型，按脚本依次输出预写的评审批次；空串表示评审方失败（没有任何输出）。"""

    name = "scripted"

    def __init__(self, answers: list[str]):
        self.answers = list(answers)

    def argv(self, prompt, workspace, output):
        answer = self.answers.pop(0) if self.answers else ""
        return [sys.executable, "-c", f"import sys; sys.stdout.write({answer!r})"]

    def read(self, stdout, output):
        return stdout, "scripted-model", "explicit_request"


def make_project(tmp: Path) -> Path:
    """隔离的最小项目：bare origin + main 上一个提交，返回工作仓库路径。"""
    origin = tmp / "origin.git"
    git("init", "--bare", "-q", str(origin), cwd=tmp)
    work = tmp / "work"
    git("init", "-b", "main", "-q", str(work), cwd=tmp)
    (work / "app.txt").write_text("v1\n", encoding="utf-8")
    git("add", "app.txt", cwd=work)
    git("-c", "user.name=plan", "-c", "user.email=plan@example.com", "commit", "-qm", "c1", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return work


def write_docs(work: Path) -> Path:
    """拆分评审夹具：计划文档链接两份存在的 docs 材料与一份缺失的任务书，另有未被链接的任务书。"""
    plans = work / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "task-101-a.md").write_text("# 任务书A\n", encoding="utf-8")
    (plans / "task-102-b.md").write_text("# 任务书B（没有任何文档链接它）\n", encoding="utf-8")
    (work / "docs" / "splitting.md").write_text("# 拆分流程\n", encoding="utf-8")
    plan = plans / "plan.md"
    plan.write_text("# 计划\n\n"
                    "- [任务书A](task-101-a.md)\n"
                    "- [流程](../splitting.md)\n"
                    "- [缺失的任务书](task-999-missing.md)\n"
                    "- [外链](https://example.com/x.md)\n", encoding="utf-8")
    return plan


class ReviewPlanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.work = make_project(self.tmp)
        self.plan = write_docs(self.work)

    def test_material_collection_and_missing_list(self):
        output = self.tmp / "verdict.md"
        self.assertEqual(plan_review.review_plan(self.plan, output, root=self.work,
                                                 reviewer=ScriptedReviewer([verdict_line("通过")])), 0)
        report = output.read_text(encoding="utf-8")
        # 收集：链接的 docs 材料 + 未被链接的任务书（docs/plans/ 下全部 task-*.md）都进材料清单。
        for rel in ("docs/plans/task-101-a.md", "docs/plans/task-102-b.md", "docs/splitting.md"):
            self.assertIn(rel, report, rel)
        self.assertIn("材料清单（3 份）", report)
        # 外链既不收集也不报缺失。
        self.assertNotIn("https://example.com/x.md", report)
        # 缺失清单：链接目标不存在时进报告并标注，不静默跳过。
        self.assertIn("docs/plans/task-999-missing.md", report)
        self.assertIn("缺失清单：`docs/plans/task-999-missing.md`", report)
        self.assertIn("未参与本次评审", report)
        # 材料全文按仓库相对路径落进只读评审工作区（评审方读得到）。
        workspace = review.review_workspace(self.work)
        for rel in ("docs/plans/task-101-a.md", "docs/plans/task-102-b.md", "docs/splitting.md"):
            self.assertTrue((workspace / "build" / "review" / "materials" / rel).exists(), rel)
        self.assertTrue((workspace / "build" / "review" / "pr.md").exists())
        # 设计文档本身不存在：写缺失报告并返回 1，不静默跳过。
        gone = self.tmp / "gone.md"
        self.assertEqual(plan_review.review_plan(gone, self.tmp / "gone-report.md", root=self.work,
                                                 reviewer=ScriptedReviewer([])), 1)
        report = (self.tmp / "gone-report.md").read_text(encoding="utf-8")
        self.assertIn("gone.md", report)
        self.assertIn("设计文档不存在", report)
        self.assertIn("缺失清单", report)

    def test_verdict_written_no_github_post(self):
        findings = [{"severity": "严重", "location": "docs/plans/plan.md:3", "problem": "缺一份任务书",
                     "fix": "补齐任务书"}]
        output = self.tmp / "verdict.md"
        with mock.patch.object(review.dispatch, "GitHub") as github:
            code = plan_review.review_plan(self.plan, output, root=self.work,
                                           reviewer=ScriptedReviewer([verdict_line("不通过", findings)]))
            github.assert_not_called()  # 拆分评审不评论任何 GitHub PR
        self.assertEqual(code, 0)
        report = output.read_text(encoding="utf-8")
        self.assertIn("## 结论：不通过", report)
        self.assertIn("拆分评审假评审", report)
        self.assertIn("| 严重 | docs/plans/plan.md:3 | 缺一份任务书 | 补齐任务书 |", report)
        self.assertIn("docs/plans/task-101-a.md", report)  # 结论之外还有材料清单

    def test_cli_wiring(self):
        # 子命令注册（cli.py COMMANDS 通用路由）与缺省输出路径（相对仓库根解析）。
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}), \
                mock.patch.object(plan_review, "review_plan", return_value=0) as plan:
            self.assertEqual(cli.main(["review-plan", "docs/plans/x.md"]), 0)
            plan.assert_called_once_with(dispatch.ROOT / "docs/plans/x.md",
                                         dispatch.ROOT / "build" / "review" / "plan-verdict.md")
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}), \
                mock.patch.object(plan_review, "review_plan", return_value=0) as plan:
            self.assertEqual(cli.main(["review-plan", "docs/plans/x.md", "--output", "build/v.md"]), 0)
            plan.assert_called_once_with(dispatch.ROOT / "docs/plans/x.md", dispatch.ROOT / "build/v.md")
        # 既有子命令解析逐字不变。
        with mock.patch.object(review, "review_calibrate", return_value=0) as calibrate:
            self.assertEqual(dispatch.main(["review-calibrate"]), 0)
            calibrate.assert_called_once_with(None, review.CALIBRATION_SAMPLES,
                                              dispatch.ROOT / "build" / "review" / "calibration-report.md")
        with mock.patch.object(review, "review_pr", return_value=0) as review_pr:
            self.assertEqual(dispatch.main(["review", "14", "--reviewer", "opencode"]), 0)
            review_pr.assert_called_once_with(14, "opencode")
        with mock.patch.object(review, "review_pending", return_value=[3]) as pending:
            self.assertEqual(dispatch.main(["review", "--pending"]), 0)
            pending.assert_called_once_with(None)
        with mock.patch.object(review, "watch", return_value=0) as watch:
            self.assertEqual(dispatch.main(["review", "--watch", "--interval", "2"]), 0)
            watch.assert_called_once_with(2.0, None)
        with mock.patch.object(dispatch, "status", return_value=0) as status:
            self.assertEqual(dispatch.main(["status"]), 0)
            status.assert_called_once_with()
        with mock.patch.object(dispatch, "stop_all", return_value=0) as stop_all:
            self.assertEqual(dispatch.main(["stop", "--all"]), 0)
            stop_all.assert_called_once_with()
        with mock.patch.object(dispatch, "state_dir", return_value=self.tmp / "state"), \
                mock.patch.object(dispatch, "Dispatcher") as dispatcher, \
                mock.patch.object(dispatch, "Config"), \
                mock.patch.object(dispatch, "GitHub"), \
                mock.patch.object(dispatch.dispatch_host, "PiHost"):
            dispatcher.return_value.run.return_value = 0
            self.assertEqual(dispatch.main(["run", "docs/plans/task-1.md"]), 0)
            dispatcher.return_value.run.assert_called_once_with("docs/plans/task-1.md", False)


if __name__ == "__main__":
    unittest.main()
