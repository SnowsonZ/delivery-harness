"""派发与独立评审的观察旁路（B46 T105）：组装设计 3.2 / 3.4 的事件元数据并写事件。

共用合同 C0/C1 与任务书 F1：观察是旁路——本模块的任何失败（事件写入、引用与产物准备、摘要组装）
都不得改变派发与评审的判定、返回码或 host/gh 调用序列；dispatch.py 与 review.py 只接线。事件里只有
引用、哈希、计数、枚举、时长（隐私过滤由 events.emit 负责）；每轮 Pi 流、verify 输出、CI 失败摘要、
升级摘要与评审材料等原始内容只存本机产物（artifacts/<sha256>），事件与审计摘要只带哈希与大小。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path

from engine.core import events
from engine.core.common import git

STAGE = "dispatch"
REVIEW_STAGE = "review"
# C6 严重度词表 → 事件输出的 ASCII 键（outputs 键只允许 [A-Za-z0-9_.-]，中文键会被过滤器丢弃）。
SEVERITIES = ("阻断", "严重", "一般", "建议")
_SEVERITY_KEYS = {"阻断": "severity.blocker", "严重": "severity.critical", "一般": "severity.major",
                  "建议": "severity.minor", "unknown": "severity.unknown"}


def _emit(stage: str, step: str, status: str, **kwargs) -> None:
    """写事件并吞掉一切失败：emit 自身不抛，组装 kwargs 的异常也不能逃出观察旁路。"""
    try:
        events.emit(stage=stage, step=step, status=status, **kwargs)
    except Exception:  # noqa: BLE001  观察旁路：失败只丢弃
        return


def _artifact_refs(prefix: str, data: bytes) -> dict:
    """本机产物引用三元字段（<prefix>.sha256/.size/.ref）；内容只存本机，事件只有哈希与大小。"""
    refs = {f"{prefix}.sha256": hashlib.sha256(data).hexdigest(), f"{prefix}.size": len(data)}
    try:
        stored = events.store_artifact(data)
    except Exception:  # noqa: BLE001  观察旁路：产物保存失败只少记 ref
        stored = None
    if stored:
        refs[f"{prefix}.ref"] = stored["ref"]
    return refs


# ---- 派发（设计 3.2）----

def admit(rel: str, root: Path, task=None, problems: list[str] | None = None) -> None:
    """1 准入：任务书 sha 与 origin/main 引用；通过与否、任务编号、类别与预算（时长/重试/CI 轮次）。"""
    try:
        if not events.enabled():
            return
        inputs = _admit_inputs(rel, root)
        if task is None:
            reason = problems[0] if problems else None
            _emit(STAGE, "admit", "fail", inputs=inputs, outputs={"problems": len(problems or [])},
                  decision={"by": "taskbook", "rule": "admit", "reason": reason}, error={"kind": "admit"})
            return
        budget = task.budget or {}
        _emit(STAGE, "admit", "ok", trace_id=task.branch, inputs=inputs,
              outputs={"task": task.id, "class": task.klass, "wall_clock_min": budget.get("wall_clock_min"),
                       "retries": budget.get("retries"), "ci_rounds": budget.get("ci_rounds")},
              decision={"by": "taskbook", "rule": "admit"})
    except Exception:  # noqa: BLE001  观察旁路
        return


def _admit_inputs(rel: str, root: Path) -> list[dict]:
    """任务书（路径@origin/main + 内容哈希）与 origin/main 提交引用；读取失败只少记。"""
    inputs: list[dict] = []
    try:
        rev = git("rev-parse", "origin/main", cwd=root, check=False)
        data = (root / rel).read_bytes()
    except Exception:  # noqa: BLE001  观察旁路
        return inputs
    if data:
        inputs.append({"kind": "taskbook", "ref": f"{rel}@{rev}" if rev else rel,
                       "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    if rev:
        inputs.append(events.ref("rev", rev))
    return inputs


def claim(branch: str, claimed: bool) -> None:
    """2 认领：远端分支已存在为 fail（已被认领）；从槽位推送新分支成功为 ok。"""
    _emit(STAGE, "claim", "ok" if claimed else "fail", trace_id=branch,
          inputs=[events.ref("branch", branch)], outputs={"claimed": claimed},
          decision=None if claimed else {"by": "dispatch", "rule": "claim", "reason": "已被认领"})


def slot(branch: str, index: int | None, start: str | None = None, base: str | None = None,
         problem: str | None = None) -> None:
    """3 槽位：槽位序号与起点提交；无空闲槽位等失败也留事件。"""
    outputs: dict = {"slot": index, "start": start}
    if problem:
        outputs["problem"] = problem
    _emit(STAGE, "slot", "fail" if problem else "ok", trace_id=branch,
          inputs=[events.ref("rev", base)] if base else None, outputs=outputs)


def guard_preflight(branch: str, guard_ref: str, denied: bool) -> None:
    """4 守卫预检：必拒载荷实际被拒为 ok；导出或探针失败为 fail。"""
    _emit(STAGE, "guard_preflight", "ok" if denied else "fail", trace_id=branch,
          inputs=[events.ref("rev", guard_ref)] if guard_ref else None, outputs={"denied": denied})


def tool_counts(host, stream: Path) -> dict:
    """执行方流的工具调用计数（每次 tool_execution_end 计一次完成）：守卫拒绝计 guard_denied，
    其余（成功或普通失败）计 guard_allowed。宿主缺 parse_observability 或其失败时按 0 计（观察旁路）。"""
    try:
        observed = host.parse_observability(stream)
        return {"guard_allowed": int(observed.get("guard_allowed", 0)),
                "guard_denied": int(observed.get("guard_denied", 0))}
    except Exception:  # noqa: BLE001  观察旁路
        return {"guard_allowed": 0, "guard_denied": 0}


def executor_round(branch: str, slot: Path, prompt: str, stream: Path, result,
                   host: dict | None = None, counts: dict | None = None) -> None:
    """5 执行方每轮：宿主与版本、模型、时长、退出原因、token/费用、守卫放行与被拒工具数、
    完整事件流的本机产物哈希。"""
    try:
        if not events.enabled():
            return
        base = git("rev-parse", "HEAD", cwd=slot, check=False) or None
        inputs = [events.ref("prompt", hashlib.sha256(prompt.encode("utf-8")).hexdigest())]
        if base:
            inputs.append(events.ref("rev", base))
        usage = result.usage or {}
        outputs = {"host": (host or {}).get("name"), "host_version": (host or {}).get("version"),
                   "model": result.model or None, "exit": result.exit,
                   "gen_ai.usage.input_tokens": usage.get("input_tokens"),
                   "gen_ai.usage.output_tokens": usage.get("output_tokens"), "cost": usage.get("cost"),
                   "guard_allowed": result.guard_allowed,
                   "guard_denied": (counts or {}).get("guard_denied"), **_stream_refs(stream)}
        _emit(STAGE, "executor_round", "ok" if result.exit == "ok" else "fail", trace_id=branch,
              duration_ms=int(result.seconds * 1000), inputs=inputs, outputs=outputs)
    except Exception:  # noqa: BLE001  观察旁路
        return


def _stream_refs(stream: Path) -> dict:
    """完整执行方流（原始字节）的本机产物引用；读取失败只少记三元字段。"""
    try:
        return _artifact_refs("stream", stream.read_bytes())
    except Exception:  # noqa: BLE001  观察旁路
        return {}


def local_verify(branch: str, slot: Path, base: str, ok: bool, signature: str, log: str,
                 repeat: bool) -> None:
    """6 本地判定每轮：通过与否、失败签名、是否打转（逐项检查结果走 verify 自己的事件）；
    verify 完整输出保存为本机产物，事件只有哈希与大小。"""
    try:
        if not events.enabled():
            return
        head = git("rev-parse", "HEAD", cwd=slot, check=False) or None
        inputs = [events.ref("rev", rev) for rev in (base, head) if rev]
        outputs: dict = {"ok": ok, "signature": signature or None, "repeat": repeat}
        if log:
            outputs.update(_artifact_refs("log", log.encode("utf-8")))
        _emit(STAGE, "local_verify", "ok" if ok else "fail", trace_id=branch, inputs=inputs, outputs=outputs)
    except Exception:  # noqa: BLE001  观察旁路
        return


def clarify(branch: str, note: Path) -> None:
    """执行方请求澄清：备注 sha256 与是否请求澄清；missing_context 条目数由 T202 填充（当前恒 0）。"""
    try:
        if not events.enabled():
            return
        data = note.read_bytes()
    except Exception:  # noqa: BLE001  观察旁路
        return
    _emit(STAGE, "clarify", "ok", trace_id=branch,
          inputs=[{"kind": "note", "ref": hashlib.sha256(data).hexdigest(), "size": len(data)}],
          outputs={"requested": True, "missing_context": 0})


def push_pr(branch: str, slot: Path, pr: int, record_path: str, body: str) -> None:
    """7 推送与开 PR（PR 实际开出后调用）：运行记录 path@提交、PR 正文哈希、提交 SHA 与 PR 号。"""
    try:
        if not events.enabled():
            return
        head = git("rev-parse", "HEAD", cwd=slot, check=False) or None
        inputs = ([{"kind": "run_record", "ref": f"{record_path}@{head}"}] if head and record_path else [])
        data = body.encode("utf-8")
        if body:
            inputs.append({"kind": "pr_body", "ref": hashlib.sha256(data).hexdigest(), "size": len(data)})
        outputs: dict = {"head": head, "pr": pr}
        if body:
            outputs.update(_artifact_refs("body", data))
        _emit(STAGE, "push_pr", "ok", trace_id=branch, inputs=inputs or None, outputs=outputs)
    except Exception:  # noqa: BLE001  观察旁路
        return


def ci_wait(branch: str, pr: int, head: str, round_no: int, ok: bool, summary: str,
            run_ids: list | None = None) -> None:
    """8 等 CI 每轮：轮次、结论、Actions 运行 ID 与失败摘要哈希。"""
    try:
        if not events.enabled():
            return
        outputs: dict = {"pr": pr, "round": round_no, "ok": ok,
                         "run_ids": ",".join(str(value) for value in run_ids or [])}
        if summary:
            outputs.update(_artifact_refs("summary", summary.encode("utf-8")))
        _emit(STAGE, "ci_wait", "ok" if ok else "fail", trace_id=branch,
              inputs=[events.ref("rev", head)] if head else None, outputs=outputs)
    except Exception:  # noqa: BLE001  观察旁路
        return


def escalate(branch: str, pr: int | None, reason: str, target: str, summary: str, sent: bool) -> None:
    """升级：原因、目标（pr 评论或 issue）与标签；状态摘要只留本机产物哈希。评论失败也留事件（fail）。"""
    try:
        if not events.enabled():
            return
        inputs = [events.ref("pr", str(pr))] if pr is not None else []
        data = summary.encode("utf-8")
        if summary:
            inputs.append({"kind": "summary", "ref": hashlib.sha256(data).hexdigest(), "size": len(data)})
        outputs: dict = {"reason": reason, "target": target, "pr": pr, "labels": "escalation",
                         "notified": sent}
        if summary:
            outputs.update(_artifact_refs("summary", data))
        _emit(STAGE, "escalate", "ok" if sent else "fail", trace_id=branch,
              inputs=inputs or None, outputs=outputs)
    except Exception:  # noqa: BLE001  观察旁路
        return


# ---- 独立评审（设计 3.4 与共用合同 C6）----

def severity_counts(findings: list | None) -> dict:
    """C6 severity_counts：阻断/严重/一般/建议/unknown 计数；缺失或未知严重度计入 unknown。"""
    counts: dict = {key: 0 for key in SEVERITIES}
    counts["unknown"] = 0
    for item in findings or []:
        severity = item.get("severity") if isinstance(item, dict) else None
        counts[severity if severity in counts else "unknown"] += 1
    return counts


def _engine_ref(workspace: Path) -> str:
    """生成 pack 的引擎固定提交：业务仓库读 engine.lock 的 commit，否则用工作区 HEAD。"""
    with contextlib.suppress(Exception):  # 观察旁路：锁文件缺失或损坏时回退 HEAD
        lock = json.loads((workspace / ".harness" / "engine.lock").read_text(encoding="utf-8"))
        commit = str(lock.get("commit") or "").strip()
        if commit:
            return commit
    return git("rev-parse", "HEAD", cwd=workspace, check=False) or "unknown"


def _material(kind: str, path: Path, ref: str, recipe: dict, *, encoding: str = "utf8",
              raw_artifact: bool = False) -> dict | None:
    """一份 C6 材料：评审实际读取文件的原始字节哈希；pack 原始字节转存本机产物（不上传）。

    ref 是引用（路径@提交、比较范围、pull 引用或 none 哨兵）；pack 的 ref 是内容寻址产物 sha256。
    读取失败只少记这一份（观察旁路）。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    entry = {"kind": kind, "ref": digest if raw_artifact else ref, "sha256": digest,
             "size": len(data), "encoding": encoding, "recipe": recipe}
    if raw_artifact:
        entry["recipe"]["artifact_sha256"] = digest
        with contextlib.suppress(Exception):  # 转存失败不改变材料哈希
            events.store_artifact(data)
    return entry


