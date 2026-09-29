"""事件公共 API（可观测性）：只包住已有的结构化结果，永不影响调用方。

事件不进任何判定（设计 2.5）：emit / set_anchor 永不抛异常，失败向 stderr 提示一次（同一进程内）
后跳过；HARNESS_EVENTS=off 或 checks.toml [events] enabled = false 时关闭，环境变量优先。
值只放引用、哈希、计数、枚举、时长：写入前强制隐私过滤（设计 2.4），只丢弃、不改写、不截断。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from engine import __version__ as ENGINE_VERSION
from engine.core import events_db
from engine.core.common import git, setting

STAGES = ("guard", "verify", "dispatch", "ci", "route", "review", "merge", "alert")
STATUSES = ("ok", "fail", "skip", "deny", "error")
ACTOR_ROLES = ("designer", "implementer", "reviewer", "approver", "engine")

_MAX_STR = 120
_MAX_REASON = 200
_MAX_REF = 200
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_FORBIDDEN_PREFIXES = ("/Users/", "/home/")
_DROP = object()

_warned = False


def _warn_once() -> None:
    """同一进程内只向 stderr 提示一次事件写入失败。"""
    global _warned
    if not _warned:
        _warned = True
        print("harness：事件写入失败，已跳过（不影响本次运行）", file=sys.stderr)


def enabled() -> bool:
    """事件开关：HARNESS_EVENTS=off 关闭；其次 checks.toml [events] enabled = false 关闭；缺省开启。"""
    env = os.environ.get("HARNESS_EVENTS")
    if env is not None:
        return env.strip().lower() not in ("", "0", "off", "false")
    try:
        return setting("events", "enabled", True) is not False
    except Exception:  # noqa: BLE001  配置损坏时按缺省处理
        return True


def default_source() -> str:
    """事件来源：真实 Actions（run_id/run_attempt/job 三键齐全）为
    "ci:<run_id>:<run_attempt>:<job>"（来源链隔离）；缺键的 CI 环境兼容返回 "ci"；否则 "local"。"""
    if os.environ.get("CI") == "true":
        run_id = os.environ.get("GITHUB_RUN_ID")
        run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
        job = os.environ.get("GITHUB_JOB")
        if run_id and run_attempt and job:
            return f"ci:{run_id}:{run_attempt}:{job}"
        return "ci"
    return "local"


def _host_class(source: str) -> str:
    """actor.host 缺省值：链来源的显示分类（隔离后的 ci:<run_id>:… 仍显示为 "ci"，其余沿用 source）。"""
    return "ci" if source.startswith("ci") else source


def _short_head() -> str:
    return git("rev-parse", "--short=7", "HEAD", cwd=events_db.ROOT, check=False, isolate=True) or "unknown"


def current_trace() -> str:
    """追踪 ID：分支名；detached HEAD 为 HEAD@<7 位提交>；在 main 上为 main@<7 位提交>。

    CI 中优先取 GITHUB_HEAD_REF（PR 分支），其次 GITHUB_REF_NAME（为 main 时同样写 main@<7 位提交>）。
    """
    if os.environ.get("CI") == "true":
        head_ref = os.environ.get("GITHUB_HEAD_REF")
        if head_ref:
            return head_ref
        ref_name = os.environ.get("GITHUB_REF_NAME")
        if ref_name:
            return f"main@{_short_head()}" if ref_name == "main" else ref_name
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=events_db.ROOT, check=False, isolate=True)
    if branch in ("HEAD", "main"):
        return f"{branch}@{_short_head()}"
    return branch or "unknown"


def ref(kind: str, ref: str, *, sha256: str | None = None, size: int | None = None) -> dict:
    """输入输出引用（值是引用而非内容）：sha256、size 为 None 的键省略。"""
    out = {"kind": kind, "ref": ref}
    if sha256 is not None:
        out["sha256"] = sha256
    if size is not None:
        out["size"] = size
    return out


def file_ref(kind: str, path: Path, rev: str | None = None) -> dict:
    """文件引用：读内容算 sha256 与大小，ref 为仓库相对路径（POSIX），rev 非空时写成 <路径>@<rev>。"""
    path = Path(path)
    content = path.read_bytes()
    try:
        relative = path.resolve().relative_to(events_db.ROOT.resolve()).as_posix()
    except ValueError:
        relative = path.as_posix()
    if rev:
        relative = f"{relative}@{rev}"
    digest = hashlib.sha256(content).hexdigest()
    return {"kind": kind, "ref": relative, "sha256": digest, "size": len(content)}


def store_artifact(data: bytes | Path) -> dict | None:
    """内容寻址保存到 artifacts/<sha256>（已存在则不重复写）并登记 artifacts 表，返回引用字典。

    Path 读取或 bytes 转换失败同样不抛异常：提示一次并返回 None；调用方把 None 放进 inputs
    时会被引用过滤器丢弃并计入 redacted，不产生半条引用。
    """
    try:
        content = Path(data).read_bytes() if isinstance(data, Path) else bytes(data)
    except Exception:  # noqa: BLE001  设计要求：观察失败不得影响调用方
        _warn_once()
        return None
    digest = hashlib.sha256(content).hexdigest()
    entry = {"kind": "artifact", "ref": digest, "sha256": digest, "size": len(content)}
    try:
        if enabled():
            events_db.save_artifact(digest, len(content), content)
    except Exception:  # noqa: BLE001  设计要求：事件失败不得影响调用方
        _warn_once()
    return entry


def _clean_str(value: str, limit: int) -> str | None:
    """合规字符串原样返回；超长、形似本机路径、含换行的返回 None（丢弃，不截断）。"""
    if len(value) > limit:
        return None
    if value.startswith(_FORBIDDEN_PREFIXES) or "C:\\" in value or "\n" in value or "\r" in value:
        return None
    return value


def _clean_scalar(value, limit: int = _MAX_STR):
    """只允许 int、float、bool、None 与合规 str；其余（嵌套 dict/list 等）丢弃。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        cleaned = _clean_str(value, limit)
        return cleaned if cleaned is not None else _DROP
    return _DROP


