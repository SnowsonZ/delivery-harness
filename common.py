"""harness 公共工具：仓库根、git 调用、规则加载、路径通配。

harness 不进发布包（scripts/ 会被整体拷进 App，harness/ 不会），只依赖标准库。
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "harness" / "rules.toml"
ZERO_SHA = "0" * 40

# git 在执行钩子时会注入这些变量；对其他仓库或工作树调用 git 时必须清掉，
# 否则命令会落到钩子所在的仓库上。
_GIT_LOCAL_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
    "GIT_COMMON_DIR",
)


def clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _GIT_LOCAL_ENV}
    if extra:
        env.update(extra)
    return env


def git(*args: str, cwd: Path | str = ROOT, check: bool = True, isolate: bool = False) -> str:
    """运行 git 并返回 stdout（去掉末尾换行）。isolate=True 时清掉钩子注入的仓库变量。"""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=clean_git_env() if isolate else None,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.rstrip("\n")


@cache
def load_rules(path: Path = RULES_PATH) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


@cache
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """把 `**`、`*`、`?` 通配转成正则。`**/` 可匹配零层目录，`*` 不跨目录。"""
    out = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile("".join(out) + r"\Z")


def path_matches(path: str, patterns: list[str]) -> str | None:
    """返回第一个命中的模式；都不命中返回 None。"""
    for pattern in patterns:
        if _glob_regex(pattern).match(path):
            return pattern
    return None


def changed_files(base: str, head: str = "HEAD", cwd: Path | str = ROOT) -> list[tuple[str, str]]:
    """base...head 的改动（按合并基计算），返回 [(状态字母, 路径)]；改名按新路径计。"""
    raw = git("diff", "--name-status", "--no-renames", f"{base}...{head}", cwd=cwd)
    files = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        files.append((status[:1], path))
    return files


def added_lines(base: str, head: str = "HEAD", cwd: Path | str = ROOT) -> dict[str, list[tuple[int, str]]]:
    """base...head 中每个文件新增的行 {路径: [(新行号, 内容)]}。"""
    diff = git("diff", "--unified=0", "--no-renames", "--no-color", f"{base}...{head}", cwd=cwd)
    return parse_added_lines(diff)


def parse_added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    result: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    line_no = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = None if target == "/dev/null" else target.removeprefix("b/")
            if current is not None:
                result.setdefault(current, [])
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            line_no = int(match.group(1)) if match else 0
        elif line.startswith("+") and current is not None:
            result[current].append((line_no, line[1:]))
            line_no += 1
    return result


def commit_field(sha: str, key: str, cwd: Path | str = ROOT) -> list[str]:
    """提交说明中所有 `Key: value` 行的值。

    不用 git 的 trailer 解析：git 只认最后一段，`Defect:` 与 `Co-Authored-By:` 之间隔一个空行
    就会被静默忽略，证据检查随之形同虚设（2026-09-25 实际踩到）。"""
    message = git("log", "-1", "--format=%B", sha, cwd=cwd)
    pattern = re.compile(rf"^{re.escape(key)}:[ \t]*(.+?)[ \t]*$", flags=re.MULTILINE)
    return pattern.findall(message)


def removed_line_count(base: str, head: str, path: str, cwd: Path | str = ROOT) -> int:
    """base...head 中某文件被删除或改写的行数（numstat 的删除列）。"""
    raw = git("diff", "--numstat", "--no-renames", f"{base}...{head}", "--", path, cwd=cwd)
    total = 0
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].isdigit():
            total += int(parts[1])
    return total
