"""C5 事件导出与幂等导入（可观测性 P3）：EventBundle v1、查询、导出、导入、写盘与 CI 下载。

查询返回设计 JSON 形状的事件字典（id 是本机定位号，不入哈希，导入可重映射）；导出按
(source, trace) 整链导出完整前缀与锚点——since/stage/status 属展示过滤，筛选后的断链子集不是可
导入包；manifest 只收录通过安全扫描的规范化 UTF-8 JSON 内容产物，原始命令输出、verify 完整日志
与 Pi 流绝不外发，SQLite/WAL/SHM 更不在其中。导入验证 schema、来源、隐私、规范化哈希与链完整性
后事务提交：按事件哈希去重，同 source/trace/seq 不同 hash 为冲突，坏包整体不写、逐项发现以
findings 返回。load_ci 下载时再按 GitHub API 核对仓库、工作流路径、run/head 与包 origin（不能只
信包自报 ci），内容只解析不执行；manifest 内容产物文件不随导入恢复（缺失由再次导出如实报告）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import urllib.parse
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from engine.core import events, events_db, events_judge
from engine.core.events_origin import ORIGIN_LIMIT as _ORIGIN_LIMIT
from engine.core.events_origin import SHA_RE as _SHA_RE
from engine.core.events_origin import origin as _origin

BUNDLE_SCHEMA_VERSION = 1
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")

_LIMIT = 120  # 通用字符串上限（同 T101 过滤口径）
_REASON_LIMIT = 200
_REF_LIMIT = 200

_BUNDLE_KEYS = ("schema_version", "origin", "events", "anchors", "artifacts", "chains", "findings")
_ORIGIN_KEYS = ("repository", "run_id", "run_attempt", "job", "workflow_ref", "head_sha", "head_branch")
_EVENT_REQUIRED = ("source", "trace_id", "seq", "prev_hash", "hash", "ts", "stage", "step",
                   "status", "engine_version", "redacted")
_EVENT_OPTIONAL = ("id", "duration_ms", "inputs", "outputs", "decision", "error", "actor")
_REF_KEYS = ("kind", "ref", "sha256", "size")
_ANCHOR_KEYS = ("source", "trace_id", "stage", "head_hash", "fixed_in", "ts")
_NESTED_LIMITS = {
    "decision": {"by": _LIMIT, "rule": _LIMIT, "reason": _REASON_LIMIT},
    "error": {"kind": _LIMIT, "signature": _LIMIT},
    "actor": {"role": _LIMIT, "host": _LIMIT, "model": _LIMIT},
}
_DECISION_COLUMNS = {"by": "decision_by", "rule": "decision_rule", "reason": "decision_reason"}
_ERROR_COLUMNS = {"kind": "error_kind", "signature": "error_signature"}
_ACTOR_COLUMNS = {"role": "actor_role", "host": "actor_host", "model": "model"}

# manifest 安全扫描的禁项：换行与本机路径、会话标记（C4 词表）、shell 结构痕迹。
_TEXT_BANS = ("\n", "\r", "/Users/", "/home/", "C:\\")
_SESSION_BANS = ("user:", "assistant:", "system:", "human:", "用户:", "助手:")
_SHELL_BANS = ("|", ";", "`", "$(", "&&", "||", ">", "<")


def _finding(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_utc(value) -> datetime | None:
    """解析 UTC ISO 时间戳（Z 或带时区，naive 按 UTC）；不合法返回 None。"""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_since(value) -> datetime:
    """query 的 since：datetime 或 ISO 字符串，统一为 aware UTC；其余类型与坏字符串抛 ValueError。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        parsed = _parse_utc(value)
        if parsed is not None:
            return parsed
    raise ValueError(f"since 无法解析为 UTC 时间（收到 {type(value).__name__}）")


def _source_matches(candidate: str, wanted: str) -> bool:
    """来源匹配：精确链（ci:<run>:<attempt>:<job>）或 local/ci/github 类型前缀。"""
    return candidate == wanted or candidate.startswith(f"{wanted}:")


# ---- 事件形状：存储行 ↔ 设计 JSON ----

def _unflatten(row: dict, columns: dict[str, str]) -> dict:
    """actor/decision/error 三列组还原为嵌套字典（只含非 None 键）。"""
    return {key: row[name] for key, name in columns.items() if row.get(name) is not None}


def _row_to_event(row: dict, refs: list[dict]) -> dict:
    """存储行 → 设计 JSON 形状：outputs 恢复为原紧凑 JSON 的对象，id 保留为本机定位号。"""
    outputs = json.loads(row["outputs"]) if row["outputs"] else {}
    return {
        "id": row["id"], "ts": row["ts"], "source": row["source"], "trace_id": row["trace_id"],
        "seq": row["seq"], "prev_hash": row["prev_hash"], "hash": row["hash"],
        "stage": row["stage"], "step": row["step"], "status": row["status"],
        "duration_ms": row["duration_ms"], "inputs": [dict(item) for item in refs], "outputs": outputs,
        "decision": _unflatten(row, _DECISION_COLUMNS), "error": _unflatten(row, _ERROR_COLUMNS),
        "actor": _unflatten(row, _ACTOR_COLUMNS),
        "engine_version": row["engine_version"], "redacted": row["redacted"],
    }


