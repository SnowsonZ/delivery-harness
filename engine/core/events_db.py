"""事件存储层：SQLite 建库与迁移、带哈希链的事务写入、链校验、产物与锚点。

库位于 git 公共目录下的 harness/harness.db（所有 worktree 共用，位于 .git 内不会被跟踪），产物位于
harness/artifacts/<sha256>。本模块只负责存储与完整性，不做隐私过滤（调用方 engine/core/events.py 负责）；
写入失败直接抛异常，由调用方吞掉。除本模块已知版本外不修改库结构：库的 user_version 较新时只读不写。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

from engine.core.common import ROOT, git

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
# events 表列序（INSERT 与链校验按此顺序取值）。
EVENT_COLUMNS = (
    "id", "ts", "source", "trace_id", "seq", "prev_hash", "hash", "stage", "step", "status",
    "duration_ms", "actor_role", "actor_host", "model", "decision_by", "decision_rule",
    "decision_reason", "error_kind", "error_signature", "outputs", "engine_version", "redacted",
)
_REF_KEYS = ("kind", "ref", "sha256", "size")

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    stage TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER,
    actor_role TEXT,
    actor_host TEXT,
    model TEXT,
    decision_by TEXT,
    decision_rule TEXT,
    decision_reason TEXT,
    error_kind TEXT,
    error_signature TEXT,
    outputs TEXT,
    engine_version TEXT,
    redacted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, trace_id, seq)
);
CREATE TABLE IF NOT EXISTS refs (
    event_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT,
    ref TEXT,
    sha256 TEXT,
    size INTEGER
);
CREATE TABLE IF NOT EXISTS artifacts (
    sha256 TEXT PRIMARY KEY,
    size INTEGER,
    path TEXT,
    created TEXT
);
CREATE TABLE IF NOT EXISTS anchors (
    id INTEGER PRIMARY KEY,
    source TEXT,
    trace_id TEXT,
    stage TEXT,
    head_hash TEXT,
    fixed_in TEXT,
    ts TEXT
);
"""


def _now() -> str:
    """UTC 毫秒 ISO8601（带 Z 后缀），作为事件与锚点的时间戳。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def common_dir() -> Path | None:
    """git 公共目录（`.git`，worktree 指向主仓库）；不在 git 仓库里返回 None。"""
    out = git("rev-parse", "--git-common-dir", cwd=ROOT, check=False, isolate=True)
    if not out:
        return None
    path = Path(out)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def db_path() -> Path | None:
    directory = common_dir()
    return directory / "harness" / "harness.db" if directory else None


def artifacts_dir() -> Path | None:
    directory = common_dir()
    return directory / "harness" / "artifacts" if directory else None


def _connect(path: Path) -> sqlite3.Connection:
    """每次操作新开连接（WAL、busy_timeout），用完由调用方 contextlib.closing 关闭。"""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _switch_to_wal(conn: sqlite3.Connection) -> None:
    """切到 WAL。多进程同时建库时该 PRAGMA 不受 busy_timeout 保护，需重试等待持有者完成。"""
    for attempt in range(20):
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() == "wal":
                return
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            time.sleep(0.05 * (attempt + 1))
    raise sqlite3.OperationalError("切换 WAL 超时")


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """库版本可用返回 True；比本代码已知版本新时返回 False（不写入、不修改、不报错）。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        return False
    if version < SCHEMA_VERSION:
        _switch_to_wal(conn)
        conn.executescript(_CREATE_TABLES)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return True


def _normalize_ref(item: dict) -> dict:
    """引用只保留四定键中非 None 的值；写入与链校验共用，保证哈希两侧一致。"""
    return {key: item[key] for key in _REF_KEYS if item.get(key) is not None}


def _hash(row: dict, inputs: list[dict]) -> str:
    """规范化 JSON（除 id、hash 外的全部列加 inputs 列表，按键排序、紧凑分隔符）的 sha256。"""
    payload = {key: value for key, value in row.items() if key not in ("id", "hash")}
    payload["inputs"] = inputs
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def insert_event(payload: dict, inputs: list[dict]) -> int | None:
    """写入一个事件并接上哈希链，返回事件 id。

    seq 分配、prev_hash 读取、插入在同一个 BEGIN IMMEDIATE 事务里，多进程并发写同一 trace 链仍完好。
    不在 git 仓库或库版本较新时返回 None（不算失败，不提示）；其余失败抛异常，由调用方吞掉。
    """
    path = db_path()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    refs = [item for item in (_normalize_ref(entry) for entry in inputs) if item]  # 过滤后的空引用不入表
    outputs_text = json.dumps(payload.get("outputs") or {}, sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False)
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            return None
        conn.execute("BEGIN IMMEDIATE")
        try:
            last = conn.execute(
                "SELECT seq, hash FROM events WHERE source=? AND trace_id=? ORDER BY seq DESC LIMIT 1",
                (payload["source"], payload["trace_id"]),
            ).fetchone()
            row = {
                "ts": _now(), "source": payload["source"], "trace_id": payload["trace_id"],
                "seq": 1 if last is None else last[0] + 1,
                "prev_hash": "" if last is None else last[1],
                "stage": payload["stage"], "step": payload["step"], "status": payload["status"],
                "duration_ms": payload.get("duration_ms"),
                "actor_role": payload.get("actor_role"), "actor_host": payload.get("actor_host"),
                "model": payload.get("model"),
                "decision_by": payload.get("decision_by"), "decision_rule": payload.get("decision_rule"),
                "decision_reason": payload.get("decision_reason"),
                "error_kind": payload.get("error_kind"), "error_signature": payload.get("error_signature"),
                "outputs": outputs_text, "engine_version": payload.get("engine_version"),
                "redacted": payload.get("redacted", 0),
            }
            row["hash"] = _hash(row, refs)
            columns = [name for name in EVENT_COLUMNS if name != "id"]
            cursor = conn.execute(
                f"INSERT INTO events ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                tuple(row[name] for name in columns),
            )
            event_id = cursor.lastrowid
            for item in refs:
                conn.execute(
                    "INSERT INTO refs (event_id,direction,kind,ref,sha256,size) VALUES (?,?,?,?,?,?)",
                    (event_id, "in", item.get("kind"), item.get("ref"), item.get("sha256"), item.get("size")),
                )
            conn.execute("COMMIT")
            return event_id
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


