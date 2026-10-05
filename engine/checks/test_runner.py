"""并行测试运行器：把 unittest 按测试文件拆成子进程并行执行，汇总结果（T713）。

    python3 <引擎>/cli.py run-tests -s <测试目录> [-p "test*.py"] [-j <并发数>] [--serial <文件>]...

文件发现口径与 `unittest discover -s <测试目录> -p <模式>` 一致（T713 修订 2）：顶层与带
`__init__.py` 的子包里、模块名合法且匹配模式的文件都算，不同子包里的同名文件各算一个。
每个测试文件一个子进程：`<当前解释器> -W error::ResourceWarning -m unittest <点分模块名>`，
工作目录取 discover 的 `-t` 口径（即测试目录本身）；discover 从调用目录启动时调用目录会随
`-m` 进入 sys.path，这里把调用目录等价保留在 PYTHONPATH 里（测试对调用目录下包的导入因此
不受换工作目录影响），其余环境继承调用方（verify 的事件重定向变量照常传下去，T710）。
--serial 列出的文件（按相对路径或文件名匹配）在并行批次结束后逐个顺序执行，留给确有共享
状态、不能并行的测试。全部子进程结束后：先打印每个失败文件的完整输出，再打印一行合计
（文件数、Ran 用例总数、失败与出错数、耗时）；任何一个文件失败退出码为 1，没有发现任何
测试文件同样按失败处理（避免空跑当通过）。只用标准库。
"""

from __future__ import annotations

import argparse
import fnmatch
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
# 与 unittest.loader 同口径：文件名须是合法模块名（大小写不敏感），否则 discover 也不收。
VALID_MODULE_NAME = re.compile(r"[_a-z]\w*\.py$", re.IGNORECASE)
DEFAULT_JOBS_CAP = 8


def discover_tests(start: Path, pattern: str) -> list[tuple[str, str]]:
    """与 `unittest discover -s start -p pattern` 相同的文件发现口径（T713 修订 2）。

    按 discover 的规则只递归进带 `__init__.py` 的子包；文件须是合法模块名且 fnmatch 匹配
    模式。返回 [(相对测试目录的 posix 路径, 点分模块名)]，按路径排序保证批次确定；不同
    子包里的同名文件模块名不同，各算一个。
    """
    found: list[tuple[str, str]] = []

    def walk(directory: Path, prefix: str) -> None:
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.is_dir():
                if (entry / "__init__.py").is_file():
                    walk(entry, prefix + entry.name + ".")
            elif entry.is_file() and VALID_MODULE_NAME.match(entry.name) \
                    and fnmatch.fnmatch(entry.name, pattern):
                found.append((entry.relative_to(start).as_posix(), prefix + entry.stem))

    walk(start, "")
    return found


def _child_env() -> dict[str, str]:
    """子进程环境：继承调用方，再把调用目录放进 PYTHONPATH 最前（与 discover 的 sys.path 口径一致）。"""
    env = dict(os.environ)
    entries = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
    if os.getcwd() not in entries:
        env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), *entries])
    return env


def run_file(top_level: Path, module: str, name: str) -> dict:
    """单个测试文件一个子进程：`-m unittest <点分模块名>`，工作目录与 -t 同口径，环境继承调用方。"""
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", module],
        cwd=str(top_level), env=_child_env(),
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
    parser.add_argument("--serial", action="append", default=[], metavar="文件",
                        help="并行批次结束后逐个顺序执行的文件（相对路径或文件名，可多次）")
    args = parser.parse_args(argv)

    start = Path(args.start)
    tests = discover_tests(start, args.pattern)
    serial_specs = list(dict.fromkeys(args.serial))

    def is_serial(name: str) -> bool:  # 相对路径精确匹配，或文件名匹配（同名的所有文件都算）
        return any(spec == name or spec == Path(name).name for spec in serial_specs)

    unknown = [spec for spec in serial_specs if not any(is_serial(name) for name, _ in tests)]
    if unknown:
        print(f"run-tests：--serial 里的文件不在 {start} 的匹配结果中：{', '.join(unknown)}", file=sys.stderr)
        return 2
    if not tests:
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

    parallel = [item for item in tests if not is_serial(item[0])]
    serial = [item for item in tests if is_serial(item[0])]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_file, start, module, name) for name, module in parallel]
        for future in as_completed(futures):
            result = future.result()
            report(result)
            results.append(result)
    for name, module in serial:  # 共享状态的测试逐个顺序执行，不与任何文件重叠
        result = run_file(start, module, name)
        report(result)
        results.append(result)

    results.sort(key=lambda r: r["name"])
    _print_report(results, started)
    return 1 if any(r["code"] != 0 for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
