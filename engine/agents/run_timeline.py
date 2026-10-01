"""运行记录时间线与固定链头（B46 T201；B77 T125 收敛为本次 attempt 窗口的安全索引）。

职责边界（任务书 F1、共用合同 C0）：事件由 dispatch_observation（T105）写入，本模块只读——从本机库
取该 trace 已发生的事件，组装 stages/anchors 摘要。dispatch.write_record 在记录落盘前调用 record_fields
取链头与摘要，写入摘要并提交后用 fix_anchor 固定 run_record 锚点；锚点指向已发生事件的前缀，不含本次
尚未发生的 push_pr/ci_wait，也不为补时间线追加推送。本模块不判定退出/预算/推送，读取或组装失败一律
退化为空摘要（锚点为空），不改变派发的判定、返回码与 gh 调用序列。

stages 是记录的索引而非事件流（B77 收敛）：只含本次 attempt 窗口的事件（按链内 ci_wait/escalate 边界
推导 attempt，最后一次边界之后为本窗口），每项只保留 stage/step/status/ts/duration_ms/attempt/round
七个标量；inputs/outputs/decision/actor/source/head_hash 等细节留在事件库。更早 attempt 已发生的
push_pr/ci_wait 各保留一条指针行（stage/step/status="prior"/attempt），标记链上有过衔接而不重复携带
历史（resume 多轮曾膨胀至 1.1MB 被卫生守卫拦下）。anchors 不变；保底：组装后 stages 超 256KB 从尾部
截断并标注 stages_truncated/stages_total（记录 JSON 的 512KB 自检在 dispatch.write_record）。
"""

from __future__ import annotations

import contextlib
import json
import re
import sys

from engine.core import events, events_db
from engine.core.common import load_rules

_SOURCE = "local"  # 记录时间线只取本机来源链；CI/GitHub 事实由各自来源记录
_ANCHOR_STAGE = "dispatch"  # 锚点在派发写记录时固定
_FIXED_IN = "run_record"
_EVENT_COLUMNS = ("hash", "ts", "stage", "step", "status", "duration_ms")
# 单条摘要瘦身（B77）：只保留七个标量字段，记录是索引，细节留在事件库。
_POINTER_STATUS = "prior"  # 更早 attempt 的 push_pr/ci_wait 指针行状态
_STAGES_BUDGET = 256 * 1024  # 组装后 stages 的保底预算，超出从尾部截断并标注
_ROUND_STEPS = frozenset(("executor_round", "local_verify"))
# 尝试边界：CI 等待与升级都意味着上一次尝试已结束，其后的事件属于下一次尝试。
_ATTEMPT_BOUNDARIES = frozenset(("ci_wait", "escalate"))
# 更早 attempt 只保留指针行的衔接步骤：其余事件不重复入记录。
_POINTER_STEPS = frozenset(("push_pr", "ci_wait"))
# 落盘前自检（B77，dispatch.write_record 调用）：记录 JSON 上限与 CI/runner 工作区路径占位。
RECORD_MAX_BYTES = 512 * 1024
# 卫生规则缺失时的内置本机路径模式（与 templates 的 home_path_pattern 同款）。
_HOME_PATH_PATTERN = (r"(/Users/|/home/|C:\\Users\\)"
                      r"(?!(x|you|me|user|name|example|test|someone)[/\\])[A-Za-z0-9._-]+[/\\]")


def record_fields(trace_id: str) -> tuple[dict, str | None]:
    """记录的新增摘要字段（trace_id/stages/anchors，截断时另有标注）与记录落盘前的本机链头。

    链头必须在记录落盘前读取：锚点指向已发生事件的前缀，不含尚未发生的 push_pr/ci_wait。
    读取失败或尚无事件时 stages/anchors 为空列表、链头为 None（锚点为空），原派发行为不变。
    """
    stages, head = _read_stages(trace_id)
    stages, total = fit_stages(stages, _STAGES_BUDGET)
    anchors = [{"source": _SOURCE, "stage": _ANCHOR_STAGE, "head_hash": head, "fixed_in": _FIXED_IN}] if head else []
    fields: dict = {"trace_id": trace_id, "stages": stages, "anchors": anchors}
    if total is not None:
        fields["stages_truncated"] = True
        fields["stages_total"] = total
    return fields, head


def fit_stages(stages: list[dict], budget: int) -> tuple[list[dict], int | None]:
    """组装后的保底（B77）：stages 序列化超过 budget 字节时从尾部逐条截断。

    返回 (保留的条目, 截断前总条数)；未截断时总数为 None。stages_truncated/stages_total 标注由调用方
    据此写入记录。
    """
    total = len(stages)
    kept = stages
    while kept and len(json.dumps(kept, ensure_ascii=False).encode("utf-8")) > budget:
        kept = kept[:-1]
    return kept, (total if len(kept) != total else None)


