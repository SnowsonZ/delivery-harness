"""按改动路径判定风险等级（R0–R3），不由执行者自报。规则在 .harness/config/rules.toml [risk]。

    R0  说明性文档、只新增测试                 门禁全绿即可自动合并
    R1  声明为行为不变的重构并通过机器核对     自动合并 + 抽样审计
    R2  产品代码、现役规格、改动已有测试       评审方评审 + 用户看证据包后合并
    R3  护栏、CI 与发布、迁移、隐私、用户配置   必须由用户批准

文档中，模板与待办清单是合同，按 R2；任务书按头部的类别判定（engine/checks/taskbook.py）：护栏与流程（K7）、
发版（K8）、architecture: true 的任务书按 R2，其余按 R0（准入由 CI 中的 verify 检查）。

R1 需要同时满足：范围内每个提交都带 `Risk: R1` trailer；最高等级只来自产品代码路径；
已有测试与黄金快照零改动；以及 r1_checks.py 的加强判定（被改函数签名不变、无新依赖与迁移、
不超规模阈值，2026-09-28 决定 2）。任何一条不满足都按 R2。变异得分不降由 build 的 harness job 核对。

    bin/harness risk --base origin/main [--head HEAD] [--github]
"""

from __future__ import annotations

import argparse
import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from engine.checks import r1_checks, taskbook
from engine.core import events
from engine.core.common import (
    ROOT,
    added_line_count,
    added_lines,
    changed_files,
    commit_field,
    git,
    load_rules,
    path_matches,
    removed_line_count,
    setting,
)

POLICY = {
    0: "门禁全绿即可自动合并",
    1: "门禁全绿可自动合并，合并后抽样审计",
    2: "评审方评审 + 用户看证据包后合并",
    3: "必须由用户批准后合并",
}


def code_patterns() -> list[str]:
    """产品代码（checks.toml [sources] code）。"""
    return list(setting("sources", "code", []))


# 产品代码不该引用测试框架：在 import 时替换断言即可让已有测试失效（评审 PR7-R7）。
TEST_FRAMEWORK = re.compile(r"\b(unittest|TestCase|pytest|XCTest)\b")


@dataclass
class FileRisk:
    path: str
    status: str
    level: int
    reason: str


@dataclass
class RiskReport:
    files: list[FileRisk] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    claimed_r1: bool = False
    r1_violations: list[str] = field(default_factory=list)
    level: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"R{self.level}"