def _event_to_row(event: dict) -> tuple[dict, list[dict]]:
    """已验证的 bundle 事件 → 存储行与规范化引用（outputs 重新落成原紧凑 JSON，None 沿用原规则）。"""
    decision = event.get("decision") or {}
    error = event.get("error") or {}
    actor = event.get("actor") or {}
    row = {
        "ts": event["ts"], "source": event["source"], "trace_id": event["trace_id"],
        "seq": event["seq"], "prev_hash": event["prev_hash"], "hash": event["hash"],
        "stage": event["stage"], "step": event["step"], "status": event["status"],
        "duration_ms": event.get("duration_ms"),
        "actor_role": actor.get("role"), "actor_host": actor.get("host"), "model": actor.get("model"),
        "decision_by": decision.get("by"), "decision_rule": decision.get("rule"),
        "decision_reason": decision.get("reason"),
        "error_kind": error.get("kind"), "error_signature": error.get("signature"),
        "outputs": json.dumps(event.get("outputs") or {}, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False),
        "engine_version": event["engine_version"], "redacted": event["redacted"],
    }
    refs = [events_db._normalize_ref(dict(item)) for item in (event.get("inputs") or [])]
    return row, refs


# ---- 查询（展示视图）----

def query(*, trace_id: str | None = None, source: str | None = None, since=None,
          stage: str | None = None, status: str | None = None) -> list[dict]:
    """查询事件（展示视图，不是可导入 bundle）：trace、来源、since、stage、status 过滤。

    source 为精确链或 local/ci/github 前缀；since 为 UTC datetime 或 ISO（含边界），坏值抛
    ValueError 由观察命令转诊断；无库返回空列表，上层按无数据处理。
    """
    cutoff = _parse_since(since) if since is not None else None
    rows = events_db.read_events(trace_id=trace_id, stage=stage, status=status)
    refs = events_db.read_refs([row["id"] for row in rows])
    result = []
    for row in rows:
        if source is not None and not _source_matches(row["source"], source):
            continue
        if cutoff is not None and ((moment := _parse_utc(row["ts"])) is None or moment < cutoff):
            continue
        result.append(_row_to_event(row, refs.get(row["id"], [])))
    return result


# ---- 导出 ----

def export_bundle(*, trace_id: str | None = None, source: str | None = None) -> dict:
    """导出 EventBundle v1：每条 (source, trace) 的完整链前缀、锚点与安全 manifest。

    source 同 query 的匹配规则；不含展示过滤；findings 记被排除产物、不中断导出；无库各列表为空。
    """
    rows = events_db.read_events(trace_id=trace_id)
    if source is not None:
        rows = [row for row in rows if _source_matches(row["source"], source)]
    refs = events_db.read_refs([row["id"] for row in rows])
    events_out = [_row_to_event(row, refs.get(row["id"], [])) for row in rows]
    chains: list[dict] = []
    chain_keys: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["source"], row["trace_id"])
        if key in chain_keys:
            chains[-1]["head_hash"] = row["hash"]
            continue
        chain_keys.add(key)
        chains.append({"source": row["source"], "trace_id": row["trace_id"], "head_hash": row["hash"]})
    anchors = [a for a in events_db.read_anchors() if (a["source"], a["trace_id"]) in chain_keys]
    artifacts, findings = _build_manifest(events_out)
    return {"schema_version": BUNDLE_SCHEMA_VERSION, "origin": _origin(), "events": events_out,
            "anchors": anchors, "artifacts": artifacts, "chains": chains, "findings": findings}


def _harvest_digests(events_out: list[dict]) -> list[str]:
    """收集指向本地产物的候选哈希：inputs 的 artifact 引用与 outputs 中的 64 位十六进制值。"""
    digests: list[str] = []
    for event in events_out:
        candidates = [item.get("sha256") for item in event["inputs"] if item.get("kind") == "artifact"]
        candidates.extend(event["outputs"].values())
        fresh = [d for d in dict.fromkeys(candidates) if isinstance(d, str) and _HASH_RE.fullmatch(d)]
        digests += [d for d in fresh if d not in digests]
    return digests