def fix_anchor(trace_id: str, head_hash: str | None) -> None:
    """记录落盘并提交后，把落盘前链头固定为 run_record 锚点；没有链头（无事件或库不可用）不登记。"""
    if head_hash:
        events.set_anchor(trace_id, _ANCHOR_STAGE, head_hash, _FIXED_IN, _SOURCE)


def fit_record(record: dict) -> None:
    """记录落盘前自检（B77）：JSON 超 512KB 时从 stages 尾部逐条截断并标注，再提交。

    stages_total 保持「截断前组装总数」口径（组装侧 256KB 保底已截断时该键已在记录里，不重复累计）。
    让卫生守卫在记录提交步杀派发的两例（stages 超 1MB）都由组装侧收敛拦在前；这里是最后保底。
    """
    if len(json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")) <= RECORD_MAX_BYTES:
        return
    dropped = 0
    while record.get("stages") and len(
            json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")) > RECORD_MAX_BYTES:
        record["stages"].pop()
        dropped += 1
    if dropped:
        record["stages_truncated"] = True
        if "stages_total" not in record:
            record["stages_total"] = len(record["stages"]) + dropped
    detail = f"stages 已截断（保留 {len(record.get('stages') or [])} 条）" if dropped else "无 stages 可截断，按原样提交"
    print(f"运行记录自检：记录 JSON 超过 {RECORD_MAX_BYTES // 1024} KB，{detail}。", file=sys.stderr)


def placeholder_workspace(text: str) -> str:
    """prompt 快照自检（B77）：CI/runner 工作区本机路径按卫生规则占位为 <ci-workspace>，守卫不再拦。"""
    cleaned, hits = _home_path_re().subn("<ci-workspace>", text)
    if hits:
        print(f"运行记录自检：prompt 快照含 {hits} 处 CI/runner 工作区路径，已占位为 <ci-workspace>。",
              file=sys.stderr)
    return cleaned


def _home_path_re() -> re.Pattern[str]:
    """卫生规则的本机路径模式（与守卫同源）；规则缺失或不可读时退回引擎内置同款模式。"""
    try:
        pattern = load_rules()["hygiene"]["home_path_pattern"]
    except Exception:  # noqa: BLE001  规则不可得时按内置模式自检，不影响派发
        pattern = _HOME_PATH_PATTERN
    return re.compile(pattern)


def _read_stages(trace_id: str) -> tuple[list[dict], str | None]:
    """读取本机链并组装本次 attempt 窗口的安全摘要；任何失败返回空摘要与 None 链头（观察旁路）。

    attempt 按链内边界推导（ci_wait/escalate 之后进入下一次尝试，边界事件本身属于它结束的尝试）；
    记录只含本次 attempt 的事件，更早 attempt 已发生的 push_pr/ci_wait 按链序各留一条指针行。
    链头仍取整条链的最后一个事件（anchors 语义不变）。
    """
    try:
        rows = _read_events(trace_id)
    except Exception:  # noqa: BLE001  观察旁路：摘要组装失败不得影响派发
        return [], None
    if not rows:
        return [], None
    attempt, round_no = 1, 0
    numbered: list[tuple[dict, int, int]] = []
    for row in rows:
        if row["step"] == "executor_round":
            round_no += 1
        numbered.append((row, attempt, round_no if row["step"] in _ROUND_STEPS else 0))
        if row["step"] in _ATTEMPT_BOUNDARIES:
            attempt, round_no = attempt + 1, 0
    current = numbered[-1][1]
    items: list[dict] = []
    for row, no, rnd in numbered:
        if no == current:
            items.append({"stage": row["stage"], "step": row["step"], "status": row["status"],
                          "ts": row["ts"], "duration_ms": row["duration_ms"], "attempt": no, "round": rnd})
        elif row["step"] in _POINTER_STEPS:
            items.append({"stage": row["stage"], "step": row["step"],
                          "status": _POINTER_STATUS, "attempt": no})
    return items, rows[-1]["hash"]


def _read_events(trace_id: str) -> list[dict]:
    """按 seq 读出本机链上的事件；库缺失返回空。连接沿用 events_db 的连接参数（busy_timeout=5000）：
    库锁竞争或恢复窗口内等待而非立即失败（B69）。摘要只需要索引列，引用与 outputs 留在库里按需查。
    """
    path = events_db.db_path()
    if path is None or not path.exists():
        return []
    with contextlib.closing(events_db._connect(path)) as conn:
        return [dict(zip(_EVENT_COLUMNS, values)) for values in conn.execute(
            f"SELECT {','.join(_EVENT_COLUMNS)} FROM events WHERE source=? AND trace_id=? ORDER BY seq",
            (_SOURCE, trace_id))]
