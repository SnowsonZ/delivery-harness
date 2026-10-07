"""审计完整性与防篡改判定（B46 T402，设计 4.2，共用合同 C7）。

在 T401 复原与逐引用核对之上补两层：

完整性（应有环节）：按风险与 PR 种类检查 R2+ 有独立评审且身份不同、自动合并有受信任 CI 的
route.result（outputs.auto_merge=true）、任务 PR 有运行记录与阶段锚点、app 模式 R0/R1 自动合并由
批准 App 批准且绑定合并 head；none 模式明确无 App 适用，不凭空要求批准。风险取受信任 CI 链上最新
route.facts 的机器判定；任务 PR＝route.facts 声明任务书类别 ∨ 评审摘要列有执行方 ∨ 已有运行记录
（2026-10-02 任务书修订口径），设计方 PR 不要求派发记录。「自动合并」取合并事实的 merger_type=Bot
（两种模式的自动合并工作流都以机器人身份合并，人工合并是 User）；风险不可判定时对应规则不适用，
缺失的 CI 事实本身已由引用核对与 missing_route 报告。

防篡改：校验各 (source, trace) 原始链（events_db.verify）；每个固定锚点（运行记录/CI artifact/PR
评论）核对「对应前缀链上有该链头」而非最终链头——合法后续追加不误报，删尾即使内部链仍合法也能由
固定锚点发现；账本核对（B118/B121）：ci:/本机链沿用链头与前缀成员核对，github: 内容寻址快照链改
为九字段语义核对（合并事实独立必检、缺失快照按种类区分被取代与失败关闭）。链未知或不可得不当完
整：事件库不可读产生如实发现并按链不可校验处理，不当「无事件」放过。

checks.toml 可选 [audit]：require_review_risk（0..3，缺省 2）、require_route_for_auto、
require_run_record_for_task、verify_anchors（布尔，缺省 true）。缺省执行设计规则；未知键、类型或
取值非法产生 configuration_error 发现（CLI 退出 2），不静默放宽——非法键按设计缺省执行并在报告中
指明。配置只影响 audit 报告：本模块只在审计时被调用，policy/guard/verify 判定不经过这里。
"""

from __future__ import annotations

import re

from engine.core import events_db
from engine.core.common import load_checks
from engine.routing import policy

# [audit] 已知键与设计缺省（C7）；缺省值即缺省执行的设计规则。
_DEFAULTS = {"require_review_risk": 2, "require_route_for_auto": True,
             "require_run_record_for_task": True, "verify_anchors": True}
_CONFIG_REF = ".harness/config/checks.toml [audit]"
_RISK_RE = re.compile(r"R([0-3])\Z")
# T201 运行记录固定的锚点；missing_anchor 只认它，CI/PR 评论锚点是额外固定点，不豁免记录锚点。
_RECORD_FIXED_IN = "run_record"
# B118/B121：github: 快照链九字段语义核对键；合并投影 = 稳定身份字段 + outputs 子集（排除
# label_count、merge_method 与批准四字段等合并后可合法变化的字段）。
_SEMANTIC_KEYS = ("seq", "stage", "step", "status", "actor", "inputs", "outputs", "decision", "error")
_MERGE_FIELD_KEYS = ("stage", "status", "actor", "decision", "error", "inputs")
_MERGE_OUTPUT_KEYS = ("pr", "merger", "merger_type", "merged_at")
_PR_REF_RE = re.compile(r"(?P<owner>[^#/]+)/(?P<repo>[^#/]+)#(?P<num>\d+)\Z")
_LEDGER_PR_RE = re.compile(r"github:(?P<pr>\d+):")


def _finding(rule: str, *, source: str, stage: str, ref: str, reason: str) -> dict:
    """C7 finding（severity 全部 error）：reason 只写分类与事实，不回显引用内容。"""
    return {"rule": rule, "severity": "error", "source": source, "stage": stage,
            "ref": ref, "reason": reason}


