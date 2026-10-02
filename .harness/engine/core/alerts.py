"""共用告警 API（B46 C4，T205 定义；T403 扩展 CLI 与工作流承载）：在 PR 评论或升级议题上发布带远端标记的告警。

渠道只有 PR/议题评论加 escalation 标签（设计 5，不新增通知渠道）。远端标记是 trace_id、NUL、
reason 的 sha256：同键先查分页评论/带标签议题再更新，不重发；无 PR 时查/建一条升级议题。
publish 只消费结构化既有状态，永不抛异常（C0）：发布失败返回 ok=False 并留本机 alert 观察事件，
不改调用方的判定、退出码与升级。details 只含安全阶段摘要、trace 查询方式与 PR/CI/账本链接。
本机事件库只是记录，不是去重权威：删掉本机库后重发同键告警仍更新同一条远端评论（换机同理）。

reason 只用 C4 稳定键。派发侧两个预警由 rules.toml 的可选 [alerts] 节启用（删除整节即全部关闭）：
最后一轮 CI 在 wait 前预警（总预算 1 则首轮即预警）；守卫拒绝预警按 [alerts] guard_denials_threshold
（每轮被守卫拒绝的工具调用数，一次调用多个拒绝理由算一次）触发，未配置不启用，非正或非法值禁用
并提示一次配置错误，不改执行预算。阈值/末轮条件与安全详情组装都在本模块，dispatch 只接线。

T403 补 CLI 与工作流承载（F3）：`alert` 子命令（cli.py 注册，QUIET_COMMANDS 不追加通用入口事件）
供评审后补之外的告警发布——CI 的 harness/auto-merge 告警 job 与本机手工调用。`--bundle` 读一个
harness-events 事件包（C5 形状，只读不导入），包里出现该 reason 的触发证据才发布；未知失败不自动
告警。工作流里 pull_request 触发的检查保持只读，push main 与 workflow_run 各由指定告警 job 承载
（模板见 templates/.github/workflows/），本模块只提供发布与证据核对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from engine.core import events
from engine.core.common import load_rules

LABEL = "escalation"
MARK = "<!-- harness-alert {} -->"  # 远端标记：trace_id、NUL、reason 的 sha256
# C4 稳定 reason 键（新增必须先经设计方修订共用合同，不造同义短 token）。
REASONS = frozenset((
    "integrity_failure", "review_rejected", "review_error", "dispatch_stall", "dispatch_timeout",
    "dispatch_loop", "dispatch_budget", "ci_last_round", "guard_denials", "budget_exhausted",
    "audit_missing_stage", "audit_anchor_mismatch",
))
_hinted = False  # 配置错误提示每进程一次（stderr 噪音控制；测试可直接复位）


def marker(trace_id: str, reason: str) -> str:
    """远端标记值：trace_id、NUL、reason 的 sha256（C4 去重键，换机后仍靠远端标记去重）。"""
    return hashlib.sha256(f"{trace_id}\0{reason}".encode()).hexdigest()


def dispatch_enabled(rules: dict | None = None) -> bool:
    """派发预警开关：rules.toml 配置了 [alerts] 节即启用；节可选，删除整节即全部关闭。

    读取失败按未配置处理（观察旁路，不影响派发判定与预算）。
    """
    try:
        data = rules if rules is not None else load_rules()
    except Exception:  # noqa: BLE001  观察旁路
        return False
    return isinstance(data, dict) and "alerts" in data


def guard_denials_threshold(rules: dict | None = None) -> int | None:
    """[alerts] guard_denials_threshold：正整数启用守卫拒绝预警；未配置返回 None（不启用）；
    非正或非法值禁用该预警并提示一次配置错误，不改执行预算。"""
    global _hinted
    value = (rules if rules is not None else load_rules()).get("alerts", {}).get("guard_denials_threshold")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if not _hinted:
            _hinted = True
            print(f"harness：rules.toml [alerts] guard_denials_threshold 配置非法（{value!r}），"
                  "守卫拒绝预警已禁用", file=sys.stderr)
        return None
    return value


def guard_denials_exceeded(denied: int, threshold: int | None) -> bool:
    """阈值条件（纯函数）：轮内被拒工具调用数（绝对计数）达到阈值即触发；未配置不触发。"""
    return threshold is not None and denied >= threshold


def is_last_ci_round(round_no: int, ci_rounds: int) -> bool:
    """末轮条件（纯函数）：本轮达到 CI 预算即最后一轮（总预算 1 则首轮即预警），在 wait 前触发。"""
    return ci_rounds >= 1 and round_no >= ci_rounds


def last_round_alert(trace_id: str, task: str, round_no: int, ci_rounds: int, *, pr, gh) -> None:
    """最后一轮 CI 预警：派发在每轮 wait_ci 前调用；未启用派发预警时为空操作。"""
    if dispatch_enabled() and is_last_ci_round(round_no, ci_rounds):
        publish(trace_id, "ci_last_round", pr=pr, task=task, gh=gh)


def guard_round_alert(trace_id: str, task: str, denied: int, *, pr, gh) -> None:
    """守卫拒绝预警：派发在每轮执行方结束后调用；未启用或未达阈值为空操作。"""
    if dispatch_enabled() and guard_denials_exceeded(denied, guard_denials_threshold()):
        publish(trace_id, "guard_denials", pr=pr, task=task, gh=gh)


def stage_lines(trace_id: str) -> list[str]:
    """安全阶段摘要（T201 时间线，只含枚举/计数/短 ID）；读取或组装失败为空列表（观察旁路）。"""
    try:
        from engine.agents import run_timeline  # 延迟导入：core 不在导入期反向依赖 agents
        summary, _head = run_timeline.record_fields(trace_id)
    except Exception:  # noqa: BLE001  观察旁路：摘要失败只少记这一段
        return []
    lines = []
    for item in summary.get("stages") or []:
        round_no = f"，round {item['round']}" if item.get("round") else ""
        lines.append(f"- `{item['stage']}/{item['step']}` {item['status']}"
                     f"（attempt {item.get('attempt', 1)}{round_no}）")
    return lines


def timeline_lines(trace_id: str, pr: int | None = None) -> list[str]:
    """时间线段：trace 与时间线入口（有 PR 指向 PR/CI artifact，无 PR 指向升级议题与 trace 命令）、
    安全阶段摘要；升级包与告警正文复用。"""
    entry = ("本 PR 的 checks 与 CI artifact（事件导出，保留 90 天）" if pr is not None
             else f"本升级议题与 `bin/harness trace {trace_id}`")
    lines = [f"- **trace**：`{trace_id}`；时间线入口：{entry}。"]
    stages = stage_lines(trace_id)
    if stages:
        lines += ["", "### 已发生的阶段（安全摘要）", *stages]
    return lines


def alert_body(trace_id: str, reason: str, *, task: str | None = None, pr: int | None = None,
               details: dict | None = None) -> str:
    """告警正文：远端标记、标题、时间线段与 details 的附加安全行（PR/CI/账本链接由调用方给）。"""
    lines = [MARK.format(marker(trace_id, reason)), f"### 告警：{task or trace_id}（{reason}）", "",
             *timeline_lines(trace_id, pr)]
    extra = (details or {}).get("lines")
    if extra:
        lines += ["", *[line for line in extra if isinstance(line, str)]]
    return "\n".join(lines) + "\n"


def publish(trace_id: str, reason: str, *, pr: int | None = None, task: str | None = None,
            details: dict | None = None, gh=None) -> dict:
    """共用告警发布（C4）：有 PR 在 PR 上评论（带 escalation 标签），无 PR 查/建一条升级议题；
    同键（trace_id+reason 的远端标记）查分页评论/议题后更新，不重发。永不抛异常：失败返回
    {ok: False, target: None, updated: False, error_kind} 并留本机 alert 事件，不改调用方的
    判定、退出码与升级。gh 适配器没有 C4 发布接口（list_comments 缺失，如只评论的最小桩）时
    是空操作：发布不可能成功，不发起远端调用也不留必然失败的本机事件。"""
    result = {"ok": False, "target": None, "updated": False, "error_kind": None}
    if gh is not None and getattr(gh, "list_comments", None) is None:
        return result
    try:
        if reason not in REASONS:
            result["error_kind"] = "unknown_reason"
        else:
            body = alert_body(trace_id, reason, task=task, pr=pr, details=details)
            if pr is not None:
                result["target"] = "pr"
                existing = _marked(gh.list_comments(pr), marker(trace_id, reason), "id")
                if existing is not None:
                    gh.edit_comment(existing, body)
                    result.update(ok=True, updated=True)
                else:
                    gh.comment(pr, body, label=LABEL)
                    result["ok"] = True
            else:
                result["target"] = "issue"
                number = _marked(gh.list_issues(LABEL), marker(trace_id, reason), "number")
                if number is not None:
                    gh.edit_issue(number, body)
                    result.update(ok=True, updated=True)
                else:
                    gh.create_issue(f"告警：{task or trace_id}（{reason}）", body, [LABEL])
                    result["ok"] = True
    except Exception as exc:  # noqa: BLE001  发布异常不传播到原判定（C0）
        result.update(target=None, updated=False, error_kind=type(exc).__name__)
    _record(trace_id, reason, result)
    return result


def _marked(items, mark: str, key: str) -> int | None:
    """分页列表（PR 评论或议题）里正文带该远端标记的条目 id；没有或形状不对为 None。"""
    for item in items or []:
        if isinstance(item, dict) and mark in str(item.get("body") or ""):
            try:
                return int(item[key])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _record(trace_id: str, reason: str, result: dict, status: str | None = None) -> None:
    """本机 alert 观察事件（C1：alert 自己记发布结果）；emit 永不抛，组装失败也只丢弃。"""
    try:
        events.emit(stage="alert", step=reason, status=status or ("ok" if result["ok"] else "error"),
                    trace_id=trace_id,
                    outputs={"target": result["target"], "updated": result["updated"]},
                    error={"kind": result["error_kind"]} if result["error_kind"] else None)
    except Exception:  # noqa: BLE001  观察旁路
        return


# ---- CLI（T403）：cli.py COMMANDS 注册 alert；无证据跳过留 skip 事件，其余经 publish ----

# --bundle 证据签名：(stage, step, status) 精确匹配；audit.finding 再按 outputs.rule 区分。
# 只映射设计 5 里由 CI 事件包承载的四类；审计的链头/锚点一致性发现（chain_invalid、ledger_mismatch）
# 与环节缺失（missing_*）同属设计 5 第七行的两个告警族。其余 reason 没有包内证据形态，配 --bundle
# 按无证据跳过（不发布）。
BUNDLE_TRIGGERS: dict[str, tuple[str, str, str, tuple[str, ...] | None]] = {
    "integrity_failure": ("verify", "integrity", "fail", None),
    "budget_exhausted": ("route", "预算", "fail", None),
    "audit_missing_stage": ("ci", "audit.finding", "fail", ("missing_",)),
    "audit_anchor_mismatch": ("ci", "audit.finding", "fail",
                              ("anchor_mismatch", "chain_invalid", "ledger_mismatch")),
}


def bundle_evidence(bundle: dict, reason: str) -> bool:
    """事件包（C5 形状）里是否有该 reason 的触发证据；包形状不对或未知失败按无证据处理。"""
    trigger = BUNDLE_TRIGGERS.get(reason)
    if trigger is None:
        return False
    stage, step, status, rules = trigger
    for event in bundle.get("events") or []:
        if not isinstance(event, dict):
            continue
        if (event.get("stage"), event.get("step"), event.get("status")) != (stage, step, status):
            continue
        if rules is None or str((event.get("outputs") or {}).get("rule") or "").startswith(rules):
            return True
    return False


def _load_bundle(path: Path) -> dict:
    """读一个事件包：目录（含 harness-events.json）或 JSON 文件；形状不对抛 TypeError。"""
    file = path / "harness-events.json" if path.is_dir() else path
    bundle = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or not isinstance(bundle.get("events"), list):
        raise TypeError(f"{file} 不是 harness-events 事件包")
    return bundle


class GhPublisher:
    """CLI/CI 侧的 C4 发布适配器：直接调用 gh（认证取 GH_TOKEN/GH_REPO 环境），不经 bin/as-agent。

    派发与评审在本机以 Agent 账号发布（dispatch.GitHub 走 as-agent）；工作流告警 job 持工作流自身的
    最小写令牌，本机手工发布用操作者自己的 gh 登录。方法形状与 dispatch.GitHub 的发布接口一致，
    publish 按同一接口调用两者。
    """

    def __init__(self, cwd: Path | str | None = None):
        self.cwd = str(cwd) if cwd is not None else None

    def _run(self, argv: list[str], stdin: str | None = None) -> str:
        result = subprocess.run(["gh", *argv], cwd=self.cwd, input=stdin, capture_output=True, text=True,
                                check=False)
        if result.returncode != 0:
            raise RuntimeError(f"gh {' '.join(argv[:3])} 失败：{result.stderr.strip()[-500:]}")
        return result.stdout.strip()

    def list_comments(self, pr: int) -> list[dict]:
        raw = self._run(["api", f"repos/{{owner}}/{{repo}}/issues/{pr}/comments", "--paginate", "--slurp"])
        return [item for page in json.loads(raw or "[]") for item in page]

    def comment(self, pr: int, body: str, label: str | None = None) -> str:
        url = self._run(["pr", "comment", str(pr), "--body-file", "-"], stdin=body)
        if label:
            self.add_label(pr, label)
        return url

    def add_label(self, pr: int, label: str) -> None:
        try:  # 不存在才创建；已有标签不被改写（与 dispatch.GitHub 同口径）
            self._run(["label", "create", label, "--color", "d93f0b"])
        except RuntimeError:
            pass
        self._run(["pr", "edit", str(pr), "--add-label", label])

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._run(["api", "-X", "PATCH", f"repos/{{owner}}/{{repo}}/issues/comments/{comment_id}",
                   "--input", "-"], stdin=json.dumps({"body": body}))

    def list_issues(self, label: str) -> list[dict]:
        raw = self._run(["issue", "list", "--state", "all", "--label", label, "--limit", "200",
                         "--json", "number,body"])
        return json.loads(raw or "[]")

    def edit_issue(self, number: int, body: str) -> None:
        self._run(["issue", "edit", str(number), "--body-file", "-"], stdin=body)

    def create_issue(self, title: str, body: str, labels: list[str]) -> str:
        argv = ["issue", "create", "--title", title, "--body-file", "-"]
        for label in labels:
            argv += ["--label", label]
        return self._run(argv, stdin=body)


def main(argv: list[str] | None = None, gh=None) -> int:
    """alert 子命令（设计 5）：发布一条告警；退出码只区分参数错误（2），发布失败退出 0。

    发布是观察旁路：gh 失败返回 ok=False 并留本机 alert 事件，不改调用方（工作流步骤/人工 shell）
    的结论；--pr 缺省查/建一条 escalation 升级议题；--trace 缺省取当前追踪 ID（CI 为 PR 分支，
    main 上 main@<短提交>）；--bundle 给出 harness-events 事件包（目录或 JSON）时先核对触发证据，
    无证据不发布（留 skip 事件）；未知 reason 或事件包不可用属参数错误。gh 参数供测试注入。
    """
    parser = argparse.ArgumentParser(prog="alert", description="发布告警（B46 设计 5：PR/议题评论 + escalation 标签）")
    parser.add_argument("reason", help="C4 稳定 reason 键：" + "、".join(sorted(REASONS)))
    parser.add_argument("--pr", type=int, help="告警落在这个 PR；缺省查/建一条 escalation 升级议题")
    parser.add_argument("--trace", default="", help="追踪 ID（缺省当前分支；main 上 main@<短提交>）")
    parser.add_argument("--task", default=None, help="任务编号（标题用；缺省用 trace）")
    parser.add_argument("--bundle", type=Path, default=None,
                        help="harness-events 事件包（目录或 JSON）：先核对触发证据，无证据不发布")
    parser.add_argument("--json", action="store_true", help="打印结果 JSON")
    args = parser.parse_args(argv)
    if args.reason not in REASONS:
        print(f"alert：未知 reason {args.reason!r}（可用：{'、'.join(sorted(REASONS))}）", file=sys.stderr)
        return 2
    trace = args.trace or events.current_trace()
    if args.bundle is not None:
        try:
            bundle = _load_bundle(args.bundle)
        except (OSError, ValueError, TypeError) as error:
            print(f"alert：事件包不可用（{error}）", file=sys.stderr)
            return 2
        if not bundle_evidence(bundle, args.reason):
            _record(trace, args.reason, {"ok": False, "target": None, "updated": False,
                                         "error_kind": None}, status="skip")
            _print_result({"ok": False, "target": None, "updated": False, "error_kind": "no_evidence"},
                          args.json)
            return 0
    result = publish(trace, args.reason, pr=args.pr, task=args.task, gh=gh if gh is not None else GhPublisher())
    _print_result(result, args.json)
    return 0


def _print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    if result["ok"]:
        state = "已更新" if result["updated"] else "已发布"
    elif result["error_kind"] == "no_evidence":
        state = "包内无触发证据，未发布"
    else:
        state = f"发布失败（{result['error_kind']}）"
    print(f"alert：{state}（target={result['target']}）")