def _clean_role(value) -> str | object:
    return value if value in ACTOR_ROLES else _DROP


def _filter_outputs(outputs) -> tuple[dict, int]:
    """outputs 必须是扁平字典：键 [A-Za-z0-9_.-]{1,64}，值合规标量；其余丢弃。"""
    if not outputs:
        return {}, 0
    if not isinstance(outputs, dict):
        return {}, 1
    clean, dropped = {}, 0
    for key, value in outputs.items():
        cleaned = _clean_scalar(value) if isinstance(key, str) and _KEY_RE.match(key) else _DROP
        if cleaned is _DROP:
            dropped += 1
        else:
            clean[key] = cleaned
    return clean, dropped


def _filter_known(mapping, limits: dict[str, int], validators: dict[str, Callable] | None = None) -> tuple[dict, int]:
    """按已定键集合过滤 dict（decision/error/actor/引用）；未知键与不合规值丢弃。"""
    if not mapping:
        return {}, 0
    if not isinstance(mapping, dict):
        return {}, 1
    clean, dropped = {}, 0
    for key, value in mapping.items():
        if key not in limits:
            dropped += 1
            continue
        if validators and key in validators:
            cleaned = validators[key](value)
        else:
            cleaned = _clean_scalar(value, limits[key])
        if cleaned is _DROP:
            dropped += 1
        else:
            clean[key] = cleaned
    return clean, dropped


def _filter_inputs(inputs) -> tuple[list[dict], int]:
    """inputs 是引用列表；非 dict 的元素与元素内不合规的键值丢弃。"""
    if not inputs:
        return [], 0
    if not isinstance(inputs, list):
        return [], 1
    clean, dropped = [], 0
    for item in inputs:
        item_clean, item_dropped = _filter_known(item, {"kind": _MAX_STR, "ref": _MAX_REF,
                                                        "sha256": _MAX_STR, "size": _MAX_STR})
        if any(value is not None for value in item_clean.values()):
            clean.append(item_clean)
            dropped += item_dropped
        else:
            dropped += max(item_dropped, 1)  # 过滤后为空的引用整条丢弃（不入表、不进哈希），至少计 1
    return clean, dropped


_DECISION_LIMITS = {"by": _MAX_STR, "rule": _MAX_STR, "reason": _MAX_REASON}
_ERROR_LIMITS = {"kind": _MAX_STR, "signature": _MAX_STR}
_ACTOR_LIMITS = {"role": _MAX_STR, "host": _MAX_STR, "model": _MAX_STR}


def _build_payload(stage: str, step: str, status: str, source: str, trace_id: str,
                   duration_ms, outputs, decision, error, actor, inputs) -> tuple[dict, list[dict]]:
    """隐私过滤后组装存储层需要的列值（redacted 为被丢弃的键值累计）。"""
    clean_outputs, n_outputs = _filter_outputs(outputs)
    clean_decision, n_decision = _filter_known(decision, _DECISION_LIMITS)
    clean_error, n_error = _filter_known(error, _ERROR_LIMITS)
    clean_actor, n_actor = _filter_known(actor, _ACTOR_LIMITS, {"role": _clean_role})
    clean_inputs, n_inputs = _filter_inputs(inputs)
    return {
        "source": source, "trace_id": trace_id, "stage": stage, "step": step, "status": status,
        "duration_ms": duration_ms,
        "actor_role": clean_actor.get("role"), "actor_host": clean_actor.get("host"),
        "model": clean_actor.get("model"),
        "decision_by": clean_decision.get("by"), "decision_rule": clean_decision.get("rule"),
        "decision_reason": clean_decision.get("reason"),
        "error_kind": clean_error.get("kind"), "error_signature": clean_error.get("signature"),
        "outputs": clean_outputs, "engine_version": ENGINE_VERSION,
        "redacted": n_outputs + n_decision + n_error + n_actor + n_inputs,
    }, clean_inputs