def load_config() -> tuple[dict, list[dict]]:
    """读 [audit] 并校验（C7）：返回（生效配置, configuration_error 发现）；非法键按设计缺省执行。"""
    config = dict(_DEFAULTS)
    try:
        section = load_checks().get("audit", {})
    except Exception as exc:  # noqa: BLE001  配置损坏明确报告，不静默放宽
        return config, [_finding("configuration_error", source="local", stage="audit", ref=_CONFIG_REF,
                                 reason=f"配置无法读取（{type(exc).__name__}），按设计缺省执行")]
    if section is None:
        section = {}
    if not isinstance(section, dict):
        return config, [_finding("configuration_error", source="local", stage="audit", ref=_CONFIG_REF,
                                 reason="[audit] 不是配置表，按设计缺省执行")]
    problems: list[dict] = []
    for key, value in section.items():
        if key not in _DEFAULTS:
            problems.append(_finding("configuration_error", source="local", stage="audit", ref=_CONFIG_REF,
                                     reason=f"未知配置键 {key}，按设计缺省执行"))
        elif key == "require_review_risk":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
                problems.append(_finding("configuration_error", source="local", stage="audit",
                                         ref=_CONFIG_REF, reason="require_review_risk 须是 0..3 的整数"))
            else:
                config[key] = value
        elif not isinstance(value, bool):
            problems.append(_finding("configuration_error", source="local", stage="audit", ref=_CONFIG_REF,
                                     reason=f"{key} 须是布尔值"))
        else:
            config[key] = value
    return config, problems


def _risk_level(label) -> int | None:
    """route.facts 的机器判定 R0..R3 → 0..3；缺失或形状不符返回 None（无法判定）。"""
    if not isinstance(label, str):
        return None
    match = _RISK_RE.fullmatch(label.strip().upper())
    return int(match[1]) if match else None


def _source_class(source: str) -> str:
    if str(source).startswith("github:"):
        return "github"
    if str(source).startswith("ci"):
        return "ci"
    return "local"


def _trusted_ci(row: dict) -> bool:
    """受信任 CI 链（C5 导入时已核对 origin head）：ci: 前缀。local 链不冒充 main 判定。"""
    return str(row.get("source") or "").startswith("ci")


def _outputs(row: dict | None) -> dict:
    value = row.get("outputs") if isinstance(row, dict) else None
    return value if isinstance(value, dict) else {}


class _Facts:
    """从复原数据提炼的适用性事实：规则只依赖这些判定，不各自翻原始行。"""

    def __init__(self, auditor):
        rows = auditor.event_rows or []
        trusted_route = [row for row in rows if row.get("stage") == "route" and _trusted_ci(row)]
        facts_rows = [row for row in trusted_route if row.get("step") == "facts"]
        latest_facts = max(facts_rows, key=lambda row: str(row.get("ts")), default=None)
        self.risk = _risk_level(_outputs(latest_facts).get("risk"))
        declared = _outputs(latest_facts).get("declared_class")
        self.reviews = [stage for stage in auditor.stages or []
                        if stage.get("evidence") == "review_comment_summary"]
        merge_rows = [row for row in rows
                      if row.get("stage") == "merge" and row.get("step") == "github.merge"]
        latest_merge = max(merge_rows, key=lambda row: str(row.get("ts")), default=None)
        merge_out = _outputs(latest_merge)
        self.auto_merged = merge_out.get("merger_type") == "Bot"
        self.approval_out = merge_out
        self.route_result_ok = any(_outputs(row).get("auto_merge") is True
                                   for row in trusted_route if row.get("step") == "result")
        self.run_records = any(stage.get("evidence") == "run_record_summary"
                               for stage in auditor.stages or [])
        # 任务 PR 三证据（任务书修订口径）：声明类别 ∨ 评审列有执行方 ∨ 已有运行记录，任一即成立。
        implemented = any(isinstance(stage.get("implementers"), list) and stage["implementers"]
                          for stage in self.reviews)
        self.task_pr = declared is not None or implemented or self.run_records
        self.record_anchors = [anchor for anchor in auditor.anchors or []
                               if anchor.get("fixed_in") == _RECORD_FIXED_IN]


