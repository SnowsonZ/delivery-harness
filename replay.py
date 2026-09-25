"""事故回放：把历史缺陷注入工作区副本，证明对应检查真的会失败（用例见 harness/replay_cases.py）。

    python3 harness/replay.py            运行全部用例（Swift 用例仅在 macOS 上运行）
    python3 harness/replay.py --strict   被跳过的用例算失败（macOS CI 用）
    python3 harness/replay.py --list     只列出覆盖情况，不运行

结论：
  拦住   注入后检查失败（期望结果）
  漏过   注入后检查仍通过：这条回归测试守不住这个缺陷
  过期   find 在文件中不存在或不唯一：用例需随代码更新
  坏底   未注入的副本上检查就已失败：结论不可信
另外检查基线（docs/review/2026-09-25-harness-baseline.md）每一条都有注入用例、守卫测试或写明原因的暂缓项。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, clean_git_env, git
from replay_cases import BASELINE, CASES, DEFERRED, GUARDED, Case

SWIFT_POLICY = (
    "mkdir -p build && xcrun swiftc -parse-as-library native/InboxPolicy.swift native/Shared/WidgetSnapshot.swift "
    "tests/InboxPolicyTests.swift -o build/policy-tests && ./build/policy-tests"
)


@dataclass
class Outcome:
    case: Case
    verdict: str  # 拦住 / 漏过 / 过期 / 坏底 / 跳过
    detail: str = ""


def _is_macos() -> bool:
    return sys.platform == "darwin" and shutil.which("xcrun") is not None


def copy_worktree(target: Path) -> None:
    """复制工作区中已跟踪与未忽略的文件（含未提交改动），回放针对当前工作区。"""
    listing = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for rel in filter(None, listing.split("\0")):
        source = ROOT / rel
        if not source.is_file():
            continue
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def run_tests(copy: Path, tests: tuple[str, ...]) -> tuple[bool, str]:
    """返回 (是否通过, 输出摘要)。"""
    python_tests = [test for test in tests if test != "swift-policy"]
    outputs = []
    passed = True
    env = clean_git_env({"PYTHONDONTWRITEBYTECODE": "1"})
    if python_tests:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", *python_tests],
            cwd=copy / "tests",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        passed = passed and completed.returncode == 0
        lines = completed.stderr.strip().splitlines()
        outputs.append(lines[-1] if lines else "")
    if "swift-policy" in tests:
        completed = subprocess.run(
            SWIFT_POLICY, shell=True, cwd=copy, capture_output=True, text=True, env=env, check=False
        )
        passed = passed and completed.returncode == 0
        outputs.append("swift-policy 通过" if completed.returncode == 0 else "swift-policy 失败")
    return passed, "；".join(filter(None, outputs))


def replay(cases: list[Case], strict: bool = False) -> list[Outcome]:
    outcomes = []
    temp = Path(tempfile.mkdtemp(prefix="replay-"))
    try:
        copy = temp / "repo"
        copy_worktree(copy)
        baseline_ok: dict[tuple[str, ...], tuple[bool, str]] = {}
        for case in cases:
            if case.platform == "macos" and not _is_macos():
                verdict = "坏底" if strict else "跳过"
                outcomes.append(Outcome(case, verdict, "需要 macOS（Swift）"))
                continue
            target = copy / case.file
            original = target.read_text(encoding="utf-8")
            count = original.count(case.find)
            if count != 1:
                outcomes.append(Outcome(case, "过期", f"find 在 {case.file} 中出现 {count} 次"))
                continue
            if case.tests not in baseline_ok:
                baseline_ok[case.tests] = run_tests(copy, case.tests)
            ok, summary = baseline_ok[case.tests]
            if not ok:
                outcomes.append(Outcome(case, "坏底", f"未注入时检查已失败：{summary}"))
                continue
            target.write_text(original.replace(case.find, case.replace), encoding="utf-8")
            try:
                passed, summary = run_tests(copy, case.tests)
            finally:
                target.write_text(original, encoding="utf-8")
            outcomes.append(Outcome(case, "漏过" if passed else "拦住", summary))
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return outcomes


def guarded_outcomes() -> list[tuple[str, str, bool, str]]:
    """守卫测试：逐个运行，必须存在且通过。"""
    results = []
    for defect, tests in GUARDED.items():
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", *tests],
            cwd=ROOT / "tests",
            capture_output=True,
            text=True,
            env=clean_git_env({"PYTHONDONTWRITEBYTECODE": "1"}),
            check=False,
        )
        lines = completed.stderr.strip().splitlines()
        results.append((defect, ", ".join(test.rsplit(".", 1)[-1] for test in tests), completed.returncode == 0, lines[-1] if lines else ""))
    return results


def coverage_gaps() -> list[str]:
    covered = {case.defect for case in CASES} | set(GUARDED) | set(DEFERRED)
    return [defect for defect in BASELINE if defect not in covered]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strict", action="store_true", help="被跳过的用例算失败")
    parser.add_argument("--list", action="store_true", help="只列出覆盖情况")
    parser.add_argument("--only", help="只运行这些缺陷编号（逗号分隔）")
    args = parser.parse_args(argv)

    gaps = coverage_gaps()
    if args.list:
        for defect in BASELINE:
            kinds = []
            if any(case.defect == defect for case in CASES):
                kinds.append(f"注入 ×{sum(case.defect == defect for case in CASES)}")
            if defect in GUARDED:
                kinds.append("守卫测试")
            if defect in DEFERRED:
                kinds.append(f"暂缓：{DEFERRED[defect]}")
            print(f"{defect:<9} {'；'.join(kinds) or '未覆盖'}")
        return 1 if gaps else 0

    cases = CASES
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in CASES if case.defect in wanted]
    failed = False
    print("注入回放：")
    for outcome in replay(cases, args.strict):
        mark = {"拦住": "✓", "跳过": "-"}.get(outcome.verdict, "✗")
        failed = failed or mark == "✗"
        print(f"  {mark} {outcome.case.defect:<9} {outcome.verdict}  {outcome.case.title}  [{outcome.detail}]")
    if not args.only:
        print("守卫测试：")
        for defect, names, ok, summary in guarded_outcomes():
            failed = failed or not ok
            print(f"  {'✓' if ok else '✗'} {defect:<9} {names}  [{summary}]")
        print("暂缓（原因见 harness/replay_cases.py DEFERRED）：" + "、".join(DEFERRED))
        if gaps:
            failed = True
            print(f"✗ 基线未覆盖：{'、'.join(gaps)}")
    print("回放失败。" if failed else "回放通过：每个注入都被拦住。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
