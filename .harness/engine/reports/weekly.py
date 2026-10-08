"""周报：每周一由 quality 工作流汇总交付指标，写进固定的「每周质量报告」议题（目标态设计 14.1、14.4、10.3、11.4）。

只读数据、不改策略。数据来源：main 上的运行记录（docs/runs/）与质量、变异基线，GitHub 的 PR、议题与 Actions
运行（gh 只读查询）。样本不足时只报计数或标「样本不足」，不报比例（设计 14.5）。

与前 4 周的中位数比较，以下信号翻倍即在顶部标红（设计 10.3）：重试与 CI 轮次、守卫拒绝、升级数、被改动的已有测试、
逃逸登记。每周的原始数据以隐藏注释存在当周评论里，下周据此比较。

    bin/harness weekly                  打印本周报告
    bin/harness weekly --publish        更新「每周质量报告」议题（正文为最新一期，另发一条当周评论）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from engine.checks import r1_checks
from engine.core.common import ROOT, ci_workflows, git, setting
from engine.reports import metrics, weekly_events
from engine.routing import policy, risk

REPORT_TITLE = setting("reports", "weekly_title", "每周质量报告")
REPORT_LABEL = "weekly-report"
DATA_MARK = re.compile(r"<!-- weekly-data (\{.*?\}) -->", re.DOTALL)
SIGNALS = {
    "retries_ci": "重试与 CI 轮次",
    "guard_denials": "守卫拒绝",
    "escalations": "升级数",
    "modified_tests": "被改动的已有测试",
    "escapes": "逃逸登记",
}
# 人工时间的标准分钟（设计 14.1；每月由用户校正）。
MINUTES = {"approve": 3, "change_request": 15, "escalation": 10, "audit": 5}
# 自动合并与批准的机器人账号：GitHub Actions，加上项目的批准 App（checks.toml [reports] bots）。
BOTS = {"github-actions", "app/github-actions", *setting("reports", "bots", [])}
LABELS = {
    "escape": ("d93f0b", "合并后才发现的逃逸缺陷，正文写明引入的 PR"),
    "audit": ("fbca04", "抽样审计"),
    "escalation": ("d93f0b", "执行方或派发脚本升级给设计方或用户"),
    "escalation:needed": ("0e8a16", "升级确有必要"),
    "escalation:unneeded": ("c5def5", "升级本可避免"),
    "budget-exceeded": ("d93f0b", "超出任务预算，不自动合并"),
    REPORT_LABEL: ("5319e7", "每周质量报告"),
}


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def _parse_time(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


@dataclass
class Week:
    end: dt.datetime
    prs: list[dict] = field(default_factory=list)  # 本周合并的 PR
    escapes: list[dict] = field(default_factory=list)  # 本周登记的 escape 议题
    escalations: list[dict] = field(default_factory=list)  # 本周的升级（议题与带标签的 PR）
    audits_closed: int = 0
    records: list[dict] = field(default_factory=list)  # 本周结束的运行记录
    quality_runs: list[dict] = field(default_factory=list)  # 本周 quality 工作流运行
    modified_tests: int = 0
    ci_rounds: dict[int, int] = field(default_factory=dict)  # 任务 PR → 达到全绿的 build 轮次

    @property
    def start(self) -> dt.datetime:
        return self.end - dt.timedelta(days=7)

    @property
    def key(self) -> str:
        year, week, _ = self.end.isocalendar()
        return f"{year}-W{week:02d}"


# ---- 收集（只读） ----

PR_FIELDS = "number,title,labels,mergedAt,createdAt,headRefName,mergedBy,author,reviews,mergeCommit"
# 提交列表只对人审 PR 单独取：列表里连带提交会超出 GitHub GraphQL 的节点上限。


def collect(end: dt.datetime, gh=_gh, cwd: Path = ROOT) -> Week:
    week = Week(end)

    def in_week(item: dict, key: str = "createdAt") -> bool:
        return bool(item.get(key)) and week.start <= _parse_time(item[key]) < end

    def issues(label: str) -> list[dict]:
        return json.loads(gh("issue", "list", "--state", "all", "--label", label, "--limit", "500",
                             "--json", "number,title,body,labels,createdAt,closedAt,state"))

    since = week.start.date().isoformat()
    merged = json.loads(gh("pr", "list", "--state", "merged", "--search", f"merged:>={since}", "--limit", "100",
                           "--json", PR_FIELDS))
    week.prs = [pr for pr in merged if in_week(pr, "mergedAt")]
    for pr in week.prs:
        if not _auto(pr):
            pr["commits"] = json.loads(gh("pr", "view", str(pr["number"]), "--json", "commits"))["commits"]
    week.escapes = [item for item in issues("escape") if in_week(item)]
    week.escalations = [item for item in issues("escalation") if in_week(item)]
    week.escalations += [{"number": pr["number"], "labels": pr["labels"], "createdAt": pr["mergedAt"], "pr": True}
                         for pr in week.prs if "escalation" in _labels(pr)]
    week.audits_closed = sum(1 for item in issues("audit") if in_week(item, "closedAt"))
    runs = json.loads(gh("run", "list", "--workflow", "quality", "--limit", "50", "--json", "event,createdAt,conclusion"))
    week.quality_runs = [run for run in runs if in_week(run)]
    records = load_records(cwd)
    week.records = [record for record in records if in_week(record, "ended_at")]
    dispatched = {record.get("branch") for record in records}
    for pr in week.prs:
        week.modified_tests += _modifies_tests(pr, cwd)
        if pr["headRefName"] in dispatched:
            builds = []
            for workflow in ci_workflows():
                builds += json.loads(gh("run", "list", "--workflow", workflow, "--branch", pr["headRefName"],
                                        "--limit", "100", "--json", "headSha,status,event"))
            week.ci_rounds[pr["number"]] = len({run["headSha"] for run in builds
                                                if run["status"] == "completed" and run["event"] == "pull_request"})
    return week


def load_records(cwd: Path = ROOT) -> list[dict]:
    records = []
    for path in sorted((cwd / "docs" / "runs").glob("task-*/*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return records


def _labels(pr: dict) -> set[str]:
    return {label["name"] for label in pr.get("labels", [])}


def _modifies_tests(pr: dict, cwd: Path) -> int:
    oid = (pr.get("mergeCommit") or {}).get("oid")
    if not oid:
        return 0
    try:
        first, second = git("rev-parse", f"{oid}^1", f"{oid}^2", cwd=cwd).split()
        report = risk.classify(first, second, cwd)
    except (RuntimeError, ValueError):
        return 0
    return int(any("改动已有测试" in flag or "删除已有测试" in flag for flag in report.flags))


# ---- 指标（纯计算） ----

def _ratio(hits: int, total: int, small: int = 5) -> str:
    if total == 0:
        return "样本不足"
    if total < small:
        return f"{hits}/{total}（样本少，只报计数）"
    return f"{hits}/{total} = {hits / total:.0%}"


def _auto(pr: dict) -> bool:
    return (pr.get("mergedBy") or {}).get("login") in BOTS


def _human_reviews(pr: dict) -> list[dict]:
    author = (pr.get("author") or {}).get("login")
    return [review for review in pr.get("reviews", [])
            if (review.get("author") or {}).get("login") not in BOTS | {author}]


def unmodified(pr: dict) -> bool:
    """人审 PR：首次评审后没有新提交、也没有要求修改（设计 14.1）。"""
    reviews = sorted(_human_reviews(pr), key=lambda review: review.get("submittedAt") or "")
    if not reviews:
        return True
    if any(review.get("state") == "CHANGES_REQUESTED" for review in reviews):
        return False
    first = _parse_time(reviews[0].get("submittedAt"))
    return not any(_parse_time(commit.get("committedDate")) > first for commit in pr.get("commits", [])
                   if commit.get("committedDate"))


def compute(week: Week, quality: dict, mutation: dict) -> dict:
    human = [pr for pr in week.prs if not _auto(pr)]
    by_class: dict[str, list[dict]] = {}
    for pr in week.prs:
        klass = next((name.removeprefix("class:") for name in _labels(pr) if name.startswith("class:")), "未标类别")
        by_class.setdefault(klass, []).append(pr)
    reviews = [review for pr in week.prs for review in _human_reviews(pr)]
    approvals = sum(1 for review in reviews if review.get("state") == "APPROVED")
    change_requests = len(reviews) - approvals
    closed_escalations = [item for item in week.escalations if item.get("closedAt")]
    needed = sum(1 for item in week.escalations if "escalation:needed" in _labels(item))
    judged = needed + sum(1 for item in week.escalations if "escalation:unneeded" in _labels(item))
    minutes = (approvals * MINUTES["approve"] + change_requests * MINUTES["change_request"]
               + len(closed_escalations) * MINUTES["escalation"] + week.audits_closed * MINUTES["audit"])
    cost = sum(record.get("cost") or 0 for record in week.records)
    blamed = {int(ref) for issue in week.escapes for ref in re.findall(r"#(\d+)", f"{issue['title']}\n{issue['body']}")}
    qualified = [pr for pr in week.prs if pr["number"] not in blamed]
    denials: dict[str, int] = {}
    for record in week.records:
        for reason, count in (record.get("guard_denials") or {}).items():
            denials[reason] = denials.get(reason, 0) + count
    cycles = []
    for pr in week.prs:
        started = [_parse_time(record["started_at"]) for record in week.records
                   if record.get("branch") == pr["headRefName"] and record.get("started_at")]
        if started:
            cycles.append((_parse_time(pr["mergedAt"]) - min(started)).total_seconds() / 3600)
    handling = [(_parse_time(item["closedAt"]) - _parse_time(item["createdAt"])).total_seconds() / 3600
                for item in closed_escalations if item.get("createdAt")]
    retries = sum(record.get("retries") or 0 for record in week.records)
    scheduled = [run for run in week.quality_runs if run.get("event") == "schedule"]
    return {
        "质量": {
            "无修改合并率": _ratio(sum(unmodified(pr) for pr in human), len(human)),
            "逃逸缺陷率": _ratio(len(week.escapes), len(week.prs)) + "（按登记周）",
            "变更失败率": "样本不足（尚无正式发版）",
            "复杂度趋势": f"超标函数 {quality.get('complex_functions')}，超 800 行文件 {quality.get('files_over_800')}（Swift 待 P8）",
            "变异得分": "、".join(f"{name} {score:.0%}" for name, score in sorted(mutation.items())) or "样本不足",
        },
        "稳定": {"pass^3": "样本不足（评测集待阶段三 P9）"},
        "效率": {
            "合格交付数": f"{len(qualified)}（本周合并且未被逃逸议题指认；14 天回看待数据积累）",
            "交付周期": f"中位 {statistics.median(cycles):.1f} 小时（{len(cycles)} 个派发任务）" if cycles else "样本不足",
            "CI 轮次": (f"中位 {statistics.median(week.ci_rounds.values())} 轮（{len(week.ci_rounds)} 个任务 PR）"
                        if week.ci_rounds else "样本不足"),
        },
        "自治": {
            "人工干预率": f"要求修改 {change_requests} 次，批准 {approvals} 次（{len(week.prs)} 个合并）",
            "升级率": _ratio(len(week.escalations), len(week.records) or len(week.prs)),
            "升级精度": _ratio(needed, judged),
            "升级处理时长": f"中位 {statistics.median(handling):.1f} 小时" if handling else "样本不足",
            "人审覆盖率": "；".join(f"{klass} {_ratio(sum(not _auto(pr) for pr in prs), len(prs))}"
                                  for klass, prs in sorted(by_class.items())) or "样本不足",
        },
        "成本": {
            "每个合格交付的成本": (f"token ${cost:.3f} + 人工约 {minutes} 分钟，合计 {len(qualified)} 个合格交付"
                                   if qualified else "样本不足"),
        },
        "安全": {
            "守卫拒绝": "；".join(f"{reason} {count}" for reason, count in sorted(denials.items())) or "0",
        },
        "运行": {
            "定时质量检查（B30）": f"本周定时触发 {len(scheduled)} 次，手动 {len(week.quality_runs) - len(scheduled)} 次",
        },
        "_signals": {
            "retries_ci": retries + sum(week.ci_rounds.values()),
            "guard_denials": sum(denials.values()),
            "escalations": len(week.escalations),
            "modified_tests": week.modified_tests,
            "escapes": len(week.escapes),
        },
    }


def spikes(current: dict[str, int], history: list[dict[str, int]]) -> list[str]:
    """与前 4 周中位数比较，翻倍即标红；前几周都为 0 时，本周达到 3 才标（避免 0→1 的噪声）；没有历史不标。"""
    flagged = []
    recent = history[-4:]
    if not recent:
        return []  # 第一期没有可比的历史
    for key, name in SIGNALS.items():
        value = current.get(key, 0)
        base = statistics.median([week.get(key, 0) for week in recent]) if recent else 0
        if (base > 0 and value >= 2 * base) or (base == 0 and value >= 3):
            flagged.append(f"🔴 **{name}**：本周 {value}，前 {len(recent)} 周中位 {base}")
    return flagged


def budget_rows(autonomy: dict, gh=_gh) -> list[str]:
    rows = []
    for klass, config in sorted(autonomy.get("classes", {}).items()):
        if "max_escapes" not in config:
            continue
        try:
            used, merged = policy.escapes_in_window(klass, config["window"], gh)
            state = "超支：该类自动合并已暂停" if used > config["max_escapes"] else "正常"
            rows.append(f"| {klass} {config.get('name', '')} | 最近 {merged} 次合并（窗口 {config['window']}） | "
                        f"逃逸 {used} / 预算 {config['max_escapes']} | {state} |")
        except (RuntimeError, json.JSONDecodeError, KeyError):
            rows.append(f"| {klass} | — | 读取失败 | — |")
    try:
        audits = json.loads(gh("issue", "list", "--state", "all", "--label", "audit", "--limit", "500",
                               "--json", "state"))
        closed = sum(1 for item in audits if item["state"] == "CLOSED")
        rows.append(f"| K3 抽审 | 已开 {len(audits)} 个 audit 议题 | 已审完 {closed} 个 | "
                    f"{'满 10 个无问题，可由用户在 autonomy.toml 调低比例' if closed >= 10 else '未满 10 个'} |")
    except (RuntimeError, json.JSONDecodeError, KeyError):
        rows.append("| K3 抽审 | — | 读取失败 | — |")
    return rows


def trial_rows(history_map: dict, week_records: list[dict], gh=_gh) -> list[str]:
    """试跑记录自动汇总（设计 11.4）：每个任务的评审轮次、CI 轮次、升级、人工介入、逃逸。"""
    rows = []
    escapes = json.loads(gh("issue", "list", "--state", "all", "--label", "escape", "--limit", "500",
                            "--json", "title,body"))
    blamed = {int(ref) for issue in escapes for ref in re.findall(r"#(\d+)", f"{issue['title']}\n{issue['body']}")}
    tasks = {task: list(info.get("prs", [])) for task, info in history_map.items() if not task.startswith("_")}
    for record in week_records:
        branch = record.get("branch", "")
        found = json.loads(gh("pr", "list", "--state", "all", "--head", branch, "--json", "number"))
        tasks.setdefault(record["task"], []).extend(item["number"] for item in found)
    for task, numbers in sorted(tasks.items()):
        for number in sorted(set(numbers)):
            try:
                pr = json.loads(gh("pr", "view", str(number), "--json", PR_FIELDS + ",state"))
            except (RuntimeError, json.JSONDecodeError):
                continue
            reviews = _human_reviews(pr)
            changes = sum(1 for review in reviews if review.get("state") != "APPROVED")
            rows.append(f"| {task} | #{number}（{pr.get('state', '').lower()}） | {len(reviews)} | {changes} | "
                        f"{'是' if 'escalation' in _labels(pr) else '否'} | {'是' if number in blamed else '否'} |")
    return rows


def render(week: Week, data: dict, flagged: list[str], budgets: list[str], trials: list[str]) -> str:
    lines = [f"## {REPORT_TITLE} {week.key}（{week.start.date()} – {week.end.date()}）", ""]
    lines += flagged or ["本周没有突增信号。"]
    lines += [""]
    for group, items in data.items():
        if group.startswith("_"):
            continue
        lines += [f"### {group}", "", "| 指标 | 本周 |", "|---|---|"]
        lines += [f"| {name} | {value} |" for name, value in items.items()]
        lines += [""]
    lines += ["### 误差预算", "", "| 类别 | 窗口 | 消耗 | 状态 |", "|---|---|---|---|"] + (budgets or ["| — | — | — | — |"])
    lines += ["", "### 试跑记录（自动汇总）", "", "| 任务 | PR | 人工评审 | 要求修改 | 升级 | 逃逸 |",
              "|---|---|---|---|---|---|"] + (trials or ["| — | — | — | — | — | — |"])
    lines += [""] + ([metrics.baseline_line(), ""] if metrics.baseline_line() else [])
    lines += ["错误分析：由不是本周主要设计方的评审方按设计 11.1 做，结论存 `docs/review/weekly/<年-周>.md`。", ""]
    lines += [f"<!-- weekly-data {json.dumps({'week': week.key, **data['_signals']}, ensure_ascii=False)} -->"]
    return "\n".join(lines) + "\n"


# ---- 发布 ----

def history_from_comments(comments: list[dict], current_key: str) -> list[dict]:
    weeks = {}
    for comment in comments:
        match = DATA_MARK.search(comment.get("body", ""))
        if match:
            data = json.loads(match[1])
            if data.get("week") != current_key:
                weeks[data["week"]] = data
    return [weeks[key] for key in sorted(weeks)]


def ensure_labels(gh=_gh) -> None:
    for name, (color, description) in LABELS.items():
        try:
            gh("label", "create", name, "--color", color, "--description", description)
        except RuntimeError:
            pass  # 已存在：不改写


def report_issue(gh=_gh) -> dict | None:
    found = json.loads(gh("issue", "list", "--state", "all", "--label", REPORT_LABEL, "--limit", "5",
                          "--json", "number,title"))
    return next((item for item in found if item["title"] == REPORT_TITLE), None)


def publish(text: str, gh=_gh) -> int:
    issue = report_issue(gh)
    if issue is None:
        url = gh("issue", "create", "--title", REPORT_TITLE, "--label", REPORT_LABEL, "--body", text).strip()
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    else:
        number = issue["number"]
        gh("issue", "edit", str(number), "--body", text)
    gh("issue", "comment", str(number), "--body", text)
    return number


def build(end: dt.datetime, gh=_gh, cwd: Path = ROOT, comments: list[dict] | None = None) -> str:
    week = collect(end, gh, cwd)
    quality = json.loads((cwd / ".harness" / "state" / "quality-baseline.json").read_text())
    mutation = json.loads((cwd / ".harness" / "state" / "mutation-baseline.json").read_text())
    data = compute(week, quality, mutation)
    history = history_from_comments(comments or [], week.key)
    history_map = json.loads((cwd / "docs" / "runs" / "history.json").read_text()) \
        if (cwd / "docs" / "runs" / "history.json").exists() else {}
    # 事件汇总（B46）只追加在旧文本之后：旧小节的文本、数据来源与 weekly-data 注释不变。
    return render(week, data, spikes(data["_signals"], history), budget_rows(r1_checks.load_autonomy(), gh),
                  trial_rows(history_map, week.records, gh)) + weekly_events.render_events(week, cwd=cwd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--publish", action="store_true", help="更新「每周质量报告」议题")
    args = parser.parse_args(argv)
    end = dt.datetime.now(dt.UTC)
    comments = []
    issue = report_issue()
    if issue is not None:
        comments = json.loads(_gh("issue", "view", str(issue["number"]), "--json", "comments"))["comments"]
    text = build(end, comments=comments)
    print(text)
    if args.publish:
        ensure_labels()
        print(f"已更新议题 #{publish(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
