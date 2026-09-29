"""R1（行为不变重构）的加强判定：只读 diff 与 base/head 的文件内容，不执行 PR 的代码。

2026-09-28 决定 2：R1 保持自动合并，但以下任一不满足即降为 R2（risk.py 调用）：
  - 被改函数签名不变：base 中已有的函数（Python 按 AST、Swift 按声明文本）签名不变、未被删除；新增函数不限
  - 无新依赖：新增的 Python import 只能是标准库或仓库内模块；Swift 不新增 import
  - 无数据迁移：产品代码不新增建表、改表、删表语句（迁移脚本本身按 rules.toml 判 R3）
  - 不超规模阈值：增删行数不超过 .harness/config/autonomy.toml [size] max_lines
已有测试零改动、黄金快照零差异由 risk.py 的标记核对；变异得分不降由 build 的 harness job 在声明 R1 时运行
`mutate.py --check --changed-since`（要执行代码，放在 PR 自己的 CI 里，下降即失败）。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from engine import lang
from engine.core.common import CONFIG_DIR, ROOT, added_lines, changed_files, git, path_matches, setting

AUTONOMY_PATH = CONFIG_DIR / "autonomy.toml"
SCHEMA = re.compile(r"(?i)\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX|VIEW|TRIGGER)\b")


def load_autonomy(path: Path = AUTONOMY_PATH) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


python_signatures = lang.python.signatures
swift_signatures = lang.swift.signatures


def signature_changes(path: str, before: str, after: str) -> list[str]:
    plugin = lang.for_path(path)
    if plugin is None:
        return []
    old, new = plugin.signatures(before), plugin.signatures(after)
    changes = []
    for name, signature in old.items():
        if name not in new:
            changes.append(f"`{path}` 删除了函数 `{name}`")
        elif new[name] != signature:
            changes.append(f"`{path}` 改了函数 `{name}` 的签名")
    return changes


def _local_modules(head: str, cwd: Path) -> set[str]:
    """仓库内模块名：checks.toml [sources] python_dirs 下的文件与目录名（新增对它们的 import 不算新依赖）。"""
    names = []
    for directory in setting("sources", "python_dirs", []):
        names += git("ls-tree", "--name-only", f"{head}:{directory}", cwd=cwd, check=False).split()
    return {Path(name).stem for name in names}


def new_dependencies(path: str, lines: list[str], local: set[str]) -> list[str]:
    plugin = lang.for_path(path)
    return plugin.new_dependencies(path, lines, local) if plugin else []


def changed_line_count(base: str, head: str, cwd: Path, exclude: list[str]) -> int:
    total = 0
    for line in git("diff", "--numstat", "--no-renames", f"{base}...{head}", cwd=cwd).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and not path_matches(parts[2], exclude):
            total += sum(int(value) for value in parts[:2] if value.isdigit())
    return total


def violations(base: str, head: str, cwd: Path = ROOT, autonomy: dict | None = None) -> list[str]:
    """返回不满足 R1 加强判定的理由；空列表表示满足。"""
    autonomy = autonomy or load_autonomy()
    size = autonomy.get("size", {})
    reasons = []
    merge_base = git("merge-base", base, head, cwd=cwd)
    added = added_lines(base, head, cwd)
    local = _local_modules(head, cwd)
    for status, path in changed_files(base, head, cwd):
        if not path_matches(path, setting("sources", "code", [])):
            continue
        if status in {"M", "D"}:
            before = git("show", f"{merge_base}:{path}", cwd=cwd)
            after = git("show", f"{head}:{path}", cwd=cwd) if status == "M" else ""
            reasons += signature_changes(path, before, after)
        new_text = [text for _, text in added.get(path, [])]
        reasons += new_dependencies(path, new_text, local)
        if any(SCHEMA.search(text) for text in new_text):
            reasons.append(f"`{path}` 新增了建表、改表或删表语句（数据迁移）")
    lines = changed_line_count(base, head, cwd, size.get("exclude", []))
    limit = size.get("max_lines", 400)
    if lines > limit:
        reasons.append(f"增删 {lines} 行，超过规模阈值 {limit}")
    return reasons