def review_materials(workspace: Path, base: str, head: str, taskbook_rel: str | None,
                     pr: int) -> list[dict]:
    """C6 materials：task/diff/ci/pr/pack 五类，recipe 用固定短 ID 与参数，不存命令正文。

    无任务书时 task 材料是现有的「无」哨兵字节，ref 用 none 哨兵；单份材料读取失败只少记这一份。
    """
    folder = workspace / "build" / "review"
    materials = [
        _material("task", folder / "task.md", f"{taskbook_rel}@{head}" if taskbook_rel else "none",
                  {"id": "task_v1"}),
        _material("diff", folder / "diff.patch", f"{base}...{head}", {"id": "diff_v1"}),
        _material("ci", folder / "ci.md", f"pull/{pr}/checks", {"id": "ci_v1"}),
        _material("pr", folder / "pr.md", f"pull/{pr}", {"id": "pr_v1"}),
        _material("pack", folder / "pack.md", "", {"id": "pack_v1", "engine_ref": _engine_ref(workspace),
                                                   "retention_days": 30},
                  encoding="raw_bytes", raw_artifact=True),
    ]
    return [item for item in materials if item]


def review_audit(*, trace_id: str, head: str, base: str, reviewer: str, model, model_basis: str,
                 designer, implementers: list | None, independent, same_host: bool, parsed: bool,
                 verdict: str, duration_ms: int, findings: list | None,
                 materials: list | None) -> dict:
    """C6 harness-review-audit 摘要（单行 JSON，嵌在评论标记里）：不含评论自身哈希与尚未知的 URL。"""
    return {
        "schema_version": 1, "trace_id": trace_id, "head": head, "base": base,
        "reviewer": reviewer, "model": model, "model_basis": model_basis, "designer": designer,
        "implementers": list(implementers or []), "independent": independent, "same_host": same_host,
        "parsed": parsed, "verdict": verdict, "duration_ms": duration_ms,
        "severity_counts": severity_counts(findings), "materials": list(materials or []),
    }


