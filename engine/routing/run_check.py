"""运行记录与任务归属的 CI 侧复核（目标态设计 P5、7.3、10.1）。

派发脚本（bin/dispatch）会写运行记录、给提交带 `Task:`、按预算停在 CI 轮次上限；这里由 Agent 之外的
机器再核对一遍，即使绕过派发脚本手工派发，缺记录或超预算也会被发现：

  适用     分支是 task/<名字> 且 docs/plans/task-<名字>.md 存在，或任一提交带 `Task:`（实现某份任务书的 PR）
  记录内容 每份记录全文递归检查命令正文、本机路径与会话正文（B38）；命中即新增失败的 Finding，
           只写字段位置与规则名、不回显禁项，判定只依据记录自身内容、不读事件
  自行实现 任务书就在本 PR 中新增或修改：设计方自己实现（用户 2026-09-28 决定，方案 B），不经派发脚本，
           不要求运行记录与 CI 轮次（这类 PR 本就不自动合并），仍要求每个提交带 `Task:`
  归属     范围内每个非合并提交都带 `Task: <该任务书的编号>`
  运行记录 新增了 docs/runs/<任务书名>/<序号>.json；序号最大的一份格式完整、task/class/branch 与任务书一致、
           exit 为 ok，提示词文件存在且 sha256 与记录一致
  CI 轮次  该分支已完成的 CI 轮次（按不同的 head 提交计）不超过任务书 budget.ci_rounds

权威判定在 auto-merge 的 policy.py（main 上的代码，只读 PR 数据）；CI 的 harness job 只把结果写进 summary。

    bin/harness run-check --base origin/main [--head HEAD] [--branch task/005-x] [--github]
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from engine.checks import taskbook
from engine.core import events
from engine.core.common import ROOT, changed_files, commit_field, git
from engine.guards import command_guard

RECORD_FIELDS = (
    "task", "class", "attempt", "branch", "gen_ai.agent.name", "host_version", "gen_ai.request.model",
    "prompt_sha256", "prompt_path", "guard_ref", "started_at", "ended_at", "exit", "retries",
    "failure_signatures", "guard_denials",
)
RECORD_PATH = re.compile(r"^docs/runs/(?P<stem>task-[^/]+)/(?P<number>\d+)\.json$")


@dataclass
class Finding:
    name: str
    ok: bool
    reason: str


@dataclass
class Scope:
    taskbook: str  # docs/plans/task-<名字>.md
    header: dict
    in_pr: bool = False  # 任务书随本 PR 新增或修改：设计方自行实现


def scope(base: str, head: str, branch: str, cwd: Path = ROOT) -> Scope | None:
    """这个 PR 是否在实现某份任务书。任务书先从 cwd（auto-merge 中是 main）读取；随本 PR 提交、main 上还没有的，
    从 head 读取。"""
    shas = git("rev-list", "--no-merges", f"{base}..{head}", cwd=cwd).split()
    ids = {value.strip() for sha in shas for value in commit_field(sha, "Task", cwd)}
    candidates = []
    if branch.startswith("task/"):
        candidates.append(f"docs/plans/task-{branch.removeprefix('task/')}.md")
    changed = {path: status for status, path in changed_files(base, head, cwd)}
    for task_id in sorted(ids):
        match = re.fullmatch(r"T(\d+)", task_id)
        if match:
            prefix = f"docs/plans/task-{int(match[1]):03d}-"
            candidates += sorted(p.relative_to(cwd).as_posix()
                                 for p in (cwd / "docs" / "plans").glob(f"task-{int(match[1]):03d}-*.md"))
            candidates += sorted(path for path in changed if path.startswith(prefix) and path.endswith(".md"))
    for rel in candidates:
        in_pr = changed.get(rel) in ("A", "M")
        text = git("show", f"{head}:{rel}", cwd=cwd, check=False) if in_pr else ""
        if not text and (cwd / rel).is_file():
            text = (cwd / rel).read_text(encoding="utf-8")
        if not text:
            continue
        try:
            header, _ = taskbook.parse_header(text)
        except taskbook.HeaderError:
            continue
        return Scope(rel, header, in_pr)
    if ids:  # 带了 Task: 却找不到任务书：按适用处理，归属检查会报出来
        return Scope("", {"task": min(ids)})
    return None


def check_trailers(base: str, head: str, task_id: str, cwd: Path) -> Finding:
    missing = [
        sha[:7] for sha in git("rev-list", "--no-merges", f"{base}..{head}", cwd=cwd).split()
        if task_id not in [value.strip() for value in commit_field(sha, "Task", cwd)]
    ]
    if missing:
        return Finding("归属", False, f"提交 {'、'.join(missing)} 没有 `Task: {task_id}`")
    return Finding("归属", True, f"全部提交带 `Task: {task_id}`")


def _records(base: str, head: str, target: Scope, cwd: Path) -> list[tuple[int, str]]:
    """范围内尚存（未删除）的运行记录，按序号排序。"""
    stem = Path(target.taskbook).stem
    return sorted(
        (int(match["number"]), path)
        for status, path in changed_files(base, head, cwd)
        if status != "D" and (match := RECORD_PATH.match(path)) and match["stem"] == stem
    )


def _load_record(head: str, path: str, cwd: Path) -> tuple[object, Exception | None]:
    """读取 head 上的一份记录 JSON；损坏时返回 (None, 异常)。"""
    try:
        return json.loads(git("show", f"{head}:{path}", cwd=cwd)), None
    except (json.JSONDecodeError, RuntimeError) as error:
        return None, error


def check_record(base: str, head: str, target: Scope, branch: str, cwd: Path) -> Finding:
    records = _records(base, head, target, cwd)
    if not records:
        stem = Path(target.taskbook).stem
        return Finding("运行记录", False, f"缺运行记录：没有新增 `docs/runs/{stem}/<序号>.json`（未经 bin/dispatch？）")
    number, path = records[-1]
    record, error = _load_record(head, path, cwd)
    if error is not None:
        return Finding("运行记录", False, f"`{path}` 不是有效的 JSON（{error}）")
    missing = [key for key in RECORD_FIELDS if key not in record]
    if missing:
        return Finding("运行记录", False, f"`{path}` 缺字段：{'、'.join(missing)}")
    header = target.header
    expected = {"task": header.get("task"), "class": header.get("class"), "attempt": number}
    if branch.startswith("task/"):
        expected["branch"] = branch
    wrong = [f"{key}={record.get(key)!r}（应为 {value!r}）" for key, value in expected.items() if record.get(key) != value]
    if wrong:
        return Finding("运行记录", False, f"`{path}` 与任务书不一致：{'、'.join(wrong)}")
    if record["exit"] != "ok":
        return Finding("运行记录", False, f"`{path}` 的退出方式为 {record['exit']}，不是 ok")
    # 按原始字节比对（common.git 会去掉结尾换行）。
    prompt = subprocess.run(["git", "show", f"{head}:{record['prompt_path']}"], cwd=cwd, capture_output=True,
                            check=False)
    if prompt.returncode != 0 or hashlib.sha256(prompt.stdout).hexdigest() != record["prompt_sha256"]:
        return Finding("运行记录", False, f"提示词 `{record['prompt_path']}` 缺失或 sha256 与记录不一致")
    return Finding("运行记录", True, f"`{path}`：{record['gen_ai.agent.name']} {record['gen_ai.request.model']}，"
                                     f"重试 {record['retries']} 次")


# ---- 记录内容检查（B38/T203，共用合同 C3 字段/值表的最小判定集）----
#
# 先对所有键/值拒绝本机路径、换行、超长、会话/命令形态与 shell 结构，再按已知字段校验声明语法；
# 未知自由文本 fail-closed。guard_denials 的键只有与内置 guard 规则理由完全相等的旧摘要可保留
# （即使理由提及命令或连接符），且该例外仍不能含路径或换行；例外集合从本模块运行处的已批准代码
# （COMMAND_RULES/TOOL_RULES 及 implementer 规则）静态取得，不执行 PR 代码。检查只读记录内容，
# 不读事件库。

_MAX_TEXT = 200  # 与 events 的 decision.reason 上限同档：更长的字符串按超长拒绝，不截断
_MAX_PROBLEMS = 8  # 失败原因里最多列出的命中处数（超出只报总数，不回显内容）
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_HEX_RE = re.compile(r"[0-9a-f]{6,64}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/?#=+-]*\Z")  # 短 ID/模型/版本/分支/相对引用/URL
_PATTERN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@?*+-]*\Z")  # kind=pattern 的规定通配模式
_SEGMENT_RE = re.compile(r"[A-Za-z0-9.@_-]+\Z")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\Z")
_NOSPACE_RE = re.compile(r"\S{1,200}\Z")
_CJK_TEXT_RE = re.compile(r"[\w #?=&/:.,@+()（）！？，。：、；%*-]+\Z")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SESSION_RE = re.compile(r"(?i)\b(?:user|assistant|system|human)\s*[:：]|(?:用户|助手)\s*[:：]")
_EXECUTABLES = ("sh", "bash", "zsh", "python", "python3", "node", "npm", "npx", "pip", "curl",
                "wget", "ssh", "scp", "make", "rm", "ls", "cat", "git", "gh")
_COMMAND_PREFIX_RE = re.compile(r"(?i)\s*(?:" + "|".join(_EXECUTABLES) + r")\s+\S")
_BARE_COMMAND_RE = re.compile(r"(?i)\s*(?:" + "|".join(_EXECUTABLES) + r")\s*\Z")
_SHELL_META = ("|", ";", "`", ">", "<", "$(", "&&", "||")
_LOCAL_PATHS = ("/Users/", "/home/", "C:\\")

# 内置规则理由的精确集合（旧 guard_denials 摘要例外）；主线上是已批准代码，CI 侧原只报告。
_TRUSTED_GUARD_REASONS = frozenset(
    reason for _pattern, _key, reason in itertools.chain(
        command_guard.COMMAND_RULES, command_guard.IMPLEMENTER_COMMAND_RULES,
        command_guard.TOOL_RULES, command_guard.IMPLEMENTER_TOOL_RULES)
)
_K_CLASSES = frozenset(f"K{level}" for level in range(8))
_EXIT_WORDS = frozenset(("ok", "timeout", "stall", "stopped", "error", "loop", "retries", "clarify"))
_ALERT_REASONS = frozenset((
    "integrity_failure", "review_rejected", "review_error", "dispatch_stall", "dispatch_timeout",
    "dispatch_loop", "dispatch_budget", "ci_last_round", "guard_denials", "budget_exhausted",
    "audit_missing_stage", "audit_anchor_mismatch",
))
_DECISION_REASONS = frozenset(events.STATUSES) | _EXIT_WORDS | _ALERT_REASONS
_FIXED_IN_VALUES = frozenset(("run_record", "ci_artifact", "pr_comment"))
_MC_CATEGORIES = frozenset(("context", "tool", "spec", "other"))
_MC_SUMMARIES = frozenset(("context_unavailable", "required_tool_unavailable", "spec_unavailable",
                           "spec_ambiguous", "other_missing"))
_MC_STATUSES = frozenset(("reported", "unknown", "invalid"))


def _forbidden(text: str, *, length: bool = True) -> str | None:
    """字符串的通用禁项（换行、本机路径、超长、会话正文、命令正文、shell 结构）：返回规则名或 None。"""
    if "\n" in text or "\r" in text:
        return "换行"
    if any(marker in text for marker in _LOCAL_PATHS):
        return "本机路径"
    if length and len(text) > _MAX_TEXT:
        return "超长"
    if _SESSION_RE.search(text):
        return "会话正文"
    if _COMMAND_PREFIX_RE.match(text):
        return "命令正文"
    if any(meta in text for meta in _SHELL_META):
        return "Shell 结构"
    return None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _repo_relative(text: str) -> bool:
    """仓库相对路径（可带 @提交）：拒绝绝对路径、~ 展开与路径穿越。"""
    if text.startswith(("/", "~")) or ":" in text:
        return False
    return all(segment and segment not in (".", "..") and _SEGMENT_RE.fullmatch(segment)
               for segment in text.split("/"))


def _ref_form(value: str) -> bool:
    """引用值：仓库相对路径/带提交路径、受支持 URL 或规定短 ID。"""
    return _TOKEN_RE.fullmatch(value) is not None or _repo_relative(value)


def _safe_value(text: str) -> bool:
    """摘要位置字符串的合法值形态：哈希、短 ID/引用/URL、无空白短词或含中文的安全摘要。"""
    if _HEX_RE.fullmatch(text) or _TOKEN_RE.fullmatch(text) or _NOSPACE_RE.fullmatch(text):
        return True
    return bool(_CJK_RE.search(text) and _CJK_TEXT_RE.fullmatch(text))


def _check_str(value, location, problems, ok, rule="字段形态") -> None:
    """已知字符串字段：先通用禁项，后声明语法。"""
    if not isinstance(value, str):
        problems.append((location, "非法类型"))
        return
    if hit := _forbidden(value):
        problems.append((location, hit))
        return
    if not ok(value):
        problems.append((location, rule))


def _enum_factory(words):
    def check(value, location, problems):
        if not isinstance(value, str) or value not in words:
            if isinstance(value, str) and (hit := _forbidden(value)):
                problems.append((location, hit))
            else:
                problems.append((location, "非法类型"))
    return check


def _int_factory(minimum, *, none=False):
    def check(value, location, problems):
        if value is None and none:
            return
        if not _is_int(value) or value < minimum:
            problems.append((location, "非法类型"))
    return check


def _number_factory(*, none=False):
    def check(value, location, problems):
        if value is None and none:
            return
        if not _is_number(value):
            problems.append((location, "非法类型"))
    return check


def _none_or(check):
    def wrapper(value, location, problems):
        if value is not None:
            check(value, location, problems)
    return wrapper


def _p_token(value, location, problems) -> None:
    _check_str(value, location, problems, _TOKEN_RE.fullmatch)


def _p_ts(value, location, problems) -> None:
    _check_str(value, location, problems, _TIMESTAMP_RE.fullmatch)


def _p_sha(value, location, problems) -> None:
    _check_str(value, location, problems, _HEX64_RE.fullmatch)


def _p_path(value, location, problems) -> None:
    _check_str(value, location, problems, _repo_relative)


def _p_signatures(value, location, problems) -> None:
    """失败签名：检查名与哈希的短串，或派发固定的无空白问题描述。"""
    if not isinstance(value, list):
        problems.append((location, "非法类型"))
        return
    for index, item in enumerate(value):
        _check_str(item, f"{location}[{index}]", problems, _NOSPACE_RE.fullmatch)


_p_size = _int_factory(0)


def _scan_struct(value, location, problems, fields: dict, *, exact_keys=False, required=()) -> None:
    """结构化 dict：已知键按字段校验；未知键拒绝（在父位置报告，不回显键名）；exact_keys 时缺键也拒绝。"""
    if not isinstance(value, dict):
        problems.append((location, "非法类型"))
        return
    for key in (required or (fields if exact_keys else ())):
        if key not in value:
            problems.append((f"{location}.{key}", "缺字段"))
    for key, item in value.items():
        check = fields.get(key)
        if check is None:
            problems.append((location, "未知字段"))
            _scan_free(item, location, problems)
        else:
            check(item, f"{location}.{key}", problems)


def _scan_list(value, location, problems, fields, *, exact_keys=False, required=()) -> None:
    if not isinstance(value, list):
        problems.append((location, "非法类型"))
        return
    for index, item in enumerate(value):
        _scan_struct(item, f"{location}[{index}]", problems, fields, exact_keys=exact_keys, required=required)


def _scan_free(value, location, problems) -> None:
    """未知字段的任意 JSON 值：通用禁项逐键逐值生效；标量字符串再按自由文本值形态拒绝。"""
    if isinstance(value, str):
        if hit := _forbidden(value):
            problems.append((location, hit))
        elif not _safe_value(value):
            problems.append((location, "未知自由文本"))
    elif isinstance(value, dict):
        for key, item in value.items():
            hit = _forbidden(key, length=False) if isinstance(key, str) else None
            if hit:
                problems.append((location, hit))
            elif not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                problems.append((location, "键不合规"))
            _scan_free(item, location, problems)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_free(item, f"{location}[{index}]", problems)


def _p_output_value(value, location, problems) -> None:
    """outputs 标量值：数字/布尔/None 判型；字符串按摘要值形态，裸 shell 命令名也拒绝。"""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            problems.append((location, "非法类型"))
        return
    if not isinstance(value, str):
        problems.append((location, "非法类型"))
        return
    if hit := _forbidden(value):
        problems.append((location, hit))
        return
    if _BARE_COMMAND_RE.fullmatch(value.strip()):
        problems.append((location, "命令正文"))
        return
    if not _safe_value(value):
        problems.append((location, "未知自由文本"))


def _p_outputs(value, location, problems) -> None:
    """扁平 outputs：键是短标识，值是标量或安全形态字符串。"""
    if not isinstance(value, dict):
        problems.append((location, "非法类型"))
        return
    for key, item in value.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            hit = _forbidden(key, length=False) if isinstance(key, str) else None
            problems.append((location, hit or "键不合规"))
            _scan_free(item, location, problems)
        else:
            _p_output_value(item, f"{location}.{key}", problems)


def _p_refs(value, location, problems) -> None:
    """输入引用列表：kind/ref/sha256/size；kind=pattern 的 ref 允许规定通配模式。

    未知键的位置用固定占位（评审 #51 严重项）：键名属记录内容，不得进入 Finding。
    """
    if not isinstance(value, list):
        problems.append((location, "非法类型"))
        return
    for index, item in enumerate(value):
        child = f"{location}[{index}]"
        if not isinstance(item, dict):
            problems.append((child, "非法类型"))
            continue
        for key, item_value in item.items():
            if key == "kind":
                _p_token(item_value, f"{child}.kind", problems)
            elif key == "ref":
                is_pattern = item.get("kind") == "pattern"
                _check_str(item_value, f"{child}.ref", problems,
                           _PATTERN_RE.fullmatch if is_pattern else _ref_form)
            elif key == "sha256":
                _p_sha(item_value, f"{child}.sha256", problems)
            elif key == "size":
                _p_size(item_value, f"{child}.size", problems)
            else:
                hidden = f"{child}.未知键（已隐去）"
                problems.append((hidden, "未知字段"))
                _scan_free(item_value, hidden, problems)


def _p_guard_denials(value, location, problems) -> None:
    """守卫拒绝计数：键只有与内置规则理由完全相等的旧摘要可保留（不能含路径/换行），计数非负整数。"""
    if not isinstance(value, dict):
        problems.append((location, "非法类型"))
        return
    for key, count in value.items():
        hit = _forbidden(key, length=False)
        if hit in ("换行", "本机路径"):
            problems.append((location, hit))
        elif key not in _TRUSTED_GUARD_REASONS:
            problems.append((location, "未知理由"))
        elif not _is_int(count) or count < 0:
            problems.append((location, "非法类型"))


_DECISION_FIELDS = {"by": _none_or(_p_token), "rule": _none_or(_p_token),
                    "reason": _none_or(_enum_factory(_DECISION_REASONS))}
_ACTOR_FIELDS = {"role": _enum_factory(frozenset(events.ACTOR_ROLES)), "host": _p_token, "model": _p_token}
_STAGE_FIELDS = {
    "stage": _enum_factory(frozenset(events.STAGES)), "step": _p_token,
    "status": _enum_factory(frozenset(events.STATUSES)), "ts": _p_ts,
    "duration_ms": _int_factory(0, none=True), "attempt": _int_factory(1), "round": _int_factory(0),
    "inputs": _p_refs, "outputs": _p_outputs, "decision": _none_or(
        lambda value, location, problems: _scan_struct(value, location, problems, _DECISION_FIELDS,
                                                       exact_keys=True)),
    "actor": _none_or(lambda value, location, problems: _scan_struct(value, location, problems,
                                                                     _ACTOR_FIELDS)),
    "source": _p_token, "head_hash": _p_sha,
}
_ANCHOR_FIELDS = {"source": _p_token, "stage": _enum_factory(frozenset(events.STAGES)),
                  "head_hash": _p_sha, "fixed_in": _enum_factory(_FIXED_IN_VALUES)}
_MC_FIELDS = {"category": _enum_factory(_MC_CATEGORIES), "summary": _enum_factory(_MC_SUMMARIES),
              "ref": _p_token}
_TOP_FIELDS = {
    "task": _p_token, "class": _enum_factory(_K_CLASSES), "attempt": _int_factory(1),
    "branch": _p_token, "trace_id": _p_token,
    "gen_ai.agent.name": _p_token, "host_version": _p_token, "gen_ai.request.model": _p_token,
    "prompt_sha256": _p_sha, "prompt_path": _p_path, "guard_ref": _p_token,
    "started_at": _p_ts, "ended_at": _p_ts, "executor_seconds": _number_factory(),
    "exit": _enum_factory(_EXIT_WORDS), "retries": _int_factory(0),
    "failure_signatures": _p_signatures, "ci_rounds_before": _int_factory(0),
    "guard_denials": _p_guard_denials,
    "gen_ai.usage.input_tokens": _int_factory(0, none=True),
    "gen_ai.usage.output_tokens": _int_factory(0, none=True),
    "cost": _number_factory(none=True), "escalation": _none_or(_enum_factory(_EXIT_WORDS)),
    "stages": lambda value, location, problems: _scan_list(value, location, problems, _STAGE_FIELDS,
                                                           exact_keys=True),
    "anchors": lambda value, location, problems: _scan_list(value, location, problems, _ANCHOR_FIELDS,
                                                            exact_keys=True),
    "missing_context": lambda value, location, problems: _scan_list(value, location, problems, _MC_FIELDS,
                                                                    required=("category", "summary")),
    "missing_context_status": _enum_factory(_MC_STATUSES),
}


def scan_record(record) -> list[tuple[str, str]]:
    """记录全文递归扫描（B38）：返回 (字段位置, 规则名)；只写位置与规则名，不回显禁项。"""
    problems: list[tuple[str, str]] = []
    if not isinstance(record, dict):
        return [("记录", "非法类型")]
    for key, value in record.items():
        check = _TOP_FIELDS.get(key)
        if check is None:
            problems.append(("记录", "未知字段"))
            _scan_free(value, "记录", problems)
        else:
            check(value, str(key), problems)
    return problems


def check_record_content(base: str, head: str, target: Scope, cwd: Path = ROOT) -> Finding | None:
    """记录内容检查（B38）：记录里有命令正文、本机路径或会话正文即失败的 Finding。

    由 check() 在 check_record 的 JSON/身份/提示词哈希核对之后调用；记录缺失、JSON 无效或未命中时
    返回 None——干净记录的 Finding 与事件序列与之前逐字相同，坏记录才多一条失败 Finding。
    判定只依据记录自身内容（scan_record），不读事件库。
    """
    records = _records(base, head, target, cwd)
    if not records:
        return None
    _number, path = records[-1]
    record, error = _load_record(head, path, cwd)
    if error is not None or not isinstance(record, dict):
        return None
    problems = scan_record(record)
    if not problems:
        return None
    shown = problems[:_MAX_PROBLEMS]
    detail = "；".join(f"{location}：{rule}" for location, rule in shown)
    if len(problems) > len(shown):
        detail += f"（共 {len(problems)} 处）"
    return Finding("记录内容", False, f"`{path}` 命中内容禁项：{detail}")


def ci_rounds(runs: list[dict]) -> int:
    """已完成的 CI 轮次：按不同的 head 提交计（同一提交的重跑不算新一轮）。"""
    return len({run.get("headSha") or run.get("head_sha") for run in runs
                if run.get("status") == "completed" and (run.get("headSha") or run.get("head_sha"))})


def check_ci(rounds: int | None, header: dict) -> Finding:
    limit = (header.get("budget") or {}).get("ci_rounds")
    if rounds is None:
        return Finding("CI 轮次", False, "读不到该分支的 CI 运行，按超预算处理")
    if not isinstance(limit, int):
        return Finding("CI 轮次", False, "任务书没有 budget.ci_rounds")
    ok = rounds <= limit
    return Finding("CI 轮次", ok, f"已完成 {rounds} 轮，预算 {limit} 轮" + ("" if ok else "：超出预算"))


def _record(target: Scope | None, findings: list[Finding] | None) -> None:
    """观察旁路：适用性与每个 Finding 一条事件（规则、通过与原因）；失败不影响返回值。"""
    try:
        if target is None:
            events.emit(stage="ci", step="run_check", status="skip", outputs={"applicable": False})
            return
        events.emit(stage="ci", step="run_check", status="ok",
                    outputs={"applicable": True, "taskbook": target.taskbook or None,
                             "task": target.header.get("task") or None, "in_pr": target.in_pr})
        for finding in findings or []:
            events.emit(stage="ci", step="run_check.finding", status="ok" if finding.ok else "fail",
                        outputs={"finding": finding.name, "ok": finding.ok},
                        decision={"by": "run_check", "rule": finding.name, "reason": finding.reason})
    except Exception:  # noqa: BLE001  设计要求：事件失败不得影响调用方
        return


def check(base: str, head: str, branch: str, cwd: Path = ROOT, rounds: int | None = None,
          with_ci: bool = True) -> list[Finding] | None:
    """不适用（不是在实现任务书）时返回 None。"""
    target = scope(base, head, branch, cwd)
    if target is None:
        _record(None, None)  # 观察：不适用
        return None
    task_id = target.header.get("task", "")
    findings = [check_trailers(base, head, task_id, cwd)]
    if not target.taskbook:
        findings.append(Finding("任务书", False, f"`Task: {task_id}` 找不到对应的任务书"))
        _record(target, findings)
        return findings
    if target.in_pr:
        findings.append(Finding("运行记录", True, f"任务书 `{target.taskbook}` 随本 PR 提交：设计方自行实现，不要求运行记录"))
        _record(target, findings)
        return findings
    findings.append(check_record(base, head, target, branch, cwd))
    content = check_record_content(base, head, target, cwd)
    if content is not None:
        findings.append(content)
    if with_ci:
        findings.append(check_ci(rounds, target.header))
    _record(target, findings)  # 观察：适用性与各项发现
    return findings


def render(findings: list[Finding] | None) -> str:
    if findings is None:
        return "### 运行记录复核\n\n不适用：不是在实现某份任务书的分支。\n"
    lines = ["### 运行记录复核", "", "| 检查 | 结果 | 理由 |", "|---|---|---|"]
    lines += [f"| {item.name} | {'✅' if item.ok else '❌'} | {item.reason} |" for item in findings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--branch", help="PR 的分支名（默认取 GITHUB_HEAD_REF 或当前分支）")
    parser.add_argument("--github", action="store_true", help="写 GITHUB_STEP_SUMMARY（只报告，不失败）")
    args = parser.parse_args(argv)
    branch = args.branch or os.environ.get("GITHUB_HEAD_REF") or git("rev-parse", "--abbrev-ref", args.head)
    text = render(check(args.base, args.head, branch, cwd=ROOT, with_ci=False))
    print(text)
    if args.github and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(text + "（CI 轮次与合并与否由 auto-merge 的合并路由判定。）\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
