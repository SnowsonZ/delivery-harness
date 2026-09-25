"""定向变异测试：衡量关键函数的测试强度（判定器本身被检验）。

对 TARGETS 中列出的函数逐个施加小的 AST 变异（比较符、算术符、布尔运算、常量、删除调用语句），
在工作区副本上运行对应测试：测试失败 = 变异被杀死；仍通过 = 存活（测试的盲区线索，也可能是等价变异）。

    python3 harness/mutate.py                 运行全部目标，打印得分与存活变异
    python3 harness/mutate.py --check         与 harness/mutation-baseline.json 比较，任一目标得分下降即失败
    python3 harness/mutate.py --update        运行后写回基线（得分提高后执行，基线只升不降）

较慢（每个变异跑一次测试），不在每次提交时运行；由定期的 quality workflow 与人工触发。
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, clean_git_env
from replay import copy_worktree

BASELINE = ROOT / "harness" / "mutation-baseline.json"


@dataclass(frozen=True)
class Target:
    name: str
    file: str
    functions: tuple[str, ...]
    tests: tuple[str, ...]


TARGETS = [
    Target(
        "价格转换与防护",
        "scripts/pricing_fetch.py",
        ("transform", "_price_jump", "_drop_conflicting_aliases"),
        ("test_pricing_fetch", "test_properties.PricingTransformProperties"),
    ),
    Target(
        "热力金额分级",
        "scripts/daily_report.py",
        ("cost_thresholds", "cost_level"),
        ("test_properties.CostThresholdProperties", "test_daily_report"),
    ),
    Target(
        "过去日报告合并",
        "scripts/daily_report.py",
        ("_merge_day_tasks", "_task_total"),
        ("test_properties.MergeNoDowngradeProperties", "test_daily_report"),
    ),
    Target("模型规范名", "scripts/model_names.py", ("canonical",), ("test_model_names",)),
]

SWAPS = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.And: ast.Or,
    ast.Or: ast.And,
}


@dataclass(frozen=True)
class Mutant:
    index: int
    line: int
    operator: str
    before: str


def _function_ranges(tree: ast.Module, names: tuple[str, ...]) -> list[tuple[int, int]]:
    return [
        (node.lineno, node.end_lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]


def _in_ranges(node: ast.AST, ranges: list[tuple[int, int]]) -> bool:
    line = getattr(node, "lineno", None)
    return line is not None and any(start <= line <= end for start, end in ranges)


def _candidates(tree: ast.Module, ranges: list[tuple[int, int]]) -> list[tuple[ast.AST, str]]:
    """(节点, 变异描述)；顺序稳定，作为变异编号。"""
    found = []
    for node in ast.walk(tree):
        if not _in_ranges(node, ranges):
            continue
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if type(op) in SWAPS:
                    found.append((node, f"{type(op).__name__}→{SWAPS[type(op)].__name__}"))
        elif isinstance(node, (ast.BinOp, ast.BoolOp)) and type(node.op) in SWAPS:
            found.append((node, f"{type(node.op).__name__}→{SWAPS[type(node.op)].__name__}"))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append((node, "去掉 not"))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            found.append((node, f"{node.value}→{node.value + 1}"))
        elif isinstance(node, ast.Constant) and type(node.value) is bool:
            found.append((node, f"{node.value}→{not node.value}"))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            found.append((node, "删除调用语句"))
    return found


def _mutate(tree: ast.Module, ranges: list[tuple[int, int]], index: int) -> ast.Module:
    mutated = copy.deepcopy(tree)
    node, _ = _candidates(mutated, ranges)[index]
    if isinstance(node, ast.Compare):
        node.ops = [SWAPS[type(op)]() if type(op) in SWAPS else op for op in node.ops]
    elif isinstance(node, (ast.BinOp, ast.BoolOp)):
        node.op = SWAPS[type(node.op)]()
    elif isinstance(node, ast.UnaryOp):
        replacement = node.operand
        node.__class__ = type(replacement)
        node.__dict__.update(replacement.__dict__)
    elif isinstance(node, ast.Constant):
        node.value = (not node.value) if type(node.value) is bool else node.value + 1
    elif isinstance(node, ast.Expr):
        node.value = ast.Constant(value=None)
    return ast.fix_missing_locations(mutated)


def enumerate_mutants(source: str, functions: tuple[str, ...]) -> list[Mutant]:
    tree = ast.parse(source)
    ranges = _function_ranges(tree, functions)
    return [
        Mutant(index, node.lineno, operator, ast.unparse(node)[:80])
        for index, (node, operator) in enumerate(_candidates(tree, ranges))
    ]


def _tests_pass(copy_root: Path, tests: tuple[str, ...]) -> bool:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", *tests],
            cwd=copy_root / "tests",
            capture_output=True,
            env=clean_git_env({"PYTHONDONTWRITEBYTECODE": "1"}),
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False  # 变异导致死循环：算被杀死
    return completed.returncode == 0


def run_target(target: Target, copy_root: Path) -> dict:
    path = copy_root / target.file
    original = path.read_text(encoding="utf-8")
    tree = ast.parse(original)
    ranges = _function_ranges(tree, target.functions)
    mutants = enumerate_mutants(original, target.functions)
    if not _tests_pass(copy_root, target.tests):
        return {"name": target.name, "error": "未变异时测试就失败", "total": len(mutants), "killed": 0, "survivors": []}
    survivors = []
    try:
        for mutant in mutants:
            path.write_text(ast.unparse(_mutate(tree, ranges, mutant.index)) + "\n", encoding="utf-8")
            if _tests_pass(copy_root, target.tests):
                survivors.append(f"{target.file}:{mutant.line} {mutant.operator}  `{mutant.before}`")
    finally:
        path.write_text(original, encoding="utf-8")
    killed = len(mutants) - len(survivors)
    return {"name": target.name, "total": len(mutants), "killed": killed, "survivors": survivors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="得分低于基线即失败")
    mode.add_argument("--update", action="store_true", help="写回基线（只升不降）")
    parser.add_argument("--only", help="只运行这些目标（名称，逗号分隔）")
    args = parser.parse_args(argv)

    targets = TARGETS
    if args.only:
        wanted = set(args.only.split(","))
        targets = [target for target in TARGETS if target.name in wanted]
    temp = Path(tempfile.mkdtemp(prefix="mutate-"))
    try:
        copy_root = temp / "repo"
        copy_worktree(copy_root)
        results = [run_target(target, copy_root) for target in targets]
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    failed = False
    for result in results:
        total, killed = result["total"], result["killed"]
        score = killed / total if total else 1.0
        previous = baseline.get(result["name"])
        trend = f"（基线 {previous:.0%}）" if previous is not None else ""
        print(f"{result['name']}：杀死 {killed}/{total} = {score:.0%}{trend}")
        if result.get("error"):
            failed = True
            print(f"  ✗ {result['error']}")
        for survivor in result["survivors"]:
            print(f"  存活 {survivor}")
        if args.check and previous is not None and score + 1e-9 < previous:
            failed = True
            print(f"  ✗ 得分低于基线 {previous:.0%}")
        result["score"] = round(score, 4)
    if args.update:
        for result in results:
            if not result.get("error"):
                baseline[result["name"]] = max(result["score"], baseline.get(result["name"], 0.0))
        BASELINE.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        print(f"基线已写入 {BASELINE.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
