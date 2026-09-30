"""合并账本与 PR 锚点（B46 T305，设计 4.1，共用合同 C6）：把一个已合并 PR 的全部环节压成一条
长期 JSON 账本，追加到 harness-audit 分支，并在 PR 上留一条可更新的锚点评论。

    python .harness/engine/reports/ledger.py [--pr <号>] [--head <提交>]

--head 缺省取 GITHUB_SHA（本地为 HEAD）；--pr 缺省从合并提交信息解析（同 github_events 的解析）。
只被合并后 main 上的受信任工作流 job 调用（引擎来自默认分支检出，不执行 PR 代码），不注册进 cli.py
（观察旁路，共用合同 C1）。

build_ledger(pr, *, gh=None, cwd=ROOT) 只依赖三类可复取来源，不依赖任何既有本机库（runner 上的临时库
只作导入中转）：git 里合并 head 上的运行记录（C3 摘要，标 evidence_kind=run_record_summary）、经
T302 load_ci 幂等导入的 CI/route 事件包（C5 原始事件）、T304 github_events 同步的合并/抽审/逃逸事实
快照，以及 PR 评论里 T105 写下的 harness-review-audit 单行 JSON（只按标记/schema v1/head/材料字段
解析；缺标记、字段不完整、错误 head、未来版本都如实列 missing，不猜旧评论正文）。运行记录一律按
合并 head 读取（路径@merge_sha），不用最新 head 的记录顶替。不足的环节写 missing，不捏造。

账本写 harness-audit/<合并 UTC 年份>/<PR 号>.json，仅已合并 PR（关闭未合并为 B52，不写）。原始日志、
Pi 流、本机 SQLite 与本机内容产物一律不入账本（引用与哈希除外）。publish_ledger 用 GITHUB_TOKEN 做
普通 fast-forward 追加：同 PR 相同字节幂等跳过；不同字节冲突不覆盖旧文件；推送竞争 fetch 后有限重试，
禁强推、禁删除。harness-audit 的 ruleset 模板只禁删除/强推、保护历史，并不阻止普通修改旧文件——
「只追加」的边界由 writer 幂等/冲突自查保证，本模块与工作流注释都不宣称 ruleset 独自保证只追加。
后登记 escape 由 github_events 以新 API 快照追加事件，不覆写已固定的合并账本。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 直接脚本运行时 sys.path[0] 是 reports 目录，先把包根插进去才能导入 engine 包；
# -m 方式下包根已在 sys.path，此行幂等。
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from engine.core import events_db, events_io  # 导入须在上面的 sys.path 准备之后
from engine.core.common import ROOT, clean_git_env, git
from engine.reports import github_events

LEDGER_SCHEMA_VERSION = 1
AUDIT_BRANCH = "harness-audit"  # 账本专用分支（设计 4.1 方案 A，用户已审定）
ANCHOR_PREFIX = "harness-audit:"  # PR 锚点评论的稳定标记前缀（后接 PR 号）
_REVIEW_RE = re.compile(r"<!--\s*harness-review-audit\s+(\{.*?\})\s*-->")
_LEGACY_REVIEW_MARK = "<!-- independent-review "
_REVIEW_FIELDS = ("schema_version", "trace_id", "head", "base", "reviewer", "model", "model_basis",
                  "designer", "implementers", "independent", "same_host", "parsed", "verdict",
                  "duration_ms", "severity_counts", "materials")
_MATERIAL_FIELDS = ("kind", "ref", "sha256", "size", "encoding", "recipe")
_PUSH_ATTEMPTS = 3  # 推送竞争的有限重试次数；耗尽即报告失败，绝不强推
_FAILED = "\0failed"  # _fetch_base 的失败哨兵（区分「分支不存在」与「远端不可达」）


class LedgerError(RuntimeError):
    """账本无法建立（PR 未合并、合并事实缺失、API 整体失败）；调用方明确报告，不当成功。"""


def _finding(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def _missing(item: str, reason: str, detail: str = "") -> dict:
    return {"item": item, "reason": reason, "detail": detail}


def _missing_items(item: str, findings: list[dict]) -> list[dict]:
    return [{"item": item, "reason": entry["code"], "detail": entry["detail"]} for entry in findings]


def canonical(data) -> str:
    """规范化 JSON：键排序、紧凑、UTF-8（GitHub 事实与账本字节的统一编码，复取按同编码比对）。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GhClient:
    """缺省 gh 客户端：只读 API 与 artifact 下载，外加 PR 锚点评论的 POST/PATCH；失败抛 RuntimeError。

    同一客户端可注入 github_events.sync 与 events_io.load_ci（它们只用到 repo/pr/api/download）。
    """

    def _run(self, argv: list[str], stdin: bytes | None = None) -> bytes:
        result = subprocess.run(["gh", *argv], input=stdin, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or f"gh 退出码 {result.returncode}")
        return result.stdout

    def repo(self) -> str:
        value = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("GH_REPO")
        if value and value.strip():
            return value.strip()
        return self._run(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).decode("utf-8").strip()

    def pr(self, pr: int) -> dict:
        data = json.loads(self._run(["pr", "view", str(pr), "--json", "headRefName,headRefOid,baseRepository"]))
        base = (data.get("baseRepository") or {}).get("name")
        owner = ((data.get("baseRepository") or {}).get("owner") or {}).get("login")
        return {"headRefName": data.get("headRefName"), "headRefOid": data.get("headRefOid"),
                "repository": f"{owner}/{base}" if owner and base else None}

    def api(self, route: str, *, method: str = "GET", payload=None):
        argv = ["api", route]
        if method != "GET":
            argv += ["-X", method]
        if payload is not None:
            argv += ["--input", "-"]
            return json.loads(self._run(argv, stdin=canonical(payload).encode("utf-8")))
        return json.loads(self._run(argv))

    def download(self, url: str) -> bytes:
        return self._run(["api", url])