def classify_file(status: str, path: str, base: str, head: str, rules: dict, cwd: Path,
                  *, trace_id: str | None = None) -> tuple[FileRisk, str | None]:
    """判定单个文件的等级；trace_id 非空时附带写逐文件观察事件（路径、命中规则、等级），不影响返回值。"""
    risk = rules["risk"]

    def record(rule: str, item: FileRisk, flag: str | None) -> tuple[FileRisk, str | None]:
        events.emit("route", "risk.file", "ok", trace_id=trace_id,
                    outputs={"path": item.path, "status": item.status, "level": item.level},
                    decision={"by": "risk", "rule": rule, "reason": item.reason})
        return item, flag

    if path_matches(path, risk["golden"]):
        if status == "A":
            return record("golden", FileRisk(path, status, 0, "新增黄金快照（新增测试）"), None)
        return record("golden", FileRisk(path, status, 2, "黄金快照改动 = 行为变化"), f"黄金快照改动：`{path}`")
    if path_matches(path, risk["tests"]):
        if status == "A":
            return record("tests", FileRisk(path, status, 0, "新增测试"), None)
        if status == "D":
            return record("tests", FileRisk(path, status, 2, "删除已有测试"), f"删除已有测试：`{path}`")
        removed = removed_line_count(base, head, path, cwd)
        if removed == 0:
            return record("tests", FileRisk(path, status, 0, "只在已有测试文件中追加"), None)
        return record("tests",
                      FileRisk(path, status, 2, f"改动已有测试（删改 {removed} 行）"),
                      f"改动已有测试：`{path}`（删改 {removed} 行），判定器被修改须由评审方确认")
    shrinking = (
        status == "M"
        and path_matches(path, risk.get("shrink_only", []))
        and not added_line_count(base, head, path, cwd)
        and removed_line_count(base, head, path, cwd)
    )
    if shrinking:
        return record("shrink_only", FileRisk(path, status, 0, "只能缩减的清单被缩减"), None)
    growing = (
        status == "M"
        and path_matches(path, risk.get("grow_only_cases", []))
        and replay_cases_grew_only(base, head, path, cwd)
    )
    if growing:
        return record("grow_only_cases", FileRisk(path, status, 2, "回放清单只追加新用例"), None)
    pattern = path_matches(path, risk["r3"])
    if pattern:
        return record(pattern, FileRisk(path, status, 3, f"命中 R3 规则 `{pattern}`"), None)
    pattern = path_matches(path, risk.get("contracts", []))
    if pattern:
        return record(pattern, FileRisk(path, status, 2, f"模板与待办清单须经用户审（`{pattern}`）"), None)
    if path_matches(path, risk.get("taskbooks", [])):
        return record("taskbook", classify_taskbook(status, path, head, cwd), None)
    pattern = path_matches(path, risk["r0"])
    if pattern:
        return record(pattern, FileRisk(path, status, 0, f"命中 R0 规则 `{pattern}`"), None)
    pattern = path_matches(path, risk["r2"])
    if pattern:
        return record(pattern, FileRisk(path, status, 2, f"命中 R2 规则 `{pattern}`"), None)
    return record("unclassified", FileRisk(path, status, 2, "未归类路径按 R2"), None)


def _cases_value(stmt: ast.stmt) -> ast.expr | None:
    """顶层语句是给名字 ``CASES`` 的赋值（含带注解赋值）时返回值表达式，其余返回 None。"""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        name = stmt.targets[0].id
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        name = stmt.target.id
    else:
        return None
    return stmt.value if name == "CASES" else None


def _single_cases_index(stmts: list[ast.stmt]) -> int | None:
    """``CASES`` 赋值语句在顶层语句里的下标；不存在或出现多次返回 None。"""
    index = [i for i, stmt in enumerate(stmts) if _cases_value(stmt) is not None]
    return index[0] if len(index) == 1 else None


