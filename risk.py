"""按改动路径判定风险等级（R0–R3），不由执行者自报。规则在 harness/rules.toml [risk]。

    R0  说明性文档、只新增测试                 门禁全绿即可自动合并
    R1  声明为行为不变的重构并通过机器核对     自动合并 + 抽样审计
    R2  产品代码、现役规格、改动已有测试       评审方评审 + 用户看证据包后合并
    R3  护栏、CI 与发布、迁移、隐私、用户配置   必须由用户批准

R1 需要同时满足：范围内每个提交都带 `Risk: R1` trailer；最高等级只来自产品代码路径；
已有测试与黄金快照零改动。任何一条不满足都按 R2。

    python3 harness/risk.py --base origin/main [--head HEAD] [--github]
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    ROOT,
    added_line_count,
    changed_files,
    commit_field,
    git,
    load_rules,
    path_matches,
    removed_line_count,
)

POLICY = {
    0: "门禁全绿即可自动合并",
    1: "门禁全绿可自动合并，合并后抽样审计",
    2: "评审方评审 + 用户看证据包后合并",
    3: "必须由用户批准后合并",
}
CODE_PATTERNS = ["scripts/**", "native/**"]


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
    level: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"R{self.level}"


def classify_file(status: str, path: str, base: str, head: str, rules: dict, cwd: Path) -> tuple[FileRisk, str | None]:
    risk = rules["risk"]
    if path_matches(path, risk["golden"]):
        if status == "A":
            return FileRisk(path, status, 0, "新增黄金快照（新增测试）"), None
        return FileRisk(path, status, 2, "黄金快照改动 = 行为变化"), f"黄金快照改动：`{path}`"
    if path_matches(path, risk["tests"]):
        if status == "A":
            return FileRisk(path, status, 0, "新增测试"), None
        if status == "D":
            return FileRisk(path, status, 2, "删除已有测试"), f"删除已有测试：`{path}`"
        removed = removed_line_count(base, head, path, cwd)
        if removed == 0:
            return FileRisk(path, status, 0, "只在已有测试文件中追加"), None
        return (
            FileRisk(path, status, 2, f"改动已有测试（删改 {removed} 行）"),
            f"改动已有测试：`{path}`（删改 {removed} 行），判定器被修改须由评审方确认",
        )
    shrinking = (
        status == "M"
        and path_matches(path, risk.get("shrink_only", []))
        and not added_line_count(base, head, path, cwd)
        and removed_line_count(base, head, path, cwd)
    )
    if shrinking:
        return FileRisk(path, status, 0, "只能缩减的清单被缩减"), None
    pattern = path_matches(path, risk["r3"])
    if pattern:
        return FileRisk(path, status, 3, f"命中 R3 规则 `{pattern}`"), None
    pattern = path_matches(path, risk["r0"])
    if pattern:
        return FileRisk(path, status, 0, f"命中 R0 规则 `{pattern}`"), None
    pattern = path_matches(path, risk["r2"])
    if pattern:
        return FileRisk(path, status, 2, f"命中 R2 规则 `{pattern}`"), None
    return FileRisk(path, status, 2, "未归类路径按 R2"), None


def commits_claim_r1(base: str, head: str, cwd: Path) -> bool:
    shas = git("rev-list", f"{base}..{head}", cwd=cwd).split()
    if not shas:
        return False
    for sha in shas:
        if "R1" not in commit_field(sha, "Risk", cwd):
            return False
    return True


def classify(base: str, head: str = "HEAD", cwd: Path = ROOT, rules: dict | None = None) -> RiskReport:
    rules = rules or load_rules()
    report = RiskReport()
    for status, path in changed_files(base, head, cwd):
        file_risk, flag = classify_file(status, path, base, head, rules, cwd)
        report.files.append(file_risk)
        if flag:
            report.flags.append(flag)
    guarded = [item.path for item in report.files if item.level == 3]
    if guarded:
        shown = "、".join(f"`{path}`" for path in guarded[:8])
        more = f" 等 {len(guarded)} 个" if len(guarded) > 8 else ""
        report.flags.append(f"改动护栏、CI、发布或高风险路径（R3，需用户批准）：{shown}{more}")
    report.level = max((item.level for item in report.files), default=0)
    appended = [item.path for item in report.files if item.reason == "只在已有测试文件中追加"]
    if appended:
        report.notes.append(
            "追加到已有测试文件：" + "、".join(f"`{path}`" for path in appended)
            + "。已有测试另按 base 版本在 head 代码上运行（`harness/base_tests.py`），追加的代码影响不到判定（PR7-R2）"
        )
    report.claimed_r1 = commits_claim_r1(base, head, cwd)
    if report.claimed_r1:
        r2_files = [item for item in report.files if item.level == 2]
        only_code = all(path_matches(item.path, CODE_PATTERNS) for item in r2_files)
        if report.level == 2 and only_code and not report.flags:
            report.level = 1
            report.notes.append("每个提交都声明 `Risk: R1`，且只改产品代码、已有测试与黄金快照零改动 → R1")
        else:
            report.notes.append("提交声明了 `Risk: R1`，但机器核对不满足（见上方标记或非代码路径）→ 维持原等级")
    return report


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
                handle.write(f"risk={report.label}\nauto_merge={'true' if report.level <= 1 else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
