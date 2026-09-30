"""B59 评审校准：真实历史样本清单的装载、review_calibrate 的打分机制与 bin/dispatch review-calibrate 子命令。

打分用假评审方（脚本化输出）走真实的 run_reviewer → parse_output 通道，在隔离的最小 git 项目里跑，
不碰网络也不碰真实 PR。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch, review


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.rstrip("\n")


def verdict_line(verdict: str) -> str:
    return json.dumps({"verdict": verdict, "summary": "校准假评审", "findings": []}, ensure_ascii=False)


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


def make_project(tmp: Path) -> list[str]:
    """隔离的最小项目：bare origin + main 上两个提交（作为样本 head），返回两个提交号。"""
    origin = tmp / "origin.git"
    git("init", "--bare", "-q", str(origin), cwd=tmp)
    work = tmp / "work"
    git("init", "-b", "main", "-q", str(work), cwd=tmp)
    heads = []
    for index, text in enumerate(("v1\n", "v2\n"), 1):
        (work / "app.txt").write_text(text, encoding="utf-8")
        git("add", "app.txt", cwd=work)
        git("-c", "user.name=calibration", "-c", "user.email=calibration@example.com",
            "commit", "-qm", f"c{index}", cwd=work)
        heads.append(git("rev-parse", "HEAD", cwd=work))
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return heads


class ReviewCalibrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.heads = make_project(self.tmp)
        self.manifest = self.tmp / "samples.json"
        self.write_manifest([("7", self.heads[0], "不通过", "r1"), ("8", self.heads[1], "通过", "r2")])

    def write_manifest(self, samples: list[tuple[str, str, str, str]]) -> None:
        entries = [{"pr": int(pr), "head": head, "expected": expected, "reason": reason}
                   for pr, head, expected, reason in samples]
        self.manifest.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def run_calibration(self, answers: list[str]) -> Path:
        output = self.tmp / "report.md"
        code = review.review_calibrate(None, self.manifest, output, root=self.tmp / "work",
                                reviewer=ScriptedReviewer(answers))
        self.assertEqual(code, 0)
        return output

    def test_scoring_and_errors(self):
        # 按样本期望回答：TPR＝TNR＝1.0，没有偏差。
        report = self.run_calibration([verdict_line("不通过"), verdict_line("通过")]).read_text(encoding="utf-8")
        self.assertEqual(report.count("1.000（1/1）"), 2)
        self.assertIn("偏差 0 条", report)
        # 全部反着答：TPR＝TNR＝0.0，两条样本都记偏差。
        report = self.run_calibration([verdict_line("通过"), verdict_line("不通过")]).read_text(encoding="utf-8")
        self.assertEqual(report.count("0.000（0/1）"), 2)
        self.assertIn("偏差 2 条", report)
        self.assertEqual(report.count("| 是 |"), 2)
        # 评审方失败（没有输出）进 errors、不进分母：4 条样本里 2 条失败，其余 2 条照常计分。
        self.write_manifest([("7", self.heads[0], "不通过", "r1"), ("8", self.heads[1], "通过", "r2"),
                             ("9", self.heads[0], "不通过", "r3"), ("10", self.heads[1], "通过", "r4")])
        report = self.run_calibration(["", verdict_line("通过"), verdict_line("不通过"), ""]).read_text(encoding="utf-8")
        self.assertEqual(report.count("1.000（1/1）"), 2)
        self.assertIn("errors 2 条", report)
        self.assertEqual(report.count("评审方失败："), 2)
        self.assertEqual(report.count("未计分"), 2)

    def test_manifest_loading_and_real_samples(self):
        # 缺字段与非法值都要报明确错误，并指明是哪条样本。
        cases = [
            ([{"pr": 1, "head": "a" * 40, "expected": "不通过"}], "reason"),
            ([{"pr": 1, "head": "a" * 40, "expected": "差不多", "reason": "r"}], "expected"),
            ([{"pr": 1, "head": "abc", "expected": "通过", "reason": "r"}], "head"),
            ([{"pr": 0, "head": "a" * 40, "expected": "通过", "reason": "r"}], "pr"),
            ([{"head": "a" * 40, "expected": "通过", "reason": "r"}], "pr"),
        ]
        for entries, keyword in cases:
            path = self.tmp / "bad.json"
            path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                review.load_samples(path)
            self.assertIn(keyword, str(caught.exception))
            self.assertIn("第 1 条", str(caught.exception))
        self.assertEqual(len(review.load_samples(self.manifest)), 2)
        # 真实清单：6 条样本，PR 号、head 与期望与任务书的裁决表逐条一致（#41/#43 为合并时的 head）。
        samples = review.load_samples(review.CALIBRATION_SAMPLES)
        self.assertEqual([item["pr"] for item in samples], [49, 49, 51, 41, 33, 43])
        self.assertEqual([item["head"] for item in samples], [
            "bce25432cbc2644d6c11336e302f3e1b97f1c21b",
            "639bf1cedb7729aa4d6d7d1fa4426c60a40464b7",
            "2eb3cbef73ed69405b1d521c556adef44084504b",  # B75：#51 样本指向第二轮修复 head
            "80203b88605e8cdd30ff3c1c0cb175e64dc422f9",
            "b8e48fb99223faa7615b15707e214d2478db4366",
            "7fcd54bf3a2c4fa4bde18e9e454689b5e9f196ee",
        ])
        self.assertEqual([item["expected"] for item in samples],
                         ["不通过", "通过", "不通过", "通过", "不通过", "通过"])
        self.assertTrue(all(item["reason"].strip() for item in samples))

    def test_cli_wiring(self):
        # 子命令注册与缺省输出路径。
        with mock.patch.object(review, "review_calibrate", return_value=0) as review_calibrate:
            self.assertEqual(dispatch.main(["review-calibrate"]), 0)
            review_calibrate.assert_called_once_with(None, review.CALIBRATION_SAMPLES,
                                              dispatch.ROOT / "build" / "review" / "calibration-report.md")
        with mock.patch.object(review, "review_calibrate", return_value=0) as review_calibrate:
            self.assertEqual(dispatch.main(["review-calibrate", "--reviewer", "pi",
                                            "--output", "build/x/report.md"]), 0)
            review_calibrate.assert_called_once_with("pi", review.CALIBRATION_SAMPLES, dispatch.ROOT / "build/x/report.md")
        # 清单装载失败时明确报错并返回 2。
        with mock.patch.object(review, "review_calibrate", side_effect=ValueError("校准样本第 1 条：expected 非法")):
            self.assertEqual(dispatch.main(["review-calibrate"]), 2)
        # 既有子命令解析逐字不变。
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