def _literal_case_call(node: ast.expr) -> bool:
    """清单项是否为对名字 ``Case`` 的调用：无位置参数，只有具名关键字参数，且每个参数值都是字面量。"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "Case":
        return False
    if node.args or any(keyword.arg is None for keyword in node.keywords):
        return False  # 位置参数或 ** 解包不算「只有关键字参数」
    for keyword in node.keywords:
        try:
            ast.literal_eval(keyword.value)
        except Exception:  # noqa: BLE001  函数调用、f 字符串、名字引用等非字面量一律判不成立
            return False
    return True


def replay_cases_grew_only(base: str, head: str, path: str, cwd: Path) -> bool:
    """回放清单是否只向 ``CASES`` 末尾追加了全字面量的 ``Case(...)``（rules.toml [risk] grow_only_cases）。

    只用 ``ast.parse`` 读 base 与 head 两个版本的文件内容，不执行 PR 内容（判级时 PR 只能当数据读，
    见 engine/core/cases.py 的说明）。其余顶层语句（``BASELINE``、``GUARDED``、``DEFERRED``、import、
    文档字符串）必须 ``ast.dump`` 完全相同——在 ``DEFERRED``、``GUARDED`` 里新增一行同样是「只增行」，
    那是削弱护栏，必须拦下。任何一步失败（含读取或解析异常）都返回 False，不抛异常。
    """
    try:
        base_stmts = ast.parse(git("show", f"{base}:{path}", cwd=cwd)).body
        head_stmts = ast.parse(git("show", f"{head}:{path}", cwd=cwd)).body
        if len(base_stmts) != len(head_stmts):
            return False
        index = _single_cases_index(base_stmts)
        if index is None or index != _single_cases_index(head_stmts):
            return False
        for offset, (base_stmt, head_stmt) in enumerate(zip(base_stmts, head_stmts)):
            if offset == index:
                continue
            if ast.dump(base_stmt) != ast.dump(head_stmt):
                return False  # BASELINE、GUARDED、DEFERRED、import、文档字符串都不得改动
        base_list, head_list = _cases_value(base_stmts[index]), _cases_value(head_stmts[index])
        if not isinstance(base_list, ast.List) or not isinstance(head_list, ast.List):
            return False
        if len(head_list.elts) <= len(base_list.elts):
            return False
        kept = len(base_list.elts)
        if [ast.dump(item) for item in base_list.elts] != [ast.dump(item) for item in head_list.elts[:kept]]:
            return False  # 已有用例被修改、删除或调换顺序
        return all(_literal_case_call(item) for item in head_list.elts[kept:])
    except Exception:  # noqa: BLE001  base 没有该文件、语法错误等读取与解析失败一律按不成立处理
        return False


def classify_taskbook(status: str, path: str, head: str, cwd: Path) -> FileRisk:
    """任务书按类别判级（2026-09-28 决定 1）。只读 head 上的文件内容，不执行 PR 的代码。"""
    if status == "D":
        return FileRisk(path, status, 2, "删除任务书")
    try:
        header, _ = taskbook.parse_header(git("show", f"{head}:{path}", cwd=cwd))
    except (taskbook.HeaderError, RuntimeError) as error:
        return FileRisk(path, status, 2, f"任务书头部无法解析（{error}）")
    errors = taskbook.check_header(header, path)
    if errors:
        return FileRisk(path, status, 2, f"任务书头部不合格（{errors[0]}）")
    report = taskbook.Report(path, header)
    if report.needs_user_review:
        kind = "架构级" if header.get("architecture") is True else f"{header['class']} 护栏、流程或发版"
        return FileRisk(path, status, 2, f"{kind}任务书须经用户审")
    return FileRisk(path, status, 0, f"{header['class']} 任务书，准入检查由 verify 执行")


def commits_claim_r1(base: str, head: str, cwd: Path) -> bool:
    shas = git("rev-list", f"{base}..{head}", cwd=cwd).split()
    if not shas:
        return False
    for sha in shas:
        if "R1" not in commit_field(sha, "Risk", cwd):
            return False
    return True


def classify(base: str, head: str = "HEAD", cwd: Path = ROOT, rules: dict | None = None, *,
             trace_id: str | None = None) -> RiskReport:
    """判定整份改动的风险；trace_id 给出时事件用它，缺省由 emit 回退 current_trace()（旧调用仍有效）。

    base 引用不存在（空仓库首个 PR、fresh clone 还没有 origin/main）时改动无法与 base 对比：接到
    r1_checks 的「无法核验」结果按既有降级口径处理，取 R2 并给出可读理由。
    """
    rules = rules or load_rules()
    report = RiskReport()
    if r1_checks.base_missing(base, cwd):
        reason = r1_checks.unverifiable_reason(base)
        report.level = 2
        report.r1_violations = [reason]
        report.notes.append(f"{reason}，改动无法与 base 对比，逐文件判定与 r1 加强判定都不可用 → 按降级口径取 R2")
        _emit_summary(report, base, head, trace_id)
        return report
    for status, path in changed_files(base, head, cwd):
        file_risk, flag = classify_file(status, path, base, head, rules, cwd, trace_id=trace_id)
        report.files.append(file_risk)
        if flag:
            report.flags.append(flag)
    guarded = [item.path for item in report.files if item.level == 3]
    if guarded:
        shown = "、".join(f"`{path}`" for path in guarded[:8])
        more = f" 等 {len(guarded)} 个" if len(guarded) > 8 else ""
        report.flags.append(f"改动护栏、CI、发布或高风险路径（R3，需用户批准）：{shown}{more}")
    report.level = max((item.level for item in report.files), default=0)
    touching = sorted(
        path
        for path, lines in added_lines(base, head, cwd).items()
        if path_matches(path, code_patterns()) and any(TEST_FRAMEWORK.search(text) for _, text in lines)
    )
    if touching:
        report.flags.append(
            "产品代码新增了对测试框架的引用（可能在 import 时让已有测试失效，PR7-R7）："
            + "、".join(f"`{path}`" for path in touching)
        )
    appended = [item.path for item in report.files if item.reason == "只在已有测试文件中追加"]
    if appended:
        report.notes.append(
            "追加到已有测试文件：" + "、".join(f"`{path}`" for path in appended)
            + "。已有测试另按 base 版本在 head 代码上运行（`engine/checks/base_tests.py`），追加的代码影响不到判定（PR7-R2）"
        )
    report.claimed_r1 = commits_claim_r1(base, head, cwd)
    if report.claimed_r1:
        r2_files = [item for item in report.files if item.level == 2]
        only_code = all(path_matches(item.path, code_patterns()) for item in r2_files)
        eligible = report.level == 2 and only_code and not report.flags
        strengthened = r1_checks.violations(base, head, cwd) if eligible else []
        if eligible and not strengthened:
            report.level = 1
            report.notes.append(
                "每个提交都声明 `Risk: R1`，只改产品代码，已有测试与黄金快照零改动，被改函数签名不变，"
                "无新依赖与迁移，不超规模阈值 → R1（变异得分由 build 的 harness job 核对）"
            )
        else:
            report.r1_violations = strengthened
            report.notes.append("提交声明了 `Risk: R1`，但机器核对不满足（见上方标记、非代码路径或下列理由）→ 维持原等级")
            report.notes += [f"- {reason}" for reason in strengthened]
    _emit_summary(report, base, head, trace_id)
    return report


def _emit_summary(report: RiskReport, base: str, head: str, trace_id: str | None) -> None:
    """风险汇总观察事件：最终等级、R1 声明与是否被降级、被改动的已有测试数（观察旁路，不影响判定）。"""
    changed_tests = sum(1 for item in report.files if item.reason.startswith("改动已有测试"))
    events.emit("route", "risk.summary", "ok", trace_id=trace_id,
                inputs=[events.ref("rev", base), events.ref("rev", head)],
                outputs={"risk": report.label, "level": report.level, "claimed_r1": report.claimed_r1,
                         "downgraded": report.claimed_r1 and report.level != 1,
                         "changed_tests": changed_tests},
                decision={"by": "risk", "rule": "risk", "reason": f"{report.label}：{POLICY[report.level]}"})


def render_markdown(report: RiskReport) -> str:
    lines = [f"### 风险等级：**{report.label}** — {POLICY[report.level]}", ""]
    if report.flags:
        lines += ["需要评审关注："] + [f"- ⚠️ {flag}" for flag in dict.fromkeys(report.flags)] + [""]
    lines += report.notes + ([""] if report.notes else [])
    lines += ["<details><summary>逐文件判定</summary>", "", "| 文件 | 状态 | 等级 | 依据 |", "|---|---|---|---|"]
    lines += [f"| `{item.path}` | {item.status} | R{item.level} | {item.reason} |" for item in report.files]
    lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--github", action="store_true", help="写 GITHUB_STEP_SUMMARY 与 GITHUB_OUTPUT")
    args = parser.parse_args(argv)
    report = classify(args.base, args.head)
    text = render_markdown(report)
    print(text)
    if args.github:
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
                handle.write(text + "\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
                handle.write(
                    f"risk={report.label}\nclaimed_r1={'true' if report.claimed_r1 else 'false'}\n"
                    f"auto_merge={'true' if report.level <= 1 else 'false'}\n"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
