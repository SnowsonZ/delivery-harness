"""事件存储层：SQLite 建库与迁移、带哈希链的事务写入、链校验、产物与锚点。

库位于 git 公共目录下的 harness/harness.db（所有 worktree 共用，位于 .git 内不会被跟踪），产物位于
harness/artifacts/<sha256>。本模块只负责存储与完整性，不做隐私过滤（调用方 engine/core/events.py 负责）；
写入失败直接抛异常，由调用方吞掉。除本模块已知版本外不修改库结构：库的 user_version 较新时只读不写。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
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


def _harness_dir() -> Path | None:
    """事件与产物的 harness 目录：默认 git 公共目录下的 harness/。

    环境变量 HARNESS_EVENTS_REDIRECT 由 verify 在启动项目检查子进程时设置（B92），值是 JSON：
    {"dir": <临时目录>, "for_common_dir": <设置方解析到的 git 公共目录绝对路径>}。当前解析到的
    公共目录与 for_common_dir 完全相同时返回 <dir>/harness，该子进程的事件与产物导进临时库；
    变量不存在、不是合法 JSON、字段缺失或类型不对、公共目录不同（测试 patch ROOT 指向夹具仓库、
    夹具里再起的子进程）时一律返回原路径。解析失败不抛异常：事件只是观察，不影响任何判定。
    """
    directory = common_dir()
    if directory is None:
        return None
    raw = os.environ.get("HARNESS_EVENTS_REDIRECT")
    if not raw:
        return directory / "harness"
    try:
        data = json.loads(raw)
        target, origin = data.get("dir"), data.get("for_common_dir")
        if (isinstance(target, str) and isinstance(origin, str)
                and Path(origin).resolve() == directory):
            return Path(target) / "harness"
    except Exception:  # noqa: BLE001  解析失败按原路径处理，不抛异常（事件只是观察）
        return directory / "harness"
    return directory / "harness"


def db_path() -> Path | None:
    directory = _harness_dir()
    return directory / "harness.db" if directory else None


def artifacts_dir() -> Path | None:
    directory = _harness_dir()
    return directory / "artifacts" if directory else None


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


# ---- 读取与批量导入（T301 events_io 的存储原语）：不改表结构，user_version 保持 1 ----

def read_events(source: str | None = None, trace_id: str | None = None,
                stage: str | None = None, status: str | None = None) -> list[dict]:
    """按可选精确过滤读取事件全列（含 id），按 (source, trace_id, seq) 排序。

    没有库或库版本比本代码新时返回空列表（与 chain_head 的缺省行为一致：上层按无数据处理）。
    """
    path = db_path()
    if path is None or not path.exists():
        return []
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            return []
        where, params = [], []
        for column, value in (("source", source), ("trace_id", trace_id),
                              ("stage", stage), ("status", status)):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"SELECT {','.join(EVENT_COLUMNS)} FROM events{clause} ORDER BY source, trace_id, seq",
            params,
        ).fetchall()
    return [dict(zip(EVENT_COLUMNS, values)) for values in rows]


def read_refs(event_ids: list[int]) -> dict[int, list[dict]]:
    """按事件 id 批量读取规范化输入引用（保留行序，None 值键省略），返回 {event_id: [引用]}。"""
    path = db_path()
    if path is None or not path.exists() or not event_ids:
        return {}
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            return {}
        placeholders = ",".join("?" * len(event_ids))
        rows = conn.execute(
            f"SELECT event_id, kind, ref, sha256, size FROM refs WHERE event_id IN ({placeholders}) ORDER BY rowid",
            event_ids,
        ).fetchall()
    grouped: dict[int, list[dict]] = {}
    for event_id, *values in rows:
        grouped.setdefault(event_id, []).append(_normalize_ref(dict(zip(_REF_KEYS, values))))
    return grouped


def read_anchors(source: str | None = None, trace_id: str | None = None) -> list[dict]:
    """按可选链过滤读取锚点（不含 id，字段与表列一致），保持表内顺序。"""
    path = db_path()
    if path is None or not path.exists():
        return []
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            return []
        where, params = [], []
        if source is not None:
            where.append("source=?")
            params.append(source)
        if trace_id is not None:
            where.append("trace_id=?")
            params.append(trace_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            "SELECT source, trace_id, stage, head_hash, fixed_in, ts FROM anchors"
            f"{clause} ORDER BY id",
            params,
        ).fetchall()
    keys = ("source", "trace_id", "stage", "head_hash", "fixed_in", "ts")
    return [dict(zip(keys, values)) for values in rows]


def present_hashes(digests: list[str]) -> set[str]:
    """返回这些事件 hash 中已存在库里的部分（events_io 按事件哈希幂等去重用）。"""
    path = db_path()
    if path is None or not path.exists() or not digests:
        return set()
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            return set()
        placeholders = ",".join("?" * len(digests))
        rows = conn.execute(f"SELECT hash FROM events WHERE hash IN ({placeholders})", digests).fetchall()
    return {row[0] for row in rows}


def import_rows(events: list[tuple[dict, list[dict]]], anchors: list[dict]) -> int:
    """事务写入已验证的导入事件与锚点，返回写入的事件数；任一失败整体回滚。

    事件按调用方给定的 seq/prev_hash/hash 原样写入（不重排、不重算哈希）；(source, trace_id, seq)
    撞上已有行时以 IntegrityError 抛出，不覆盖；锚点按整行幂等（六字段完全相同的不重复写）。
    不在 git 仓库或库版本比本代码新时抛 RuntimeError，由调用方转为发现报告，不写半包。
    """
    path = db_path()
    if path is None:
        raise RuntimeError("不在 git 仓库，没有可导入的事件库")
    path.parent.mkdir(parents=True, exist_ok=True)
    anchor_keys = ("source", "trace_id", "stage", "head_hash", "fixed_in", "ts")
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            raise RuntimeError("事件库 schema 比本代码新，不导入")
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = [name for name in EVENT_COLUMNS if name != "id"]
            for row, refs in events:
                cursor = conn.execute(
                    f"INSERT INTO events ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                    tuple(row[name] for name in columns),
                )
                for item in refs:
                    conn.execute(
                        "INSERT INTO refs (event_id,direction,kind,ref,sha256,size) VALUES (?,?,?,?,?,?)",
                        (cursor.lastrowid, "in", item.get("kind"), item.get("ref"),
                         item.get("sha256"), item.get("size")),
                    )
            for anchor in anchors:
                values = [anchor[name] for name in anchor_keys]
                conn.execute(
                    "INSERT INTO anchors (source,trace_id,stage,head_hash,fixed_in,ts)"
                    " SELECT ?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM anchors"
                    " WHERE source=? AND trace_id=? AND stage=? AND head_hash=? AND fixed_in=? AND ts=?)",
                    [*values, *values],
                )
            conn.execute("COMMIT")
            return len(events)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


# ---- 保留期清理（B40）：无守护进程，每天首次 emit 顺带执行，失败只报告不改变业务 ----

# 只删内容寻址目录里名字为 64 位十六进制的普通文件，与派发原始流 round-N.jsonl。
_HASH_NAME = re.compile(r"[0-9a-f]{64}\Z")
_ROUND_NAME = re.compile(r"round-\d+\.jsonl\Z")


def claim_cleanup_day(today: str) -> bool:
    """当天首次进入返回 True：以 O_CREAT|O_EXCL 原子创建 .cleanup-<日期> 标志（兼任跨进程锁）。

    标志放在 git 公共目录的 harness/ 下且不删除：同日再次 emit 与并发进程都得到 False。
    时钟回退到更早日期按回退后的日期领旗；即使因此多跑一轮，删除阈值也随当前时钟前移，不会多删。
    """
    directory = common_dir()
    if directory is None:
        return False
    try:
        harness_dir = directory / "harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(harness_dir / f".cleanup-{today}", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{today}\n")
    return True


def _parse_utc(value) -> datetime | None:
    """解析库里的 UTC ISO 时间戳；损坏或缺失时返回 None（调用方按无法判断跳过）。"""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _remove_expired_artifact(directory: Path, digest: str) -> str | None:
    """删除过期产物文件；返回问题描述（None 表示已删除或本就缺失）。不追随符号链接。"""
    target = directory / digest
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None  # 文件已不在：过期的索引行一并清理
    except OSError as exc:
        return f"产物无法确认，已跳过：{exc.strerror or exc}"
    if not stat.S_ISREG(info.st_mode):
        return "产物位置不是普通文件（可能是符号链接），已跳过"
    try:
        target.unlink()
    except OSError as exc:
        return f"产物删除失败，已跳过：{exc.strerror or exc}"
    return None


def cleanup_artifacts(cutoff: datetime) -> list[str]:
    """删除过期本机产物：以 UTC created 判定（早于 cutoff 才过期），文件与索引行一起清。

    只删内容寻址目录内名字合法的普通文件；符号链接、目录、非法名字与路径穿越一律跳过并计入
    问题列表。events/refs/anchors 行不在此清理范围；文件已不在时仍清掉过期的索引行。
    """
    problems: list[str] = []
    path = db_path()
    directory = artifacts_dir()
    if path is None or directory is None or not path.exists():
        return problems
    with closing(_connect(path)) as conn:
        if not _schema_ready(conn):
            return problems
        for digest, created in conn.execute("SELECT sha256, created FROM artifacts").fetchall():
            moment = _parse_utc(created)
            if moment is None or moment >= cutoff:
                continue
            if not _HASH_NAME.fullmatch(str(digest)):
                problems.append("产物索引名字不合法，已跳过（不删除）")
                continue
            problem = _remove_expired_artifact(directory, str(digest))
            if problem:
                problems.append(problem)
                continue
            conn.execute("DELETE FROM artifacts WHERE sha256=?", (digest,))
    return problems


def _alive(pid: int) -> bool:
    """探活：系统明确 ProcessLookupError 才算不活动；权限不明等其余情形按活动处理（保守，不误删）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _protected_tasks(slots: Path) -> tuple[set[str], list[str]]:
    """收集仍活动槽位锁保护的 task 目录名，返回 (受保护集合, 问题列表)。只读锁文件，不修改派发状态。

    任一锁坏 JSON、缺 pid、pid 非正整数或读取失败时问题列表非空：调用方当轮整体跳过原始流清理，
    不按「读不到等于终止」删除。存活锁保护该 task 的全部 attempt 目录（F4）。
    """
    protected: set[str] = set()
    problems: list[str] = []
    try:
        locks = sorted(slots.glob("*.json"))
    except OSError:
        return set(), ["槽位锁目录无法读取"]
    for lock in locks:
        try:
            data = json.loads(lock.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            problems.append("槽位锁读取失败")
            continue
        pid = data.get("pid") if isinstance(data, dict) else None
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            problems.append("槽位锁缺少有效 pid")
            continue
        task = data.get("task")
        if not isinstance(task, str) or not task:
            problems.append("槽位锁缺少 task")
            continue
        if _alive(pid):
            protected.add(task)
    return protected, problems


def _is_real_dir(path: Path) -> bool:
    """lstat 确认是真实目录（不追随符号链接）。"""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _delete_expired_stream(path: Path, cutoff_ts: float) -> list[str]:
    """删除 mtime（UTC）早于 cutoff 的 round-N.jsonl 普通文件；删除前重新 lstat，符号链接不追随。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"原始流无法确认，已跳过：{exc.strerror or exc}"]
    if not stat.S_ISREG(info.st_mode):
        return ["原始流不是普通文件（可能是符号链接），已跳过"]
    if info.st_mtime >= cutoff_ts:
        return []
    try:
        path.unlink()
    except OSError as exc:
        return [f"原始流删除失败，已跳过：{exc.strerror or exc}"]
    return []


def _cleanup_attempt_streams(attempt_dir: Path, cutoff_ts: float) -> list[str]:
    """删除一个 attempt 目录里的过期 round-N.jsonl；目录异常跳过并报告，符号链接不追随。"""
    if not _is_real_dir(attempt_dir):
        return ["attempt 目录不是普通目录，已跳过"]
    try:
        rounds = sorted(attempt_dir.iterdir())
    except OSError:
        return ["原始流目录无法读取，已跳过"]
    problems: list[str] = []
    for round_file in rounds:
        if _ROUND_NAME.fullmatch(round_file.name):
            problems.extend(_delete_expired_stream(round_file, cutoff_ts))
    return problems


def _cleanup_task_streams(task_dir: Path, protected: set[str], cutoff_ts: float) -> list[str]:
    """一个任务目录的原始流清理：活动锁保护的任务连同全部 attempt 目录整目录跳过。"""
    if task_dir.name in protected:
        return []
    if not _is_real_dir(task_dir):
        return ["原始流任务目录不是普通目录，已跳过"]
    try:
        attempt_dirs = sorted(task_dir.iterdir())
    except OSError:
        return ["attempt 目录无法读取，已跳过"]
    problems: list[str] = []
    for attempt_dir in attempt_dirs:
        problems.extend(_cleanup_attempt_streams(attempt_dir, cutoff_ts))
    return problems


def cleanup_dispatch_streams(runs: Path, cutoff: datetime) -> list[str]:
    """删除已终止运行的过期派发原始流（runs/<任务>/<attempt>/round-N.jsonl）。

    runs 指向 <git 公共目录>/dispatch/runs；活动锁（pid 存活）保护该任务的全部 attempt 目录，
    无锁任务按原始流 mtime（UTC）判定。锁读取或探活不确定时当轮整体跳过并报告；
    只删名字为 round-N.jsonl 的普通文件，符号链接与目录不删、不跟随，不修改锁与派发状态。
    """
    protected, lock_problems = _protected_tasks(runs.parent / "slots")
    if lock_problems:
        return [*lock_problems, "原始流清理当轮跳过"]
    if not _is_real_dir(runs):
        return []
    cutoff_ts = cutoff.timestamp()
    try:
        task_dirs = sorted(runs.iterdir())
    except OSError:
        return ["原始流目录无法读取"]
    problems: list[str] = []
    for task_dir in task_dirs:
        problems.extend(_cleanup_task_streams(task_dir, protected, cutoff_ts))
    return problems


def run_retention_cleanup(cutoff: datetime) -> list[str]:
    """B40 保留期清理入口：过期产物文件与索引 + 终止运行的过期原始流，返回问题列表。

    产物按库里的 UTC created、原始流按文件 mtime（UTC）判定，都只在早于 cutoff 时删除。
    任一异常由调用方（engine.core.events）吞掉并提示一次，不影响 emit 与业务返回。
    """
    problems = cleanup_artifacts(cutoff)
    common = common_dir()
    if common is not None:
        problems.extend(cleanup_dispatch_streams(common / "dispatch" / "runs", cutoff))
    return problems