# ---- build：从可复取来源复原账本（不依赖既有本机库）----


def _event_stage(row: dict) -> dict:
    """C5 事件的账本视图：id 是本机定位号不入账本，其余原样；标 evidence_kind=event。"""
    entry = {key: value for key, value in row.items() if key != "id"}
    entry["evidence_kind"] = "event"
    return entry


def _chain_view(prefix: str, trace: str) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """本机临时库里该来源前缀与 trace 的已导入事件 →（stage 视图、链、锚点、来源清单）。

    链头取该链 seq 最大事件；锚点只保留属于这些链的。本机（local）原始链事件一概不入账本——运行
    记录里的摘要与锚点除外（它们已随记录提交发布，标 evidence_kind=run_record_summary；设计 4.1：
    账本是引用与哈希的摘要，本机原始流不外发）。
    """
    rows = events_io.query(source=prefix, trace_id=trace)
    stages = [_event_stage(row) for row in rows]
    heads: dict[str, tuple[int, str]] = {}
    for row in rows:
        source, seq = str(row["source"]), int(row["seq"])
        if source not in heads or seq > heads[source][0]:
            heads[source] = (seq, str(row["hash"]))
    ordered = sorted(heads)
    chains = [{"source": source, "trace_id": trace, "head_hash": heads[source][1]} for source in ordered]
    sources = [{"source": source, "class": prefix, "head_hash": heads[source][1],
                "events": sum(1 for row in rows if row["source"] == source)} for source in ordered]
    known = set(heads)
    anchors = [anchor for anchor in events_db.read_anchors(trace_id=trace) if anchor["source"] in known]
    anchors.sort(key=lambda anchor: (str(anchor.get("ts")), str(anchor.get("source"))))
    return stages, chains, anchors, sources


