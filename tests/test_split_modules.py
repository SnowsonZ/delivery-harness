"""T707 拆分的回归断言：体量预算、重新导出同一性、原模块 patch 语义与导入顺序。

对应任务书验收表第 1–4 行：dispatch.py ≤ 660 行、review.py ≤ 680 行、三个新模块各 ≤ 400 行
且 files_over_800 为 0；移出的名字从原模块取到的是同一个对象；在原模块上 patch
（review.make_reviewer、dispatch.state_dir）后调用移出的函数仍然生效；先单独导入新模块
再导入原模块没有循环导入。T708（B89）把后两条加强为：模块内部调用一律经原模块属性
（tests test_patch_semantics_for_internal_calls），导入顺序用全新子进程判定，新模块顶层
不得回导入原模块。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import types
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
        """验收 1：拆分后的体量预算（B81 阻塞点：质量棘轮 files_over_800）。

        dispatch.py / review.py 的 660 / 680 是 T707 拆分当时的一次性目标，不是长期约束：后续任务
        （T703、T702）本来就要往里加代码，长期约束只有下面与质量棘轮同口径的 800 行全局断言。
        """
        agents = Path(dispatch.__file__).parent
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
        """验收 4（B89）：全新子进程里按「三个新模块 → 原模块」顺序导入，退出码为 0；把新模块改成
        顶层回导入原模块时退出码非 0（顶层回导入在部分初始化的模块上取不到名字）。另用 AST 断言
        三个新模块的模块层没有对原模块的导入，把顶层回导入直接判死（TYPE_CHECKING 块与函数内的
        按需导入不受影响）。"""
        order = ["engine.agents.review_calibration", "engine.agents.dispatch_slots",
                 "engine.agents.dispatch_text", "engine.agents.review", "engine.agents.dispatch"]
        result = subprocess.run([sys.executable, "-c", "import " + ", ".join(order)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = Path(dispatch.__file__).parent
        for name, original in (("dispatch_text.py", "engine.agents.dispatch"),
                               ("dispatch_slots.py", "engine.agents.dispatch"),
                               ("review_calibration.py", "engine.agents.review")):
            for node in ast.parse((agents / name).read_text(encoding="utf-8")).body:
                targets = [alias.name for alias in node.names] if isinstance(node, ast.Import) \
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for target in targets:
                    self.assertNotEqual(target, original, f"{name} 在模块层导入了原模块 {original}")

    def test_patch_semantics_for_internal_calls(self):
        """验收 5（B89）：在原模块上 patch 移出名字后，移出函数内部的调用走到 patch 后的版本。"""
        # review.load_samples：review_calibrate 内部经 review.<名字> 取样本清单（真实装载必拒非法清单）
        with tempfile.TemporaryDirectory() as tmp:
            work, _head = make_project(Path(tmp))
            manifest = Path(tmp) / "samples.json"
            manifest.write_text("不是合法 JSON", encoding="utf-8")
            with mock.patch.object(review, "load_samples", return_value=[]) as load_samples:
                code = review.review_calibrate("probe", manifest, Path(tmp) / "report.md", root=work,
                                               reviewer=ProbeReviewer())
            self.assertEqual(code, 0)
            load_samples.assert_called_once_with(manifest)
            self.assertIn("TPR", (Path(tmp) / "report.md").read_text(encoding="utf-8"))
        # review.sample_deviates：score_samples 内部；review._cell：render_calibration_report 内部
        with mock.patch.object(review, "sample_deviates", return_value=True) as deviates:
            summary = review_calibration.score_samples([{"expected": "通过", "flagged": True, "parsed": True}])
        self.assertEqual(summary["deviations"], 1)
        deviates.assert_called_once()
        counts = {"total": 1, "bad": 0, "good": 0, "caught": 0, "released": 0, "deviations": 0,
                  "errors": 1, "unparsed": 0, "tpr": None, "tnr": None}
        with mock.patch.object(review, "_cell", side_effect=lambda text: f"[{text}]") as cell:
            report = review_calibration.render_calibration_report(
                Path("s.json"), [{"pr": 1, "head": "f" * 40, "expected": "通过", "reason": "依据",
                                  "error": "注入失败"}], counts, "probe", "m")
        self.assertIn("[注入失败]", report)
        cell.assert_any_call("注入失败")
        # dispatch._salvage_and_remove：reclaim_stale_slots 与 return_slot 内部；
        # dispatch._registered_worktrees：_salvage_and_remove 内部
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            (state / "slots").mkdir(parents=True)
            for index, branch in ((1, "task/1-x"), (2, "task/2-y")):
                (state / "slots" / f"{index}.json").write_text(
                    json.dumps({"pid": 4194304, "branch": branch, "task": "T1"}), encoding="utf-8")
            config = dispatch.Config(slots=2, slot_root=root / "slots", stall_seconds=30, poll_seconds=0.05,
                                     ci_timeout_seconds=60, verify=[], review_after_ci=False)
            with mock.patch.object(dispatch, "state_dir", return_value=state), \
                    mock.patch.object(dispatch, "_alive", return_value=False), \
                    mock.patch.object(dispatch, "_salvage_and_remove") as salvage:
                reclaimed = dispatch_slots.reclaim_stale_slots(root, config, push=object())
                expected = [dispatch.slot_path(root, config, index) for index in (1, 2)]
                dispatch_slots.return_slot(root, config, 1, object())
            self.assertEqual(reclaimed, ["task/1-x", "task/2-y"])
            self.assertEqual([call.args[1] for call in salvage.call_args_list[:2]], expected)
            self.assertEqual(salvage.call_args_list[2].args[1], expected[0])  # return_slot 同样经原模块归还
            with mock.patch.object(dispatch, "_registered_worktrees", return_value=set()) as registered:
                (root / "slot-1").mkdir()
                dispatch_slots._salvage_and_remove(root, root / "slot-1", object())
            registered.assert_called_once_with(root)
        # dispatch._title：_pr_title 内部；dispatch._manual_section：pr_body 内部
        task = types.SimpleNamespace(path="docs/plans/task-1-x.md", id="T1", klass="K1", risk="R1",
                                     spec_refs=[], branch="task/1-x")
        with mock.patch.object(dispatch, "_title", return_value="T1：样本") as title:
            self.assertEqual(dispatch_text._pr_title(Path(tmp), task), "T1：样本")
        title.assert_called_once_with(Path(tmp), task)
        attempt = types.SimpleNamespace(usage={}, model="m", executor_seconds=60.0, retries=0, guard_denials={})
        with mock.patch.object(dispatch, "_manual_section", return_value="人工验收指引") as section:
            body = dispatch_text.pr_body(task, attempt, 7, "0123456789abcdef", root=Path(tmp))
        self.assertIn("人工验收指引", body)
        section.assert_called_once_with(Path(tmp), task)


if __name__ == "__main__":
    unittest.main()
