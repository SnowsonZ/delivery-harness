"""并行测试运行器：发现交给 unittest，按模块分片并行执行，汇总结果（T713 修订 3）。

    python3 <引擎>/cli.py run-tests -s <测试目录> [-p "test*.py"] [-j <并发数>] [--serial <模块名>]...

父进程和每个子进程都执行与 `python -m unittest discover -s <目录> -p <模式>` 完全相同的发现
（`unittest.TestLoader().discover(start, pattern)`，参数取法与 discover 的默认值一致，含包级
`__init__.py` 里的测试与 load_tests 协议），把得到的套件按测试所在模块（`test.__class__
.__module__` 的完整点分名）分组；导入失败等 `_FailedTest` 的组键是 unittest.loader，同样按
失败用例对待。分片按模块做：一个模块一个子进程（同一模块的 setUpModule、setUpClass 不被
拆散），子进程在调用方的工作目录里（不切换 cwd，环境照常继承，调用目录放进 PYTHONPATH
最前以保持与 `python -m unittest` 一致的导入口径，verify 的事件重定向变量照常传下去，T710）
重新做同样的发现，**整个执行期间保留 discover 建立的导入路径**（与 `python -m unittest
discover` 一致；只有父进程——只做发现、不执行测试——在发现后还原 sys.path，T713 修订 4），
只运行分配给本片的模块组，unittest 的完整输出与**实际执行的**测试 id（TestResult 的
startTest 事件记下的用例，跳过也算已执行；分配给本片却没有执行的不上报，T713 修订 4）
经结果文件交回父进程。--serial 列出的模块从并行分片中剔除，在并行批次结束后逐个顺序执行；
每个参数都必须恰好对应一个已发现的模块，否则退出码 2，不执行任何测试。汇总时核对所有
分片实际运行的测试 id 与父进程发现的测试 id（多重集合）完全相等（不漏、不重），否则退出码
1 并列出差异；任何一个模块失败退出码 1，没有发现任何测试同样按失败处理（避免空跑当
通过）。只用标准库。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_JOBS_CAP = 8
# 内部子进程模式标志：父进程派发分片用，不是对外接口。
_SHARD_FLAG = "--_shard"
_RUNNER_PATH = Path(__file__).resolve()


def _iter_tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    """深度优先展开测试套件，产出所有 TestCase（含 _FailedTest 与 load_tests 附加的用例）。"""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def discover_groups(start: str, pattern: str, *, restore_path: bool = True) -> dict[str, list[unittest.TestCase]]:
    """与 `unittest discover -s start -p pattern` 完全相同的发现，按测试所在模块分组。

    组键是 `test.__class__.__module__` 的完整点分名；不同子包里的同名文件模块名不同，各算
    一个。discover 会把 top_level_dir 插进 sys.path：父进程只做发现、不执行测试，发现后
    还原（restore_path=True）；子进程要在整个执行期间保留 discover 建立的导入路径，运行期
    间延迟导入测试目录里的辅助模块必须与 `python -m unittest discover` 一样可用，所以传
    False（T713 修订 4）。
    """
    saved_path = list(sys.path)
    try:
        suite = unittest.TestLoader().discover(start, pattern)
    finally:
        if restore_path:
            sys.path[:] = saved_path
    groups: dict[str, list[unittest.TestCase]] = {}
    for test in _iter_tests(suite):
        groups.setdefault(test.__class__.__module__, []).append(test)
    return groups


def _child_env() -> dict[str, str]:
    """子进程环境：继承调用方，再把调用目录放进 PYTHONPATH 最前。

    子进程按脚本方式启动（sys.path[0] 是本文件所在目录而不是调用目录），补上调用目录才与
    `python -m unittest discover` 的导入口径一致；工作目录保持调用方的不变（T713 修订 3）。
    """
    env = dict(os.environ)
    entries = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
    cwd = os.getcwd()
    if cwd not in entries:
        env["PYTHONPATH"] = os.pathsep.join([cwd, *entries])
    return env


class _RecordingResult(unittest.TextTestResult):
    """在 startTest 事件里记下实际执行过的测试 id（T713 修订 4）。

    跳过的用例同样会经过 startTest（随后 addSkip），算已执行；`result.stop()` 等原因导致
    分配给本片却没有执行的用例不会出现，父进程的 id 集合核对因此能把漏跑暴露出来。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_ids: list[str] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.started_ids.append(test.id())
        super().startTest(test)


def _load_result(path: Path) -> dict | None:
    """读取子进程的结果文件；读不到或不是合法 JSON 对象时按「子进程失败」处理（返回 None）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _shard_main(argv: list[str]) -> int:
    """子进程模式：做同样的发现，只运行分配给本片的模块组，结果写回结果文件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--pattern", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--module", action="append", required=True)
    args = parser.parse_args(argv)

    groups = discover_groups(args.start, args.pattern, restore_path=False)
    selected: list[unittest.TestCase] = []
    missing = [name for name in args.module if name not in groups]
    for name in args.module:
        selected.extend(groups.get(name, []))
    stream = io.StringIO()
    result = unittest.TextTestRunner(
        stream=stream, verbosity=1, resultclass=_RecordingResult).run(unittest.TestSuite(selected))
    print(stream.getvalue(), end="")
    payload = {
        "ran": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "ids": list(result.started_ids),  # 实际执行的测试 id（startTest 事件），非待运行清单
        "code": 0 if result.wasSuccessful() and not missing else 1,
    }
    Path(args.result_file).write_text(json.dumps(payload), encoding="utf-8")
    return payload["code"]