def _parse_review_audit(body: str, head_sha: str) -> tuple[dict | None, dict | None]:
    """只按 C6 固定标记与 schema v1 解析评审审计摘要：返回（摘要, None）或（None, missing 条目）。

    未知版本、字段不完整、材料项缺字段、head 与合并 head 不一致都明确报 missing，不猜旧评论正文。
    """
    match = _REVIEW_RE.search(body)
    if match is None:
        return None, _missing("review", "marker_missing", "评论没有 harness-review-audit 审计标记")
    try:
        audit = json.loads(match[1])
    except ValueError:
        return None, _missing("review", "malformed", "审计标记内的 JSON 无法解析")
    if not isinstance(audit, dict):
        return None, _missing("review", "malformed", "审计标记内不是 JSON 对象")
    if audit.get("schema_version") != LEDGER_SCHEMA_VERSION:
        return None, _missing("review", "version_unsupported", f"schema_version={audit.get('schema_version')!r}")
    absent = [name for name in _REVIEW_FIELDS if name not in audit]
    if absent:
        return None, _missing("review", "field_incomplete", "缺少字段：" + ",".join(sorted(absent)))
    materials = audit.get("materials")
    bad = not isinstance(materials, list) or any(
        not isinstance(item, dict) or any(key not in item for key in _MATERIAL_FIELDS) for item in materials)
    if bad:
        return None, _missing("review", "field_incomplete", "materials 项缺固定字段")
    if audit.get("head") != head_sha:
        return None, _missing("review", "head_mismatch", "评审 head 与合并 head 不一致（不用作本 head 的评审）")
    return audit, None


def _review_entries(client, repo: str, pr: int, head_sha: str, missing: list[dict]) -> list[tuple[dict, dict]]:
    """读取 PR 评论里的评审审计摘要：返回（摘要, 评论）列表；缺标记/坏样本逐条列 missing。

    只有旧 independent-review 标记的评论说明生产方过旧，同样报 marker_missing；完全没有评审评论报
    not_found。API 失败经 github_events._collect_pages 记入 findings 后转 missing。
    """
    findings: list[dict] = []
    comments = github_events._collect_pages(client, f"repos/{repo}/issues/{pr}/comments", "PR 评论", findings)
    missing.extend(_missing_items("review_comments", findings))
    entries: list[tuple[dict, dict]] = []
    saw_marker = saw_legacy = False
    for comment in comments or []:
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str):
            continue
        if _REVIEW_RE.search(body) is None:
            saw_legacy = saw_legacy or _LEGACY_REVIEW_MARK in body
            continue
        saw_marker = True
        audit, problem = _parse_review_audit(body, head_sha)
        if audit is None:
            missing.append(problem)
            continue
        entries.append((audit, comment))
    if not entries and not saw_marker and not findings:
        reason = "marker_missing" if saw_legacy else "not_found"
        detail = "评论只有旧 independent-review 标记，无审计摘要" if saw_legacy else "PR 没有带审计标记的评审评论"
        missing.append(_missing("review", reason, detail))
    return entries


def _run_records(cwd: Path, merge_sha: str, trace: str, missing: list[dict]) -> list[tuple[str, bytes, dict]]:
    """合并 head 上的运行记录：git ls-tree 枚举 docs/runs，逐份取原始字节并按 trace 匹配。

    只读合并 head（路径@merge_sha），不用工作区或最新 head 顶替；解析失败或 trace 不符的记录跳过。
    """
    listing = git("ls-tree", "-r", "--name-only", merge_sha, "--", "docs/runs", cwd=cwd,
                  check=False, isolate=True)
    records: list[tuple[str, bytes, dict]] = []
    for path in [line.strip() for line in listing.splitlines() if line.strip().endswith(".json")]:
        done = subprocess.run(["git", "show", f"{merge_sha}:{path}"], cwd=cwd, capture_output=True,
                              env=clean_git_env(), check=False)
        if done.returncode != 0:
            missing.append(_missing("run_record", "unreadable", f"{path}@{merge_sha} 无法读取"))
            continue
        raw = done.stdout
        try:
            record = json.loads(raw)
        except ValueError:
            missing.append(_missing("run_record", "malformed", f"{path}@{merge_sha} 不是 JSON"))
            continue
        if not isinstance(record, dict):
            continue
        if record.get("trace_id") == trace or record.get("branch") == trace:
            records.append((path, raw, record))
    if not records:
        missing.append(_missing("run_record", "not_found", f"合并 head 上没有 trace {trace} 的运行记录"))
    return records


