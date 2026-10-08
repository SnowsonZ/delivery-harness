"""审计引用复原与哈希核对（B46 T401，设计 4.2，共用合同 C7）。

    python3 cli.py audit <PR号> [--json]
    python3 cli.py audit --all-merged [--since 30d] [--json]

复原一个（或 --all-merged 批量分页确定性时间窗口内的全部）已合并 PR 的安全环节视图：按 API 的
trace/head、C6 合并账本与本机/CI 已导入事件列出各阶段的输入、输出与决定，来源分别标注
local/ci/github。下载 CI 由 C5/T302 共享的 load_ci 完成，GitHub 事实更新由 T304 共享的 sync 完成，
本命令不写合并路由；cli.py QUIET_COMMANDS 不记 cli.audit 入口事件，每次运行结束由本模块写一条
ci/audit.summary 与每条 finding 一条 ci/audit.finding 观察事件（T404，C7 供周报，失败隔离见
_emit_observations）。

逐引用核对四类：git 文件按 path@rev 用 git show 取原始字节（结尾换行原样参与哈希，不去尾、不再
规范化）；GitHub 评论按不可变正文原字节、可变 PR 字段按评审材料同一渲染规则复取（变动报
snapshot_changed，不与固定绑定提交的事实混比）；本机产物按 sha256/size 并区分保留期到期；CI 事件
包的规范化哈希核对由 load_ci 导入完成，发现按来源映射进报告。只访问当前仓库受支持的 GitHub 资源
（评论、PR 字段、Actions 包），引用内容只解析与比对、绝不执行；path 不能逃出仓库。失败区分
hash_mismatch / reference_unavailable / reference_expired，未核对在 coverage 里如实计数，不冒充通过。
完整性规则、锚点防篡改与 [audit] 配置由 audit_completeness 在复原核对之后判定（B46 T402）。

inspect_pr(pr, *, gh=None, cwd=ROOT) 返回 C7 基础形状：pr/trace_id/head_sha/stages/references/
findings/coverage/ok；finding 有 rule/severity/source/stage/ref/reason，reason 不回显私密内容。
退出码：0 全部适用引用核对通过；1 有发现（哈希不符、引用不可得、到期或完整性/防篡改发现）；
2 参数错误、整体 API 故障、[audit] 配置错误或 PR 无法审计。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engine.core import events, events_db, events_io
from engine.core.common import ROOT, clean_git_env
from engine.reports import audit_completeness, github_events, ledger
from engine.routing import signals

# 批量模式 --since 的相对时长单位（折算为秒）。
_DURATION_RE = re.compile(r"(\d+)([dhms])\Z")
_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

# C7 稳定 rule：T401 产生引用核对四类；完整性/锚点/账本一致性等其余稳定 rule 由 T402 扩展。
_RULE_FOR_STATUS = {
    "hash_mismatch": "hash_mismatch",
    "snapshot_changed": "snapshot_changed",
    "unavailable": "reference_unavailable",
    "expired": "reference_expired",
}
_STATUSES = ("verified", "hash_mismatch", "snapshot_changed", "unavailable", "expired", "unchecked")
_SOURCE_CLASSES = ("local", "ci", "github")
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_GIT_REF_RE = re.compile(r"(?P<path>.+)@(?P<rev>[0-9a-f]{7,40})\Z")
# 只接受 owner/repo#PR/comments/ID 形状的评论引用；其余（任意 URL、其他仓库）一律拒绝，不发起请求。
_COMMENT_REF_RE = re.compile(r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+)"
                             r"#(?P<pr>\d+)/comments/(?P<id>\d+)\Z")
# PR 锚点评论（T305 ledger._anchor_body 的固定行）里记录的账本路径、提交与文件 sha256。
_ANCHOR_FILE_RE = re.compile(r"- 文件：`(?P<path>[^`]+)` @ (?P<commit>[0-9a-f]{40})")
_ANCHOR_SHA_RE = re.compile(r"- 文件 sha256：`(?P<sha256>[0-9a-f]{64})`")


class AuditError(RuntimeError):
    """PR 无法审计（整体 API 故障、PR 不存在或未合并）；CLI 转退出码 2。"""


def _parse_window(text: str) -> datetime:
    """批量模式 --since 的窗口起点：<n><d|h|m|s> 相对时长或 ISO 时间戳，坏值抛 ValueError。

    相对时长按审计自身的时间基准（events_db._now）折算而不取墙钟：批量范围与事件/锚点同一时钟，
    因此是确定性时间范围（同一时钟下两次运行覆盖同一集合）。
    """
    if match := _DURATION_RE.fullmatch(text.strip()):
        now = _parse_utc(events_db._now())
        if now is None:
            raise ValueError("--since 无法确定审计时间基准")
        return now - timedelta(seconds=int(match[1]) * _UNITS[match[2]])
    parsed = _parse_utc(text)
    if parsed is None:
        raise ValueError(f"--since 无法解析为时长或 UTC 时间：{text}")
    return parsed


# 缺省 gh 客户端：repo/pr/api/download 全都具备（load_ci 与 sync 只用这些）；测试可注入替换。
GhClient = ledger.GhClient


def _finding(rule: str, *, source: str, stage: str, ref: str, reason: str,
             severity: str = "error") -> dict:
    """C7 finding：rule/severity/source/stage/ref/reason；reason 只写分类，不回显引用内容。"""
    return {"rule": rule, "severity": severity, "source": source, "stage": stage,
            "ref": ref, "reason": reason}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_utc(value) -> datetime | None:
    """UTC ISO 时间戳 → aware UTC；缺失或损坏返回 None（调用方按无法判定处理）。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _source_class(source: str) -> str:
    """来源显示分类（C2/C7）：github:/ci 前缀各成一类，其余（含 local）归本机。"""
    if source.startswith("github:"):
        return "github"
    if source.startswith("ci"):
        return "ci"
    return "local"