def _independent(review: dict) -> bool:
    """评审身份独立：independent 不为 False，且评审者不同于设计方、不在执行方列表里（不区分大小写）。"""
    if review.get("independent") is False:
        return False
    reviewer = str(review.get("reviewer") or "").strip().lower()
    designer = str(review.get("designer") or "").strip().lower()
    implementers = {str(item).strip().lower() for item in review.get("implementers") or []
                    if isinstance(item, str)}
    return bool(reviewer) and reviewer != designer and reviewer not in implementers


def _review(facts: _Facts, config: dict, ref: str) -> list[dict]:
    """missing_review（无合格独立评审）与 reviewer_not_independent（逐条身份冲突）。"""
    findings: list[dict] = []
    if facts.risk is not None and facts.risk >= config["require_review_risk"] \
            and not any(_independent(review) for review in facts.reviews):
        reason = "没有评审摘要，应有独立评审" if not facts.reviews else "现有评审身份不独立，无合格独立评审"
        findings.append(_finding("missing_review", source="github", stage="review", ref=ref, reason=reason))
    for review in facts.reviews:
        if _independent(review):
            continue
        comment = review.get("comment") if isinstance(review.get("comment"), dict) else {}
        where = f"{ref}/comments/{comment['id']}" if comment.get("id") is not None else ref
        findings.append(_finding("reviewer_not_independent", source="github", stage="review", ref=where,
                                 reason="评审者与设计方或执行方身份相同（或 independent 显式为 false）"))
    return findings


def _route(facts: _Facts, config: dict, ref: str) -> list[dict]:
    """missing_route：自动合并必须有受信任 CI 链上 auto_merge=true 的 route.result。"""
    if config["require_route_for_auto"] and facts.auto_merged and not facts.route_result_ok:
        return [_finding("missing_route", source="ci", stage="route", ref=ref,
                         reason="自动合并没有受信任 CI 来源（ci: 前缀）的 route.result（auto_merge=true）")]
    return []


def _record_anchors(facts: _Facts, config: dict, ref: str) -> list[dict]:
    """missing_run_record / missing_anchor：任务 PR 必须有运行记录，且记录固定了阶段锚点。"""
    if not config["require_run_record_for_task"] or not facts.task_pr:
        return []
    if not facts.run_records:
        return [_finding("missing_run_record", source="local", stage="dispatch", ref=ref,
                         reason="任务 PR 在合并 head 上没有运行记录")]
    if not facts.record_anchors:
        return [_finding("missing_anchor", source="local", stage="dispatch", ref=ref,
                         reason="运行记录没有固定的阶段锚点（fixed_in=run_record）")]
    return []


def _approval(facts: _Facts, config: dict, mode: str | None, ref: str) -> list[dict]:
    """approval_actor / approval_head：app 模式 R0/R1 自动合并须批准者 App 且绑定合并 head。

    none 模式无 App 适用（不凭空要求批准）；人工合并不是自动合并，同样不适用。
    """
    if mode != "app" or not facts.auto_merged:
        return []
    if facts.risk is None or facts.risk > 1:
        return []
    findings: list[dict] = []
    if facts.approval_out.get("approver_type") != "Bot":
        findings.append(_finding("approval_actor", source="github", stage="merge", ref=ref,
                                 reason=f"app 模式 R{facts.risk} 自动合并的批准者不是 App"
                                        f"（approver_type={facts.approval_out.get('approver_type')}）"))
    if facts.approval_out.get("approval_bound") is not True:
        detail = "批准没有绑定提交" if facts.approval_out.get("approval_commit") is None \
            else "批准绑定的提交不是合并的 head"
        findings.append(_finding("approval_head", source="github", stage="merge", ref=ref, reason=detail))
    return findings