def _merge_facts(pull, pr: int) -> tuple[str, str, str, str]:
    """从 PR 响应取 (trace, head_sha, merge_sha, merged_at)；未合并或字段缺失抛 LedgerError。"""
    if not isinstance(pull, dict) or pull.get("merged") is not True:
        raise LedgerError(f"PR {pr} 未合并（账本只覆盖已合并 PR，关闭未合并为 B52）")
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    trace, head_sha = head.get("ref"), head.get("sha")
    merge_sha, merged_at = pull.get("merge_commit_sha"), pull.get("merged_at")
    shaped = all(isinstance(value, str) and value for value in (trace, head_sha, merge_sha, merged_at))
    if not shaped or len(head_sha) != 40 or len(merge_sha) != 40:
        raise LedgerError(f"PR {pr} 的合并事实缺失（headRefName/head/merge_commit_sha/merged_at）")
    return trace, head_sha, merge_sha, merged_at


def _facts_fields(stages: list[dict], ledger: dict, missing: list[dict]) -> None:
    """class/risk 取最新 route.facts 事件的机器判定，approval 取合并事实快照；取不到如实记 missing。"""
    facts = [stage for stage in stages if stage.get("stage") == "route" and stage.get("step") == "facts"]
    if facts:
        latest = max(facts, key=lambda stage: str(stage.get("ts")))
        outputs = latest.get("outputs") or {}
        ledger["class"], ledger["risk"] = outputs.get("machine_class"), outputs.get("risk")
    else:
        missing.append(_missing("route", "not_found", "没有已导入的 route.facts 事件，class/risk 未知"))
    merges = [stage for stage in stages if stage.get("stage") == "merge" and stage.get("step") == "github.merge"]
    if merges:
        outputs = max(merges, key=lambda stage: str(stage.get("ts"))).get("outputs") or {}
        ledger["approval"] = {key: outputs.get(key) for key in
                              ("approver", "approver_type", "approval_commit", "approval_bound")}
    else:
        missing.append(_missing("approval", "not_found", "没有合并事实快照，批准者未知"))


def build_ledger(pr: int, *, gh=None, cwd: Path = ROOT) -> dict:
    """复原一个已合并 PR 的合并账本（C6）；PR 未合并或合并事实缺失抛 LedgerError。

    返回的账本含 schema_version/repository/pr/trace_id/head_sha/merged_at/merge_sha/class/risk/
    approval/stages/chains/anchors/references/sources/missing；凡缺失环节都列 missing，不捏造。
    """
    client = GhClient() if gh is None else gh
    missing: list[dict] = []
    try:
        repo = events_io._repo_identity(str(client.repo()))
        pull = client.api(f"repos/{repo}/pulls/{pr}")
    except (RuntimeError, ValueError, AttributeError) as exc:
        raise LedgerError(f"读取 PR {pr} 失败：{exc}") from exc
    trace, head_sha, merge_sha, merged_at = _merge_facts(pull, pr)
    ledger: dict = {"schema_version": LEDGER_SCHEMA_VERSION, "repository": repo, "pr": pr,
                    "trace_id": trace, "head_sha": head_sha, "merged_at": merged_at,
                    "merge_sha": merge_sha, "class": None, "risk": None, "approval": None,
                    "stages": [], "chains": [], "anchors": [], "references": [], "sources": [],
                    "missing": missing}
    facts = github_events.sync(pr=pr, gh=client)  # T304 产品入口：合并/抽审/逃逸事实（不可变快照）
    missing.extend(_missing_items("github_facts", facts["findings"]))
    packages = events_io.load_ci(pr, head=head_sha, gh=client)  # T302 产品入口：CI/route 事件包
    missing.extend(_missing_items("ci_events", packages["findings"]))

    stages: list[dict] = []
    for prefix in ("ci", "github"):
        part, chains, anchors, sources = _chain_view(prefix, trace)
        stages += part
        ledger["chains"] += chains
        ledger["anchors"] += anchors
        ledger["sources"] += sources
    for path, raw, record in _run_records(cwd, merge_sha, trace, missing):
        ledger["references"].append({"kind": "run_record", "ref": f"{path}@{merge_sha}",
                                     "sha256": _digest(raw), "size": len(raw), "encoding": "raw_bytes"})
        stages += [dict(item, evidence_kind="run_record_summary")
                   for item in record.get("stages") or [] if isinstance(item, dict)]
        ledger["anchors"] += [dict(item, trace_id=trace) for item in record.get("anchors") or []
                              if isinstance(item, dict)]
    for audit, comment in _review_entries(client, repo, pr, head_sha, missing):
        body = str(comment.get("body")).encode("utf-8")
        stages.append({"stage": "review", "evidence_kind": "review_comment_summary",
                       "ts": comment.get("created_at"), "trace_id": audit.get("trace_id"),
                       "head": audit.get("head"), "base": audit.get("base"),
                       "reviewer": audit.get("reviewer"), "model": audit.get("model"),
                       "model_basis": audit.get("model_basis"), "designer": audit.get("designer"),
                       "implementers": audit.get("implementers"), "independent": audit.get("independent"),
                       "same_host": audit.get("same_host"), "parsed": audit.get("parsed"),
                       "verdict": audit.get("verdict"), "duration_ms": audit.get("duration_ms"),
                       "severity_counts": audit.get("severity_counts"), "materials": audit.get("materials"),
                       "comment": {"id": comment.get("id"), "url": comment.get("html_url")}})
        ledger["references"].append({"kind": "review_comment", "ref": f"{repo}#{pr}/comments/{comment.get('id')}",
                                     "sha256": _digest(body), "size": len(body), "encoding": "raw_bytes"})
    _facts_fields(stages, ledger, missing)
    ledger["stages"] = sorted(stages, key=lambda stage: (str(stage.get("ts")), str(stage.get("stage")),
                                                          str(stage.get("step"))))
    for key in ("chains", "references", "sources"):
        ledger[key].sort(key=lambda item: canonical(item))
    ledger["anchors"].sort(key=lambda anchor: (str(anchor.get("ts")), str(anchor.get("source"))))
    return ledger


