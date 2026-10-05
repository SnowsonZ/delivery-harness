"""并行测试运行器：把 unittest 按测试文件拆成子进程并行执行，汇总结果（T713）。

    python3 <引擎>/cli.py run-tests -s <测试目录> [-p "test*.py"] [-j <并发数>] [--serial <文件名>]...

每个测试文件一个子进程，命令与单进程 discover 同口径：`<当前解释器> -W error::ResourceWarning
-m unittest discover -s <测试目录> -p <该文件名>`；工作目录与环境继承调用方（verify 设置的
事件重定向变量因此照常传下去，T710）。--serial 列出的文件在并行批次结束后逐个顺序执行，留给
确有共享状态、不能并行的测试。全部子进程结束后：先打印每个失败文件的完整输出，再打印一行合计
（文件数、Ran 用例总数、失败与出错数、耗时）；任何一个文件失败退出码为 1，没有发现任何测试文件
同样按失败处理（避免空跑当通过）。只用标准库。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# unittest 结果行的计数（Ran N tests / FAILED (failures=x, errors=y)）；解析不到的项按 0 计。
_RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_FAILURES = re.compile(r"failures=(\d+)")
_ERRORS = re.compile(r"errors=(\d+)")
DEFAULT_JOBS_CAP = 8


def discover_names(start: Path, pattern: str) -> list[str]:
    """测试目录里匹配模式的文件名（顶层、按名排序，保证批次确定）。"""
    return sorted(path.name for path in start.glob(pattern) if path.is_file())


def run_file(start: Path, name: str) -> dict:
    """单个测试文件的子进程：命令与单进程 discover 同口径，环境与工作目录继承调用方。"""
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", "discover",
         "-s", str(start), "-p", name],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", check=False,
    )
    ran = _RAN.search(completed.stdout)
    failures = _FAILURES.search(completed.stdout)
    errors = _ERRORS.search(completed.stdout)
    return {
        "name": name,
        "code": completed.returncode,
        "output": completed.stdout,
        "ran": int(ran.group(1)) if ran else 0,
        "failures": int(failures.group(1)) if failures else 0,
        "errors": int(errors.group(1)) if errors else 0,
        "seconds": time.monotonic() - started,
    }


def _print_report(results: list[dict], started: float) -> None:
    """先打印每个失败文件的完整输出，再打印一行合计。"""
    for result in (r for r in results if r["code"] != 0):
        print(f"──── {result['name']} 的完整输出 ────")
        print(result["output"].rstrip("\n"))
    total = (len(results), sum(r["ran"] for r in results),
             sum(r["failures"] for r in results), sum(r["errors"] for r in results))
    print(f"合计：{total[0]} 个文件，Ran {total[1]} 个用例，失败 {total[2]}、出错 {total[3]}，"
          f"用时 {time.monotonic() - started:.1f}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", dest="start", required=True, help="测试目录")
    parser.add_argument("-p", dest="pattern", default="test*.py", help="文件模式（默认 test*.py）")
    parser.add_argument("-j", dest="jobs", type=int, default=0, help="并发数（默认 min(CPU 核数, 8)）")
    parser.add_argument("--serial", action="append", default=[], metavar="文件名",
                        help="并行批次结束后逐个顺序执行的文件（可多次）")
    args = parser.parse_args(argv)

    start = Path(args.start)
    names = discover_names(start, args.pattern)
    serial = list(dict.fromkeys(args.serial))
    unknown = [name for name in serial if name not in names]
    if unknown:
        print(f"run-tests：--serial 里的文件不在 {start} 的匹配结果中：{', '.join(unknown)}", file=sys.stderr)
        return 2
    if not names:
        print(f"run-tests：{start} 里没有匹配 {args.pattern} 的测试文件", file=sys.stderr)
        return 1

    jobs = max(args.jobs, 0) or min(os.cpu_count() or 1, DEFAULT_JOBS_CAP)
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

    parallel = [name for name in names if name not in set(serial)]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_file, start, name) for name in parallel]
        for future in as_completed(futures):
            result = future.result()
            report(result)
            results.append(result)
    for name in serial:  # 共享状态的测试逐个顺序执行，不与任何文件重叠
        result = run_file(start, name)
        report(result)
        results.append(result)

    results.sort(key=lambda r: r["name"])
    _print_report(results, started)
    return 1 if any(r["code"] != 0 for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
