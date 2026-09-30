"""Agent 层命令守卫：在工具调用执行前拒绝破坏性或绕过护栏的操作。

硬边界在 GitHub 与 git 两层（Zcode 宿主没有工具调用 hook，靠不了 Agent 层）；这一层给能挂钩子的
Agent 提供最早的反馈，并阻止 Agent 自己设置只属于人的覆盖变量。

输入格式（stdin）：
  --format claude   Claude Code / Codex PreToolUse 载荷：{"tool_name": ..., "tool_input": {...}}
  --format json     {"command": "..."}、{"file_path": "..."} 或 {"tool_name": "..."}（OpenCode 插件用）
  --format plain    整段 stdin 就是命令
拒绝时退出码 2，理由写 stderr（Claude Code 会把它反馈给模型）；放行退出码 0。
拒绝在本报告边界逐条写 guard 事件（规则键、角色、工具类别、安全相对目标；理由与命令全文不进事件），
放行不记也不计数（设计 3.6，执行环节的放行数由 T105 从工具完成事件汇总）。

--role implementer 另外禁止编辑判定器与护栏（CI、hooks、harness、Agent 配置）：执行者改不了判定器；
也不能编辑合同（任务书与现役规格）与运行记录（docs/runs/），不能关闭、改动议题与标签。
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

from engine.core import events, shell_structure
from engine.core.common import path_matches, setting
from engine.guards import shell_structure as guard_structure

# (模式, 规则键, 理由)。按整条命令匹配，`&&`、`;`、管道串起来的命令同样生效。
# 规则键是拒绝事件里的稳定标识（设计 3.6）：理由文案可能调整且可能含命令字样，事件只记键、不复制理由。
COMMAND_RULES: list[tuple[str, str, str]] = [
    (
        r"\bgit\s+filter-(repo|branch)\b|(^|[;&|(]|&&)\s*(\S*/)?git-filter-repo\b",
        "history_rewrite",
        "改写历史（v0.8.0 曾因此改写 main 与 18 个 tag）",
    ),
    # 结尾用「后面不是词字符」而非空白：解释器代码里常紧跟引号或括号（os.system('git push --force')）。
    (r"\bgit\s+push\b[^;&|]*\s(--force(-with-lease)?(=\S*)?|-f)(?![\w-])", "force_push",
     "强制推送：已推送的历史不改写"),
    (r"\bgit\s+push\b[^;&|]*\s\+\S+", "force_push_refspec", "强制推送（+refspec）：已推送的历史不改写"),
    (r"\bgit\s+push\b[^;&|]*\s--(mirror|all|tags)\b", "batch_push", "批量推送（--mirror/--all/--tags）"),
    (r"\bgit\s+push\b[^;&|]*\s(\S*:)?(refs/heads/)?main(\s|$)", "push_main", "直接推送 main：请推功能分支并开 PR"),
    (r"\bgit\s+push\b[^;&|]*\s(\S*:)?(refs/tags/\S+|v\d[\w.-]*)(\s|$)", "push_tag",
     "推送 tag：推 tag 会触发发布，只能由用户执行"),
    (r"\bgit\s+tag\s+(?!(-l|--list|-n\d*|--contains|--points-at|--sort)\b)\S", "tag_write",
     "创建、移动或删除 tag：发版由用户执行"),
    (r"\bgit\s+update-ref\b[^;&|]*(refs/heads/main|refs/tags/)", "update_ref_protected", "直接改写 main 或 tag 引用"),
    (r"\bgit\s+(commit|push|merge|rebase|am)\b[^;&|]*\s--no-verify\b", "no_verify", "--no-verify 跳过 git 守卫"),
    (r"\bgit\s+(commit|merge)\b[^;&|]*\s-[a-zA-Z]*n[a-zA-Z]*(\s|$)", "no_verify_short", "-n（--no-verify）跳过 git 守卫"),
    (
        # 只拦设置或取消；`git config --get` 等只读查询放行（此前任何提及都拦，误伤自检）。
        (
            r"(?i)-c\s*core\.hookspath=|\bgit\s+config\b(?![^;&|]*--(get|get-all|get-regexp|list|show-origin)\b)"
            r"[^;&|]*core\.hookspath"
        ),
        "hookspath_write",
        "修改 core.hooksPath 会绕过 git 守卫",
    ),
    # 覆盖变量（override_var）不在此表：按 shell 结构只在真赋值位置判定，见 engine/guards/shell_structure（B60），
    # 避免 heredoc 正文与引号内的文字被误当赋值。
    (r"\bgit\s+reset\s+[^;&|]*--hard\b", "reset_hard",
     "git reset --hard 会丢弃未提交的改动（v0.8.0 X1 的修复就这样丢失）；先提交或 stash"),
    # 解析失败时的兜底（宁可误报）：-x 且没有任何 -e 排除即拒；能解析时由 shell_structure 按 [runtime] preserve 精确判断。
    (r"\bgit\s+clean\b(?=[^;&|]*\s-\w*[xX])(?![^;&|]*\s-e\s)", "clean_keep",
     "git clean -x 会删除本地运行时等被忽略的文件；用 -e 排除 checks.toml [runtime] preserve 登记的路径"),
    (
        r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\b[^;&|]*?\s(/(?!tmp/|private/tmp/|var/folders/)|~|\$HOME|\.\.)",
        "rm_outside",
        "递归删除工作区以外的路径（临时目录 /tmp 除外）",
    ),
    # 只读的 list、view 放行（审计 G11）；创建、编辑、上传、删除发布由用户执行。
    (r"\bgh\s+release\b(?!\s+(list|view)\b)", "release_write", shell_structure.RELEASE),
    # 议题与标签是逃逸、抽审、升级的登记（设计 14.3）：登记只会让系统更保守，删除或撤标签会让误差预算失真。
    (r"\bgh\s+issue\s+delete\b", "issue_delete", shell_structure.ISSUE_DELETE),
    (
        r"\bgh\s+(issue|pr)\s+edit\b[^;&|]*--remove-label[\s=][^;&|]*\b(escape|audit|escalation|budget-exceeded)\b",
        "label_erase",
        shell_structure.LABEL_ERASE,
    ),
    (
        r"\bgh\s+label\s+(delete|edit)\b[^;&|]*\b(escape|audit|escalation|budget-exceeded|class:K\d)\b",
        "label_erase",
        shell_structure.LABEL_ERASE,
    ),
    (r"\bgh\s+(run|repo)\s+delete\b", "delete_remote", "删除 CI 记录或远端资源会销毁证据"),
    (
        # gh api / curl 的写请求可绕过分支保护：PATCH 改远端 ref 即服务端强推，PUT contents 直写 main（评审 PR7-R6）。
        (
            r"(?i)\bgh\s+api\b[^;&|]*\s(-X\s*|--method[\s=]+)(POST|PUT|PATCH|DELETE)\b"
            r"|\bgh\s+api\b[^;&|]*\s(-f|-F|--field|--raw-field|--input)(\s|=)"
        ),
        "api_write",
        "GitHub API 写操作（改 ref、写文件、删资源）会绕过分支保护与评审；需要时交给用户",
    ),
    (
        # curl：与 api.github.com 同一条命令、任意位置出现写方法或请求体即拦，不依赖参数顺序（评审 PR7-R9）。
        # 选项区分大小写（-D 是只读的 dump-header），方法名不区分。
        (
            r"\bcurl\b(?=[^;&|]*api\.github\.com)"
            r"(?=[^;&|]*\s(-X\s*|--request[\s=]+)(?i:POST|PUT|PATCH|DELETE)\b"
            r"|[^;&|]*\s(-[dFT]\S*|--data\S*|--form\S*|--upload-file|--json)(\s|=|$))"
        ),
        "api_write",
        "GitHub API 写操作（改 ref、写文件、删资源）会绕过分支保护与评审；需要时交给用户",
    ),
]
# 合并 PR 不归 Agent（用户 2026-09-25 决定 D4）：R0/R1 由仓库 auto-merge 在门禁全绿后合并，R2 以上由用户合并。
MERGE_REASON = "合并 PR：R0/R1 由仓库 auto-merge 合并，R2 及以上由用户合并，Agent 不自行合并（方案 §13 D4）"
COMMAND_RULES.append((r"\bgh\s+pr\s+merge\b", "merge_pr", MERGE_REASON))
# 批准也不归 Agent（D3）：ruleset 要求非推送者批准最后一次推送，Agent 批准就等于替用户放行。
APPROVE_REASON = "批准 PR：合并前须由非推送者批准；R0/R1 由 auto-merge 的 App 批准，R2 及以上由用户批准，Agent 不批准（方案 §13 D3）"
COMMAND_RULES.append((r"\bgh\s+pr\s+review\b[^;&|]*\s(-a|--approve)(\s|=|$)", "approve_pr", APPROVE_REASON))
# 执行者另外不能关闭、重开、改动议题（含改标签）：逃逸与抽审由设计评审方处理（设计 14.3）。
ISSUE_REASON = shell_structure.ISSUE_IMPLEMENTER
IMPLEMENTER_COMMAND_RULES: list[tuple[str, str, str]] = [
    (r"\bgh\s+issue\s+(close|reopen|edit|transfer|lock|unlock|pin|unpin|delete)\b", "issue_write", ISSUE_REASON),
    (r"\bgh\s+pr\s+edit\b[^;&|]*--(add|remove)-label\b", "issue_write", ISSUE_REASON),
    (r"\bgh\s+label\b(?!\s+list\b)", "issue_write", ISSUE_REASON),
]
# 按工具名拒绝的 MCP 等非命令工具（如 GitHub MCP 的 merge_pull_request、enable_pr_auto_merge）。
TOOL_RULES: list[tuple[str, str, str]] = [
    (r"(?i)(^|[_.])(delete_issue|delete_label|label_write|update_label)$", "tool_label_erase",
     shell_structure.LABEL_ERASE),
    (r"(?i)(^|[_.])(merge_pull_request|enable_pr_auto_merge|merge_pr)$", "tool_merge_pr", MERGE_REASON),
    # MCP 的评审工具按名字看不出是批准还是评论，一律拒绝；Agent 的评审意见写进报告或用 gh pr comment。
    (
        (
            r"(?i)(^|[_.])(pull_request_review_write|create_pull_request_review|create_and_submit_pull_request_review"
            r"|submit_pending_pull_request_review|approve_pull_request|approve_pr)$"
        ),
        "tool_approve_pr",
        APPROVE_REASON,
    ),
]
# 执行者（--role implementer）不能编辑的路径：判定器与护栏由评审方维护。
PROTECTED_FOR_IMPLEMENTER = [
    ".github/**",
    ".githooks/**",
    ".harness/**",
    ".claude/**",
    ".opencode/**",
    ".pi/**",
    ".zcode/**",
    ".codex/**",
]


def protected_for_implementer() -> list[str]:
    """引擎固定的护栏路径，加上项目在 rules.toml [guard] implementer_protected 中登记的（如锁定工具版本的文件）。"""
    return PROTECTED_FOR_IMPLEMENTER + list(setting("guard", "implementer_protected", [], source="rules"))

# 受保护目录里执行方按规范必须更新的数据文件（逐个列出，H0926-2）。其单调性由 CI 兜底：
# 缺口清单只能缩减，新增条目 risk.py 判 R3（rules.toml [risk] shrink_only）。
IMPLEMENTER_EDITABLE = [".harness/state/acceptance-gaps.txt"]

# 合同对执行方只读（设计文档 4.1）：任务书与现役规格是判定器的一部分。
CONTRACTS_FOR_IMPLEMENTER = ["docs/plans/task-*.md", "docs/specs/**"]
# 运行记录由派发脚本生成，是度量与审计的输入（设计 10.1），执行方不能编辑。
RUNS_FOR_IMPLEMENTER = ["docs/runs/**"]
IMPLEMENTER_TOOL_RULES: list[tuple[str, str, str]] = [
    (
        r"(?i)(^|[_.])(issue_write|update_issue|close_issue|reopen_issue|add_issue_labels?|remove_issue_labels?)$",
        "tool_issue_write",
        ISSUE_REASON,
    ),
]

_COMPILED = [(re.compile(pattern), key, reason) for pattern, key, reason in COMMAND_RULES]
_TOOLS = [(re.compile(pattern), key, reason) for pattern, key, reason in TOOL_RULES]
_IMPLEMENTER_COMMANDS = [(re.compile(pattern), key, reason)
                         for pattern, key, reason in IMPLEMENTER_COMMAND_RULES]
_IMPLEMENTER_TOOLS = [(re.compile(pattern), key, reason) for pattern, key, reason in IMPLEMENTER_TOOL_RULES]

# 结构化规则（shell_structure）给出的理由 → 规则键；随后命令字符串规则表覆盖同名理由（同类拒绝共用一键）。
_COMMAND_REASON_KEYS: dict[str, str] = {
    shell_structure.FILTER: "history_rewrite",
    shell_structure.FORCE: "force_push",
    shell_structure.FORCE_REFSPEC: "force_push_refspec",
    shell_structure.BATCH: "batch_push",
    shell_structure.PUSH_MAIN: "push_main",
    shell_structure.PUSH_TAG: "push_tag",
    shell_structure.TAG: "tag_write",
    shell_structure.UPDATE_REF: "update_ref_protected",
    shell_structure.NO_VERIFY: "no_verify",
    shell_structure.NO_VERIFY_SHORT: "no_verify_short",
    shell_structure.HOOKS_PATH: "hookspath_write",
    shell_structure.OVERRIDE: "override_var",
    shell_structure.RESET_HARD: "reset_hard",
    shell_structure.RM_OUTSIDE: "rm_outside",
    shell_structure.RELEASE: "release_write",
    shell_structure.ISSUE_DELETE: "issue_delete",
    shell_structure.LABEL_ERASE: "label_erase",
    shell_structure.ISSUE_IMPLEMENTER: "issue_write",
    shell_structure.DELETE_REMOTE: "delete_remote",
    shell_structure.API_WRITE: "api_write",
    shell_structure.MERGE: "merge_pr",
    shell_structure.APPROVE: "approve_pr",
}
for _rules in (COMMAND_RULES, IMPLEMENTER_COMMAND_RULES):
    for _pattern, _key, _reason in _rules:
        _COMMAND_REASON_KEYS[_reason] = _key
_TOOL_REASON_KEYS = {reason: key for _pattern, key, reason in TOOL_RULES + IMPLEMENTER_TOOL_RULES}
# 带填充值的理由（clean_keep 的保留路径清单）按前缀取键。
_REASON_KEY_PREFIXES = [(shell_structure.CLEAN_X.split("{paths}")[0], "clean_keep")]


def rule_key(reason: str, category: str = "command") -> str:
    """拒绝理由 → 稳定规则键。理由文本可能含命令、路径或会话正文，永远不进事件；
    未登记的理由（如将来新增规则暂未配键）统一记 guard_denial，只影响粒度、不影响拒绝本身。"""
    table = _TOOL_REASON_KEYS if category == "tool" else _COMMAND_REASON_KEYS
    if reason in table:
        return table[reason]
    for prefix, key in _REASON_KEY_PREFIXES:
        if reason.startswith(prefix):
            return key
    return "guard_denial"


def check_tool(name: str, role: str = "designer") -> list[str]:
    rules = _TOOLS + (_IMPLEMENTER_TOOLS if role == "implementer" else [])
    return [reason for pattern, _key, reason in rules if pattern.search(name)]


def check_command(command: str, role: str = "designer") -> list[str]:
    """按命令结构判断：只检查真正会执行的命令及其参数，echo、grep、提交说明、提示词里的文字不算（方案 §13 E3）。"""
    try:
        reasons = shell_structure.check(command, lambda text: check_command_text(text, role), role)
    except ValueError:  # 引号不配对等无法解析：退回字符串规则（宁可误报）
        reasons = check_command_text(command, role)
    # 覆盖变量单独按结构判定（B60）：heredoc 正文与引号内文字是数据，只在真赋值位置拒绝。
    reasons += guard_structure.check(command)
    return list(dict.fromkeys(reasons))


def check_command_text(command: str, role: str = "designer") -> list[str]:
    """字符串规则：整段文本里出现危险字样即拒。只用于会执行代码的文本（解释器代码、交给 shell 的 stdin）
    与无法解析的命令。覆盖变量不在此列：它按 shell 结构判定（guard_structure.check），
    否则解释器代码与 stdin 正文里的文字会被误当赋值（B60）。"""
    normalized = " " + re.sub(r"\s+", " ", command.strip()) + " "
    # 再检查一遍去掉引号与反斜杠的形式：`"HARNESS"'_ALLOW_TAG'`、`HAR\\NESS_...` 这类拆写（PR7-R6）。
    unquoted = re.sub(r"[\"'\\]", "", normalized)
    rules = _COMPILED + (_IMPLEMENTER_COMMANDS if role == "implementer" else [])
    return [reason for pattern, _key, reason in rules
            if pattern.search(normalized) or pattern.search(unquoted)]


def _relative(path: str, root: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix().removeprefix("./")


def _edit_denials(path: str, role: str, root: Path) -> list[tuple[str, str]]:
    """编辑拒绝的 (规则键, 理由)。理由含目标路径，事件里只取键与安全目标（见 _safe_target）。"""
    if role != "implementer":
        return []
    relative = _relative(path, root)
    pattern = path_matches(relative, protected_for_implementer())
    if pattern and relative not in IMPLEMENTER_EDITABLE:
        return [("edit_protected", f"执行者不能编辑判定器与护栏（{relative} 命中 {pattern}）；需要改动请升级给评审方")]
    pattern = path_matches(relative, CONTRACTS_FOR_IMPLEMENTER)
    if pattern:
        return [("edit_contract", f"执行者不能编辑合同（{relative} 命中 {pattern}）：任务书与规格由设计方维护，需要改时写升级包")]
    pattern = path_matches(relative, RUNS_FOR_IMPLEMENTER)
    if pattern:
        return [("edit_runs", f"执行者不能编辑运行记录（{relative} 命中 {pattern}）：运行记录由派发脚本生成")]
    return []


def check_edit(path: str, role: str, root: Path) -> list[str]:
    return [reason for _key, reason in _edit_denials(path, role, root)]


def _safe_target(path: str, root: Path) -> str | None:
    """编辑拒绝的仓库相对目标：只在能安全归化为仓库内相对路径时返回，否则省略（None）。

    不把本机绝对路径清洗成貌似可信的相对路径：根外绝对路径、逃出仓库的相对路径一律省略。
    """
    candidate = Path(path)
    try:
        if candidate.is_absolute():
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        relative = candidate.as_posix().removeprefix("./")
    except (ValueError, OSError):
        return None
    parts = Path(relative).parts
    if not relative or parts in ((".",), ("/",)) or ".." in parts:
        return None
    return relative


def denial_entries(payload: dict, role: str = "designer",
                   root: Path | None = None) -> list[tuple[str, str, str, str | None]]:
    """报告边界的拒绝条目 (理由, 类别, 规则键, 安全相对目标)：evaluate 的理由序列即本表的理由投影。

    类别为 tool / command / edit；目标只对编辑拒绝从载荷的文件路径安全提取，命令拒绝不解析命令文本、
    一律省略。理由可能含命令、路径或会话正文，调用方不得把它写进事件。
    """
    root = root or Path.cwd()
    command, path = extract(payload)
    entries: list[tuple[str, str, str, str | None]] = []
    tool = payload.get("tool_name")
    if isinstance(tool, str):
        entries += [(reason, "tool", rule_key(reason, "tool"), None) for reason in check_tool(tool, role)]
    if command:
        entries += [(reason, "command", rule_key(reason), None) for reason in check_command(command, role)]
    if path:
        target = _safe_target(path, root)
        entries += [(reason, "edit", key, target) for key, reason in _edit_denials(path, role, root)]
    return entries


def extract(payload: dict) -> tuple[str | None, str | None]:
    """从各家载荷中取出 (命令, 文件路径)。"""
    tool_input = payload.get("tool_input", payload)
    if not isinstance(tool_input, dict):
        return None, None
    command = tool_input.get("command") or tool_input.get("cmd")
    if isinstance(command, list):
        command = shlex.join(str(part) for part in command)  # 保留参数边界（bash -lc "…" 的整段）
    path = tool_input.get("file_path") or tool_input.get("filePath") or tool_input.get("path")
    return (command if isinstance(command, str) else None), (path if isinstance(path, str) else None)


def evaluate(payload: dict, role: str = "designer", root: Path | None = None) -> list[str]:
    """拒绝理由列表：按工具、命令、编辑顺序聚合，按理由去重（口径与埋点前的判定一致）。"""
    return list(dict.fromkeys(entry[0] for entry in denial_entries(payload, role, root)))


def _emit_denials(entries: list[tuple[str, str, str, str | None]], role: str) -> None:
    """拒绝报告边界逐条记事件（设计 3.6）：只有规则键、角色、类别与安全相对目标。

    理由与命令全文不进事件；emit 失败自行吞掉并提示一次，退出码与 stderr 的拒绝文本不受影响。
    """
    seen: set[tuple[str, str, str | None]] = set()
    for _reason, category, key, target in entries:
        identity = (category, key, target)
        if identity in seen:
            continue
        seen.add(identity)
        outputs = {"category": category}
        if target:
            outputs["target"] = target
        events.emit("guard", "command", "deny", decision={"by": "guard", "rule": key},
                    outputs=outputs, actor={"role": role})


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
    entries = denial_entries(payload, args.role)
    if not entries:
        return 0
    print("harness 守卫拒绝了这次操作：", file=sys.stderr)
    for reason in dict.fromkeys(entry[0] for entry in entries):
        print(f"  - {reason}", file=sys.stderr)
    print("  如确需执行，请把原因和命令交给用户决定（规则见 .harness/config/rules.toml）。", file=sys.stderr)
    _emit_denials(entries, args.role)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