# ---- publish：fast-forward 追加 harness-audit 分支与 PR 锚点评论 ----


def _ledger_problem(ledger) -> dict | None:
    """账本最小合法性：schema、PR 号、合并时间（定路径年份）与 trace；不合法不给写。"""
    if not isinstance(ledger, dict):
        return _finding("ledger_invalid", "账本不是 JSON 对象")
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION:
        return _finding("ledger_invalid", f"schema_version={ledger.get('schema_version')!r} 不是 1")
    pr = ledger.get("pr")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        return _finding("ledger_invalid", "pr 缺失或非法")
    merged_at = ledger.get("merged_at")
    if not isinstance(merged_at, str) or len(merged_at) < 4 or not merged_at[:4].isdigit():
        return _finding("ledger_invalid", "merged_at 缺失或无 UTC 年份，无法定账本路径")
    if not isinstance(ledger.get("trace_id"), str) or not ledger["trace_id"]:
        return _finding("ledger_invalid", "trace_id 缺失")
    return None


def _git(work: Path, *args: str, check: bool = True, stdin: bytes | None = None) -> str:
    """临时克隆里的 git 调用：stdout 去尾换行；check=False 时失败返回空串。"""
    done = subprocess.run(["git", "-C", str(work), *args], input=stdin, capture_output=True,
                          env=clean_git_env(), check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("utf-8", "replace").rstrip("\n")


def _fetch_base(work: Path, findings: list[dict]) -> str | None:
    """远端 harness-audit 分支当前头：分支不存在为 None；远端不可达返回 _FAILED 并记发现。"""
    try:
        listed = _git(work, "ls-remote", "origin", f"refs/heads/{AUDIT_BRANCH}")
    except RuntimeError as exc:
        findings.append(_finding("remote", f"查询 {AUDIT_BRANCH} 分支失败：{exc}"))
        return _FAILED
    if not listed.strip():
        return None
    try:
        _git(work, "fetch", "--quiet", "origin", AUDIT_BRANCH)
        return _git(work, "rev-parse", "FETCH_HEAD")
    except RuntimeError as exc:
        findings.append(_finding("remote", f"取回 {AUDIT_BRANCH} 分支失败：{exc}"))
        return _FAILED


def _file_bytes(work: Path, base: str, path: str, findings: list[dict]) -> bytes | None:
    """base 上的账本文件字节：不存在为 None；读取失败抛 RuntimeError 由调用方记发现。"""
    if not _git(work, "ls-tree", base, "--", path).strip():
        return None
    done = subprocess.run(["git", "-C", str(work), "cat-file", "blob", f"{base}:{path}"],
                          capture_output=True, env=clean_git_env(), check=False)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip() or "cat-file 失败")
    return done.stdout