def emit(stage: str, step: str, status: str, *, trace_id: str | None = None,
         duration_ms: int | None = None, inputs: list[dict] | None = None, outputs: dict | None = None,
         decision: dict | None = None, error: dict | None = None, actor: dict | None = None,
         source: str | None = None) -> int | None:
    """写一个事件，返回事件 id；关闭、失败、被过滤掉整条事件时返回 None。永不抛异常。

    trace_id 缺省 current_trace()；source 缺省按环境取（真实 Actions 为 ci:<run_id>:<run_attempt>:<job>，
    其余 CI 为 "ci"，本地为 "local"）；actor 缺省 {"role": "engine", "host": <来源的显示分类>}。
    step 按字符串隐私规则过滤，非法整条不写。
    """
    try:
        if not enabled():
            return None
        step_text = str(step or "").strip()
        if stage not in STAGES or status not in STATUSES or not step_text:
            return None
        if _clean_str(step_text, _MAX_STR) is None:
            return None
        source = source or default_source()
        payload, clean_inputs = _build_payload(stage, str(step), status, source,
                                               trace_id or current_trace(), duration_ms,
                                               outputs, decision, error,
                                               actor or {"role": "engine", "host": _host_class(source)}, inputs)
        return events_db.insert_event(payload, clean_inputs)
    except Exception:  # noqa: BLE001  设计要求：事件失败不得影响调用方
        _warn_once()
        return None


class Span:
    """span 上下文：块内可设置 status（默认 ok）、inputs、outputs、decision、error。"""

    _FIXED_KEYS = frozenset(("trace_id", "duration_ms", "inputs", "outputs",
                             "decision", "error", "actor", "source"))

    def __init__(self, stage: str, step: str, fixed: dict):
        self.stage = stage
        self.step = step
        self.fixed = fixed
        self.status = "ok"
        self.inputs = None
        self.outputs = None
        self.decision = None
        self.error = None
        self._start = 0.0


@contextmanager
def span(stage: str, step: str, **fixed) -> Iterator[Span]:
    """计时包装：退出时 emit 一个事件（duration_ms 为块耗时）。

    块内抛异常：status 设为 "error"、error={"kind": 异常类型名}，事件写入后重新抛出原异常。
    fixed 里 emit 认可的键（trace_id、actor、source 等）原样传递；未知键丢弃——
    观察侧失败不得传播，也不得遮盖块内的业务异常。
    """
    holder = Span(stage, step, dict(fixed))
    holder._start = time.monotonic()
    try:
        yield holder
    except BaseException as exc:
        holder.status = "error"
        holder.error = {"kind": type(exc).__name__}
        raise
    finally:
        kwargs = {
            "stage": holder.stage, "step": holder.step, "status": holder.status,
            "duration_ms": int((time.monotonic() - holder._start) * 1000),
            "inputs": holder.inputs, "outputs": holder.outputs,
            "decision": holder.decision, "error": holder.error,
        }
        kwargs.update({key: value for key, value in holder.fixed.items() if key in Span._FIXED_KEYS})
        emit(**kwargs)


def set_anchor(trace_id: str, stage: str, head_hash: str, fixed_in: str,
               source: str | None = None) -> None:
    """在 anchors 表登记链头锚点（fixed_in：run_record / ci_artifact / pr_comment）。永不抛异常。"""
    try:
        if not enabled():
            return
        events_db.add_anchor(source or default_source(), trace_id, stage, head_hash, fixed_in)
    except Exception:  # noqa: BLE001  设计要求：事件失败不得影响调用方
        _warn_once()


def chain_head(trace_id: str, source: str | None = None) -> str | None:
    """该 (source, trace) 最后一个事件的 hash（source 缺省同 emit）；没有事件为 None。"""
    try:
        return events_db.chain_head(source or default_source(), trace_id)
    except Exception:  # noqa: BLE001  设计要求：事件失败不得影响调用方
        return None


def verify_chain(trace_id: str | None = None, source: str | None = None) -> list[str]:
    """校验哈希链，返回问题描述列表（空列表表示完好）；trace_id、source 为 None 时校验全部。"""
    try:
        return events_db.verify(trace_id, source)
    except Exception as exc:  # noqa: BLE001  设计要求：链校验失败只报告，不影响调用方
        return [f"链校验失败：{exc}"]