def _chains(auditor, config: dict) -> list[dict]:
    """chain_invalid（原始链校验失败）；锚点核对见 _anchors。"""
    findings: list[dict] = []
    hashes: dict[str, set] = {}
    last_stage: dict[str, str] = {}
    last_seq: dict[str, int] = {}
    for row in auditor.event_rows or []:
        source = str(row.get("source") or "")
        hashes.setdefault(source, set()).add(row.get("hash"))
        if source not in last_seq or int(row.get("seq") or 0) > last_seq[source]:
            last_seq[source] = int(row.get("seq") or 0)
            last_stage[source] = str(row.get("stage") or "dispatch")
    if getattr(auditor, "db_error", None):
        findings.append(_finding("chain_invalid", source="local", stage="audit", ref="harness.db",
                                 reason="事件库无法读取，链与锚点不可校验（不当完好）"))
    for chain in auditor.chains or []:
        source = str(chain.get("source") or "")
        try:
            problems = events_db.verify(trace_id=chain.get("trace_id"), source=source)
        except Exception as exc:  # noqa: BLE001  坏库：链不可校验要如实报告
            problems = [f"链校验无法执行（{type(exc).__name__}）"]
        if problems:
            findings.append(_finding("chain_invalid", source=_source_class(source),
                                     stage=last_stage.get(source, "dispatch"),
                                     ref=f"{source}/{chain.get('trace_id')}",
                                     reason=f"原始链校验失败：{problems[0]}"))
    return findings + _anchors(auditor, config, hashes)


def _anchors(auditor, config: dict, hashes: dict[str, set]) -> list[dict]:
    """anchor_mismatch：固定锚点按前缀成员核对——锚点之后的合法追加不算篡改，链头消失即发现。"""
    if not config["verify_anchors"]:
        return []
    findings: list[dict] = []
    seen: set[tuple] = set()
    for anchor in auditor.anchors or []:
        key = (anchor.get("source"), anchor.get("stage"), anchor.get("head_hash"), anchor.get("fixed_in"))
        if key in seen:
            continue
        seen.add(key)
        known = hashes.get(str(anchor.get("source") or ""))
        if known is not None and anchor.get("head_hash") in known:
            continue  # 前缀成员：链在锚点之后继续增长属合法追加
        findings.append(_finding("anchor_mismatch", source=_source_class(anchor.get("source") or ""),
                                 stage=str(anchor.get("stage") or "dispatch"),
                                 ref=f"{anchor.get('fixed_in')}:{anchor.get('stage')}"
                                     f"@{str(anchor.get('head_hash') or '')[:12]}",
                                 reason="固定锚点的链头不在对应链上（链缺失或锚点之后的历史被改动）"))
    return findings


def _ledger_finding(ref: str, reason: str) -> dict:
    """账本核对的 ledger_mismatch 发现（全部 error、stage=merge；reason 不回显引用内容）。"""
    return _finding("ledger_mismatch", source="github", stage="merge", ref=ref, reason=reason)


def _view(event: dict, keys, output_keys=()) -> dict:
    """比较视图：取 keys 字段与 outputs 的 output_keys 子集；inputs 里 PR 引用（owner/repo#N）的
    仓库部分转小写（GitHub 仓库名不区分大小写），语义核对与合并投影共用同一归一。"""
    outputs = event.get("outputs") if isinstance(event.get("outputs"), dict) else {}
    normalized = []
    for item in event.get("inputs") if isinstance(event.get("inputs"), list) else ():
        ref = item.get("ref") if isinstance(item, dict) else None
        match = _PR_REF_RE.fullmatch(ref) if isinstance(ref, str) else None
        normalized.append(
            {**item, "ref": f"{match['owner'].lower()}/{match['repo'].lower()}#{match['num']}"} if match else item)
    viewed = {key: (normalized if key == "inputs" else event.get(key)) for key in keys}
    viewed.update((key, outputs.get(key)) for key in output_keys)
    return viewed


def _by_seq(events: list[dict]) -> list[dict] | None:
    """按 seq 排序；任一 seq 不是整数（账本被损坏或篡改）返回 None，调用方失败关闭，不让异常逃出审计。"""
    if any(not isinstance(event.get("seq"), int) for event in events):
        return None
    return sorted(events, key=lambda event: event["seq"])


