"""pilot 分支的受保护验收：执行测试并输出结构化结果。

由 .github/workflows/pilot-acceptance.yml 调用。工作流先从 pilot（PR 的 base）检出本文件与测试基线，
复制到 runner 临时目录后才检出候选，所以候选改不到正在执行的本文件。只依赖标准库。

子命令：
  run        执行全部测试，或 --ids 指定的测试；--expect fail|pass 用于修复证据
  check      把一次全量执行的结果与测试基线比对（执行核对）
  baseline   从一次全量执行的结果生成测试基线
  task-base  在 base..head 中找出带 Pilot-Task-Base 尾注的任务基点提交，读出复现测试

设计依据：agent-workflow-bp/designs/pilot-architecture/binding.md §3。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unittest

LOADER_FAILURE = "unittest.loader._FailedTest."
FAILING = ("fail", "error", "xpass")


class Recorder(unittest.TestResult):
    """逐条记录每个测试的结果：ok、fail、error、skip、xfail、xpass。"""

    def __init__(self) -> None:
        super().__init__()
        self.outcomes: dict[str, str] = {}

    def _mark(self, test: unittest.TestCase, outcome: str) -> None:
        current = self.outcomes.get(test.id())
        if current in FAILING:  # 一个测试的子测试多次失败时保留第一次失败
            return
        self.outcomes[test.id()] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self._mark(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._mark(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._mark(test, "xfail")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._mark(test, "xpass")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._mark(test, "fail" if issubclass(err[0], test.failureException) else "error")


def _suite(ids: list[str]) -> unittest.TestSuite:
    # 与 `python -m unittest discover -s tests` 一致：仓库根与 tests 目录都在导入路径上
    root = os.getcwd()
    tests = os.path.join(root, "tests")
    for path in (tests, root):
        if path not in sys.path:
            sys.path.insert(0, path)
    loader = unittest.TestLoader()
    return loader.loadTestsFromNames(ids) if ids else loader.discover(tests, top_level_dir=tests)


def _run(ids: list[str]) -> dict:
    result = Recorder()
    _suite(ids).run(result)
    outcomes = result.outcomes
    return {
        "tests_run": result.testsRun,
        "outcomes": outcomes,
        "loader_failures": sorted(t for t in outcomes if t.startswith(LOADER_FAILURE)),
        "counts": {k: sum(1 for v in outcomes.values() if v == k)
                   for k in ("ok", "fail", "error", "skip", "xfail", "xpass")},
    }


def _write(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)


def cmd_run(args) -> int:
    data = _run(args.ids)
    problems: list[str] = []
    if args.expect is None:
        if data["tests_run"] == 0:
            problems.append("一个测试都没有执行")
        problems += [f"{t}: {o}" for t, o in sorted(data["outcomes"].items()) if o in FAILING]
        problems += [f"{t}: 加载失败" for t in data["loader_failures"]]
    else:
        for test_id in args.ids:
            outcome = data["outcomes"].get(test_id)
            if outcome is None:
                problems.append(f"{test_id}: 没有执行（名称不存在或加载失败）")
            elif args.expect == "fail" and outcome not in ("fail", "error"):
                problems.append(f"{test_id}: 期望失败，实际 {outcome}")
            elif args.expect == "pass" and outcome != "ok":
                problems.append(f"{test_id}: 期望通过，实际 {outcome}")
        if data["loader_failures"]:
            problems += [f"{t}: 加载失败" for t in data["loader_failures"]]
    data.update(expect=args.expect, ids=args.ids, problems=problems, passed=not problems)
    _write(args.out, data)
    for line in problems:
        print(f"::error::{line}")
    print(f"tests_run={data['tests_run']} counts={data['counts']} passed={data['passed']}")
    return 0 if data["passed"] else 1


def _classes(outcomes: dict[str, str]) -> set[str]:
    return {t.rsplit(".", 1)[0] for t, o in outcomes.items()
            if o != "skip" and not t.startswith(LOADER_FAILURE)}


def cmd_check(args) -> int:
    with open(args.result, encoding="utf-8") as fh:
        result = json.load(fh)
    problems: list[str] = []
    baseline = None
    if args.baseline and os.path.exists(args.baseline):
        with open(args.baseline, encoding="utf-8") as fh:
            baseline = json.load(fh)
    if not baseline or not baseline.get("tests_run") or not baseline.get("classes"):
        problems.append("测试基线缺失或为空：验收失败关闭")
    else:
        if result["tests_run"] == 0:
            problems.append("本次一个测试都没有执行")
        if result["tests_run"] < baseline["tests_run"]:
            problems.append(f"执行数 {result['tests_run']} 少于基线 {baseline['tests_run']}")
        skipped = result["counts"].get("skip", 0)
        if skipped > baseline.get("skipped", 0):
            problems.append(f"跳过数 {skipped} 多于基线 {baseline.get('skipped', 0)}")
        missing = sorted(set(baseline["classes"]) - _classes(result["outcomes"]))
        problems += [f"基线中的测试类没有执行：{c}" for c in missing]
    report = {"baseline": baseline, "tests_run": result["tests_run"],
              "skipped": result["counts"].get("skip", 0), "problems": problems, "passed": not problems}
    _write(args.out, report)
    for line in problems:
        print(f"::error::{line}")
    print(f"execution check passed={report['passed']}")
    return 0 if report["passed"] else 1


def cmd_baseline(args) -> int:
    with open(args.result, encoding="utf-8") as fh:
        result = json.load(fh)
    if result["tests_run"] == 0 or any(o in FAILING for o in result["outcomes"].values()):
        print("::error::全量执行不是全绿，不能作为基线")
        return 1
    baseline = {"tests_run": result["tests_run"], "skipped": result["counts"].get("skip", 0),
                "classes": sorted(_classes(result["outcomes"]))}
    _write(args.out, baseline)
    print(f"baseline tests_run={baseline['tests_run']} classes={len(baseline['classes'])}")
    return 0


def cmd_task_base(args) -> int:
    fmt = "%H%x1f%P%x1f%(trailers:key=Pilot-Task-Base,valueonly)%x1f%(trailers:key=Pilot-Repro-Tests,valueonly)%x1e"
    out = subprocess.run(["git", "log", "--no-merges", f"--format={fmt}", f"{args.base}..{args.head}"],
                         capture_output=True, text=True, check=True).stdout
    found = []
    for record in filter(None, (r.strip() for r in out.split("\x1e"))):
        sha, parents, task, repro = (record.split("\x1f") + ["", "", "", ""])[:4]
        if task.strip():
            found.append({"sha": sha, "parents": parents.split(), "task": task.strip(),
                          "repro_tests": repro.split()})
    problems: list[str] = []
    if len(found) != 1:
        problems.append(f"带 Pilot-Task-Base 尾注的提交应恰好一个，实际 {len(found)} 个")
    else:
        if found[0]["parents"] != [args.base]:
            problems.append("任务基点提交的父提交不是 PR 的 base")
        if not found[0]["repro_tests"]:
            problems.append("任务基点提交没有 Pilot-Repro-Tests 尾注")
    report = {"base": args.base, "head": args.head, "task_base": found[0] if len(found) == 1 else None,
              "candidates": found, "problems": problems, "passed": not problems}
    _write(args.out, report)
    for line in problems:
        print(f"::error::{line}")
    if report["passed"]:
        print(f"task_base={found[0]['sha']} task={found[0]['task']} repro={' '.join(found[0]['repro_tests'])}")
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--ids", nargs="*", default=[])
    p.add_argument("--expect", choices=["fail", "pass"])
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("check")
    p.add_argument("--result", required=True)
    p.add_argument("--baseline")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("baseline")
    p.add_argument("--result", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_baseline)
    p = sub.add_parser("task-base")
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_task_base)
    args = parser.parse_args()
    if args.command == "run" and args.expect and not args.ids:
        parser.error("--expect 需要配合 --ids")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