def _commit_file(work: Path, base: str | None, path: str, data: bytes, message: str) -> str:
    """把账本文件提交到 base 之上（base 为 None 时是首个根提交），返回新提交 sha。"""
    blob = _git(work, "hash-object", "-w", "--stdin", stdin=data)
    _git(work, "read-tree", "--empty") if base is None else _git(work, "read-tree", f"{base}^{{tree}}")
    _git(work, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    tree = _git(work, "write-tree")
    args = ["commit-tree", tree, "-m", message] + ([] if base is None else ["-p", base])
    return _git(work, *args)


def _push(work: Path, commit: str) -> tuple[bool, str]:
    """普通 fast-forward 推送（禁 --force/--delete）：成功与远端拒绝都如实返回。"""
    done = subprocess.run(["git", "-C", str(work), "push", "origin", f"{commit}:refs/heads/{AUDIT_BRANCH}"],
                          capture_output=True, env=clean_git_env(), check=False)
    return done.returncode == 0, done.stderr.decode("utf-8", "replace").strip()


def _attempt_append(work: Path, pr: int, path: str, data: bytes, result: dict) -> str:
    """一轮追加：fetch 远端头 → 同字节幂等 / 异字节冲突不覆盖 / 缺文件则提交并推送。"""
    findings: list[dict] = result["findings"]
    base = _fetch_base(work, findings)
    if base == _FAILED:
        return "failed"
    try:
        existing = _file_bytes(work, base, path, findings) if base else None
    except RuntimeError as exc:
        findings.append(_finding("remote", f"读取既有账本失败：{exc}"))
        return "failed"
    if existing == data:
        result["commit"] = base  # 同 PR 相同字节：幂等命中，不新增提交
        return "identical"
    if existing is not None:
        result["conflict"] = True
        findings.append(_finding("ledger_conflict",
                                 f"{path} 已有不同字节：不覆盖旧账本（ruleset 只保护历史，覆盖检测靠 writer 自查）"))
        return "conflict"
    commit = _commit_file(work, base, path, data, f"audit：PR #{pr} 合并账本（{path}）")
    ok, err = _push(work, commit)
    if ok:
        result["commit"], result["updated"] = commit, True
        return "written"
    findings.append(_finding("push_rejected", f"推送被拒（可能有并发追加，fetch 后重试）：{err}"))
    return "retry"


def _anchor_body(ledger: dict, result: dict) -> str:
    """PR 锚点评论：稳定标记 + 分支/路径/提交/文件 sha256/各链链头；内容全部是引用与哈希。"""
    chains = "；".join(f"{chain['source']} @ {chain['head_hash']}" for chain in ledger.get("chains") or [])
    return "\n".join([
        f"<!-- {ANCHOR_PREFIX}{ledger['pr']} -->",
        "合并账本已固定（合并账本与链头锚点）：",
        f"- 分支：`{result['branch']}`",
        f"- 文件：`{result['path']}` @ {result['commit']}",
        f"- 文件 sha256：`{result['sha256']}`",
        f"- 链头：{chains or '（无事件链）'}",
    ]) + "\n"


def _upsert_anchor_comment(client, ledger: dict, result: dict, findings: list[dict]) -> str | None:
    """同标记评论更新而非新增：找到 harness-audit:<PR> 标记的既有评论就 PATCH，否则 POST。"""
    marker = f"<!-- {ANCHOR_PREFIX}{ledger['pr']} -->"
    body = _anchor_body(ledger, result)
    route = f"repos/{ledger['repository']}/issues/{ledger['pr']}/comments"
    pages = github_events._collect_pages(client, route, "PR 评论", findings)
    existing = next((item for item in pages or [] if isinstance(item, dict) and marker in str(item.get("body"))), None)
    try:
        if existing is not None:
            client.api(f"repos/{ledger['repository']}/issues/comments/{existing.get('id')}",
                       method="PATCH", payload={"body": body})
            return "updated"
        client.api(route, method="POST", payload={"body": body})
        return "created"
    except (RuntimeError, ValueError, AttributeError) as exc:
        findings.append(_finding("comment", f"锚点评论写入失败：{exc}"))
        return None


def publish_ledger(ledger: dict, *, gh=None) -> dict:
    """把账本 fast-forward 追加到 harness-audit 分支并维护 PR 锚点评论（C6）。

    返回 {ok, branch, path, commit, sha256, updated, conflict, comment, findings}；账本写入成功即
    ok（评论失败只记 findings）；同字节幂等 updated=False；冲突与远端故障 ok=False 且不动远端。
    """
    findings: list[dict] = []
    result = {"ok": False, "branch": AUDIT_BRANCH, "path": None, "commit": None, "sha256": None,
              "updated": False, "conflict": False, "comment": None, "findings": findings}
    problem = _ledger_problem(ledger)
    if problem is not None:
        findings.append(problem)
        return result
    data = canonical(ledger).encode("utf-8") + b"\n"
    result["sha256"] = _digest(data)
    result["path"] = f"{ledger['merged_at'][:4]}/{ledger['pr']}.json"
    origin = git("remote", "get-url", "origin", cwd=events_db.ROOT, check=False, isolate=True)
    if not origin:
        findings.append(_finding("no_origin", "仓库没有 origin 远程，无法推送账本"))
        return result
    work = Path(tempfile.mkdtemp(prefix="harness-audit-"))
    try:
        cloned = subprocess.run(["git", "clone", "--quiet", "--no-checkout", origin, str(work)],
                                capture_output=True, env=clean_git_env(), check=False)
        if cloned.returncode != 0:
            findings.append(_finding("clone", "克隆远程失败：" + cloned.stderr.decode("utf-8", "replace").strip()))
            return result
        status = "retry"
        for _ in range(_PUSH_ATTEMPTS):
            status = _attempt_append(work, ledger["pr"], result["path"], data, result)
            if status != "retry":
                break
        if status == "retry":
            findings.append(_finding("push_exhausted", f"推送竞争 {_PUSH_ATTEMPTS} 次未成功，放弃（不强推）"))
        if result["commit"] is not None:
            result["ok"] = True
            result["comment"] = _upsert_anchor_comment(GhClient() if gh is None else gh, ledger, result, findings)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def main(argv: list[str] | None = None) -> int:
    """工作流入口：解析 PR → build_ledger → publish_ledger；发现与缺失都明确报告。"""
    parser = argparse.ArgumentParser(prog="ledger", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pr", type=int, help="写指定 PR 的账本（缺省从合并提交信息解析）")
    parser.add_argument("--head", metavar="提交", help="用该提交解析关联 PR（缺省 GITHUB_SHA 或本地 HEAD）")
    args = parser.parse_args(argv)
    client = GhClient()
    findings: list[dict] = []
    head = args.head or os.environ.get("GITHUB_SHA") \
        or git("rev-parse", "HEAD", cwd=events_db.ROOT, check=False, isolate=True) or ""
    pr = args.pr
    if pr is None:
        try:
            repo = events_io._repo_identity(str(client.repo()))
        except (RuntimeError, ValueError, AttributeError) as exc:
            print(f"ledger：确定仓库失败：{exc}", file=sys.stderr)
            return 1
        pr = github_events._resolve_pr(client, repo, head, findings)
        if pr is None:
            for finding in findings:
                print(f"ledger：{finding['detail']}", file=sys.stderr)
            return 1
    try:
        ledger = build_ledger(pr, gh=client)
    except LedgerError as exc:
        print(f"ledger：{exc}", file=sys.stderr)
        return 1
    for item in ledger["missing"]:
        print(f"ledger：缺 {item['item']}（{item['reason']}）{item['detail']}")
    result = publish_ledger(ledger, gh=client)
    for finding in result["findings"]:
        print(f"ledger：{finding['detail']}", file=sys.stderr)
    if result["ok"]:
        print(f"ledger：账本 {result['branch']}/{result['path']}（commit {result['commit'][:12]}…，"
              f"sha256 {result['sha256'][:12]}…，{'更新' if result['updated'] else '幂等命中'}，"
              f"锚点评论 {result['comment'] or '未写入'}）")
    return 0 if result["ok"] and not result["findings"] else 1


if __name__ == "__main__":
    sys.exit(main())
