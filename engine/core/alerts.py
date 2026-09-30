"""共用告警 API（B46 C4，T205）：在 PR 评论或升级议题上发布带远端标记的告警。

渠道只有 PR/议题评论加 escalation 标签（设计 5，不新增通知渠道）。远端标记是 trace_id、NUL、
reason 的 sha256：同键先查分页评论/带标签议题再更新，不重发；无 PR 时查/建一条升级议题。
publish 只消费结构化既有状态，永不抛异常（C0）：发布失败返回 ok=False 并留本机 alert 观察事件，
不改调用方的判定、退出码与升级。details 只含安全阶段摘要、trace 查询方式与 PR/CI/账本链接。

reason 只用 C4 稳定键。派发侧两个预警由 rules.toml 的可选 [alerts] 节启用（删除整节即全部关闭）：
最后一轮 CI 在 wait 前预警（总预算 1 则首轮即预警）；守卫拒绝预警按 [alerts] guard_denials_threshold
（每轮被守卫拒绝的工具调用数，一次调用多个拒绝理由算一次）触发，未配置不启用，非正或非法值禁用
并提示一次配置错误，不改执行预算。阈值/末轮条件与安全详情组装都在本模块，dispatch 只接线。
"""

from __future__ import annotations

import hashlib
import sys

from engine.core import events
from engine.core.common import load_rules

LABEL = "escalation"
MARK = "<!-- harness-alert {} -->"  # 远端标记：trace_id、NUL、reason 的 sha256
# C4 稳定 reason 键（新增必须先经设计方修订共用合同，不造同义短 token）。
REASONS = frozenset((
    "integrity_failure", "review_rejected", "review_error", "dispatch_stall", "dispatch_timeout",
    "dispatch_loop", "dispatch_budget", "ci_last_round", "guard_denials", "budget_exhausted",
    "audit_missing_stage", "audit_anchor_mismatch",
))
_hinted = False  # 配置错误提示每进程一次（stderr 噪音控制；测试可直接复位）


def marker(trace_id: str, reason: str) -> str:
    """远端标记值：trace_id、NUL、reason 的 sha256（C4 去重键，换机后仍靠远端标记去重）。"""
    return hashlib.sha256(f"{trace_id}\0{reason}".encode()).hexdigest()


def dispatch_enabled(rules: dict | None = None) -> bool:
    """派发预警开关：rules.toml 配置了 [alerts] 节即启用；节可选，删除整节即全部关闭。

    读取失败按未配置处理（观察旁路，不影响派发判定与预算）。
    """
    try:
        data = rules if rules is not None else load_rules()
    except Exception:  # noqa: BLE001  观察旁路
        return False
    return isinstance(data, dict) and "alerts" in data


def guard_denials_threshold(rules: dict | None = None) -> int | None:
    """[alerts] guard_denials_threshold：正整数启用守卫拒绝预警；未配置返回 None（不启用）；
    非正或非法值禁用该预警并提示一次配置错误，不改执行预算。"""
    global _hinted
    value = (rules if rules is not None else load_rules()).get("alerts", {}).get("guard_denials_threshold")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if not _hinted:
            _hinted = True
            print(f"harness：rules.toml [alerts] guard_denials_threshold 配置非法（{value!r}），"
                  "守卫拒绝预警已禁用", file=sys.stderr)
        return None
    return value


def guard_denials_exceeded(denied: int, threshold: int | None) -> bool:
    """阈值条件（纯函数）：轮内被拒工具调用数（绝对计数）达到阈值即触发；未配置不触发。"""
    return threshold is not None and denied >= threshold


def is_last_ci_round(round_no: int, ci_rounds: int) -> bool:
    """末轮条件（纯函数）：本轮达到 CI 预算即最后一轮（总预算 1 则首轮即预警），在 wait 前触发。"""
    return ci_rounds >= 1 and round_no >= ci_rounds


def last_round_alert(trace_id: str, task: str, round_no: int, ci_rounds: int, *, pr, gh) -> None:
    """最后一轮 CI 预警：派发在每轮 wait_ci 前调用；未启用派发预警时为空操作。"""
    if dispatch_enabled() and is_last_ci_round(round_no, ci_rounds):
        publish(trace_id, "ci_last_round", pr=pr, task=task, gh=gh)


def guard_round_alert(trace_id: str, task: str, denied: int, *, pr, gh) -> None:
    """守卫拒绝预警：派发在每轮执行方结束后调用；未启用或未达阈值为空操作。"""
    if dispatch_enabled() and guard_denials_exceeded(denied, guard_denials_threshold()):
        publish(trace_id, "guard_denials", pr=pr, task=task, gh=gh)