def run_shard(start: str, pattern: str, module: str, result_file: Path) -> dict:
    """一个模块一个子进程：在调用方工作目录里做同样的发现，只运行该模块组，交回结果。"""
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", str(_RUNNER_PATH), _SHARD_FLAG,
         "--start", start, "--pattern", pattern, "--result-file", str(result_file),
         "--module", module],
        env=_child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", check=False,
    )
    payload = _load_result(result_file)
    if payload is None:  # 子进程崩溃或结果不可读：缺少的测试 id 由汇总时的核对拦下
        payload = {"ran": 0, "failures": 0, "errors": 0, "ids": [], "code": 1}
    return {
        "name": module,
        "code": payload.get("code", 1) if completed.returncode == 0 else 1,
        "output": completed.stdout,
        "ran": payload.get("ran", 0),
        "failures": payload.get("failures", 0),
        "errors": payload.get("errors", 0),
        "ids": list(payload.get("ids", [])),
        "seconds": time.monotonic() - started,
    }


def _print_differences(expected: Counter, ran: Counter) -> None:
    """列出分片结果与发现结果的差异（多重集合相减），每类最多展示 10 个。"""
    print("run-tests：分片实际运行的测试 id 与发现结果不一致：", file=sys.stderr)
    for label, items in (("缺少", sorted((expected - ran).elements())),
                         ("多出", sorted((ran - expected).elements()))):
        if items:
            preview = "、".join(items[:10])
            suffix = f" 等 {len(items)} 个" if len(items) > 10 else ""
            print(f"  {label}（{len(items)}）：{preview}{suffix}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] == _SHARD_FLAG:
        return _shard_main(args[1:])

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", dest="start", required=True, help="测试目录")
    parser.add_argument("-p", dest="pattern", default="test*.py", help="文件模式（默认 test*.py）")
    parser.add_argument("-j", dest="jobs", type=int, default=0, help="并发数（默认 min(CPU 核数, 8)）")
    parser.add_argument("--serial", action="append", default=[], metavar="模块",
                        help="并行批次结束后逐个顺序执行的模块（完整点分模块名，可多次）")
    args = parser.parse_args(argv)

    groups = discover_groups(args.start, args.pattern) if Path(args.start).is_dir() else {}
    serial_specs = list(dict.fromkeys(args.serial))
    unknown = [spec for spec in serial_specs if spec not in groups]
    if unknown:
        print(f"run-tests：--serial 里的模块不在 {args.start} 的发现结果中：{', '.join(unknown)}",
              file=sys.stderr)
        return 2
    if not groups:
        print(f"run-tests：{args.start} 不是存在的测试目录，或里面没有匹配 {args.pattern} 的测试",
              file=sys.stderr)
        return 1

    expected = Counter(test.id() for tests in groups.values() for test in tests)
    jobs = max(args.jobs, 0) or min(os.cpu_count() or 1, DEFAULT_JOBS_CAP)
    serial = set(serial_specs)
    parallel_modules = sorted(name for name in groups if name not in serial)
    started = time.monotonic()
    results: list[dict] = []
    lock = threading.Lock()

    def report(result: dict) -> None:
        with lock:  # 多线程完成回调里逐行打印，避免交错
            if result["code"] == 0:
                print(f"✓ {result['name']}（{result['seconds']:.1f}s）", flush=True)
            else:
                print(f"✗ {result['name']}（失败 {result['failures']}、出错 {result['errors']}，"
                      f"{result['seconds']:.1f}s）", flush=True)

    with tempfile.TemporaryDirectory(prefix="dh-run-tests-") as tmp:  # 每片一个结果文件，不入库
        files = {name: Path(tmp) / f"{index:04d}.json"
                 for index, name in enumerate(parallel_modules + serial_specs)}
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(run_shard, args.start, args.pattern, name, files[name])
                       for name in parallel_modules]
            for future in as_completed(futures):
                result = future.result()
                report(result)
                results.append(result)
        for name in serial_specs:  # 共享状态的模块逐个顺序执行，不与任何分片重叠
            result = run_shard(args.start, args.pattern, name, files[name])
            report(result)
            results.append(result)

    results.sort(key=lambda r: r["name"])
    for result in (r for r in results if r["code"] != 0):
        print(f"──── {result['name']} 的完整输出 ────")
        print(result["output"].rstrip("\n"))
    ran_total = sum(r["ran"] for r in results)
    failures = sum(r["failures"] for r in results)
    errors = sum(r["errors"] for r in results)
    print(f"合计：{len(results)} 个模块，Ran {ran_total} 个用例，失败 {failures}、出错 {errors}，"
          f"用时 {time.monotonic() - started:.1f}s")
    ran = Counter(i for r in results for i in r["ids"])
    if ran != expected:
        _print_differences(expected, ran)
        return 1
    print(f"分片测试 id：运行 {sum(ran.values())}、发现 {sum(expected.values())}，一致")
    return 1 if any(r["code"] != 0 for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
