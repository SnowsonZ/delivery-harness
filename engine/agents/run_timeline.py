"""运行记录时间线与固定链头（B46 T201）：把本机事件链组装成运行记录里的安全摘要（共用合同 C3）。

职责边界（任务书 F1、共用合同 C0）：事件由 dispatch_observation（T105）写入，本模块只读——从本机库
取该 trace 已发生的事件，组装 stages/anchors 摘要。dispatch.write_record 在记录落盘前调用 record_fields
取链头与摘要，写入摘要并提交后用 fix_anchor 固定 run_record 锚点；锚点指向已发生事件的前缀，不含本次
尚未发生的 push_pr/ci_wait，也不为补时间线追加推送。本模块不判定退出/预算/推送，读取或组装失败一律
退化为空摘要（锚点为空），不改变派发的判定、返回码与 gh 调用序列。

stages 覆盖任务准入至本地检查的各已发生阶段（含更早尝试已发生的 push_pr/ci_wait），每项含 stage/step/
status/ts/duration_ms/attempt/round/inputs/outputs/decision/actor/source/head_hash；attempt/round 按链内
步骤推导（executor_round 计轮，ci_wait/escalate 之后进入下一次尝试）。decision.reason 只用稳定结果短 ID
（事件状态或既有 Attempt 退出词），原始理由正文以 sha256 留在 outputs；字符串沿用 emit 的隐私规则
（超长、形似本机路径、含换行一律丢弃），记录不含原始事件流、完整日志或会话。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3

from engine.core import events, events_db

_SOURCE = "local"  # 记录时间线只取本机来源链；CI/GitHub 事实由各自来源记录
_ANCHOR_STAGE = "dispatch"  # 锚点在派发写记录时固定
_FIXED_IN = "run_record"
_REASON_HASH_KEY = "decision.reason.sha256"  # 原始理由正文的哈希（正文本身不入记录）
_EVENT_COLUMNS = ("id", "hash", "ts", "stage", "step", "status", "duration_ms", "actor_role",
                  "actor_host", "model", "decision_by", "decision_rule", "decision_reason",
                  "outputs", "source")
_REF_KEYS = ("kind", "ref", "sha256")
_MAX_STR = 120
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_FORBIDDEN_PREFIXES = ("/Users/", "/home/")
# decision.reason 的稳定结果短 ID：C0 状态之外可用既有 Attempt 退出词（C3 词表，不造同义短 token）。
_EXIT_WORDS = frozenset(("timeout", "stall", "stopped", "error", "loop", "retries", "clarify"))
_ROUND_STEPS = frozenset(("executor_round", "local_verify"))
# 尝试边界：CI 等待与升级都意味着上一次尝试已结束，其后的事件属于下一次尝试。
_ATTEMPT_BOUNDARIES = frozenset(("ci_wait", "escalate"))


def record_fields(trace_id: str) -> tuple[dict, str | None]:
    """记录的新增摘要字段（trace_id/stages/anchors）与记录落盘前的本机链头。

    链头必须在记录落盘前读取：锚点指向已发生事件的前缀，不含尚未发生的 push_pr/ci_wait。
    读取失败或尚无事件时 stages/anchors 为空列表、链头为 None（锚点为空），原派发行为不变。
    """
    stages, head = _read_stages(trace_id)
    anchors = [{"source": _SOURCE, "stage": _ANCHOR_STAGE, "head_hash": head, "fixed_in": _FIXED_IN}] if head else []
    return {"trace_id": trace_id, "stages": stages, "anchors": anchors}, head


def fix_anchor(trace_id: str, head_hash: str | None) -> None:
    """记录落盘并提交后，把落盘前链头固定为 run_record 锚点；没有链头（无事件或库不可用）不登记。"""
    if head_hash:
        events.set_anchor(trace_id, _ANCHOR_STAGE, head_hash, _FIXED_IN, _SOURCE)


def _read_stages(trace_id: str) -> tuple[list[dict], str | None]:
    """读取 (local, trace) 的已发生事件并按链序组装摘要；任何失败返回空摘要与 None 链头（观察旁路）。"""
    try:
        rows, refs = _read_events(trace_id)
    except Exception:  # noqa: BLE001  观察旁路：摘要组装失败不得影响派发
        return [], None
    items: list[dict] = []
    attempt, round_no = 1, 0
    for row in rows:
        if row["step"] == "executor_round":
            round_no += 1
        outputs = _outputs(row)
        items.append({
            "stage": row["stage"], "step": row["step"], "status": row["status"], "ts": row["ts"],
            "duration_ms": row["duration_ms"],
            "attempt": attempt, "round": round_no if row["step"] in _ROUND_STEPS else 0,
            "inputs": refs.get(row["id"], []), "outputs": outputs,
            "decision": _decision(row, outputs), "actor": _actor(row),
            "source": row["source"], "head_hash": row["hash"],  # 各阶段结束时的链头前缀
        })
        if row["step"] in _ATTEMPT_BOUNDARIES:
            attempt, round_no = attempt + 1, 0
    return items, (rows[-1]["hash"] if rows else None)


def _read_events(trace_id: str) -> tuple[list[dict], dict[int, list[dict]]]:
    """按 seq 读出本机链上的事件与输入引用；库缺失抛给调用方按空摘要处理。"""
    path = events_db.db_path()
    if path is None or not path.exists():
        return [], {}
    with contextlib.closing(sqlite3.connect(path)) as conn:
        rows = [dict(zip(_EVENT_COLUMNS, values)) for values in conn.execute(
            f"SELECT {','.join(_EVENT_COLUMNS)} FROM events WHERE source=? AND trace_id=? ORDER BY seq",
            (_SOURCE, trace_id))]
        refs: dict[int, list[dict]] = {}
        joined = ("SELECT refs.event_id, refs.kind, refs.ref, refs.sha256, refs.size FROM refs"
                  " JOIN events ON refs.event_id = events.id"
                  " WHERE events.source=? AND events.trace_id=? AND refs.direction='in'"
                  " ORDER BY refs.event_id, refs.rowid")
        for event_id, kind, ref, sha256, size in conn.execute(joined, (_SOURCE, trace_id)):
            entry = _ref(kind, ref, sha256, size)
            if entry:
                refs.setdefault(event_id, []).append(entry)
        return rows, refs


def _clean_str(value: str, limit: int) -> str | None:
    """沿用 emit 的字符串隐私规则：超长、形似本机路径、含换行的丢弃，不截断、不改写。"""
    if len(value) > limit or value.startswith(_FORBIDDEN_PREFIXES) or "C:\\" in value \
            or "\n" in value or "\r" in value:
        return None
    return value


def _ref(kind, ref, sha256, size) -> dict | None:
    """一条输入引用：字符串键按隐私规则清洗，任一不合规整条丢弃（不保留半条引用）。"""
    entry: dict = {}
    for key, value in zip(_REF_KEYS, (kind, ref, sha256)):
        if value is None:
            continue
        cleaned = _clean_str(str(value), _MAX_STR)
        if cleaned is None:
            return None
        entry[key] = cleaned
    if isinstance(size, int) and not isinstance(size, bool):
        entry["size"] = size
    return entry or None


def _outputs(row: dict) -> dict:
    """outputs 列还原为扁平字典并按隐私规则重过滤；解析失败或形状不对按空处理。"""
    try:
        parsed = json.loads(row["outputs"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    clean: dict = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or len(key) > _MAX_STR or not _KEY_RE.match(key):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            clean[key] = value
        elif isinstance(value, str) and (cleaned := _clean_str(value, _MAX_STR)) is not None:
            clean[key] = cleaned
    return clean


def _decision(row: dict, outputs: dict) -> dict | None:
    """决定摘要：by/rule 取事件原值，reason 只留稳定结果短 ID；原始理由正文以哈希留在 outputs。

    非成功的 executor_round 优先用 outputs 里的 Attempt 退出词（timeout/stall/…），其余用事件状态；
    都属于 C3 固定词表，不把任意理由正文塞回记录，也不重做判定。
    """
    by, rule, reason = row["decision_by"], row["decision_rule"], row["decision_reason"]
    if by is None and rule is None and reason is None:
        return None
    short = row["status"]
    if short != "ok" and row["step"] == "executor_round" and outputs.get("exit") in _EXIT_WORDS:
        short = outputs["exit"]
    if isinstance(reason, str) and reason and reason != short:
        outputs[_REASON_HASH_KEY] = hashlib.sha256(reason.encode("utf-8")).hexdigest()
    return {"by": by, "rule": rule, "reason": short}


def _actor(row: dict) -> dict | None:
    """角色摘要：role/host/model 取事件原值并按隐私规则清洗，全空时为 None。"""
    actor: dict = {}
    for column, key in (("actor_role", "role"), ("actor_host", "host"), ("model", "model")):
        value = row[column]
        if value is None:
            continue
        cleaned = _clean_str(str(value), _MAX_STR)
        if cleaned is not None:
            actor[key] = cleaned
    return actor or None
