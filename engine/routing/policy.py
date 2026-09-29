"""合并路由：决定一个 PR 能否自动合并，每条判定写明理由（目标态设计 9.3、第十五节）。

在 auto-merge 工作流的判定步骤里运行（main 上的定义与代码），只把 PR 当数据读：diff、提交说明、
标签、议题。按顺序判定，任一条不满足即转用户评审：

  1. 风险     risk.py 判为 R0 或 R1（R1 含 r1_checks.py 的加强判定）
  2. 类别     机器按路径与 trailer 判定的类别在 autonomy.toml 中为 L4；提交带 `Task: T<编号>` 时，
              main 上该任务书声明的类别须与机器判定一致，否则按「未分类」处理
  3. 预算     该类最近 window 次合并中的 escape 议题不超过 max_escapes；PR 不带 budget-exceeded 标签
  4. 规模     增删行数（不计黄金快照与运行记录）不超过 autonomy.toml [size] max_lines
  5. 运行记录 实现某份任务书的 PR（run_check.py）：提交都带 `Task:`、有格式完整且 exit 为 ok 的运行记录、
              已完成的 build 轮次不超过任务书 budget.ci_rounds；不是在实现任务书的 PR 不要求
读不到标签、议题或 build 运行时按不满足处理（宁可转人审）。停机不在这里：用 Actions → auto-merge →
Disable workflow（docs/specs/delivery-harness.md §8）。

K3 按 PR 编号哈希每 audit_every 个抽 1 个，合并后由工作流开 audit 议题。

    bin/harness policy --base origin/main --head <sha> --pr <编号> --branch <分支> [--github]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from engine.checks import r1_checks, taskbook
from engine.core.common import ROOT, commit_field, git, path_matches, setting
from engine.routing import risk, run_check

CONTRACT_PATTERNS = ["docs/plans/task-*.md", "docs/templates/**", "docs/plans/backlog.md", "docs/specs/**"]


def ui_patterns() -> list[str]:
    """UI 代码（checks.toml [sources] ui）：改动按 K6 归类，不自动合并。"""
    return list(setting("sources", "ui", []))


BUDGET_LABEL = "budget-exceeded"
ESCAPE_LABEL = "escape"


@dataclass
class Facts:
    risk: risk.RiskReport
    machine_class: str
    declared_class: str | None = None  # 提交里 Task: 指向的任务书声明的类别；没有 Task: 时为 None
    declared_problem: str | None = None  # Task: 无法对应到 main 上的任务书时的原因
    changed_lines: int = 0
    labels: set[str] | None = field(default_factory=set)  # None = 读取失败
    escapes: int | None = 0  # 该类窗口内的逃逸数；None = 读取失败
    window: int = 0  # 窗口内实际找到的合并数
    run_findings: list | None = None  # run_check 的结果；None = 不是在实现任务书


@dataclass
class Rule:
    name: str
    ok: bool
    reason: str


def machine_class(report: risk.RiskReport, defects: bool) -> str:
    """按判级结果、改动路径与 Defect trailer 判定类别（设计 15.1）；任务书的声明只用于交叉核对。"""
    paths = [item.path for item in report.files]
    if report.level == 3:
        return "K7"
    if report.level == 1:
        return "K3"
    if report.level == 2:
        if paths and all(path_matches(path, CONTRACT_PATTERNS) for path in paths):
            return "K0"
        if defects:
            return "K4"
        return "K6" if any(path_matches(path, ui_patterns()) for path in paths) else "K5"
    if any(path_matches(path, ["tests/**"]) for path in paths):
        return "K2"
    if any(path_matches(path, ["docs/plans/task-*.md"]) for path in paths):
        return "K0"
    return "K1"


def audit_sampled(pr: int, every: int) -> bool:
    """按 PR 编号哈希抽样：稳定、可复算，执行方无法挑选。"""
    return every > 0 and int(hashlib.sha256(str(pr).encode()).hexdigest(), 16) % every == 0


def decide(facts: Facts, autonomy: dict) -> list[Rule]:
    rules = []
    level = facts.risk.level
    rules.append(Rule("风险", level <= 1, f"{facts.risk.label}：" + risk.POLICY[level]))

    klass = facts.machine_class
    config = autonomy.get("classes", {}).get(klass, {})
    if facts.declared_problem:
        rules.append(Rule("类别", False, f"未分类：{facts.declared_problem}"))
    elif facts.declared_class and klass != "K0" and facts.declared_class != klass:
        rules.append(Rule("类别", False, f"未分类：任务书声明 {facts.declared_class}，机器判定 {klass}"))
    else:
        level_name = config.get("level", "L3")
        name = config.get("name", "")
        rules.append(Rule("类别", level_name == "L4", f"{klass} {name}：自治等级 {level_name}" + ("" if level_name == "L4" else "，不自动合并")))

    if facts.labels is None or facts.escapes is None:
        rules.append(Rule("预算", False, "读不到 PR 标签或 escape 议题，按超预算处理"))
    elif BUDGET_LABEL in facts.labels:
        rules.append(Rule("预算", False, f"PR 带 {BUDGET_LABEL} 标签"))
    elif "max_escapes" in config:
        limit = config["max_escapes"]
        ok = facts.escapes <= limit
        rules.append(Rule(
            "预算", ok,
            f"{klass} 最近 {facts.window} 次合并（窗口 {config.get('window')}）逃逸 {facts.escapes}，预算 ≤ {limit}"
            + ("" if ok else "：该类自动合并暂停，直到用户在 autonomy.toml 恢复"),
        ))
    else:
        rules.append(Rule("预算", True, f"{klass} 未设误差预算"))

    limit = autonomy.get("size", {}).get("max_lines", 400)
    ok = facts.changed_lines <= limit
    rules.append(Rule("规模", ok, f"增删 {facts.changed_lines} 行，阈值 {limit}" + ("" if ok else "：请拆分")))

    if facts.run_findings is None:
        rules.append(Rule("运行记录", True, "不是在实现任务书的 PR，不要求"))
    else:
        failed = [item for item in facts.run_findings if not item.ok]
        reason = "；".join(f"{item.name}：{item.reason}" for item in (failed or facts.run_findings))
        rules.append(Rule("运行记录", not failed, reason))
    return rules


def render(rules: list[Rule], facts: Facts, audit: bool) -> str:
    auto = all(rule.ok for rule in rules)
    lines = [
        f"### 合并路由：{'自动合并' if auto else '转用户评审'}（{facts.risk.label}，{facts.machine_class}）",
        "",
        "| 判定 | 结果 | 理由 |",
        "|---|---|---|",
    ]
    lines += [f"| {rule.name} | {'✅' if rule.ok else '❌'} | {rule.reason} |" for rule in rules]
    if facts.risk.r1_violations:
        lines += ["", "R1 加强判定未通过："] + [f"- {reason}" for reason in facts.risk.r1_violations]
    if audit:
        lines += ["", f"本 PR 被抽中审计（{facts.machine_class}），合并后开 audit 议题。"]
    lines += ["", "停机：Actions → auto-merge → Disable workflow。"]
    return "\n".join(lines) + "\n"


# ---- 从 git 与 GitHub 读取事实（只读） ----

def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def declared(base: str, head: str, cwd: Path) -> tuple[str | None, str | None]:
    shas = git("rev-list", f"{base}..{head}", cwd=cwd).split()
    tasks = {value.strip() for sha in shas for value in commit_field(sha, "Task", cwd)}
    if not tasks:
        return None, None
    if len(tasks) > 1:
        return None, f"提交指向多个任务书（{'、'.join(sorted(tasks))}）"
    task = tasks.pop()
    match = re.fullmatch(r"T(\d+)", task)
    files = sorted((cwd / "docs" / "plans").glob(f"task-{int(match[1]):03d}-*.md")) if match else []
    if len(files) != 1:
        return None, f"`Task: {task}` 在 main 上找不到唯一的任务书"
    try:
        header, _ = taskbook.parse_header(files[0].read_text(encoding="utf-8"))
    except taskbook.HeaderError as error:
        return None, f"任务书头部无法解析（{error}）"
    return header.get("class"), None


def escapes_in_window(klass: str, window: int, gh=_gh) -> tuple[int, int]:
    """该类最近 window 次合并的 PR 中，被 escape 议题（带 class:<类别> 标签）指认的个数。已关闭的议题照样计数。"""
    merged = json.loads(gh("pr", "list", "--state", "merged", "--label", f"class:{klass}", "--limit", str(window),
                           "--json", "number"))
    numbers = {item["number"] for item in merged}
    issues = json.loads(gh("issue", "list", "--state", "all", "--label", ESCAPE_LABEL, "--label", f"class:{klass}",
                           "--limit", "500", "--json", "number,title,body"))
    blamed = {int(ref) for issue in issues for ref in re.findall(r"#(\d+)", f"{issue['title']}\n{issue['body']}")}
    return len(numbers & blamed), len(numbers)


def branch_rounds(branch: str, gh=_gh) -> int | None:
    try:
        runs = json.loads(gh("run", "list", "--workflow", "build", "--branch", branch, "--limit", "100",
                             "--json", "headSha,status,event"))
    except (RuntimeError, json.JSONDecodeError, OSError):
        return None
    return run_check.ci_rounds([run for run in runs if run.get("event") == "pull_request"])


def gather(base: str, head: str, pr: int | None, cwd: Path = ROOT, autonomy: dict | None = None, gh=_gh,
           branch: str = "") -> Facts:
    autonomy = autonomy or r1_checks.load_autonomy()
    report = risk.classify(base, head, cwd)
    shas = git("rev-list", f"{base}..{head}", cwd=cwd).split()
    defects = any(commit_field(sha, "Defect", cwd) for sha in shas)
    facts = Facts(report, machine_class(report, defects))
    facts.declared_class, facts.declared_problem = declared(base, head, cwd)
    facts.changed_lines = r1_checks.changed_line_count(base, head, cwd, autonomy.get("size", {}).get("exclude", []))
    config = autonomy.get("classes", {}).get(facts.machine_class, {})
    try:
        if pr is None:
            raise RuntimeError("没有关联的 PR")
        facts.labels = {label["name"] for label in json.loads(gh("pr", "view", str(pr), "--json", "labels"))["labels"]}
        if "max_escapes" in config:
            facts.escapes, facts.window = escapes_in_window(facts.machine_class, config.get("window", 20), gh)
    except (RuntimeError, json.JSONDecodeError, KeyError, OSError):
        facts.labels, facts.escapes = None, None
    if run_check.scope(base, head, branch, cwd) is not None:
        facts.run_findings = run_check.check(base, head, branch, cwd, rounds=branch_rounds(branch, gh))
    return facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--branch", default="", help="PR 的分支名（运行记录与 CI 轮次复核用）")
    parser.add_argument("--github", action="store_true", help="写 GITHUB_STEP_SUMMARY 与 GITHUB_OUTPUT")
    args = parser.parse_args(argv)
    autonomy = r1_checks.load_autonomy()
    facts = gather(args.base, args.head, args.pr, autonomy=autonomy, branch=args.branch)
    rules = decide(facts, autonomy)
    auto = all(rule.ok for rule in rules)
    every = autonomy.get("classes", {}).get(facts.machine_class, {}).get("audit_every", 0)
    audit = auto and args.pr is not None and audit_sampled(args.pr, every)
    text = render(rules, facts, audit)
    print(text)
    print(risk.render_markdown(facts.risk))
    if args.github:
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
                handle.write(text + "\n" + risk.render_markdown(facts.risk) + "\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
                handle.write(
                    f"risk={facts.risk.label}\nclass={facts.machine_class}\n"
                    f"auto_merge={'true' if auto else 'false'}\naudit={'true' if audit else 'false'}\n"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