def stage_lines(trace_id: str) -> list[str]:
    """安全阶段摘要（T201 时间线，只含枚举/计数/短 ID）；读取或组装失败为空列表（观察旁路）。"""
    try:
        from engine.agents import run_timeline  # 延迟导入：core 不在导入期反向依赖 agents
        summary, _head = run_timeline.record_fields(trace_id)
    except Exception:  # noqa: BLE001  观察旁路：摘要失败只少记这一段
        return []
    lines = []
    for item in summary.get("stages") or []:
        round_no = f"，round {item['round']}" if item.get("round") else ""
        lines.append(f"- `{item['stage']}/{item['step']}` {item['status']}"
                     f"（attempt {item.get('attempt', 1)}{round_no}）")
    return lines


def timeline_lines(trace_id: str, pr: int | None = None) -> list[str]:
    """时间线段：trace 与时间线入口（有 PR 指向 PR/CI artifact，无 PR 指向升级议题与 trace 命令）、
    安全阶段摘要；升级包与告警正文复用。"""
    entry = ("本 PR 的 checks 与 CI artifact（事件导出，保留 90 天）" if pr is not None
             else f"本升级议题与 `bin/harness trace {trace_id}`")
    lines = [f"- **trace**：`{trace_id}`；时间线入口：{entry}。"]
    stages = stage_lines(trace_id)
    if stages:
        lines += ["", "### 已发生的阶段（安全摘要）", *stages]
    return lines


def alert_body(trace_id: str, reason: str, *, task: str | None = None, pr: int | None = None,
               details: dict | None = None) -> str:
    """告警正文：远端标记、标题、时间线段与 details 的附加安全行（PR/CI/账本链接由调用方给）。"""
    lines = [MARK.format(marker(trace_id, reason)), f"### 告警：{task or trace_id}（{reason}）", "",
             *timeline_lines(trace_id, pr)]
    extra = (details or {}).get("lines")
    if extra:
        lines += ["", *[line for line in extra if isinstance(line, str)]]
    return "\n".join(lines) + "\n"


def publish(trace_id: str, reason: str, *, pr: int | None = None, task: str | None = None,
            details: dict | None = None, gh=None) -> dict:
    """共用告警发布（C4）：有 PR 在 PR 上评论（带 escalation 标签），无 PR 查/建一条升级议题；
    同键（trace_id+reason 的远端标记）查分页评论/议题后更新，不重发。永不抛异常：失败返回
    {ok: False, target: None, updated: False, error_kind} 并留本机 alert 事件，不改调用方的
    判定、退出码与升级。"""
    result = {"ok": False, "target": None, "updated": False, "error_kind": None}
    try:
        if reason not in REASONS:
            result["error_kind"] = "unknown_reason"
        else:
            body = alert_body(trace_id, reason, task=task, pr=pr, details=details)
            if pr is not None:
                result["target"] = "pr"
                existing = _marked(gh.list_comments(pr), marker(trace_id, reason), "id")
                if existing is not None:
                    gh.edit_comment(existing, body)
                    result.update(ok=True, updated=True)
                else:
                    gh.comment(pr, body, label=LABEL)
                    result["ok"] = True
            else:
                result["target"] = "issue"
                number = _marked(gh.list_issues(LABEL), marker(trace_id, reason), "number")
                if number is not None:
                    gh.edit_issue(number, body)
                    result.update(ok=True, updated=True)
                else:
                    gh.create_issue(f"告警：{task or trace_id}（{reason}）", body, [LABEL])
                    result["ok"] = True
    except Exception as exc:  # noqa: BLE001  发布异常不传播到原判定（C0）
        result.update(target=None, updated=False, error_kind=type(exc).__name__)
    _record(trace_id, reason, result)
    return result


def _marked(items, mark: str, key: str) -> int | None:
    """分页列表（PR 评论或议题）里正文带该远端标记的条目 id；没有或形状不对为 None。"""
    for item in items or []:
        if isinstance(item, dict) and mark in str(item.get("body") or ""):
            try:
                return int(item[key])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _record(trace_id: str, reason: str, result: dict) -> None:
    """本机 alert 观察事件（C1：alert 自己记发布结果）；emit 永不抛，组装失败也只丢弃。"""
    try:
        events.emit(stage="alert", step=reason, status="ok" if result["ok"] else "error",
                    trace_id=trace_id,
                    outputs={"target": result["target"], "updated": result["updated"]},
                    error={"kind": result["error_kind"]} if result["error_kind"] else None)
    except Exception:  # noqa: BLE001  观察旁路
        return