def _semantic_diff(ledger_events: list[dict], runtime_events: list[dict]) -> str | None:
    """github: 链九字段语义核对（inputs 归一，按 seq 排序逐条）：一致返回 None；差异只写数量与字段名。"""
    left, right = _by_seq(ledger_events), _by_seq(runtime_events)
    if left is None or right is None:
        return "事件 seq 形状不符（失败关闭）"
    if len(left) != len(right):
        return f"事件数量不同（账本 {len(left)} 条、运行层 {len(right)} 条）"
    for position, (expected, actual) in enumerate(zip(left, right), start=1):
        views = _view(expected, _SEMANTIC_KEYS), _view(actual, _SEMANTIC_KEYS)
        fields = "、".join(key for key in _SEMANTIC_KEYS if views[0][key] != views[1][key])
        if fields:
            return f"第 {position} 条事件的字段不同：{fields}"
    return None


def _events_by_source(entries, mark_key: str) -> dict[str, list[dict]]:
    """按 source 分组事件：账本取 stages 的 evidence_kind=event，运行层取 auditor.stages 的 evidence=event。"""
    grouped: dict[str, list[dict]] = {}
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get(mark_key) == "event":
            grouped.setdefault(str(entry.get("source") or ""), []).append(entry)
    return grouped


def _merge_fact_findings(ledger_events: dict[str, list[dict]],
                         runtime_events: dict[str, list[dict]]) -> list[dict]:
    """合并事实一致性（B121，独立必检、不被同 source 通过短路）：账本每个 github.merge 事件对运行
    层同 PR（source 前缀限定，同分支名被另一 PR 复用不掺入）的全部合并事件核对稳定投影；候选为
    空与投影不同分 reason（不把「读不到」说成「不同」），reason 只写字段名——merger 是 login，
    改名有意多报待人工确认。"""
    def merges(grouped: dict[str, list[dict]]) -> list[dict]:
        return [event for group in grouped.values() for event in group
                if event.get("step") == "github.merge"]
    problems: list[tuple[str, str]] = []
    for merge in merges(ledger_events):
        ref = f"ledger:{merge.get('source')}/{merge.get('trace_id')}"
        match = _LEDGER_PR_RE.match(str(merge.get("source") or ""))
        if match is None:
            problems.append((ref, "账本合并事件的来源形状不符（失败关闭）"))
            continue
        candidates = [event for event in merges(runtime_events)
                      if str(event.get("source") or "").startswith(f"github:{match['pr']}:")]
        if not candidates:
            problems.append((ref, "运行层没有该 PR 的合并事实（可能同步失败，见同步发现）"))
            continue
        expected = _view(merge, _MERGE_FIELD_KEYS, _MERGE_OUTPUT_KEYS)
        fields = sorted({key for candidate in candidates for key, value in expected.items()
                         if value != _view(candidate, _MERGE_FIELD_KEYS, _MERGE_OUTPUT_KEYS)[key]})
        if fields:
            problems.append((ref, f"账本固定的合并事实与运行层不同（字段：{'、'.join(fields)}）"))
    return [_ledger_finding(ref, reason) for ref, reason in problems]


def _github_chain_findings(auditor, source: str, head, trace, known: bool,
                           ledger_events: dict[str, list[dict]],
                           runtime_events: dict[str, list[dict]]) -> list[dict]:
    """github: 快照链：链头相等即逐字节相同；否则九字段语义核对，不再认「链头在前缀里」（快照按
    内容寻址、事实变了另起新 source，无合法追加）；运行层没有该 source 时按整个事件集合判定种类
    （合并快照由合并事实一致性承担、抽审/逃逸旧快照可被取代、其余失败关闭）；账本没有事件原文
    时不放宽。"""
    ref = f"ledger:{source}/{trace}"
    if not known:
        events = ledger_events.get(source) or []
        if not events:
            return [_ledger_finding(ref, "账本快照在运行层没有事件，且账本没有事件原文可核对")]
        ordered = _by_seq(events)
        if ordered is None:
            return [_ledger_finding(ref, "账本快照事件的 seq 形状不符（失败关闭）")]
        steps = [str(row.get("step")) for row in ordered]
        if steps[0] == "github.merge" and all(step == "github.merge_label" for step in steps[1:]):
            return []  # 合并快照：事实合法变化会另起新 source，核对由合并事实一致性检查承担
        if steps in (["github.audit_sample"], ["github.escape"]):
            return []  # 抽审/逃逸事实本就会被后来的事实取代，旧快照缺失合法
        return [_ledger_finding(ref, "账本快照在运行层没有事件，且种类无法识别（失败关闭）")]
    if head == _chain_head(auditor, source, trace):
        return []
    if not ledger_events.get(source):
        return [_ledger_finding(ref, "账本只固定了链头，没有事件原文可核对，且链头与运行层不一致")]
    if diff := _semantic_diff(ledger_events[source], runtime_events.get(source) or []):
        return [_ledger_finding(ref, f"账本快照与运行层不一致（{diff}）")]
    return []


