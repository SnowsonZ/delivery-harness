"""Agent 层命令守卫：在工具调用执行前拒绝破坏性或绕过护栏的操作。

硬边界在 GitHub 与 git 两层（Zcode 宿主没有工具调用 hook，靠不了 Agent 层）；这一层给能挂钩子的
Agent 提供最早的反馈，并阻止 Agent 自己设置只属于人的覆盖变量。

输入格式（stdin）：
  --format claude   Claude Code / Codex PreToolUse 载荷：{"tool_name": ..., "tool_input": {...}}
  --format json     {"command": "..."} 或 {"file_path": "..."}（OpenCode 插件用）
  --format plain    整段 stdin 就是命令
拒绝时退出码 2，理由写 stderr（Claude Code 会把它反馈给模型）；放行退出码 0。

--role implementer 另外禁止编辑判定器与护栏（CI、hooks、harness、Agent 配置）：执行者改不了判定器。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import path_matches

# (模式, 理由)。按整条命令匹配，`&&`、`;`、管道串起来的命令同样生效。
COMMAND_RULES: list[tuple[str, str]] = [
    (
        r"\bgit\s+filter-(repo|branch)\b|(^|[;&|(]|&&)\s*(\S*/)?git-filter-repo\b",
        "改写历史（v0.8.0 曾因此改写 main 与 18 个 tag）",
    ),
    (r"\bgit\s+push\b[^;&|]*\s(--force(-with-lease)?(=\S*)?|-f)(\s|$)", "强制推送：已推送的历史不改写"),
    (r"\bgit\s+push\b[^;&|]*\s\+\S+", "强制推送（+refspec）：已推送的历史不改写"),
    (r"\bgit\s+push\b[^;&|]*\s--(mirror|all|tags)\b", "批量推送（--mirror/--all/--tags）"),
    (r"\bgit\s+push\b[^;&|]*\s(\S*:)?(refs/heads/)?main(\s|$)", "直接推送 main：请推功能分支并开 PR"),
    (r"\bgit\s+push\b[^;&|]*\s(\S*:)?(refs/tags/\S+|v\d[\w.-]*)(\s|$)", "推送 tag：推 tag 会触发发布，只能由用户执行"),
    (r"\bgit\s+tag\s+(?!(-l|--list|-n\d*|--contains|--points-at|--sort)\b)\S", "创建、移动或删除 tag：发版由用户执行"),
    (r"\bgit\s+update-ref\b[^;&|]*(refs/heads/main|refs/tags/)", "直接改写 main 或 tag 引用"),
    (r"\bgit\s+(commit|push|merge|rebase|am)\b[^;&|]*\s--no-verify\b", "--no-verify 跳过 git 守卫"),
    (r"\bgit\s+(commit|merge)\b[^;&|]*\s-[a-zA-Z]*n[a-zA-Z]*(\s|$)", "-n（--no-verify）跳过 git 守卫"),
    (
        # 只拦设置或取消；`git config --get` 等只读查询放行（此前任何提及都拦，误伤自检）。
        (
            r"(?i)-c\s*core\.hookspath=|\bgit\s+config\b(?![^;&|]*--(get|get-all|get-regexp|list|show-origin)\b)"
            r"[^;&|]*core\.hookspath"
        ),
        "修改 core.hooksPath 会绕过 git 守卫",
    ),
    (r"\bHARNESS_(ALLOW_[A-Z]+|SKIP_VERIFY)\s*=", "覆盖变量只供人使用，Agent 不能自行放开守卫"),
    (r"\bgit\s+reset\s+[^;&|]*--hard\b", "git reset --hard 会丢弃未提交的改动（v0.8.0 X1 的修复就这样丢失）；先提交或 stash"),
    (r"\bgit\s+clean\b(?=[^;&|]*\s-\w*[xX])(?![^;&|]*-e\s+scratch/iterm-probe-venv)", "git clean -x 会删除 scratch/iterm-probe-venv（AGENTS.md）；加 -e scratch/iterm-probe-venv"),
    (
        r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\b[^;&|]*?\s(/(?!tmp/|private/tmp/|var/folders/)|~|\$HOME|\.\.)",
        "递归删除工作区以外的路径（临时目录 /tmp 除外）",
    ),
    (r"\bgh\s+release\b", "gh release：发版由用户执行"),
    (r"\bgh\s+(run|repo)\s+delete\b|\bgh\s+api\b[^;&|]*-X\s*DELETE", "删除 CI 记录或远端资源会销毁证据"),
]
# 执行者（--role implementer）不能编辑的路径：判定器与护栏由评审方维护。
PROTECTED_FOR_IMPLEMENTER = [
    ".github/**",
    ".githooks/**",
    "harness/**",
    ".claude/**",
    ".opencode/**",
    "requirements-dev.txt",
    "ruff.toml",
]

_COMPILED = [(re.compile(pattern), reason) for pattern, reason in COMMAND_RULES]


def check_command(command: str) -> list[str]:
    normalized = " " + re.sub(r"\s+", " ", command.strip()) + " "
    return [reason for pattern, reason in _COMPILED if pattern.search(normalized)]


def _relative(path: str, root: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix().removeprefix("./")


def check_edit(path: str, role: str, root: Path) -> list[str]:
    if role != "implementer":
        return []
    relative = _relative(path, root)
    pattern = path_matches(relative, PROTECTED_FOR_IMPLEMENTER)
    if pattern:
        return [f"执行者不能编辑判定器与护栏（{relative} 命中 {pattern}）；需要改动请升级给评审方"]
    return []


def extract(payload: dict) -> tuple[str | None, str | None]:
    """从各家载荷中取出 (命令, 文件路径)。"""
    tool_input = payload.get("tool_input", payload)
    if not isinstance(tool_input, dict):
        return None, None
    command = tool_input.get("command") or tool_input.get("cmd")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    path = tool_input.get("file_path") or tool_input.get("filePath") or tool_input.get("path")
    return (command if isinstance(command, str) else None), (path if isinstance(path, str) else None)


def evaluate(payload: dict, role: str = "designer", root: Path | None = None) -> list[str]:
    root = root or Path.cwd()
    command, path = extract(payload)
    reasons = []
    if command:
        reasons += check_command(command)
    if path:
        reasons += check_edit(path, role, root)
    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--format", choices=["claude", "json", "plain"], default="claude")
    parser.add_argument("--role", choices=["designer", "implementer"], default="designer")
    args = parser.parse_args(argv)
    raw = sys.stdin.read()
    if args.format == "plain":
        payload = {"command": raw}
    else:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            print("harness 守卫：无法解析工具调用载荷，按拒绝处理。", file=sys.stderr)
            return 2
    reasons = evaluate(payload, args.role)
    if not reasons:
        return 0
    print("harness 守卫拒绝了这次操作：", file=sys.stderr)
    for reason in dict.fromkeys(reasons):
        print(f"  - {reason}", file=sys.stderr)
    print("  如确需执行，请把原因和命令交给用户决定（docs/specs/delivery-harness.md）。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