def review(*, trace: str, head: str, reviewer: str, verdict: str, duration_ms: int,
           findings: list | None, materials: list | None, designer=None, model=None,
           model_basis: str = "unknown", parsed: bool = True, independent=None, same_host: bool = False,
           comment: str | None = None, comment_body: str = "", failure: str = "",
           error_kind: str = "") -> None:
    """评审观察事件：材料引用、评审方与模型、时长、结论、按严重度计数、身份是否分离、评论 URL 与
    评论字节哈希（评论发布后）；评审方自身失败记 error.kind（status error），结论不通过记 status fail。"""
    try:
        if not events.enabled():
            return
        inputs = ([events.ref("rev", head)] if head else []) + [
            {"kind": item["kind"], "ref": item["ref"], "sha256": item["sha256"], "size": item["size"]}
            for item in materials or []]
        outputs: dict = {"reviewer": reviewer, "model": model, "verdict": verdict, "parsed": parsed,
                         "designer": designer, "independent": independent, "same_host": same_host,
                         **{_SEVERITY_KEYS[key]: count for key, count in severity_counts(findings).items()}}
        if failure:
            outputs["failure"] = failure
        if comment:
            outputs["comment.url"] = comment
        if comment_body:
            outputs.update(_artifact_refs("comment", comment_body.encode("utf-8")))
        status = "error" if error_kind else ("fail" if verdict == "不通过" else "ok")
        _emit(REVIEW_STAGE, "review", status, trace_id=trace or None, duration_ms=duration_ms,
              inputs=inputs or None, outputs=outputs, error={"kind": error_kind} if error_kind else None)
    except Exception:  # noqa: BLE001  观察旁路
        return
