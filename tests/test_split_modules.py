"""T707 拆分的回归断言：体量预算、重新导出同一性、原模块 patch 语义与导入顺序。

对应任务书验收表第 1–4 行：dispatch.py ≤ 660 行、review.py ≤ 680 行、三个新模块各 ≤ 400 行
且 files_over_800 为 0；移出的名字从原模块取到的是同一个对象；在原模块上 patch
（review.make_reviewer、dispatch.state_dir）后调用移出的函数仍然生效；先单独导入新模块
再导入原模块没有循环导入。
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch, dispatch_slots, dispatch_text, review, review_calibration


def line_count(path: Path) -> int:
    """物理行数，与 engine/checks/quality.py 的 measure 同口径。"""
    return len(path.read_text(encoding="utf-8").splitlines())


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.rstrip("\n")


def make_project(tmp: Path) -> tuple[Path, str]:
    """隔离的最小项目：bare origin + main 上一个提交（作为样本 head），返回 (work 目录, head)。"""
    origin = tmp / "origin.git"
    git("init", "--bare", "-q", str(origin), cwd=tmp)
    work = tmp / "work"
    git("init", "-b", "main", "-q", str(work), cwd=tmp)
    (work / "app.txt").write_text("v1\n", encoding="utf-8")
    git("add", "app.txt", cwd=work)
    git("-c", "user.name=calibration", "-c", "user.email=calibration@example.com",
        "commit", "-qm", "c1", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return work, head


class ProbeReviewer(review.Reviewer):
    """探针评审方：make_reviewer 被 patch 后由 review_calibrate 实际使用的假评审方。"""

    name = "probe"

    def argv(self, prompt, workspace, output):
        answer = json.dumps({"verdict": "通过", "summary": "拆分探针", "findings": []}, ensure_ascii=False)
        return [sys.executable, "-c", f"import sys; sys.stdout.write({answer!r})"]

    def read(self, stdout, output):
        return stdout, "probe-model", "explicit_request"


class SplitModulesTest(unittest.TestCase):
    def test_line_budgets(self):
        """验收 1：拆分后的体量预算（B81 阻塞点：质量棘轮 files_over_800）。"""
        agents = Path(dispatch.__file__).parent
        self.assertLessEqual(line_count(agents / "dispatch.py"), 660)
        self.assertLessEqual(line_count(agents / "review.py"), 680)
        for name in ("dispatch_text.py", "dispatch_slots.py", "review_calibration.py"):
            self.assertLessEqual(line_count(agents / name), 400, name)
        # quality.measure 的统计口径（engine 下 **/*.py，物理行数 > 800 才计入）下 files_over_800 为 0。
        engine_root = agents.parent
        oversize = [str(path.relative_to(engine_root)) for path in sorted(engine_root.rglob("*.py"))
                    if line_count(path) > 800]
        self.assertEqual(oversize, [])

    def test_reexports_are_identical(self):
        """验收 2：任务书第 2 条列出的每个名字从原模块取到的是同一个对象。"""
        for name in ("_title", "_pr_title", "_manual_section", "pr_body", "escalation_body"):
            self.assertIs(getattr(dispatch, name), getattr(dispatch_text, name), name)
        for name in ("acquire_slot", "update_slot", "release_slot", "_registered_worktrees",
                     "_salvage_and_remove", "reclaim_stale_slots", "return_slot", "prepare_slot"):
            self.assertIs(getattr(dispatch, name), getattr(dispatch_slots, name), name)
        for name in ("load_samples", "prepare_head_sample", "sample_deviates", "score_samples",
                     "_cell", "render_calibration_report", "review_calibrate"):
            self.assertIs(getattr(review, name), getattr(review_calibration, name), name)

    def test_patch_semantics_preserved(self):
        """验收 3：在原模块上 patch 后，调用移出的函数确实走到 patch 后的名字。"""
        with tempfile.TemporaryDirectory() as tmp:
            work, head = make_project(Path(tmp))
            manifest = Path(tmp) / "samples.json"
            manifest.write_text(json.dumps(
                [{"pr": 7, "head": head, "expected": "通过", "reason": "拆分探针"}], ensure_ascii=False),
                encoding="utf-8")
            # review.make_reviewer：review_calibrate（已移出）必须经原模块属性调用才拦得到。
            with mock.patch.object(review, "make_reviewer", return_value=ProbeReviewer()) as make:
                code = review.review_calibrate("probe", manifest, Path(tmp) / "report.md", root=work)
            self.assertEqual(code, 0)
            make.assert_called_once_with("probe")
            self.assertIn("TPR", (Path(tmp) / "report.md").read_text(encoding="utf-8"))
        # dispatch.state_dir：移出的槽位函数同样按原模块属性取 state_dir。
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "slots").mkdir()
            (state / "slots" / "1.json").write_text('{"pid": 1}', encoding="utf-8")
            root = Path(tmp) / "repo"
            with mock.patch.object(dispatch, "state_dir", return_value=state) as state_dir:
                dispatch.update_slot(root, 1, attempt=2)
            state_dir.assert_called_once_with(root)
            self.assertEqual(json.loads((state / "slots" / "1.json").read_text(encoding="utf-8"))["attempt"], 2)

    def test_import_order_independent(self):
        """验收 4：先单独导入三个新模块再导入原模块，都没有循环导入错误。"""
        names = ["engine.agents.review_calibration", "engine.agents.dispatch_slots",
                 "engine.agents.dispatch_text", "engine.agents.review", "engine.agents.dispatch"]
        saved = {name: sys.modules.pop(name) for name in names}
        try:
            for name in names:
                importlib.import_module(name)
        finally:
            import engine.agents  # 恢复包属性与 sys.modules 的原状

            for name, module in saved.items():
                sys.modules[name] = module
                setattr(engine.agents, name.rsplit(".", 1)[1], module)


if __name__ == "__main__":
    unittest.main()