def chain_head(source: str, trace_id: str) -> str | None:
    """该 (source, trace) 最后一个事件的 hash；没有事件或没有库返回 None。"""
    path = db_path()
    if path is None or not path.exists():
        return None
    with closing(_connect(path)) as conn:
        row = conn.execute(
            "SELECT hash FROM events WHERE source=? AND trace_id=? ORDER BY seq DESC LIMIT 1",
            (source, trace_id),
        ).fetchone()
        return row[0] if row else None


def verify(trace_id: str | None = None, source: str | None = None) -> list[str]:
    """校验哈希链，返回问题描述列表（空列表表示完好）；trace_id、source 为 None 时校验全部。

    先看库版本：schema 比本代码新时明确报告不可校验，不修改库、不当作完好。
    """
    path = db_path()
    if path is None or not path.exists():
        return []
    problems: list[str] = []
    with closing(_connect(path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            return [f"harness.db 的 schema 版本较新（user_version={version} > {SCHEMA_VERSION}），不可校验"]
        where, params = [], []
        if trace_id is not None:
            where.append("trace_id=?")
            params.append(trace_id)
        if source is not None:
            where.append("source=?")
            params.append(source)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        groups = conn.execute("SELECT DISTINCT source, trace_id FROM events" + clause, params).fetchall()
        columns = ",".join(EVENT_COLUMNS)
        for src, trace in groups:
            rows = conn.execute(
                f"SELECT {columns} FROM events WHERE source=? AND trace_id=? ORDER BY seq",
                (src, trace),
            ).fetchall()
            problems.extend(_verify_group(conn, src, trace, rows))
    return problems


def _verify_group(conn: sqlite3.Connection, source: str, trace_id: str,
                  rows: list[tuple]) -> list[str]:
    """校验一条 (source, trace) 链：seq 连续、prev_hash 相接、内容与 hash 一致。"""
    problems = []
    expected_seq = 1
    prev_hash = ""
    for values in rows:
        row = dict(zip(EVENT_COLUMNS, values))
        refs = conn.execute(
            "SELECT kind,ref,sha256,size FROM refs WHERE event_id=? ORDER BY rowid", (row["id"],)
        ).fetchall()
        inputs = [_normalize_ref(dict(zip(_REF_KEYS, ref_row))) for ref_row in refs]
        if row["seq"] != expected_seq:
            problems.append(f"{source}/{trace_id}: 事件 {row['id']} 的 seq 应为 {expected_seq}，实际 {row['seq']}")
        if row["prev_hash"] != prev_hash:
            problems.append(f"{source}/{trace_id}: 事件 {row['id']} 的 prev_hash 与前一事件 hash 不符")
        if _hash(row, inputs) != row["hash"]:
            problems.append(f"{source}/{trace_id}: 事件 {row['id']} 的内容与 hash 不符")
        expected_seq = row["seq"] + 1
        prev_hash = row["hash"]
    return problems


_TMP_SEQ = count()


def _write_artifact_file(target: Path, content: bytes) -> None:
    """产物落盘：先写临时文件再原子替换；失败不留下半文件与临时残留，已有内容不重复写。

    临时文件名带进程号与计数，多进程/多线程并发存同一产物互不冲突；os.replace 原子生效，
    内容寻址保证同名即同内容，先后替换结果一致。
    """
    if target.exists():
        return
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}-{next(_TMP_SEQ)}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)  # 替换成功后临时文件已不存在；失败时清掉残留


def save_artifact(digest: str, size: int, content: bytes) -> None:
    """内容寻址保存产物（临时文件写完后原子替换，已存在则不重复写）并登记 artifacts 表。"""
    directory = artifacts_dir()
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / digest
    _write_artifact_file(target, content)
    path = db_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            return
        conn.execute("INSERT OR IGNORE INTO artifacts (sha256,size,path,created) VALUES (?,?,?,?)",
                     (digest, size, target.as_posix(), _now()))


def add_anchor(source: str, trace_id: str, stage: str, head_hash: str, fixed_in: str) -> None:
    """在 anchors 表登记一条链头锚点（fixed_in：run_record / ci_artifact / pr_comment）。"""
    path = db_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            return
        conn.execute("INSERT INTO anchors (source,trace_id,stage,head_hash,fixed_in,ts) VALUES (?,?,?,?,?,?)",
                     (source, trace_id, stage, head_hash, fixed_in, _now()))