def _artifact_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _safe_json_document(content: bytes) -> bool:
    """内容是否为安全可发布的规范化 UTF-8 JSON 对象：可解析、字节即规范化形式、逐字段无禁项。"""
    try:
        parsed = json.loads(text := content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical == text and _json_strings_safe(parsed)


def _json_strings_safe(node) -> bool:
    """递归扫描 JSON 值：字符串不得含换行/本机路径/会话标记/shell 结构，键须合规短标识。"""
    if isinstance(node, str):
        if any(ban in node for ban in (*_TEXT_BANS, *_SHELL_BANS)):
            return False
        return not any(ban in node.lower() or ban in node for ban in _SESSION_BANS)
    if isinstance(node, dict):
        return all(isinstance(key, str) and _KEY_RE.fullmatch(key) is not None
                   and _json_strings_safe(value) for key, value in node.items())
    if isinstance(node, list):
        return all(_json_strings_safe(item) for item in node)
    return node is None or isinstance(node, (bool, int, float))


def _build_manifest(events_out: list[dict]) -> tuple[list[dict], list[dict]]:
    """安全 manifest：只收录内容可核验且通过安全扫描的产物；排除项逐条写 findings（不回显内容）。"""
    entries: list[dict] = []
    findings: list[dict] = []
    directory = events_db.artifacts_dir()
    for digest in _harvest_digests(events_out):
        label = f"产物 {digest[:12]}…"
        if directory is None:
            findings.append(_finding("artifact_missing", f"{label} 无本地产物目录，未列入 manifest"))
            continue
        content = _artifact_bytes(directory / digest)
        if content is None:
            findings.append(_finding("artifact_missing", f"{label} 不在本地产物目录，未列入 manifest"))
        elif hashlib.sha256(content).hexdigest() != digest:
            findings.append(_finding("artifact_hash", f"{label} 字节与哈希不符，未列入 manifest"))
        elif not _safe_json_document(content):
            findings.append(_finding("artifact_unsafe", f"{label} 不是安全的规范化 JSON，未列入 manifest"))
        else:
            entries.append({"sha256": digest, "size": len(content), "file": digest})
    return entries, findings


def write_bundle(bundle: dict, target: Path) -> None:
    """把 bundle 序列化为规范化 UTF-8 JSON 文本并原子写入 target（先临时文件再替换）。

    序列化或写盘失败抛异常，不当成功；不校验内容（校验属于 import_bundle / 设计方验收）。
    """
    target = Path(target)
    text = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


# ---- 导入 ----

def _event_prefix(event, index: int) -> str:
    digest = event.get("hash") if isinstance(event, dict) else None
    head = f"事件[{index}] {digest[:12]}…" if isinstance(digest, str) and _HASH_RE.fullmatch(digest) else f"事件[{index}]"
    return head


def _validate_envelope(bundle, findings: list[dict]) -> bool:
    """顶层形状：七个固定键、schema_version 只认 v1（未来版本明确拒写）、origin 与列表类型。"""
    if not isinstance(bundle, dict):
        findings.append(_finding("schema", "bundle 不是 JSON 对象"))
        return False
    if set(bundle) != set(_BUNDLE_KEYS):
        missing, extra = sorted(set(_BUNDLE_KEYS) - set(bundle)), sorted(set(bundle) - set(_BUNDLE_KEYS))
        findings.append(_finding("schema", f"bundle 顶层键不符（缺 {missing}，多 {extra}）"))
        return False
    version = bundle["schema_version"]
    if not _is_int(version):
        findings.append(_finding("schema", "schema_version 不是整数"))
        return False
    if version > BUNDLE_SCHEMA_VERSION:
        findings.append(_finding("future_schema", f"bundle schema 版本较新（{version} > {BUNDLE_SCHEMA_VERSION}），不导入"))
        return False
    if version != BUNDLE_SCHEMA_VERSION:
        findings.append(_finding("schema", f"未知 schema 版本 {version}"))
        return False
    if not _validate_origin(bundle["origin"], findings):
        return False
    for name in ("events", "anchors", "artifacts", "chains", "findings"):
        if not isinstance(bundle[name], list):
            findings.append(_finding("schema", f"bundle.{name} 不是列表"))
            return False
    return True


def _validate_origin(origin, findings: list[dict]) -> bool:
    if not isinstance(origin, dict) or not set(origin) <= set(_ORIGIN_KEYS):
        findings.append(_finding("schema", "bundle.origin 形状不符"))
        return False
    for key, value in origin.items():
        if not isinstance(value, str) or events._clean_str(value, _ORIGIN_LIMIT) is None:
            findings.append(_finding("schema", f"bundle.origin.{key} 不是合规字符串"))
            return False
        if key == "head_sha" and _SHA_RE.fullmatch(value) is None:
            findings.append(_finding("schema", "bundle.origin.head_sha 不是完整提交 SHA"))
            return False
    return True


def _valid_text(value, limit: int) -> bool:
    return isinstance(value, str) and bool(value) and events._clean_str(value, limit) is not None


def _validate_core_fields(event: dict, prefix: str, findings: list[dict]) -> bool:
    """枚举、序号、哈希、时间戳与计数列：只接受 emit 可能写出的值。"""
    if event["stage"] not in events.STAGES or event["status"] not in events.STATUSES:
        findings.append(_finding("schema", f"{prefix} stage/status 不在枚举内"))
        return False
    if not _is_int(event["seq"]) or event["seq"] < 1 or not _is_int(event["redacted"]) or event["redacted"] < 0:
        findings.append(_finding("schema", f"{prefix} seq/redacted 不是非负整数"))
        return False
    if not (isinstance(event["prev_hash"], str)
            and (event["prev_hash"] == "" or _HASH_RE.fullmatch(event["prev_hash"]))):
        findings.append(_finding("schema", f"{prefix} prev_hash 形状不符"))
        return False
    if not isinstance(event["hash"], str) or _HASH_RE.fullmatch(event["hash"]) is None:
        findings.append(_finding("schema", f"{prefix} hash 不是 64 位十六进制"))
        return False
    if not isinstance(event["ts"], str) or _parse_utc(event["ts"]) is None:
        findings.append(_finding("schema", f"{prefix} ts 不是可解析的 UTC 时间"))
        return False
    if (duration := event.get("duration_ms")) is not None and not _is_int(duration):
        findings.append(_finding("schema", f"{prefix} duration_ms 不是整数"))
        return False
    if "id" in event and (not _is_int(event["id"]) or event["id"] < 1):
        findings.append(_finding("schema", f"{prefix} id 不是正整数"))
        return False
    return True


def _validate_texts(event: dict, prefix: str, findings: list[dict]) -> bool:
    """自由文本列：沿用 T101 隐私过滤口径（超长、本机路径、换行即禁项），step 另要求原样无首尾空白。"""
    for name, limit in (("source", _LIMIT), ("trace_id", _REF_LIMIT), ("step", _LIMIT), ("engine_version", _LIMIT)):
        if not _valid_text(event[name], limit):
            findings.append(_finding("privacy", f"{prefix} 的 {name} 含隐私禁项或超限"))
            return False
    if event["step"] != event["step"].strip():
        findings.append(_finding("schema", f"{prefix} 的 step 带首尾空白（emit 会先剥离）"))
        return False
    parts = event["source"].split(":") if event["source"].startswith("ci:") else []
    if parts and (len(parts) != 4 or any(not part for part in parts)):
            findings.append(_finding("schema", f"{prefix} 的 source 不是 ci:<run>:<attempt>:<job> 形状"))
            return False
    return True


def _validate_scalar(value) -> bool:
    """outputs 值：标量或合规短字符串（None 合法，沿用原过滤口径）。"""
    return value is None or isinstance(value, (bool, int, float)) or _valid_text(value, _LIMIT)


def _validate_outputs(event: dict, prefix: str, findings: list[dict]) -> bool:
    """outputs 扁平字典：键为合规短标识，值为标量或合规短字符串。"""
    outputs = event.get("outputs")
    if not isinstance(outputs, dict):
        findings.append(_finding("schema", f"{prefix} 的 outputs 不是对象"))
        return False
    for key, value in outputs.items():
        if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None or not _validate_scalar(value):
            findings.append(_finding("schema", f"{prefix} 的 outputs 键值不合规"))
            return False
    return True


def _validate_nested_mapping(nested, name: str, limits: dict[str, int], prefix: str, findings: list[dict]) -> bool:
    """decision/error/actor 之一：键集合固定，值合规（role 另受枚举约束）。"""
    if nested is None:
        return True
    if not isinstance(nested, dict) or not set(nested) <= set(limits):
        findings.append(_finding("schema", f"{prefix} 的 {name} 键集合不符"))
        return False
    for key, value in nested.items():
        if value is None:
            continue
        if key == "role" and value not in events.ACTOR_ROLES:
            findings.append(_finding("schema", f"{prefix} 的 actor.role 不在枚举内"))
            return False
        if key != "role" and not _valid_text(value, limits[key]):
            findings.append(_finding("privacy", f"{prefix} 的 {name}.{key} 含隐私禁项或超限"))
            return False
    return True


def _validate_nested(event: dict, prefix: str, findings: list[dict]) -> bool:
    """outputs/decision/error/actor/inputs：键集合、值类型与隐私口径逐项核对，未知键拒绝。"""
    if not _validate_outputs(event, prefix, findings):
        return False
    for name, limits in _NESTED_LIMITS.items():
        if not _validate_nested_mapping(event.get(name), name, limits, prefix, findings):
            return False
    return _validate_inputs(event, prefix, findings)


def _validate_inputs(event: dict, prefix: str, findings: list[dict]) -> bool:
    """inputs 引用列表：四定键、字符串隐私口径、sha256 形状；过滤后为空的引用在真实事件里不存在。"""
    inputs = event.get("inputs")
    if inputs is None:
        return True
    if not isinstance(inputs, list):
        findings.append(_finding("schema", f"{prefix} 的 inputs 不是列表"))
        return False
    for item in inputs:
        if not isinstance(item, dict) or not set(item) <= set(_REF_KEYS):
            findings.append(_finding("schema", f"{prefix} 的引用键集合不符"))
            return False
        for name, limit in (("kind", _LIMIT), ("ref", _REF_LIMIT), ("sha256", 64)):
            if item.get(name) is not None and not _valid_text(item[name], limit):
                findings.append(_finding("privacy", f"{prefix} 的引用 {name} 含隐私禁项或超限"))
                return False
        if item.get("sha256") is not None and _HASH_RE.fullmatch(item["sha256"]) is None:
            findings.append(_finding("schema", f"{prefix} 的引用 sha256 形状不符"))
            return False
        if (size := item.get("size")) is not None and not (_is_int(size) and size >= 0):
            # emit 只写非负 int（len(content)）；数字字符串/浮点经 SQLite INTEGER 亲和改型后
            # 与导入时验证的哈希口径不一致，负数则是 emit 不可能写出的形状，一律拒绝。
            findings.append(_finding("schema", f"{prefix} 的引用 size 不是非负整数"))
            return False
        if not any(value is not None for value in item.values()):
            findings.append(_finding("schema", f"{prefix} 的引用过滤后为空（emit 会整条丢弃）"))
            return False
    return True


def _validate_event(event, index: int, findings: list[dict]) -> tuple[dict, list[dict]] | None:
    """校验一个 bundle 事件并转换为（存储行, 规范化引用）；不合法记 finding 返回 None。"""
    prefix = _event_prefix(event, index)
    if not isinstance(event, dict):
        findings.append(_finding("schema", f"{prefix} 不是对象"))
        return None
    unknown = set(event) - set(_EVENT_REQUIRED) - set(_EVENT_OPTIONAL)
    missing = [name for name in _EVENT_REQUIRED if name not in event]
    if unknown or missing:
        findings.append(_finding("schema", f"{prefix} 字段不符（缺 {sorted(missing)}，多 {sorted(unknown)}）"))
        return None
    if not (_validate_texts(event, prefix, findings) and _validate_core_fields(event, prefix, findings)
            and _validate_nested(event, prefix, findings)):
        return None
    row, refs = _event_to_row(event)
    if events_db._hash(row, refs) != row["hash"]:
        findings.append(_finding("hash_mismatch", f"{prefix} 内容与 hash 不符（不重算掩盖）"))
        return None
    return row, refs


def _chain_heads(entries: list, findings: list[dict]) -> dict[tuple[str, str], str]:
    """校验 bundle.chains 项形状并建 (source, trace) → head_hash 索引。"""
    heads: dict[tuple[str, str], str] = {}
    for entry in entries:
        ok = (isinstance(entry, dict) and set(entry) == {"source", "trace_id", "head_hash"}
              and all(_valid_text(entry[name], _REF_LIMIT) for name in ("source", "trace_id"))
              and _HASH_RE.fullmatch(str(entry["head_hash"])) is not None)
        if not ok:
            findings.append(_finding("schema", "chains 项形状不符"))
            continue
        heads[(entry["source"], entry["trace_id"])] = entry["head_hash"]
    return heads


def _check_merged_chain(key: tuple[str, str], existing: dict[int, dict],
                        fresh: list[tuple[dict, list[dict]]], expected_head: str,
                        findings: list[dict]) -> set[str] | None:
    """合并已有行与新增行后核对链完整性：seq 从 1 连续、prev_hash 相接、新增时链头与 chains 一致。"""
    fresh_map = {row["seq"]: row for row, _ in fresh}
    merged = {seq: row["hash"] for seq, row in existing.items()}
    merged.update({seq: row["hash"] for seq, row in fresh_map.items()})
    numbers = sorted(merged)
    if numbers != list(range(1, len(numbers) + 1)):
        findings.append(_finding("chain_gap", f"{key[0]}/{key[1]} 合并后 seq 有缺口或未从 1 开始"))
        return None
    previous = ""
    for seq in numbers:
        row = existing.get(seq) or fresh_map[seq]
        if row["prev_hash"] != previous:
            findings.append(_finding("chain_link", f"{key[0]}/{key[1]} seq={seq} 的 prev_hash 不相接"))
            return None
        previous = row["hash"]
    if fresh_map and expected_head != previous:
        findings.append(_finding("chain_head_mismatch", f"{key[0]}/{key[1]} 链头与 bundle.chains 不符"))
        return None
    return set(merged.values())


def _plan_chains(bundle: dict, validated: list[tuple[dict, list[dict]]], findings: list[dict]):
    """按链规划导入：包内去重、与库去重（按哈希）、seq 冲突与合并链完整性。

    返回 (新增 (row, refs) 列表, skipped 数, 每链合并哈希集合)；有发现时不写任何行。
    """
    heads = _chain_heads(bundle["chains"], findings)
    grouped: dict[tuple[str, str], list[tuple[dict, list[dict]]]] = {}
    for triple in validated:
        row = triple[0]
        grouped.setdefault((row["source"], row["trace_id"]), []).append(triple)
    if set(grouped) != set(heads):
        findings.append(_finding("schema", "chains 与事件链不互相覆盖"))
        return [], 0, {}
    fresh_all: list[tuple[dict, list[dict]]] = []
    skipped = 0
    chain_hashes: dict[tuple[str, str], set[str]] = {}
    for key in sorted(grouped):
        triples = sorted(grouped[key], key=lambda item: item[0]["seq"])
        seqs = [row["seq"] for row, _ in triples]
        if len(set(seqs)) != len(seqs):
            findings.append(_finding("schema", f"{key[0]}/{key[1]} 包内 seq 重复"))
            continue
        existing = {row["seq"]: row for row in events_db.read_events(source=key[0], trace_id=key[1])}
        present = events_db.present_hashes([row["hash"] for row, _ in triples])
        fresh = [triple for triple in triples if triple[0]["hash"] not in present]
        skipped += len(triples) - len(fresh)
        for row, _ in fresh:
            hit = existing.get(row["seq"])
            if hit is not None and hit["hash"] != row["hash"]:
                findings.append(_finding("seq_conflict", f"{key[0]}/{key[1]} seq={row['seq']} 已有不同 hash，不覆盖"))
        hashes = _check_merged_chain(key, existing, fresh, heads[key], findings)
        if hashes is not None:
            chain_hashes[key] = hashes
            fresh_all.extend(fresh)
    return fresh_all, skipped, chain_hashes


def _valid_anchor(anchor, chain_hashes: dict[tuple[str, str], set[str]], findings: list[dict]) -> bool:
    """锚点形状与链归属：六字段、head_hash 必须是所在链某个已合并前缀的链头。"""
    if not isinstance(anchor, dict) or set(anchor) != set(_ANCHOR_KEYS):
        findings.append(_finding("anchor", "锚点字段不符"))
        return False
    for name, limit in (("source", _REF_LIMIT), ("trace_id", _REF_LIMIT), ("stage", _LIMIT), ("fixed_in", _LIMIT)):
        if not _valid_text(anchor[name], limit):
            findings.append(_finding("anchor", f"锚点的 {name} 含隐私禁项、超限或为空"))
            return False
    if not isinstance(anchor["head_hash"], str) or _HASH_RE.fullmatch(anchor["head_hash"]) is None:
        findings.append(_finding("anchor", "锚点 head_hash 不是 64 位十六进制"))
        return False
    if _parse_utc(anchor["ts"]) is None:
        findings.append(_finding("anchor", "锚点 ts 不是可解析的 UTC 时间"))
        return False
    hashes = chain_hashes.get((anchor["source"], anchor["trace_id"]))
    if hashes is None:
        findings.append(_finding("anchor", "锚点所属链不在本包事件中"))
        return False
    if anchor["head_hash"] not in hashes:
        findings.append(_finding("anchor_mismatch", "锚点 head_hash 不在对应链的前缀内"))
        return False
    return True


def _plan_anchors(bundle: dict, chain_hashes: dict[tuple[str, str], set[str]], findings: list[dict]) -> list[dict]:
    """校验并按整行幂等挑选要写的新锚点（与库内及包内完全相同的不再写）。"""
    columns = ("source", "trace_id", "stage", "head_hash", "fixed_in", "ts")
    existing = {tuple(row[name] for name in columns) for row in events_db.read_anchors()}
    planned: list[dict] = []
    seen: set[tuple] = set()
    for anchor in bundle["anchors"]:
        if not _valid_anchor(anchor, chain_hashes, findings):
            continue
        key = tuple(anchor[name] for name in columns)
        if key in existing or key in seen:
            continue
        seen.add(key)
        planned.append({name: anchor[name] for name in columns})
    return planned


def import_bundle(bundle: dict) -> dict:
    """幂等导入 EventBundle，返回 {imported, skipped, findings}；坏包整体不写。

    验证（schema、来源、隐私、哈希、链完整性）全过后才在单事务里写事件、引用与锚点；任何发现都
    不写，findings 逐项给出 code 与不含内容的 detail。
    """
    findings: list[dict] = []
    if not _validate_envelope(bundle, findings):
        return {"imported": 0, "skipped": 0, "findings": findings}
    validated = [triple for index, event in enumerate(bundle["events"])
                 if (triple := _validate_event(event, index, findings)) is not None]
    fresh, skipped, chain_hashes = _plan_chains(bundle, validated, findings)
    anchors = [] if findings else _plan_anchors(bundle, chain_hashes, findings)
    if findings:
        return {"imported": 0, "skipped": 0, "findings": findings}
    try:
        imported = events_db.import_rows(fresh, anchors)
    except sqlite3.IntegrityError:
        return {"imported": 0, "skipped": 0,
                "findings": [_finding("seq_conflict", "写入时撞上已有事件（并发），整体未写")]}
    except RuntimeError as exc:
        return {"imported": 0, "skipped": 0, "findings": [_finding("storage", str(exc))]}
    return {"imported": imported, "skipped": skipped, "findings": []}


# ---- CI 事件包下载（C5 load_ci，T302）：只信 API，不信包自报；下载内容只解析、不执行 ----

_TRUSTED_PATHS = frozenset(f".github/workflows/{name}{ext}"
                           for name in ("harness", "auto-merge") for ext in (".yml", ".yaml"))
_PACKAGE_RE = re.compile(r"harness-events-(\d+)-(\d+)-(.+)\Z")
INFORMATIONAL_FINDINGS = frozenset({"head_mismatch"})  # 信息性（B124/T502）：旧 head 运行是预期历史，不导入也不算失败；调用方共用


class GhClient:
    """load_ci 缺省 gh 客户端：只读查询与 artifact 下载，失败抛 RuntimeError。"""

    def _run(self, argv: list[str]) -> bytes:
        result = subprocess.run(["gh", *argv], capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or f"gh 退出码 {result.returncode}")
        return result.stdout

    def pr(self, pr: int) -> dict:
        # baseRepository 不是 gh pr view 的合法 JSON 字段（GraphQL 有、gh 没有，gh 2.92 报 Unknown JSON field）：
        # 改从 url 解析仓库名，url 缺失或形状不符时 repository 为 None，由调用方的形状校验报告。
        # createdAt 是 PR 创建时间（判定运行分页提前停止的基准，events_judge 原样使用，缺失读到底）。
        data = json.loads(self._run(["pr", "view", str(pr), "--json", "headRefName,headRefOid,url,createdAt"]))
        match = re.search(r"\Ahttps?://[^/]+/([^/]+)/([^/]+)/pull/[0-9]+\Z", str(data.get("url") or ""))
        return {"headRefName": data.get("headRefName"), "headRefOid": data.get("headRefOid"),
                "repository": f"{match[1]}/{match[2]}" if match else None,
                "createdAt": data.get("createdAt")}

    def api(self, route: str):
        return json.loads(self._run(["api", route]))

    def download(self, url: str) -> bytes:
        return self._run(["api", url])


def _repo_identity(value: str) -> str:
    """GitHub 仓库身份（owner/repo）：兼容 https/ssh 远程 URL；无 URL 形状时原样参与比较。"""
    match = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z", value.strip())
    return f"{match[1]}/{match[2]}" if match else value


def _list_all(client, route: str, key: str, findings: list[dict]) -> list[dict] | None:
    """按 total_count 分页取全列表；API 失败、形状不符或 50 页不收敛记 api 发现并放弃。"""
    items: list[dict] = []
    for page in range(1, 51):
        try:
            data = client.api(f"{route}&page={page}")
        except (RuntimeError, ValueError) as exc:
            findings.append(_finding("api", f"列出 {key} 第 {page} 页失败：{exc}"))
            return None
        batch = data.get(key) if isinstance(data, dict) else None
        if not isinstance(batch, list):
            findings.append(_finding("api", f"{key} 响应形状不符（第 {page} 页）"))
            return None
        items += [item for item in batch if isinstance(item, dict)]
        total = data.get("total_count")
        if not batch or (isinstance(total, int) and len(items) >= total):
            return items
    findings.append(_finding("api", f"{key} 分页 50 页未收敛，已放弃"))
    return None


def _package_member(members: dict[str, bytes]) -> dict | None:
    """zip 内唯一的 harness-events JSON 包成员（按形状初筛）；没有或多个都视为坏包。"""
    found: list[dict] = []
    for name, content in sorted(members.items()):
        if name.lower().endswith(".json"):
            try:
                parsed = json.loads(content)
            except ValueError:
                parsed = None
            if (isinstance(parsed, dict) and parsed.get("schema_version") == BUNDLE_SCHEMA_VERSION
                    and "origin" in parsed and isinstance(parsed.get("events"), list)):
                found.append(parsed)
    return found[0] if len(found) == 1 else None


def _import_package(data: bytes, match: re.Match, branch: str, head: str, repo: str,
                    findings: list[dict]) -> tuple[int, int] | None:
    """一个下载完成的包：zip→bundle→origin/来源核对→幂等导入；坏包记发现返回 None。"""
    run_id, attempt, job = match.groups()
    label = f"包 {run_id}/{attempt}/{job}"
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        members = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    except zipfile.BadZipFile:
        findings.append(_finding("package_corrupt", f"{label} 不是有效的 zip"))
        return None
    bundle = _package_member(members)
    if bundle is None:
        findings.append(_finding("package_corrupt", f"{label} 找不到唯一的 harness-events JSON"))
        return None
    origin = bundle.get("origin") if isinstance(bundle.get("origin"), dict) else {}
    expected = {"run_id": run_id, "run_attempt": attempt, "job": job, "head_sha": head, "head_branch": branch}
    wrong = [key for key, want in expected.items() if str(origin.get(key)) != want]
    if "repository" not in origin or _repo_identity(str(origin["repository"])) != _repo_identity(repo):
        wrong.append("repository")
    if wrong:
        findings.append(_finding("origin_mismatch", f"{label} origin 与 API 不符（{','.join(wrong)}）"))
        return None
    if any(event.get("source") != f"ci:{run_id}:{attempt}:{job}" for event in bundle["events"]):
        findings.append(_finding("source_mismatch", f"{label} 事件来源不是 ci:{run_id}:{attempt}:{job}"))
        return None
    result = import_bundle(bundle)
    findings.extend(result["findings"])
    return result["imported"], result["skipped"]


def _download_run(client, run: dict, packages: list[dict], branch: str, head: str, repo: str,
                  findings: list[dict]) -> tuple[int, int]:
    """下载并导入一个运行的事件包，返回（新增，跳过）；过期/名字不符/下载失败逐项记发现。"""
    imported = skipped = 0
    for item in packages:
        name = str(item.get("name"))
        if item.get("expired"):
            findings.append(_finding("artifact_expired", f"artifact {name} 已过期，未下载"))
            continue
        match = _PACKAGE_RE.fullmatch(name)
        if match[1] != str(run.get("id")) or match[2] != str(run.get("run_attempt")):
            findings.append(_finding("artifact_name", f"artifact {name} 物理名与运行不符，未下载"))
            continue
        try:
            data = client.download(str(item.get("archive_download_url")))
        except (RuntimeError, ValueError) as exc:
            findings.append(_finding("api", f"下载 {name} 失败：{exc}"))
            continue
        if result := _import_package(data, match, branch, head, repo, findings):
            imported, skipped = imported + result[0], skipped + result[1]
    return imported, skipped


def load_ci(pr: int, *, head: str | None = None, gh=None) -> dict:
    """下载 PR 关联的 CI 事件包（harness 与 auto-merge 多 job/attempt）并幂等导入（C5）。

    只信 API：仓库、工作流路径、run/head 与物理名逐项核对包 origin 与事件 source；过期/缺失/
    坏包/其他 head 的运行逐项记 findings；分页不收敛明确报告。auto-merge 的判定运行由
    workflow_run 触发、在 API 里归在默认分支，分支查询查不到（B117）：由 events_judge 按工作流
    列运行、以默认分支定义渲染的运行名关联 PR，判定包按该运行自己的 API 记录核对。
    """
    findings: list[dict] = []
    client = GhClient() if gh is None else gh
    try:
        info = client.pr(pr)
        branch, resolved, repo = info.get("headRefName"), head or info.get("headRefOid"), info.get("repository")
    except (RuntimeError, ValueError, AttributeError) as exc:
        return {"imported": 0, "skipped": 0, "findings": [_finding("api", f"查询 PR {pr} 失败：{exc}")]}
    if not (isinstance(branch, str) and branch and isinstance(resolved, str)
            and _SHA_RE.fullmatch(resolved) and isinstance(repo, str) and repo):
        return {"imported": 0, "skipped": 0,
                "findings": [_finding("api", f"PR {pr} 的 head/仓库信息缺失或形状不符")]}
    route = f"repos/{repo}/actions/runs?branch={urllib.parse.quote(branch, safe='')}&per_page=100"
    runs = _list_all(client, route, "workflow_runs", findings)
    if runs is None:
        return {"imported": 0, "skipped": 0, "findings": findings}

    def import_run(run: dict, run_branch: str, run_head: str) -> None:
        nonlocal imported, skipped
        artifacts = _list_all(client, f"repos/{repo}/actions/runs/{run.get('id')}/artifacts?per_page=100",
                              "artifacts", findings)
        if artifacts is None:
            return
        packages = [item for item in artifacts if _PACKAGE_RE.fullmatch(str(item.get("name") or ""))]
        if not packages:
            findings.append(_finding("artifact_missing", f"运行 {run.get('id')} 没有 harness-events 事件包"))
        got = _download_run(client, run, packages, run_branch, run_head, repo, findings)
        imported, skipped = imported + got[0], skipped + got[1]

    imported = skipped = 0
    trusted = [run for run in runs if run.get("path") in _TRUSTED_PATHS]
    heads = [run.get("head_sha") for run in trusted]
    good = [head for head in heads if isinstance(head, str) and _SHA_RE.fullmatch(head)]
    stale = set(good) - {resolved}
    findings.extend([_finding("api", f"{bad} 个运行的 head_sha 缺失或形状不符，未导入")]
                    if (bad := len(heads) - len(good)) else [])  # 畸形记录非旧 head 历史（B124/T502）
    for run in sorted(trusted, key=lambda item: str(item.get("id"))):
        if run.get("head_sha") == resolved:
            import_run(run, branch, resolved)
    # B117：判定运行在 API 里归在默认分支，分支查询查不到；按工作流列运行（不带任何筛选参数——
    # 带参数的运行列表会被平台按 1,000 条静默截断），由默认分支定义渲染的运行名关联 PR，包 origin
    # 按该运行自己的 API 记录核对（拿 PR 的 head 去核对必然 origin_mismatch）。
    matched = events_judge.collect_judge_runs(
        client, repo=repo, pr=pr, resolved=resolved, trusted=_TRUSTED_PATHS,
        list_all=_list_all, created_at=info.get("createdAt"), stale=stale, findings=findings)
    if stale:
        findings.append(_finding("head_mismatch", f"{len(stale)} 个其他 head 的运行未导入"
                                                 f"（只导入 head {resolved[:7]}…）"))
    events_judge.import_matched(matched, import_run, findings)
    return {"imported": imported, "skipped": skipped, "findings": findings}
