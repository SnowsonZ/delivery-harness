"""仓库卫生检查：禁止路径、超大文件、凭据、本机真实路径。

三种范围：
  --tracked         全部已跟踪文件（路径、大小、凭据）；verify 默认用它
  --staged          暂存区（pre-commit 用）；新增行另查本机路径
  --range BASE      BASE...HEAD 的改动（pre-push / CI 用）；新增行另查本机路径

本机路径只查新增行：已有测试夹具里留有真实用户名，全量扫描会一上线就误报。
对应 v0.8.0 R1（真实快照与 .o 被 `git add -A` 带进公开仓库）。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, git, load_rules, parse_added_lines, path_matches


@dataclass(frozen=True)
class Violation:
    path: str
    kind: str
    detail: str

    def render(self) -> str:
        return f"{self.path}: [{self.kind}] {self.detail}"


def _compiled(rules: dict) -> tuple[list[re.Pattern[str]], re.Pattern[str]]:
    hygiene = rules["hygiene"]
    secrets = [re.compile(pattern) for pattern in hygiene["secret_patterns"]]
    return secrets, re.compile(hygiene["home_path_pattern"])


def check_paths(paths: list[str], rules: dict) -> list[Violation]:
    violations = []
    for path in paths:
        pattern = path_matches(path, rules["hygiene"]["forbidden"])
        # allowed 是逐个列出的入库例外（如 Zcode 的项目守卫配置），不接受通配。
        if pattern and path not in rules["hygiene"].get("allowed", []):
            violations.append(Violation(path, "禁止路径", f"命中 {pattern}；本机数据与产物不入库"))
    return violations


def check_sizes(sizes: dict[str, int], rules: dict) -> list[Violation]:
    limit = rules["hygiene"]["max_file_kb"] * 1024
    return [
        Violation(path, "超大文件", f"{size // 1024} KB > {limit // 1024} KB")
        for path, size in sizes.items()
        if size > limit
    ]


def check_added_lines(added: dict[str, list[tuple[int, str]]], rules: dict) -> list[Violation]:
    secrets, home = _compiled(rules)
    violations = []
    for path, lines in added.items():
        if path == "harness/rules.toml":  # 规则文件本身写着这些模式
            continue
        for line_no, text in lines:
            if any(pattern.search(text) for pattern in secrets):
                violations.append(Violation(f"{path}:{line_no}", "疑似凭据", "新增行命中凭据模式"))
            elif home.search(text):
                violations.append(
                    Violation(f"{path}:{line_no}", "本机路径", "新增行含真实用户目录；夹具请用 /Users/x/ 等占位")
                )
    return violations


def check_secrets_in_files(paths: list[str], rules: dict, cwd: Path) -> list[Violation]:
    secrets, _ = _compiled(rules)
    violations = []
    for path in paths:
        if path == "harness/rules.toml":
            continue
        full = cwd / path
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in secrets):
                violations.append(Violation(f"{path}:{line_no}", "疑似凭据", "已跟踪文件命中凭据模式"))
    return violations


def _blob_sizes(spec_lines: str) -> dict[str, int]:
    """解析 `git ls-files -s` / `git ls-tree -l` 风格的输出求对象大小。"""
    sizes = {}
    for line in spec_lines.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 4 and parts[3].isdigit():  # ls-tree -l: mode type sha size
            sizes[path] = int(parts[3])
    return sizes


def scan_tracked(cwd: Path = ROOT, rules: dict | None = None) -> list[Violation]:
    rules = rules or load_rules()
    paths = [p for p in git("ls-files", cwd=cwd).splitlines() if p]
    sizes = _blob_sizes(git("ls-tree", "-r", "-l", "HEAD", cwd=cwd, check=False))
    for path in paths:  # 已暂存但未提交的文件按工作区大小算
        if path not in sizes and (cwd / path).is_file():
            sizes[path] = (cwd / path).stat().st_size
    return check_paths(paths, rules) + check_sizes(sizes, rules) + check_secrets_in_files(paths, rules, cwd)


def scan_staged(cwd: Path = ROOT, rules: dict | None = None) -> list[Violation]:
    rules = rules or load_rules()
    changed = []
    for line in git("diff", "--cached", "--name-status", "--no-renames", cwd=cwd).splitlines():
        status, _, path = line.partition("\t")
        if status[:1] != "D":
            changed.append(path)
    sizes = {}
    for path in changed:
        raw = git("cat-file", "-s", f":{path}", cwd=cwd, check=False)
        if raw.isdigit():
            sizes[path] = int(raw)
    added = parse_added_lines(git("diff", "--cached", "--unified=0", "--no-renames", "--no-color", cwd=cwd))
    return check_paths(changed, rules) + check_sizes(sizes, rules) + check_added_lines(added, rules)


def scan_range(base: str, head: str = "HEAD", cwd: Path = ROOT, rules: dict | None = None) -> list[Violation]:
    rules = rules or load_rules()
    changed = []
    for line in git("diff", "--name-status", "--no-renames", f"{base}...{head}", cwd=cwd).splitlines():
        status, _, path = line.partition("\t")
        if status[:1] != "D":
            changed.append(path)
    sizes = {}
    for path in changed:
        raw = git("cat-file", "-s", f"{head}:{path}", cwd=cwd, check=False)
        if raw.isdigit():
            sizes[path] = int(raw)
    diff = git("diff", "--unified=0", "--no-renames", "--no-color", f"{base}...{head}", cwd=cwd)
    return check_paths(changed, rules) + check_sizes(sizes, rules) + check_added_lines(parse_added_lines(diff), rules)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--tracked", action="store_true", help="全部已跟踪文件（默认）")
    scope.add_argument("--staged", action="store_true", help="暂存区")
    scope.add_argument("--range", metavar="BASE", help="BASE...HEAD 的改动")
    args = parser.parse_args(argv)

    if args.staged:
        violations = scan_staged()
    elif args.range:
        violations = scan_range(args.range)
    else:
        violations = scan_tracked()

    for violation in violations:
        print(violation.render())
    if violations:
        print(f"仓库卫生：{len(violations)} 处问题。", file=sys.stderr)
        return 1
    print("仓库卫生：通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
