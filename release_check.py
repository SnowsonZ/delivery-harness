"""发版前核对（推 v* tag 时由 CI 的 harness job 运行）。把 AGENTS.md 的发版规则变成机器检查：

  1. tag 名 = "v" + BUNDLE_SHORT_VERSION（scripts/build_inbox_app.py）
  2. BUNDLE_VERSION 是整数，且大于上一个 v* tag 的值（两处版本号不能只改其一）
  3. tag 指向的提交在 main 上（发版只从合入 main 的代码打）

版本号本身必须先与用户确认；release job 另有 environment 审批，由用户放行。

    python3 harness/release_check.py --tag v0.8.1 [--commit HEAD] [--main origin/main]
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, git

BUILD_SCRIPT = "scripts/build_inbox_app.py"


def read_versions(source: str) -> dict[str, str]:
    versions = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("BUNDLE_SHORT_VERSION", "BUNDLE_VERSION") and isinstance(node.value, ast.Constant):
                versions[name] = str(node.value.value)
    return versions


def previous_tag(tag: str, commit: str, cwd: Path) -> str | None:
    """commit 之前最近的另一个 v* tag。"""
    for candidate in git("tag", "--merged", commit, "--sort=-creatordate", "--list", "v*", cwd=cwd).splitlines():
        if candidate and candidate != tag:
            return candidate
    return None


def check(tag: str, commit: str = "HEAD", main: str = "origin/main", cwd: Path = ROOT) -> list[str]:
    problems = []
    current = read_versions(git("show", f"{commit}:{BUILD_SCRIPT}", cwd=cwd))
    short, build = current.get("BUNDLE_SHORT_VERSION"), current.get("BUNDLE_VERSION")
    if tag != f"v{short}":
        problems.append(f"tag {tag} 与 BUNDLE_SHORT_VERSION {short!r} 不一致")
    if not build or not re.fullmatch(r"\d+", build):
        problems.append(f"BUNDLE_VERSION {build!r} 不是整数")
    else:
        prev = previous_tag(tag, commit, cwd)
        if prev:
            old = read_versions(git("show", f"{prev}:{BUILD_SCRIPT}", cwd=cwd, check=False)).get("BUNDLE_VERSION")
            if old and old.isdigit() and int(build) <= int(old):
                problems.append(f"BUNDLE_VERSION {build} 没有大于上一个 tag {prev} 的 {old}（两处版本号需同步）")
    on_main = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, main], cwd=cwd, capture_output=True, check=False
    )
    if on_main.returncode != 0:
        problems.append(f"tag 指向的提交不在 {main} 上：发版只从合入 main 的代码打")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--main", default="origin/main")
    args = parser.parse_args(argv)
    problems = check(args.tag, args.commit, args.main)
    for problem in problems:
        print(f"✗ {problem}")
    if problems:
        return 1
    print(f"✓ {args.tag} 与构建版本一致，且在 main 上")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