def _ledger(auditor) -> list[dict]:
    """ledger_mismatch（B118/B121）：ci:/本机链沿用链头与前缀成员核对（合法追加不误报）；github:
    快照链在两个环境各记一份时间戳、链头必然不同，改为语义核对；合并事实独立必检；缺失快照按种类。"""
    doc = getattr(auditor, "ledger_doc", None)
    if not isinstance(doc, dict):
        return []  # 账本缺失/不可解析由 T401 的引用核对报告，不在这里重复
    hashes: dict[str, set] = {}
    for row in auditor.event_rows or []:
        hashes.setdefault(str(row.get("source") or ""), set()).add(row.get("hash"))
    ledger_events = _events_by_source(doc.get("stages") or [], "evidence_kind")
    runtime_events = _events_by_source(auditor.stages, "evidence")
    findings = _merge_fact_findings(ledger_events, runtime_events)
    for chain in doc.get("chains") or []:
        if not isinstance(chain, dict):
            findings.append(_ledger_finding("ledger:chains", "账本链条目形状不符"))
            continue
        source, head, trace = str(chain.get("source") or ""), chain.get("head_hash"), chain.get("trace_id")
        if source.startswith("github:"):
            findings += _github_chain_findings(auditor, source, head, trace, bool(hashes.get(source)),
                                               ledger_events, runtime_events)
        elif not hashes.get(source):
            continue  # 运行层没有该链：链不可得由引用核对/missing_route 报告，不在这里猜
        elif head != _chain_head(auditor, source, trace) and head not in hashes[source]:
            findings.append(_ledger_finding(
                f"ledger:{source}/{trace}", "账本链头不在运行层对应链上（账本与运行层不一致）"))
    return findings


def _chain_head(auditor, source: str, trace_id) -> str | None:
    """运行层该链当前链头；库里没有事件或读取失败返回 None（调用方再按前缀成员兜底）。"""
    try:
        return events_db.chain_head(source, trace_id or auditor.trace)
    except Exception:  # noqa: BLE001  坏库：与 None 同样交由前缀成员判定
        return None


def _approval_mode(findings: list[dict]) -> str | None:
    """被审计仓库的批准方式（与自动合并工作流同一配置）；配置损坏记 configuration_error 并跳过审批核对。"""
    try:
        return policy.platform_outputs()["approval"]
    except Exception as exc:  # noqa: BLE001  平台配置非法是配置错误，不是「none」
        findings.append(_finding("configuration_error", source="local", stage="audit",
                                 ref=".harness/config/checks.toml [platform]",
                                 reason=f"批准方式配置非法（{type(exc).__name__}），审批核对未执行"))
        return None


def check(auditor) -> list[dict]:
    """完整性与防篡改 findings（在 T401 复原核对之后调用；只读，不改复原结果）。"""
    config, findings = load_config()
    mode = _approval_mode(findings)
    facts = _Facts(auditor)
    ref = f"{auditor.repo}#{auditor.pr}"
    findings += _review(facts, config, ref)
    findings += _route(facts, config, ref)
    findings += _record_anchors(facts, config, f"docs/runs@{auditor.merge_sha}")
    findings += _approval(facts, config, mode, ref)
    findings += _chains(auditor, config)
    findings += _ledger(auditor)
    return findings
