"""周报「事件汇总（B46）」小节（T501）：追加在既有周报文本之后，不改旧小节与其数据来源。

A 主来源只用仓库里可复取的持久数据：本周合并 PR 在 harness-audit 分支上的账本（git show
origin/harness-audit:<合并年份>/<PR号>.json，读取回调可注入以便测试）与当前 checkout 里的运行记录；
B 补充来源只有本机事件库，单列、绝不并入 A，按运行环境启停——CI=true 一律关闭（runner 上的临时库不是
本机历史），非 CI 时先自己预检（只读打开读 PRAGMA user_version；无库与较新版本库都返回空，不能只看
query 是否为空）。读不到或算不出的值写「不可用」加原因，不补造 0；执行方「规则命中数」与本机
「被拒工具调用数」单位与纳入条件都不同，只分别列出，不做任何对账或比较。
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import Counter
from pathlib import Path

from engine.core.common import ROOT, clean_git_env

AUDIT_REF = "origin/harness-audit"  # 账本分支引用（quality 工作流 fetch-depth: 0，runner 上可读）
_NO_LEDGER = {"no-branch": "harness-audit 分支不存在", "absent": "分支上无该文件",
              "json": "JSON 损坏", "error": "读取失败"}


def _parse_utc(value) -> dt.datetime | None:
    """解析 UTC ISO 时间戳（naive 按 UTC）；不合法返回 None。"""
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _pairs(counter: Counter) -> str:
    return "、".join(f"{key} {count}" for key, count in sorted(counter.items()))


def _durations(totals: dict[str, tuple[int, int, int]]) -> str:
    """阶段耗时行：每 stage 的事件数、合计毫秒、最大毫秒。"""
    return "；".join(f"{stage}：事件 {count}、合计 {total} ms、最大 {peak} ms"
                     for stage, (count, total, peak) in sorted(totals.items()))


def _add_duration(totals: dict[str, tuple[int, int, int]], stage, duration) -> None:
    count, total, peak = totals.get(str(stage), (0, 0, 0))
    totals[str(stage)] = (count + 1, total + duration, max(peak, duration))


# ---- A：harness-audit 分支上的账本 ----

def git_show_reader(cwd: Path):
    """缺省账本读取回调：git show origin/harness-audit:<路径>；返回 (状态, 字节|stderr)。"""

    def read(path: str) -> tuple[str, bytes | str]:
        done = subprocess.run(["git", "show", f"{AUDIT_REF}:{path}"], cwd=cwd, capture_output=True,
                              env=clean_git_env(), check=False)
        if done.returncode == 0:
            return "ok", done.stdout
        err = done.stderr.decode("utf-8", "replace")
        if "not a git repository" in err or "invalid object name" in err or "unknown revision" in err:
            return "no-branch", err.strip()
        return ("absent" if "does not exist" in err else "error"), err.strip()

    return read


def load_ledgers(prs: list[dict], read) -> tuple[list[dict], Counter]:
    """逐 PR 读 <mergedAt 年份>/<PR 号>.json；返回（有效账本, 无账本原因计数）。

    无账本（分支不存在、分支上无该文件、读取失败、JSON 损坏）计入原因分类，不影响其余 PR 的统计。
    """
    ledgers: list[dict] = []
    missing: Counter = Counter()
    for pr in prs:
        status, payload = read(f"{str(pr.get('mergedAt') or '')[:4]}/{pr.get('number')}.json")
        ledger = None
        if status == "ok" and isinstance(payload, (bytes, bytearray)):
            try:
                ledger = json.loads(payload)
            except ValueError:
                status = "json"
        if isinstance(ledger, dict):
            ledgers.append(ledger)
        else:
            missing[_NO_LEDGER.get(status, "读取失败")] += 1
    return ledgers, missing


def ledger_stage_durations(ledgers: list[dict]) -> dict[str, tuple[int, int, int]]:
    """账本 stages 里 evidence_kind=event 且 duration_ms 非空的事件按 stage 汇总。

    run_record_summary 条目与运行记录的 executor_seconds 是同一数据，不计入（避免重复）。
    """
    totals: dict[str, tuple[int, int, int]] = {}
    for ledger in ledgers:
        for entry in ledger.get("stages") or []:
            if not isinstance(entry, dict) or entry.get("evidence_kind") != "event":
                continue
            duration = entry.get("duration_ms")
            if isinstance(duration, int) and not isinstance(duration, bool):
                _add_duration(totals, entry.get("stage"), duration)
    return totals


# ---- A：运行记录（当前 checkout 里 ended_at 在窗口内的尝试） ----

def run_record_metrics(records: list[dict]) -> dict:
    """执行方指标：executor_seconds 合计、尝试数（不去重）、exit/escalation 分布、规则命中数、
    缺上下文分类；missing_context_status 异常或缺字段的历史记录计入「覆盖不足」。"""
    exits: Counter = Counter(str(record.get("exit")) for record in records)
    escalations: Counter = Counter("无" if record.get("escalation") is None else str(record["escalation"])
                                   for record in records)
    denials: Counter = Counter()
    categories: Counter = Counter()
    statuses: Counter = Counter()
    for record in records:
        for rule, count in (record.get("guard_denials") or {}).items():
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                denials[str(rule)] += count
        for item in record.get("missing_context") or []:
            if isinstance(item, dict):
                categories[str(item.get("category"))] += 1
        status = record.get("missing_context_status")
        value = str(status) if status in ("reported", "unknown", "invalid") else "覆盖不足"
        statuses[value] += 1
    return {"attempts": len(records), "tasks": len({record.get("task") for record in records}),
            "seconds": sum(record.get("executor_seconds") or 0 for record in records),
            "exits": exits, "escalations": escalations, "denials": denials,
            "categories": categories, "statuses": statuses}


def render_events(week, *, cwd: Path = ROOT, read=None) -> str:
    """渲染事件汇总小节（B46）：A 主来源（账本 + 运行记录）。"""
    ledgers, missing = load_ledgers(week.prs, read if read is not None else git_show_reader(cwd))
    metrics = run_record_metrics(week.records)
    no_records = "不可用（窗口内没有运行记录）"
    exits = _pairs(metrics["exits"]) or no_records
    if not ledgers:
        durations = "不可用（本周没有可用账本）"
    else:
        durations = _durations(ledger_stage_durations(ledgers)) or "不可用（账本里没有带耗时的事件）"
    coverage = (f"覆盖说明：本周合并 PR {len(week.prs)} 个，有账本 {len(ledgers)} 个，"
                f"无账本 {sum(missing.values())} 个" + (f"（{_pairs(missing)}）" if missing else "") + "；"
                f"运行记录读取 {metrics['attempts']} 份尝试记录，涉及 {metrics['tasks']} 个任务"
                "（一个任务可有多次尝试，份数与任务数分开计）。")
    scope = ("范围：账本只覆盖已合并的 PR；运行记录覆盖当前 checkout 中已落盘、ended_at 在窗口内的尝试"
             "（CI 的 checkout 是默认分支，本机运行则是当前所在分支，可能含未合并分支的记录），"
             "含同任务中途失败的尝试，不与本周合并 PR 取交集；A 部分不含设计方一侧的守卫拒绝与本机阶段耗时。")
    lines = [
        "### 事件汇总（B46）",
        "",
        coverage,
        scope,
        "",
        f"- 账本阶段耗时（A）：{durations}",
        (f"- 执行方耗时与轮次（A）：executor_seconds 合计 {metrics['seconds']:g} 秒；"
         f"尝试 {metrics['attempts']} 次；exit 分布：{exits}"),
        (f"- 守卫拒绝（A）：规则命中数合计 {sum(metrics['denials'].values())}"
         f"（按拒绝理由计数，一次被拒的工具调用可命中多条理由）：{_pairs(metrics['denials']) or '无'}；"
         "设计方一侧：不可得（只记在本机）"),
        (f"- 升级原因（A）：escalation 分布：{_pairs(metrics['escalations']) or no_records}；"
         f"exit 分布：{exits}"),
        (f"- 缺上下文分类（A）：{_pairs(metrics['categories']) or '无'}；"
         f"状态分布：{_pairs(metrics['statuses']) or no_records}"
         "（missing_context_status 异常或缺字段的历史记录计入「覆盖不足」）"),
    ]
    return "\n".join(lines) + "\n"
