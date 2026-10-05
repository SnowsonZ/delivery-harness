"""合并路由：决定一个 PR 能否自动合并，每条判定写明理由（目标态设计 9.3、第十五节）。

在 auto-merge 工作流的判定步骤里运行（main 上的定义与代码），只把 PR 当数据读：diff、提交说明、
标签、议题。按顺序判定，任一条不满足即转用户评审：

  1. 风险     risk.py 判为 R0 或 R1（R1 含 r1_checks.py 的加强判定）；R2 的合同制路径例外：候选 PR
              独立评审与设计方复核标记都通过时放行（signals.py，设计 2026-10-03 第 4 节）
  2. 类别     机器按路径与 trailer 判定的类别在 autonomy.toml 中为 L4；提交带 `Task: T<编号>` 时，
              main 上该任务书声明的类别须与机器判定一致，否则按「未分类」处理
  3. 预算     该类最近 window 次合并中的 escape 议题不超过 max_escapes；PR 不带 budget-exceeded 标签
  4. 规模     增删行数（不计黄金快照与运行记录）不超过 autonomy.toml [size] max_lines
  5. 运行记录 实现某份任务书的 PR（run_check.py）：提交都带 `Task:`、有格式完整且 exit 为 ok 的运行记录、
              已完成的 CI 轮次不超过任务书 budget.ci_rounds；不是在实现任务书的 PR 不要求
读不到标签、议题或 CI 运行时按不满足处理（宁可转人审）。停机不在这里：用 Actions → auto-merge →
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
from engine.core import events
from engine.core.common import (
    ROOT,
    RULES_PATH,
    ConfigError,
    changed_files,
    ci_workflows,
    commit_field,
    git,
    path_matches,
    setting,
)
from engine.routing import risk, run_check, signals


def contract_patterns() -> list[str]:
    """合同路径（rules.toml [risk]）：taskbooks 与 contracts 的并集，两者同时决定 K0（合同）类别。"""
    return taskbook_patterns() + list(setting("risk", "contracts", [], source="rules"))


def taskbook_patterns() -> list[str]:
    """任务书路径（rules.toml [risk] taskbooks）。"""
    return list(setting("risk", "taskbooks", [], source="rules"))


def ui_patterns() -> list[str]:
    """UI 代码（checks.toml [sources] ui）：改动按 K6 归类，不自动合并。"""
    return list(setting("sources", "ui", []))


APPROVAL_MODES = ("app", "none")


def platform_outputs() -> dict[str, str]:
    """checks.toml [platform]：自动合并工作流的批准方式与 App 变量名。approval = "app"（默认，两个账号 + 批准 App）
    或 "none"（单账号：ruleset 不要求批准，工作流用 GITHUB_TOKEN 合并，风险见 SECURITY.md）。
    批准与分支同步用两个不同的 App（README「Platform setup」）：ruleset 要求最后一次推送由推送者以外的人
    批准，同一个 App 先同步分支再批准会让它的批准失效；同步键未配置时工作流不同步、只评论提示。"""
    approval = setting("platform", "approval", "app")
    if approval not in APPROVAL_MODES:
        raise ConfigError(f".harness/config/checks.toml 的 [platform] approval 只能是 {' 或 '.join(APPROVAL_MODES)}，得到 {approval!r}")
    return {
        "approval": approval,
        "app_client_id_var": setting("platform", "app_client_id_var", "HARNESS_APP_CLIENT_ID"),
        "app_private_key_secret": setting("platform", "app_private_key_secret", "HARNESS_APP_PRIVATE_KEY"),
        "sync_app_client_id_var": setting("platform", "sync_app_client_id_var", "HARNESS_SYNC_APP_CLIENT_ID"),
        "sync_app_private_key_secret": setting("platform", "sync_app_private_key_secret", "HARNESS_SYNC_APP_PRIVATE_KEY"),
        "environment": setting("platform", "environment", "harness-auto-merge"),
    }


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
    contract: bool = False  # R2 合同制路径候选（设计第 4 节）
    review: tuple[str, str] = ("", "")  # 独立评审 (ok|fail|missing, 理由)
    signoff: tuple[str, str] = ("", "")  # 设计方复核 (ok|fail|missing, 理由)
    degraded: bool = False  # 同家评审降级（设计 3.1）：自动合并后强制开 audit 议题


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
        if paths and all(path_matches(path, contract_patterns()) for path in paths):
            return "K0"
        if defects:
            return "K4"
        return "K6" if any(path_matches(path, ui_patterns()) for path in paths) else "K5"
    if any(path_matches(path, ["tests/**"]) for path in paths):
        return "K2"
    if any(path_matches(path, taskbook_patterns()) for path in paths):
        return "K0"
    return "K1"


def audit_sampled(pr: int, every: int) -> bool:
    """按 PR 编号哈希抽样：稳定、可复算，执行方无法挑选。"""
    return every > 0 and int(hashlib.sha256(str(pr).encode()).hexdigest(), 16) % every == 0


def risk_rule(facts: Facts) -> Rule:
    """「风险」规则：R0/R1 照旧；R3 与 R2 非候选照旧；R2 合同制候选看评审与复核标记（设计第 4 节）。"""
    level = facts.risk.level
    if level <= 1 or not facts.contract:
        return Rule("风险", level <= 1, f"{facts.risk.label}：" + risk.POLICY[level])
    ok = facts.review[0] == "ok" and facts.signoff[0] == "ok"
    reason = f"R2：合同制路径——独立评审 {facts.review[0]}，设计方复核 {facts.signoff[0]}"
    causes = []
    if facts.review[0] != "ok":
        causes.append(f"独立评审 {facts.review[0]}（{facts.review[1]}）")
    if facts.signoff[0] != "ok":
        causes.append(f"设计方复核 {facts.signoff[0]}（{facts.signoff[1]}）")
    if causes:
        reason += "：" + "；".join(causes)
    if facts.degraded:
        reason += "；同家评审（降级）"
    return Rule("风险", ok, reason)


def decide(facts: Facts, autonomy: dict) -> list[Rule]:
    rules = [risk_rule(facts)]

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


def pending_marker(rules: list[Rule], facts: Facts) -> str:
    """等待标记：其余四条规则全部通过，且「风险」不通过的惟一原因是缺标记时，输出 review/signoff。
    只要有任何一条 fail 或其他规则不通过就为空，照常请求所有者评审——例外由人决定，不能被「等待中」遮住。"""
    if not facts.contract:
        return ""
    risk_rule = next((rule for rule in rules if rule.name == "风险"), None)
    if risk_rule is None or risk_rule.ok or any(not rule.ok for rule in rules if rule.name != "风险"):
        return ""
    if facts.review[0] == "fail" or facts.signoff[0] == "fail":
        return ""
    if facts.review[0] == "missing":
        return "review"
    if facts.signoff[0] == "missing":
        return "signoff"
    return ""


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
    pending = pending_marker(rules, facts)
    if pending:
        lines += ["", f"等待：{'独立评审' if pending == 'review' else '设计方复核'}"]
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
    """该分支已完成的 CI 轮次：ci_workflows 各工作流的 pull_request 运行，按不同的 head 提交合计。"""
    try:
        runs = []
        for workflow in ci_workflows():
            runs += json.loads(gh("run", "list", "--workflow", workflow, "--branch", branch, "--limit", "100",
                                  "--json", "headSha,status,event"))
    except (RuntimeError, json.JSONDecodeError, OSError):
        return None
    return run_check.ci_rounds([run for run in runs if run.get("event") == "pull_request"])


def _contract_candidate(facts: Facts, target: run_check.Scope | None, pr: int | None, autonomy: dict) -> bool:
    """R2 合同制路径候选（设计第 4 节）：风险 R2、任务 PR（任务书已在 main 上，不随本 PR 修改）、
    本 PR 全部改动路径都在 [contract_route] allowed 白名单内（键缺失或为空时没有任何候选，安全缺省）、
    机器判定类别在 autonomy 里为 L4、有 PR 编号。白名单只列实现路径：任务书、规格、AGENTS.md、护栏
    等任何未列出的路径都会让 PR 转人审。"""
    if facts.risk.level != 2 or pr is None or target is None or target.in_pr:
        return False
    allowed = autonomy.get("contract_route", {}).get("allowed", [])
    paths = [item.path for item in facts.risk.files]
    if not isinstance(allowed, list) or not allowed or not paths:
        return False
    if not all(path_matches(path, allowed) for path in paths):
        return False
    config = autonomy.get("classes", {}).get(facts.machine_class, {})
    return config.get("level", "L3") == "L4"


def _contract_signals(base: str, head: str, pr: int, cwd: Path, gh) -> tuple[tuple[str, str], tuple[str, str], bool]:
    """读候选 PR 的评论并判评审、复核与同家降级（设计第 4 节第 4–5 条、3.1 节）。

    只对候选 PR 调用一次 gh pr view --json comments；读取失败按 fail 处理，不抛异常。
    """
    login = setting("identity", "agent_login")
    if not login:
        return ("fail", "未配置 agent_login"), ("fail", "未配置 agent_login"), False
    try:
        comments = json.loads(gh("pr", "view", str(pr), "--json", "comments"))["comments"]
    except (RuntimeError, json.JSONDecodeError, KeyError, OSError):
        return ("fail", "读不到评论"), ("fail", "读不到评论"), False
    review = signals.review_status(comments, login, head, base, cwd)
    signoff = signals.signoff_status(comments, login, head, base, cwd)
    degraded = review[0] == "ok" and _same_family_review(comments, login, head, base, cwd)
    return review, signoff, degraded


def _record_models(base: str, head: str, cwd: Path) -> list[str | None]:
    """本 PR 新增或修改的运行记录（docs/runs/<任务>/<序号>.json）里的 gen_ai.request.model，每条记录一项。

    模型缺失、为空或记录读不出来时记为 None（保留「未知」，由调用方按降级处理），不丢弃。
    """
    models: list[str | None] = []
    for status, path in changed_files(base, head, cwd):
        if status == "D" or not run_check.RECORD_PATH.match(path):
            continue
        try:
            record = json.loads(git("show", f"{head}:{path}", cwd=cwd))
        except (json.JSONDecodeError, RuntimeError):
            models.append(None)
            continue
        model = record.get("gen_ai.request.model") if isinstance(record, dict) else None
        models.append(str(model) if model else None)
    return models


def _same_family_review(comments: list[dict], login: str, head: str, base: str, cwd: Path) -> bool:
    """同家评审（设计 3.1）：评审模型取审计标记的 model（缺标记/为 null/model_basis 为 unknown 时按 None），
    与本 PR 运行记录里全部模型的家族比较；相同，或任一方为 None，就判为降级。"""
    family = signals.model_family(signals.audit_model(comments, login, head, base, cwd))
    families = {signals.model_family(model) for model in _record_models(base, head, cwd)}
    return family is None or not families or None in families or family in families


def gather(base: str, head: str, pr: int | None, cwd: Path = ROOT, autonomy: dict | None = None, gh=_gh,
           branch: str = "", trace: str | None = None) -> Facts:
    autonomy = autonomy or r1_checks.load_autonomy()
    report = risk.classify(base, head, cwd, trace_id=trace)
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
    target = run_check.scope(base, head, branch, cwd)
    if target is not None:
        facts.run_findings = run_check.check(base, head, branch, cwd, rounds=branch_rounds(branch, gh))
    facts.contract = _contract_candidate(facts, target, pr, autonomy)
    if facts.contract:
        facts.review, facts.signoff, facts.degraded = _contract_signals(base, head, pr, cwd, gh)
    return facts


def _event_trace(pr: int | None, branch: str) -> str | None:
    """事件 trace：--branch 优先；其次有 PR 时取 PR API 的 headRefName（不读 PR 正文）；否则 current_trace()。

    只在事件开启时解析；查询失败回退 current_trace()，不改变原有判定与调用序列。
    """
    if not events.enabled():
        return None
    if branch:
        return branch
    if pr is not None:
        try:
            name = json.loads(_gh("pr", "view", str(pr), "--json", "headRefName")).get("headRefName")
            if name:
                return str(name)
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, OSError):
            pass
    return events.current_trace()


def _config_refs() -> list[dict]:
    """main 上 autonomy.toml 与 rules.toml 的带提交引用（路径@HEAD + 内容 sha256）；单侧失败只少记一条。"""
    refs = []
    for kind, path in (("autonomy", r1_checks.AUTONOMY_PATH), ("rules", RULES_PATH)):
        entry = _config_ref(kind, path)
        if entry:
            refs.append(entry)
    return refs


def _config_ref(kind: str, path: Path) -> dict | None:
    """单个配置文件的带提交引用；读取或定位失败返回 None（观察旁路）。"""
    try:
        content = path.read_bytes()
        rev = git("rev-parse", "HEAD", cwd=path.parent)
        return {"kind": kind, "ref": f"{path.relative_to(path.parents[2]).as_posix()}@{rev}",
                "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    except Exception:  # noqa: BLE001  观察旁路：引用准备失败只少记这条引用
        return None


def _emit_route(trace: str | None, facts: Facts, rules: list[Rule], auto: bool, audit: bool) -> None:
    """路由观察事件（C1：facts、每条规则、result）；事件不进判定，配置引用读取失败只少记引用。"""
    if not events.enabled():
        return
    try:
        refs = _config_refs()
    except Exception:  # noqa: BLE001  观察旁路：引用准备失败不影响判定与输出
        refs = []
    events.emit("route", "facts", "ok", trace_id=trace, inputs=refs,
                outputs={"risk": facts.risk.label, "machine_class": facts.machine_class,
                         "declared_class": facts.declared_class, "changed_lines": facts.changed_lines,
                         "escapes": facts.escapes, "window": facts.window})
    for rule in rules:
        events.emit("route", rule.name, "ok" if rule.ok else "fail", trace_id=trace, inputs=refs,
                    outputs={"ok": rule.ok},
                    decision={"by": "policy", "rule": rule.name, "reason": rule.reason})
    try:
        approval = platform_outputs()["approval"]
    except Exception:  # noqa: BLE001  观察旁路：批准方式配置读不到就不记这个字段
        approval = None
    events.emit("route", "result", "ok" if auto else "deny", trace_id=trace, inputs=refs,
                outputs={"auto_merge": auto, "audit": audit, "approval": approval},
                decision={"by": "policy", "rule": "result", "reason": "自动合并" if auto else "转用户评审"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--branch", default="", help="PR 的分支名（运行记录与 CI 轮次复核用）")
    parser.add_argument("--github", action="store_true", help="写 GITHUB_STEP_SUMMARY 与 GITHUB_OUTPUT")
    args = parser.parse_args(argv)
    autonomy = r1_checks.load_autonomy()
    trace = _event_trace(args.pr, args.branch)
    facts = gather(args.base, args.head, args.pr, autonomy=autonomy, branch=args.branch, trace=trace)
    rules = decide(facts, autonomy)
    auto = all(rule.ok for rule in rules)
    every = autonomy.get("classes", {}).get(facts.machine_class, {}).get("audit_every", 0)
    audit = auto and args.pr is not None and (audit_sampled(args.pr, every) or facts.degraded)
    pending = pending_marker(rules, facts)
    _emit_route(trace, facts, rules, auto, audit)
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
                    f"pending={pending}\n"
                )
                handle.writelines(f"{key}={value}\n" for key, value in platform_outputs().items())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