def _safe_repo_path(path: str) -> bool:
    """仓库相对路径合法性：拒绝绝对路径、盘符、反斜杠、~ 与 . / .. 段（path 不能逃出仓库）。"""
    if not path or path.startswith(("/", "~")) or ":" in path or "\\" in path or "\0" in path:
        return False
    return not any(part in ("", ".", "..") for part in path.split("/"))


def _git_run(cwd: Path, *args: str) -> tuple[bool, str]:
    """行输出 git 调用（ls-remote/fetch/ls-tree）：返回（成功, stdout 去尾换行）。"""
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=clean_git_env(), check=False)
    return done.returncode == 0, done.stdout.decode("utf-8", "replace").rstrip("\n")


def _git_bytes(cwd: Path, rev: str, path: str) -> bytes | None:
    """git show <rev>:<path> 的原始字节（结尾换行原样保留，不做任何规范化）；失败返回 None。"""
    done = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=cwd, capture_output=True,
                          env=clean_git_env(), check=False)
    return done.stdout if done.returncode == 0 else None


def _artifact_created(digest: str) -> str | None:
    """本机产物索引里的 created（只读查询；行不存在或读取失败返回 None）。"""
    path = events_db.db_path()
    if path is None or not path.exists():
        return None
    try:
        with closing(sqlite3.connect(path, timeout=events_db.BUSY_TIMEOUT_MS / 1000)) as conn:
            row = conn.execute("SELECT created FROM artifacts WHERE sha256=?", (digest,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


class _Auditor:
    """一个已合并 PR 的复原与逐引用核对：收集 → 合并账本期望 → 逐条核对 → 汇总报告。"""

    def __init__(self, *, client, repo: str, pr: int, trace: str, head: str, merge_sha: str,
                 merged_at: str, cwd: Path):
        self.client, self.repo, self.pr = client, repo, pr
        self.trace, self.head, self.merge_sha, self.merged_at = trace, head, merge_sha, merged_at
        self.cwd = Path(cwd)
        now = _parse_utc(events_db._now()) or datetime.now(UTC)
        # 窗口与到期判定的时间基准一次取定（测试冻结时钟）；到期阈值沿用实际生效的产物保留期。
        self.cutoff = now - timedelta(days=events.artifact_days())
        self.findings: list[dict] = []
        self.refs: list[dict] = []
        self.stages: list[dict] = []
        self.anchors: list[dict] = []
        self.chains: list[dict] = []
        self.event_rows: list[dict] = []
        self.ledger_doc = None  # 解析后的账本（T402 账本一致性核对用；None=无账本/不可解析）
        self.db_error = None  # 事件库不可读标记（T402：坏库如实报告，不当作无事件）
        self.ledger_state = "absent"
        self._expect: dict[tuple, dict] = {}  # (kind, ref) → 账本记录的期望 sha256/size

    # ---- 收集 ----

    def _add(self, *, check: str, kind: str, ref, sha256, size, source: str, stage: str,
             expectation: str, event_ts: str | None = None, artifact: str | None = None,
             note: str | None = None, ledger_bytes: bytes | None = None) -> dict:
        entry = {"check": check, "kind": kind, "ref": ref, "sha256": sha256, "size": size,
                 "source": source, "stage": stage, "expectation": expectation,
                 "event_ts": event_ts, "artifact": artifact, "note": note,
                 "ledger_bytes": ledger_bytes, "status": "unchecked", "reason": note,
                 "observed_sha256": None, "observed_size": None}
        self.refs.append(entry)
        return entry

    def _collect_events(self):
        """本机/CI/GitHub 已导入事件 → 阶段视图、链头与锚点；local 事件的引用逐条登记核对。"""
        try:
            rows = events_io.query(trace_id=self.trace)
            anchors = events_db.read_anchors(trace_id=self.trace)
        except Exception:  # noqa: BLE001  坏库：如实交完整性层报告（T402），不崩溃、不当作无事件
            self.db_error = True
            rows, anchors = [], []
        self.event_rows = sorted(rows,
                                 key=lambda row: (row["ts"], row["source"], row["seq"]))
        for row in self.event_rows:
            self.stages.append({"evidence": "event", "source": row["source"],
                                "source_class": _source_class(row["source"]), "stage": row["stage"],
                                "step": row["step"], "status": row["status"], "ts": row["ts"],
                                "duration_ms": row["duration_ms"], "seq": row["seq"], "hash": row["hash"],
                                "inputs": row["inputs"], "outputs": row["outputs"],
                                "decision": row["decision"], "error": row["error"], "actor": row["actor"]})
        heads: dict[str, tuple[int, str]] = {}
        for row in self.event_rows:
            if row["source"] not in heads or row["seq"] > heads[row["source"]][0]:
                heads[row["source"]] = (row["seq"], row["hash"])
        self.chains = [{"source": source, "trace_id": self.trace, "head_hash": heads[source][1]}
                       for source in sorted(heads)]
        self.anchors.extend(anchors)
        for row in self.event_rows:
            if _source_class(row["source"]) != "local":
                continue  # CI 事件的产物在 CI 侧（C5 导入不恢复内容），到期由 load_ci 发现报告
            self._collect_input_refs(row)
            self._collect_output_refs(row)

    def _collect_input_refs(self, row: dict):
        """local 事件输入引用：artifact 引用与 path@rev 形状的 git 文件引用（须带记录的 sha256）。"""
        for item in row["inputs"]:
            if not isinstance(item, dict) or not isinstance(item.get("sha256"), str):
                continue
            if item.get("kind") == "artifact":
                ref_value = item.get("ref")
                self._add(check="artifact", kind="artifact", ref=ref_value or item["sha256"],
                          sha256=item["sha256"], size=item.get("size"), source="local",
                          stage=row["stage"], expectation="event", event_ts=row["ts"],
                          artifact=ref_value if isinstance(ref_value, str)
                          and _HASH_RE.fullmatch(ref_value) is not None else item["sha256"])
            elif _GIT_REF_RE.fullmatch(str(item.get("ref") or "")) is not None:
                self._add(check="git", kind=str(item.get("kind") or "git"), ref=item["ref"],
                          sha256=item["sha256"], size=item.get("size"), source="local",
                          stage=row["stage"], expectation="event")

    def _collect_output_refs(self, row: dict):
        """输出引用从扁平 outputs 三元字段（<prefix>.sha256/.size/.ref）派生（C1），核对本机产物。"""
        outputs = row["outputs"] if isinstance(row["outputs"], dict) else {}
        prefixes = sorted({key.rsplit(".", 1)[0] for key in outputs if key.endswith(".sha256")})
        for prefix in prefixes:
            recorded = outputs.get(f"{prefix}.sha256")
            if not isinstance(recorded, str) or _HASH_RE.fullmatch(recorded) is None:
                continue
            ref_value = outputs.get(f"{prefix}.ref")
            size_value = outputs.get(f"{prefix}.size")
            artifact = ref_value if isinstance(ref_value, str) and _HASH_RE.fullmatch(ref_value) else recorded
            self._add(check="artifact", kind="artifact",
                      ref=ref_value if isinstance(ref_value, str) else artifact, sha256=recorded,
                      size=size_value if isinstance(size_value, int) and not isinstance(size_value, bool)
                      else None,
                      source="local", stage=row["stage"], expectation="event", event_ts=row["ts"],
                      artifact=artifact)

    def _collect_comments(self):
        """PR 评论：评审审计摘要（C6 标记）→ 评审阶段视图与材料核对；锚点评论 → 账本期望。"""
        problems: list[dict] = []
        comments = github_events._collect_pages(
            self.client, f"repos/{self.repo}/issues/{self.pr}/comments", "PR 评论", problems)
        if comments is None:
            self.findings.append(_finding(
                "reference_unavailable", source="github", stage="review",
                ref=f"{self.repo}#{self.pr}/comments",
                reason=f"PR 评论列表无法读取（{problems[0]['code'] if problems else 'api'}）"))
            return None
        anchor = None
        marker = f"<!-- {ledger.ANCHOR_PREFIX}{self.pr} -->"
        for comment in comments:
            body = comment.get("body") if isinstance(comment, dict) else None
            if not isinstance(body, str):
                continue
            if marker in body:
                file_match, sha_match = _ANCHOR_FILE_RE.search(body), _ANCHOR_SHA_RE.search(body)
                if file_match and sha_match and anchor is None:
                    anchor = {"path": file_match["path"], "commit": file_match["commit"],
                              "sha256": sha_match["sha256"], "comment_id": comment.get("id")}
                continue
            # 内容相同（T705，与账本同一判定与同一 base：合并提交的第一个父提交）：同步 main 后的
            # 评审证据仍收录；same_content 出错返回假，退回严格比对。
            parsed, _problem = ledger._parse_review_audit(
                body, self.head,
                same=lambda reviewed: signals.same_content(reviewed, self.head, f"{self.merge_sha}^1",
                                                           self.cwd))
            if parsed is None:
                continue  # 解析失败或非本 head 的评审摘要不作为证据（缺评审的完整性判断属 T402）
            self.stages.append({
                "evidence": "review_comment_summary", "source": "github", "source_class": "github",
                "stage": "review", "step": "review", "ts": comment.get("created_at"), "trace_id": parsed.get("trace_id"),
                "head": parsed.get("head"), "reviewer": parsed.get("reviewer"),
                "model": parsed.get("model"), "designer": parsed.get("designer"),
                "implementers": parsed.get("implementers"), "independent": parsed.get("independent"),
                "verdict": parsed.get("verdict"), "duration_ms": parsed.get("duration_ms"),
                "severity_counts": parsed.get("severity_counts"),
                "materials": parsed.get("materials"),
                "comment": {"id": comment.get("id"), "url": comment.get("html_url")}})
            self._add(check="comment", kind="review_comment",
                      ref=f"{self.repo}#{self.pr}/comments/{comment.get('id')}",
                      sha256=None, size=len(body.encode("utf-8")), source="github", stage="review",
                      expectation=None)
            self._collect_materials(parsed)
        return anchor

    def _collect_materials(self, parsed: dict):
        """评审审计材料：task@head 与 pack 产物按原字节核对；pr 材料按同一渲染规则复取比对；
        diff/ci 是评审时按 recipe 渲染的字节（C6：不按将来复取对象重算），登记为不核对。"""
        for material in parsed.get("materials") or []:
            if not isinstance(material, dict):
                continue
            kind, ref = material.get("kind"), material.get("ref")
            sha256, size = material.get("sha256"), material.get("size")
            recipe = material.get("recipe") if isinstance(material.get("recipe"), dict) else {}
            if kind == "task" and ref == "none":
                continue  # 无任务书的「无」哨兵：没有内容可核对
            if kind == "task":
                self._add(check="git", kind="material_task", ref=ref, sha256=sha256, size=size,
                          source="local", stage="review", expectation="review_material")
            elif kind == "pack" and isinstance(sha256, str):
                digest = recipe.get("artifact_sha256") if isinstance(recipe.get("artifact_sha256"), str) else sha256
                self._add(check="artifact", kind="material_pack", ref=ref if isinstance(ref, str) else digest,
                          sha256=sha256, size=size, source="local", stage="review",
                          expectation="review_material", artifact=digest)
            elif kind == "pr" and ref == f"pull/{self.pr}" and isinstance(sha256, str):
                self._add(check="snapshot", kind="material_pr", ref=ref, sha256=sha256, size=size,
                          source="github", stage="review", expectation="review_material")
            else:
                self._add(check="skip", kind=f"material_{kind}", ref=ref, sha256=sha256, size=size,
                          source="github", stage="review", expectation="review_material",
                          note="评审时按 recipe 渲染，复取不保证同字节，不核对")

    def _collect_run_records(self):
        """合并 head 上的运行记录（路径@merge_sha 原始字节）：摘要入阶段视图，记录本身登记核对。"""
        ok, listing = _git_run(self.cwd, "ls-tree", "-r", "--name-only", self.merge_sha, "--", "docs/runs")
        if not ok:
            return
        for path in [line.strip() for line in listing.splitlines() if line.strip().endswith(".json")]:
            data = _git_bytes(self.cwd, self.merge_sha, path)
            if data is None:
                self.findings.append(_finding(
                    "reference_unavailable", source="local", stage="dispatch",
                    ref=f"{path}@{self.merge_sha}", reason="运行记录无法从合并 head 读取"))
                continue
            try:
                record = json.loads(data)
            except ValueError:
                continue
            if not isinstance(record, dict) or self.trace not in (record.get("trace_id"), record.get("branch")):
                continue
            for item in record.get("stages") or []:
                if isinstance(item, dict):
                    self.stages.append({"evidence": "run_record_summary", "source": "local",
                                        "source_class": "local", "record": f"{path}@{self.merge_sha}", **item})
            for anchor in record.get("anchors") or []:
                if isinstance(anchor, dict):
                    self.anchors.append({"trace_id": self.trace, **anchor})
            entry = self._add(check="git", kind="run_record", ref=f"{path}@{self.merge_sha}",
                              sha256=None, size=None, source="local", stage="dispatch", expectation=None)
            entry["observed_sha256"], entry["observed_size"] = _digest(data), len(data)

    def _collect_ledger(self, anchor):
        """C6 账本：锚点评论记录账本字节 sha256；账本 references 是运行记录/评审评论的核对期望。"""
        if anchor is None:
            return
        ref = f"{ledger.AUDIT_BRANCH}:{anchor['path']}@{anchor['commit']}"
        ok, _ = _git_run(self.cwd, "ls-remote", "origin", f"refs/heads/{ledger.AUDIT_BRANCH}")
        fetched = False
        data = None
        if ok:
            ok, _ = _git_run(self.cwd, "fetch", "--quiet", "origin", ledger.AUDIT_BRANCH)
            if ok:
                data = _git_bytes(self.cwd, anchor["commit"], anchor["path"])
                fetched = data is not None
        entry = self._add(check="ledger", kind="ledger", ref=ref, sha256=anchor["sha256"], size=None,
                          source="github", stage="merge", expectation="anchor_comment", ledger_bytes=data)
        if not fetched:
            entry["status"] = "unavailable"
            entry["reason"] = "账本文件无法从 harness-audit 分支读取"
            return
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        self.ledger_doc = parsed if isinstance(parsed, dict) else None
        if not isinstance(parsed, dict):
            entry["status"] = "hash_mismatch"
            entry["reason"] = "账本文件不是 JSON 对象"
            entry["observed_sha256"], entry["observed_size"] = _digest(data), len(data)
            return
        entry["observed_sha256"], entry["observed_size"] = _digest(data), len(data)
        for item in parsed.get("references") or []:
            if not isinstance(item, dict) or not isinstance(item.get("ref"), str) \
                    or not isinstance(item.get("sha256"), str):
                continue
            self._expect[(item.get("kind"), item["ref"])] = {"sha256": item["sha256"],
                                                             "size": item.get("size")}

    def _apply_expectations(self):
        """账本记录的期望补到已收集的引用上；账本里有、视图里没收集到的引用按账本记录补收核对。"""
        for entry in self.refs:
            hit = self._expect.pop((entry["kind"], entry["ref"]), None)
            if hit and entry["sha256"] is None:
                entry["sha256"] = hit["sha256"]
                entry["size"] = entry["size"] if entry["size"] is not None else hit.get("size")
                entry["expectation"] = "ledger"
        for (kind, ref), hit in self._expect.items():
            source = "github" if kind == "review_comment" else "local"
            check = "comment" if kind == "review_comment" else "git"
            self._add(check=check, kind=str(kind), ref=ref, sha256=hit["sha256"], size=hit.get("size"),
                      source=source, stage="review" if kind == "review_comment" else "dispatch",
                      expectation="ledger")

    # ---- 核对 ----

    def _compare(self, entry: dict, data: bytes) -> None:
        entry["observed_sha256"], entry["observed_size"] = _digest(data), len(data)
        if entry["sha256"] is not None and entry["observed_sha256"] != entry["sha256"]:
            entry.update(status="hash_mismatch", reason="内容 sha256 与记录不符（原始字节比对）")
        elif entry["size"] is not None and entry["observed_size"] != entry["size"]:
            entry.update(status="hash_mismatch", reason="内容大小与记录不符")
        else:
            entry.update(status="verified", reason=None)

    def _verify(self, entry: dict) -> None:
        if entry["check"] == "skip":
            return  # 保持 unchecked 与不核对的理由
        if not isinstance(entry["sha256"], str) or _HASH_RE.fullmatch(entry["sha256"]) is None:
            entry["reason"] = "没有记录的 sha256，无从核对"
            return
        checker = {"git": self._check_git, "comment": self._check_comment,
                   "artifact": self._check_artifact, "snapshot": self._check_snapshot,
                   "ledger": self._check_ledger}[entry["check"]]
        checker(entry)

    def _check_git(self, entry: dict) -> None:
        match = _GIT_REF_RE.fullmatch(str(entry["ref"]))
        if match is None or not _safe_repo_path(match["path"]):
            entry.update(status="unavailable", reason="path 不是仓库相对引用或逃出仓库，已拒绝核对")
            return
        data = _git_bytes(self.cwd, match["rev"], match["path"])
        if data is None:
            entry.update(status="unavailable", reason=f"{match['path']}@{match['rev']} 无法从 git 读取")
            return
        self._compare(entry, data)

    def _check_comment(self, entry: dict) -> None:
        match = _COMMENT_REF_RE.fullmatch(str(entry["ref"]))
        if match is None:
            entry.update(status="unavailable", reason="评论引用形状不符，已拒绝核对")
            return
        if f"{match['owner']}/{match['name']}" != self.repo or int(match["pr"]) != self.pr:
            entry.update(status="unavailable", reason="评论引用不属于当前仓库或本 PR，已拒绝核对")
            return
        try:
            comment = self.client.api(f"repos/{self.repo}/issues/comments/{match['id']}")
        except (RuntimeError, ValueError, AttributeError):
            entry.update(status="unavailable", reason="评论无法从 API 读取")
            return
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str):
            entry.update(status="unavailable", reason="评论响应形状不符")
            return
        self._compare(entry, body.encode("utf-8"))

    def _check_artifact(self, entry: dict) -> None:
        digest = entry["artifact"] or entry["sha256"]
        if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
            entry.update(status="unavailable", reason="产物引用不是 sha256，已拒绝核对")
            return
        directory = events_db.artifacts_dir()
        data = None
        if directory is not None:
            try:
                data = (directory / digest).read_bytes()
            except OSError:
                data = None
        if data is not None:
            self._compare(entry, data)
            return
        # 到期判定：产物索引行还在看 created，行已被保留期清理则回退引用事件的 ts。
        moment = _parse_utc(_artifact_created(digest)) or _parse_utc(entry.get("event_ts"))
        if moment is not None and moment < self.cutoff:
            entry.update(status="expired", reason="本机产物已过保留期清理，无法核对")
        else:
            entry.update(status="unavailable", reason="本机产物缺失，无法核对")

    def _check_snapshot(self, entry: dict) -> None:
        try:
            pull = self.client.api(f"repos/{self.repo}/pulls/{self.pr}")
        except (RuntimeError, ValueError, AttributeError):
            entry.update(status="unavailable", reason="PR 字段无法从 API 读取")
            return
        rendered = f"# {pull.get('title')}\n\n{pull.get('body') or ''}".encode() \
            if isinstance(pull, dict) else b""
        # 可变 PR 字段按评审同一渲染规则复取：变动是快照漂移（C6 snapshot_changed），不冒充字节篡改。
        if _digest(rendered) != entry["sha256"] or len(rendered) != entry["size"]:
            entry.update(status="snapshot_changed", reason="PR 标题/正文与评审材料快照不符（可变字段已变动）")
        else:
            entry.update(status="verified", reason=None)

    def _check_ledger(self, entry: dict) -> None:
        data = entry.get("ledger_bytes")
        if data is None:
            entry.update(status="unavailable", reason="账本文件无法从 harness-audit 分支读取")
            return
        self._compare(entry, data)

    # ---- 共享能力与汇总 ----

    def _import_ci(self):
        """CI 事件包交给 C5/T302 load_ci：下载、来源核对与规范化哈希导入都在那里完成。"""
        try:
            result = events_io.load_ci(self.pr, head=self.head, gh=self.client)
        except Exception as exc:  # noqa: BLE001  下载适配故障转发现，不替代审计
            result = {"findings": [{"code": "api", "detail": str(exc)}]}
        for item in result.get("findings") or []:
            code = item.get("code")
            if code in events_io.INFORMATIONAL_FINDINGS:
                continue  # B124：旧 head 的可信运行是预期历史，不算审计失败
            if code == "artifact_expired":
                rule, reason = "reference_expired", "CI 事件包 artifact 已过保留期，未下载"
            elif code == "hash_mismatch":
                rule, reason = "hash_mismatch", "CI 事件包内容与规范化哈希不符，导入被拒"
            else:
                rule, reason = "reference_unavailable", f"CI 事件包不可用（{code}）"
            self.findings.append(_finding(rule, source="ci", stage="ci",
                                          ref=f"{self.repo}#{self.pr}/actions/artifacts", reason=reason))

    def _sync_github(self):
        """GitHub 事实由 T304 共享入口同步（合并/抽审/逃逸快照幂等导入），审计不自行改写。"""
        try:
            result = github_events.sync(pr=self.pr, gh=self.client)
        except Exception as exc:  # noqa: BLE001  同步故障转发现，不替代审计
            result = {"findings": [{"code": "api", "detail": str(exc)}]}
        for item in result.get("findings") or []:
            self.findings.append(_finding(
                "reference_unavailable", source="github", stage="merge", ref=f"{self.repo}#{self.pr}",
                reason=f"GitHub 事实同步不完整（{item.get('code')}）"))

    def run(self) -> dict:
        self._import_ci()
        self._sync_github()
        self._collect_events()
        anchor = self._collect_comments()
        self._collect_run_records()
        self._collect_ledger(anchor)
        self._apply_expectations()
        for entry in self.refs:
            self._verify(entry)
        for entry in self.refs:
            rule = _RULE_FOR_STATUS.get(entry["status"])
            if rule is not None:
                self.findings.append(_finding(rule, source=entry["source"], stage=entry["stage"],
                                              ref=str(entry["ref"]), reason=entry["reason"]))
        self.findings.extend(audit_completeness.check(self))  # T402：完整性与防篡改（含 [audit] 配置校验）
        self.findings.sort(key=lambda item: (item["rule"], item["source"], item["stage"], item["ref"]))
        self.refs.sort(key=lambda item: (item["kind"], str(item["ref"])))
        for entry in self.refs:
            entry.pop("ledger_bytes", None)  # 内部核对输入（字节）不进报告，报告只留哈希与结论
        self.stages.sort(key=lambda item: (str(item.get("ts")), str(item.get("stage")), str(item.get("step"))))
        self.anchors.sort(key=lambda item: (str(item.get("ts")), str(item.get("source"))))
        entry = next((item for item in self.refs if item["kind"] == "ledger"), None)
        self.ledger_state = entry["status"] if entry is not None else "absent"
        classes = Counter(_source_class(row["source"]) for row in self.event_rows)
        statuses = Counter(item["status"] for item in self.refs)
        coverage = {"events": {name: classes.get(name, 0) for name in _SOURCE_CLASSES},
                    "chains": len(self.chains), "anchors": len(self.anchors),
                    "references": {"total": len(self.refs),
                                   **{name: statuses.get(name, 0) for name in _STATUSES}},
                    "ledger": self.ledger_state}
        return {"pr": self.pr, "repository": self.repo, "trace_id": self.trace, "head_sha": self.head,
                "merge_sha": self.merge_sha, "merged_at": self.merged_at, "stages": self.stages,
                "references": self.refs, "findings": self.findings, "chains": self.chains,
                "anchors": self.anchors, "coverage": coverage, "ok": not self.findings}


def _exit_code(report: dict) -> int:
    """0 全部通过；1 有发现；2 配置错误（configuration_error 优先，非法 [audit] 不静默放宽）。"""
    if any(item.get("rule") == "configuration_error" for item in report["findings"]):
        return 2
    return 1 if report["findings"] else 0


def _flatten_outputs(prefix: str, value, out: dict) -> None:
    """嵌套汇总（coverage、规则/severity 计数）摊平成 outputs 扁平键：emit 过滤器只收标量值。"""
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten_outputs(f"{prefix}.{key}" if prefix else str(key), item, out)
    else:
        out[prefix] = value


def _emit_observations(report: dict) -> None:
    """audit 观察事件（C1：audit 自己记汇总；C7：写安全 ci/audit.summary 与 audit.finding 供周报）。

    每次运行恰一条 ci/audit.summary（ok/fail 随报告结论），每条 finding 追加一条 ci/audit.finding；
    stage 固定 ci，不新增阶段枚举。outputs 只放安全字段（pr/head_sha/规则与 severity 计数/coverage/ok，
    finding 事件另含 rule/severity/source/stage/ref，供周报按同 PR/head 取最新、唯一规则计数去重），
    reason 原文不入事件；trace 沿用当前分支，不混入被审计 PR 的事件链。emit 永不抛，组装失败同样
    只丢弃——退出码与 stdout/stderr 不受影响（C0 三态等价）。
    """
    try:
        findings = report.get("findings") or []
        summary = {"pr": report.get("pr"), "head_sha": report.get("head_sha"), "ok": report.get("ok"),
                   "severity": dict(Counter(item.get("severity") for item in findings)),
                   "rule": dict(Counter(item.get("rule") for item in findings)),
                   "coverage": report.get("coverage")}
        outputs: dict = {}
        _flatten_outputs("", summary, outputs)
        events.emit(stage="ci", step="audit.summary", status="ok" if report.get("ok") else "fail",
                    outputs=outputs)
        for item in findings:
            events.emit(stage="ci", step="audit.finding", status="fail",
                        outputs={"pr": report.get("pr"), "head_sha": report.get("head_sha"),
                                 "rule": item.get("rule"), "severity": item.get("severity"),
                                 "source": item.get("source"), "stage": item.get("stage"),
                                 "ref": item.get("ref")})
    except Exception:  # noqa: BLE001  观察旁路（C0）：任何组装失败都不影响审计本身
        return


def inspect_pr(pr: int, *, gh=None, cwd: Path = ROOT) -> dict:
    """复原并核对一个已合并 PR（C7 基础形状）；PR 无法审计（未合并/整体 API 故障）抛 AuditError。

    报告返回前写本运行的观察事件（T404）；写入失败不影响返回值与调用方。
    """
    client = GhClient() if gh is None else gh
    try:
        repo = events_io._repo_identity(str(client.repo()))
        pull = client.api(f"repos/{repo}/pulls/{pr}")
    except (RuntimeError, ValueError, AttributeError) as exc:
        raise AuditError(f"读取 PR {pr} 失败：{exc}") from exc
    if not isinstance(pull, dict) or pull.get("merged") is not True:
        raise AuditError(f"PR {pr} 未合并或不存在（审计只覆盖已合并 PR，被拦下 PR 属 B52）")
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    trace, head_sha = head.get("ref"), head.get("sha")
    merge_sha, merged_at = pull.get("merge_commit_sha"), pull.get("merged_at")
    if not (isinstance(trace, str) and trace and isinstance(head_sha, str) and _SHA_RE.fullmatch(head_sha)
            and isinstance(merge_sha, str) and _SHA_RE.fullmatch(merge_sha)
            and isinstance(merged_at, str) and _parse_utc(merged_at) is not None):
        raise AuditError(f"PR {pr} 的合并事实缺失（headRefName/head/merge_commit_sha/merged_at）")
    report = _Auditor(client=client, repo=repo, pr=pr, trace=trace, head=head_sha, merge_sha=merge_sha,
                      merged_at=merged_at, cwd=Path(cwd)).run()
    _emit_observations(report)
    return report


# ---- CLI（cli.py COMMANDS 注册 audit；QUIET_COMMANDS 已含 audit，不追加自身事件）----


def _render(report: dict) -> str:
    head = report.get("head_sha") or ""
    coverage = report["coverage"]
    refs = coverage["references"]
    lines = [(f"audit PR {report['pr']}（{report.get('trace_id') or '—'}）：head {head[:12] or '—'}…，"
             f"合并 {report.get('merged_at') or '—'}")]
    lines.append(f"  覆盖：阶段 {len(report['stages'])}（local {coverage['events']['local']} / "
                 f"ci {coverage['events']['ci']} / github {coverage['events']['github']}，链 "
                 f"{coverage['chains']}、锚点 {coverage['anchors']}）；引用 {refs['total']}"
                 f"（核对通过 {refs['verified']}、未核对 {refs['unchecked']}、不符 {refs['hash_mismatch']}、"
                 f"不可得 {refs['unavailable']}、到期 {refs['expired']}）；账本 {coverage['ledger']}")
    if report["findings"]:
        lines.append(f"  发现 {len(report['findings'])} 项：")
        lines += [f"   - [{item['severity']}] {item['rule']} {item['source']}/{item['stage']} "
                  f"{item['ref']}：{item['reason']}" for item in report["findings"]]
    else:
        lines.append("  发现：无")
    return "\n".join(lines)


def _select_merged(client, repo: str, cutoff: datetime | None,
                   problems: list[dict]) -> list[tuple[datetime, int]] | None:
    """分页取全部已关闭 PR，按一次性确定的 UTC 窗口过滤出已合并者；列表失败返回 None。"""
    pulls = github_events._collect_pages(
        client, f"repos/{repo}/pulls?state=closed&sort=created&direction=desc", "已关闭 PR 列表", problems)
    if pulls is None:
        return None
    selected: list[tuple[datetime, int]] = []
    for pull in pulls:
        if not isinstance(pull, dict) or pull.get("merged") is not True:
            continue
        moment = _parse_utc(pull.get("merged_at"))
        if moment is None or (cutoff is not None and moment < cutoff):
            continue
        if isinstance(pull.get("number"), int):
            selected.append((moment, pull["number"]))
    selected.sort()
    return selected


def _audit_or_failure(client, repo: str, number: int) -> dict:
    """批量里单个 PR 的审计；无法审计（合并事实缺失等）转成一条发现，不中断整批。"""
    try:
        return inspect_pr(number, gh=client, cwd=ROOT)
    except AuditError:
        report = {"pr": number, "repository": repo, "trace_id": None, "head_sha": None,
                  "merge_sha": None, "merged_at": None, "stages": [], "references": [],
                  "chains": [], "anchors": [],
                  "findings": [_finding("reference_unavailable", source="github", stage="merge",
                                        ref=f"{repo}#{number}", reason="PR 无法审计：合并事实缺失")],
                  "coverage": {"events": {name: 0 for name in _SOURCE_CLASSES}, "chains": 0,
                               "anchors": 0, "references": {"total": 0, **{name: 0 for name in _STATUSES}},
                               "ledger": "absent"},
                  "ok": False}
        _emit_observations(report)  # 无法审计的运行同样留一条汇总与发现事件（C7 供周报）
        return report


def _all_merged(args, as_json: bool) -> int:
    """批量模式：分页全部已关闭 PR，按一次性确定的窗口过滤已合并者逐个审计。"""
    client = GhClient()
    try:
        repo = events_io._repo_identity(str(client.repo()))
    except (RuntimeError, ValueError, AttributeError) as exc:
        print(f"audit：确定仓库失败：{exc}", file=sys.stderr)
        return 2
    try:
        cutoff = _parse_window(args.since) if args.since else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    problems: list[dict] = []
    selected = _select_merged(client, repo, cutoff, problems)
    if selected is None:
        for item in problems:
            print(f"audit：{item['detail']}", file=sys.stderr)
        return 2
    until = _parse_utc(events_db._now()) or datetime.now(UTC)
    reports = [_audit_or_failure(client, repo, number) for _moment, number in selected]
    for report in reports:
        if not as_json:
            print("\n".join(f"  {line}" for line in _render(report).splitlines()))
    window = {"since": cutoff.astimezone(UTC).isoformat() if cutoff else None,
              "until": until.astimezone(UTC).isoformat()}
    if as_json:
        print(json.dumps({"window": window, "prs": reports}, ensure_ascii=False, indent=2))
    else:
        print(f"audit --all-merged：窗口 {window['since'] or '全部'} → {window['until']}，"
              f"共 {len(reports)} 个已合并 PR")
    return max((_exit_code(report) for report in reports), default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit", description="审计引用复原与哈希核对（设计 4.2）")
    parser.add_argument("pr", nargs="?", type=int, help="审计指定已合并 PR")
    parser.add_argument("--all-merged", action="store_true", help="批量审计时间窗口内的全部已合并 PR")
    parser.add_argument("--since", metavar="时长|UTC", help="批量模式的窗口起点（如 30d 或 ISO 时间）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    if args.pr is not None and args.all_merged:
        print("PR 号与 --all-merged 二选一（两种模式互斥）", file=sys.stderr)
        return 2
    if args.since is not None and not args.all_merged:
        print("--since 只与 --all-merged 同用", file=sys.stderr)
        return 2
    if args.pr is None and not args.all_merged:
        parser.print_usage(sys.stderr)
        print("audit：缺少审计目标（PR 号或 --all-merged）", file=sys.stderr)
        return 2
    if args.pr is not None:
        try:
            report = inspect_pr(args.pr, cwd=ROOT)  # ROOT 在调用时解析（审计所在仓库）
        except AuditError as exc:
            print(f"audit：{exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else _render(report))
        return _exit_code(report)
    return _all_merged(args, args.json)
