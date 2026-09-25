"""统一验证入口：本机、云端、CI 用同一条命令、同一组检查。

    bin/verify              默认档：工具版本、lint、仓库卫生、Python 测试、Swift 测试（仅 macOS）
    bin/verify --quick      快速档：工具版本、lint、仓库卫生（pre-commit 用）
    bin/verify --full       完整档：默认档 + 较慢的检查
    bin/verify --strict     任何被跳过的检查都算失败（macOS CI 用，防止 Swift 检查被静默跳过）

终端只打印每项结论；完整输出写到 build/verify/<检查名>.log，汇总写到 build/verify/summary.json。
通过与否以本命令的退出码和 CI 上当前 head 的运行为准，不以任何人的自述为准。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, git

LOG_DIR = ROOT / "build" / "verify"
DEV_REQUIREMENTS = ROOT / "requirements-dev.txt"
MIN_PYTHON = (3, 11)
TAIL_LINES = 30


@dataclass
class Check:
    name: str
    tiers: tuple[str, ...]
    command: list[str] | None = None
    shell: str | None = None
    requires: str | None = None  # "macos"
    why_skipped: str = ""
    func: object = None  # 进程内检查：返回 (ok, 输出)


@dataclass
class Result:
    name: str
    status: str  # pass / fail / skip
    seconds: float = 0.0
    log: str = ""
    note: str = ""
    tail: list[str] = field(default_factory=list)


def pinned_version(package: str) -> str | None:
    if not DEV_REQUIREMENTS.exists():
        return None
    for line in DEV_REQUIREMENTS.read_text().splitlines():
        match = re.match(rf"\s*{re.escape(package)}==([\w.]+)", line)
        if match:
            return match.group(1)
    return None


def check_tools() -> tuple[bool, str]:
    lines = []
    ok = True
    if sys.version_info < MIN_PYTHON:
        ok = False
        lines.append(f"Python {platform.python_version()} < {'.'.join(map(str, MIN_PYTHON))}")
    else:
        lines.append(f"Python {platform.python_version()} ✓")
    want = pinned_version("ruff")
    result = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True, check=False)
    have = result.stdout.strip().removeprefix("ruff ") if result.returncode == 0 else None
    if want is None:
        ok = False
        lines.append("requirements-dev.txt 未锁定 ruff 版本")
    elif have != want:
        ok = False
        lines.append(
            f"ruff {have or '未安装'} ≠ 锁定的 {want}；执行 `{sys.executable} -m pip install -r requirements-dev.txt`"
        )
    else:
        lines.append(f"ruff {have} ✓")
    return ok, "\n".join(lines)


def _is_macos() -> bool:
    return sys.platform == "darwin" and shutil.which("xcrun") is not None


def build_checks() -> list[Check]:
    py = sys.executable
    return [
        Check("tools", ("quick", "default", "full"), func=check_tools),
        Check("lint", ("quick", "default", "full"), command=[py, "-m", "ruff", "check", "scripts", "tests", "harness"]),
        Check("hygiene", ("quick", "default", "full"), command=[py, "harness/hygiene.py", "--tracked"]),
        Check(
            "python-tests",
            ("default", "full"),
            command=[py, "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", "tests"],
        ),
        Check(
            "swift-policy",
            ("default", "full"),
            shell="mkdir -p build && xcrun swiftc -parse-as-library native/InboxPolicy.swift "
            "native/Shared/WidgetSnapshot.swift tests/InboxPolicyTests.swift -o build/policy-tests "
            "&& ./build/policy-tests",
            requires="macos",
            why_skipped="非 macOS，Swift 检查由 macOS CI 负责",
        ),
        Check(
            "swift-runner",
            ("default", "full"),
            shell="mkdir -p build && xcrun swiftc -parse-as-library native/ProcessRunner.swift "
            "tests/ProcessRunnerTests.swift -o build/runner-tests && ./build/runner-tests",
            requires="macos",
            why_skipped="非 macOS，Swift 检查由 macOS CI 负责",
        ),
    ]


def run_check(check: Check) -> Result:
    if check.requires == "macos" and not _is_macos():
        return Result(check.name, "skip", note=check.why_skipped)
    target = LOG_DIR / f"{check.name}.log"
    started = time.monotonic()
    if check.func is not None:
        ok, output = check.func()
        target.write_text(output + "\n")
        code = 0 if ok else 1
    else:
        with open(target, "w") as log:
            completed = subprocess.run(
                check.command if check.command else check.shell,
                shell=check.command is None,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                check=False,
            )
        code = completed.returncode
    seconds = time.monotonic() - started
    relative = str(target.relative_to(ROOT))
    if code == 0:
        return Result(check.name, "pass", seconds, relative)
    tail = target.read_text(errors="replace").splitlines()[-TAIL_LINES:]
    return Result(check.name, "fail", seconds, relative, f"退出码 {code}", tail)


def head_info() -> dict:
    try:
        sha = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except RuntimeError:
        sha, dirty = "unknown", True
    return {"head": sha, "dirty": dirty}


def write_step_summary(results: list[Result], info: dict) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    icon = {"pass": "✅", "fail": "❌", "skip": "⏭️"}
    lines = [f"### verify @ `{info['head'][:12]}`", "", "| 检查 | 结果 | 用时 | 说明 |", "|---|---|---|---|"]
    for result in results:
        lines.append(f"| {result.name} | {icon[result.status]} | {result.seconds:.1f}s | {result.note} |")
    with open(target, "a") as handle:
        handle.write("\n".join(lines) + "\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    tier = parser.add_mutually_exclusive_group()
    tier.add_argument("--quick", action="store_const", dest="tier", const="quick")
    tier.add_argument("--full", action="store_const", dest="tier", const="full")
    parser.add_argument("--only", help="只跑这些检查（逗号分隔）")
    parser.add_argument("--skip", default="", help="跳过这些检查（逗号分隔，会记入汇总）")
    parser.add_argument("--strict", action="store_true", help="被跳过的检查算失败")
    parser.add_argument("--json", action="store_true", help="结束时打印汇总 JSON")
    args = parser.parse_args(argv)
    tier_name = args.tier or "default"

    checks = [check for check in build_checks() if tier_name in check.tiers]
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {check.name for check in build_checks()}
        if unknown:
            parser.error(f"未知检查：{', '.join(sorted(unknown))}")
        checks = [check for check in build_checks() if check.name in wanted]
    skipped_by_user = {name for name in args.skip.split(",") if name}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    info = head_info()
    results = []
    for check in checks:
        if check.name in skipped_by_user:
            result = Result(check.name, "skip", note="--skip 手动跳过")
        else:
            result = run_check(check)
        if result.status == "skip" and args.strict:
            result = Result(check.name, "fail", note=f"--strict 下不允许跳过（{result.note}）")
        results.append(result)
        mark = {"pass": "✓", "fail": "✗", "skip": "-"}[result.status]
        extra = f"  {result.note}" if result.note else ""
        print(f"{mark} {result.name:<13} {result.seconds:5.1f}s{extra}", flush=True)
        for line in result.tail:
            print(f"    │ {line}")
        if result.tail:
            print(f"    └ 完整输出：{result.log}")

    failed = [result.name for result in results if result.status == "fail"]
    summary = {
        **info,
        "tier": tier_name,
        "strict": args.strict,
        "platform": sys.platform,
        "python": platform.python_version(),
        "results": [
            {"name": r.name, "status": r.status, "seconds": round(r.seconds, 2), "note": r.note, "log": r.log}
            for r in results
        ],
        "ok": not failed,
    }
    (LOG_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    write_step_summary(results, info)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    dirty = "（工作区有未提交改动）" if info["dirty"] else ""
    if failed:
        print(f"verify 失败：{', '.join(failed)} @ {info['head'][:12]}{dirty}")
        return 1
    print(f"verify 通过 @ {info['head'][:12]}{dirty}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
