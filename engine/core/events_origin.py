"""导出事件包的 origin（运行环境快照）：仓库、head 与真实 Actions 键。

从 events_io 搬出（T718）：events_io.py 逼近 800 行质量棘轮。events_io 以同名导入保持 `_origin`、
`_SHA_RE`、`_ORIGIN_LIMIT` 可用。本模块不得导入 events_io（避免循环）。
"""

from __future__ import annotations

import json
import os
import re

from engine.core import events, events_db
from engine.core.common import git

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
ORIGIN_LIMIT = 300


def _pull_request_head(head_sha: str) -> str | None:
    """pull_request 运行检出的是 GitHub 合成的合并提交，PR 的真实 head 只在事件载荷里：仅在 CI 证据齐全
    （CI=true、事件类型 pull_request、载荷可读且含 40 位十六进制的 pull_request.head.sha）且 GITHUB_SHA 等于
    检出的 HEAD 时才采用；其余情形（其他事件、非 CI、载荷不可读或形状不符、以及测试夹具的临时仓库 HEAD
    与外层 GITHUB_SHA 不等）一律返回 None，调用方保持现状。只取 SHA，载荷的路径与内容不进事件包。"""
    ambient_sha = os.environ.get("GITHUB_SHA")
    if not (os.environ.get("CI") == "true" and os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
            and ambient_sha and SHA_RE.fullmatch(ambient_sha) and ambient_sha == head_sha):
        return None
    try:
        with open(os.environ.get("GITHUB_EVENT_PATH") or "", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    node = payload.get("pull_request") if isinstance(payload, dict) else None
    node = node.get("head") if isinstance(node, dict) else None
    candidate = node.get("sha") if isinstance(node, dict) else None
    return candidate if isinstance(candidate, str) and SHA_RE.fullmatch(candidate) else None


def origin() -> dict:
    """导出方环境快照：仓库、head 与真实 Actions 三键；local 导出不伪造 Actions 键。"""
    origin: dict[str, str] = {}
    remote = git("remote", "get-url", "origin", cwd=events_db.ROOT, check=False, isolate=True)
    if remote and events._clean_str(remote, ORIGIN_LIMIT) is not None:
        origin["repository"] = remote  # 本机路径形式的 remote 不外发（也是导入隐私口径）
    head_sha = git("rev-parse", "HEAD", cwd=events_db.ROOT, check=False, isolate=True)
    payload_head = _pull_request_head(head_sha)
    if payload_head:
        origin["head_sha"] = payload_head
    elif head_sha and SHA_RE.fullmatch(head_sha):
        origin["head_sha"] = head_sha
    if os.environ.get("CI") == "true":
        branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or ""
        run_id = os.environ.get("GITHUB_RUN_ID")
        run_attempt, job = os.environ.get("GITHUB_RUN_ATTEMPT"), os.environ.get("GITHUB_JOB")
        if run_id and run_attempt and job:
            origin["run_id"], origin["run_attempt"], origin["job"] = run_id, run_attempt, job
        workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF")
        if workflow_ref:
            origin["workflow_ref"] = workflow_ref
    else:
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=events_db.ROOT, check=False, isolate=True)
    if branch:
        origin["head_branch"] = branch
    return origin
