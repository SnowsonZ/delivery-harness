"""发版前核对（推 v* tag 时由 CI 的 harness job 运行）。把发版规则变成机器检查：

  1. tag 名 = "v" + 版本号（checks.toml [release] version_file 中 short_version_var 的值）
  2. 构建号（build_version_var，可不配）是整数，且大于上一个 v* tag 的值（两处版本号不能只改其一）
  3. tag 指向的提交在 main 上（发版只从合入 main 的代码打）

版本号本身必须先与用户确认；release job 另有 environment 审批，由用户放行。
版本文件目前支持 Python 源文件里的模块级常量赋值。

    bin/harness release-check --tag v0.8.1 [--commit HEAD] [--main origin/main]
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
from pathlib import Path

from engine.core.common import ROOT, git, setting


def version_file() -> str:
    return setting("release", "version_file", required=True)


def version_vars() -> tuple[str, str | None]:
    return setting("release", "short_version_var", "VERSION"), setting("release", "build_version_var")


def read_versions(source: str) -> dict[str, str]:
    """返回 {"short": 版本号, "build": 构建号}（缺的不出现）。"""
    short_var, build_var = version_vars()
    names = {short_var: "short", **({build_var: "build"} if build_var else {})}
    versions = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in names and isinstance(node.value, ast.Constant):
                versions[names[name]] = str(node.value.value)
    return versions


def previous_tag(tag: str, commit: str, cwd: Path) -> str | None:
    """commit 之前最近的另一个 v* tag。"""
    for candidate in git("tag", "--merged", commit, "--sort=-creatordate", "--list", "v*", cwd=cwd).splitlines():
        if candidate and candidate != tag:
            return candidate
    return None


def check(tag: str, commit: str = "HEAD", main: str = "origin/main", cwd: Path = ROOT) -> list[str]:
    problems = []
    source, (short_var, build_var) = version_file(), version_vars()
    current = read_versions(git("show", f"{commit}:{source}", cwd=cwd))
    short, build = current.get("short"), current.get("build")
    if tag != f"v{short}":
        problems.append(f"tag {tag} 与 {short_var} {short!r} 不一致")
    if build_var and (not build or not re.fullmatch(r"\d+", build)):
        problems.append(f"{build_var} {build!r} 不是整数")
    elif build_var:
        prev = previous_tag(tag, commit, cwd)
        if prev:
            old = read_versions(git("show", f"{prev}:{source}", cwd=cwd, check=False)).get("build")
            if old and old.isdigit() and int(build) <= int(old):
                problems.append(f"{build_var} {build} 没有大于上一个 tag {prev} 的 {old}（两处版本号需同步）")
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
