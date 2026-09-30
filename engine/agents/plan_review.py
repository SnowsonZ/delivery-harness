"""任务拆分评审（B54）：把设计/计划文档与其拆分材料交给独立评审方，按 docs/task-splitting.md 的拆分评审
检查项（完整性、正确性、可验证、可完成、一致性）评审拆分能否一并提交；报告只写本地 markdown，
不评论任何 GitHub PR。

入口经 engine/cli.py 的 COMMANDS 注册表走既有通用路由：python3 <引擎目录>/cli.py review-plan
<设计文档路径> [--output 路径]（报告缺省 build/review/plan-verdict.md，相对仓库根解析）。
材料以指定文档为主，自动收集它正文链接到的 docs 下本地 markdown（含追溯表、共用合同）与
docs/plans/ 下全部任务书（含未被正文链接的）；外链、仓库外与非 docs 目标不算材料。复用
engine/agents/review.py 的材料组装（write_materials 同格式落盘）与评审执行（run_reviewer、
parse_output）在只读评审工作区运行；结论（含 findings）、材料清单与缺失清单写入报告。设计文档
不存在时不评审，写缺失报告并返回 1；链接目标缺失进缺失清单并在报告中标注，不静默跳过。
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from engine.agents.review import (
    Reviewer,
    Verdict,
    _cell,
    checkout,
    make_reviewer,
    review_workspace,
    run_reviewer,
    write_materials,
)
from engine.core.common import ROOT, git, load_rules

PLAN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _plan_doc_links(plan_text: str, plan_dir: Path, root: Path) -> tuple[list[str], list[str]]:
    """plan 正文里链接到的 docs 下本地 markdown（docs/plans/*.md、docs/*.md 与 docs 其余子目录）。
    返回 (存在的仓库相对路径, 缺失的链接目标)，各自排序去重；外链、仓库外与 docs 外的目标不是材料。"""
    found, missing = [], []
    for target in dict.fromkeys(PLAN_LINK.findall(plan_text)):
        path = target.split("#", 1)[0]
        if not path or "://" in path:
            continue
        try:
            rel = Path(os.path.normpath(plan_dir / path)).relative_to(root).as_posix()
        except ValueError:
            continue  # 解析到仓库外
        if rel.split("/", 1)[0] == "docs" and rel.endswith(".md"):
            (found if (root / rel).exists() else missing).append(rel)
    return sorted(set(found)), sorted(set(missing))


def collect_plan_materials(plan_doc: Path, root: Path) -> tuple[str, str, list[tuple[str, str]], list[str]]:
    """B54 拆分评审材料：以 plan 文档为主材料，收集其正文链接到的 docs 下本地 markdown，加上 docs/plans/ 下
    全部任务书（含未被正文链接的）。返回 (plan 相对路径, plan 全文, [(仓库相对路径, 全文)], 缺失的链接目标)；
    plan 文档不在仓库内或读不到时，前两项为空串、文档本身进缺失清单（不静默跳过）。"""
    try:
        plan_rel = plan_doc.relative_to(root).as_posix()
        plan_text = plan_doc.read_text(encoding="utf-8")
    except (ValueError, OSError):
        try:
            name = plan_doc.relative_to(root).as_posix()
        except ValueError:
            name = plan_doc.as_posix()
        return "", "", [], [name]
    linked, missing = _plan_doc_links(plan_text, plan_doc.parent, root)
    taskbooks = [path.relative_to(root).as_posix() for path in sorted((root / "docs" / "plans").glob("task-*.md"))]
    return plan_rel, plan_text, [(rel, (root / rel).read_text(encoding="utf-8"))
                                 for rel in sorted(set(linked) | set(taskbooks))], missing


def render_plan_report(plan_rel: str, materials: list[tuple[str, str]], missing: list[str], verdict: Verdict,
                       reviewer: str, model: str, seconds: float) -> str:
    """拆分评审报告：结论（含 findings）、材料清单与缺失清单（有缺失时在报告中标注）。"""
    lines = [
        f"# 拆分评审报告：{plan_rel}", "",
        f"- 评审方：{reviewer}（{model or '模型未报告'}）；用时 {seconds / 60:.1f} 分钟",
        f"- 材料清单（{len(materials)} 份）：",
        *(f"  - `{rel}`" for rel, _ in materials),
        f"- 缺失清单：{'、'.join(f'`{item}`' for item in missing) or '无'}",
        "", f"## 结论：{verdict.verdict}", "",
        verdict.summary or "（无）", "",
    ]
    if verdict.findings:
        lines += ["| 严重度 | 位置 | 问题 | 修复要求 |", "|---|---|---|---|"]
        lines += [f"| {item.get('severity', '')} | {item.get('location', '')} | {_cell(item.get('problem', ''))} | "
                  f"{_cell(item.get('fix', ''))} |" for item in verdict.findings]
    elif not verdict.failure:
        lines.append("没有发现。")
    if verdict.failure:
        lines += ["", f"评审方失败：{verdict.failure}"]
    if missing:
        lines += ["", "注意：缺失清单中的链接目标未参与本次评审。"]
    return "\n".join(lines) + "\n"


def review_plan(plan_doc: Path, output: Path, *, root: Path = ROOT, reviewer: Reviewer | None = None) -> int:
    """B54 任务拆分评审：以设计/计划文档为主材料，自动收集其链接的 docs 本地材料与全部任务书，复用独立评审的
    材料组装（write_materials 同格式落盘）与评审执行（run_reviewer、parse_output）在只读评审工作区评审；结论
    （含 findings）与材料清单写入 output（markdown），不评论任何 GitHub PR。设计文档或链接目标缺失时进缺失
    清单并在报告中标注；文档本身缺失时不评审，写缺失报告并返回 1。"""
    name = load_rules().get("review", {}).get("reviewer")
    runner = reviewer or (make_reviewer(name) if name else None)
    if runner is None:
        print("未能判定评审方：在 rules.toml [review] 配置 reviewer")
        return 2
    plan_doc = plan_doc if plan_doc.is_absolute() else root / plan_doc
    output = output if output.is_absolute() else root / output
    plan_rel, plan_text, materials, missing = collect_plan_materials(plan_doc, root)
    if not plan_rel:  # 设计文档不存在或不在仓库内：写缺失报告，不评审
        report = render_plan_report(missing[0], [], missing,
                                    Verdict("需用户验收", [], "设计文档不存在，未评审", parsed=False),
                                    runner.name, "", 0.0)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(f"拆分评审：设计文档缺失（{missing[0]}），报告已写入 {output}")
        return 1
    workspace = review_workspace(root)
    git("fetch", "--quiet", "origin", "main", cwd=workspace)
    checkout(workspace, "origin/main")
    base = git("rev-parse", "origin/main", cwd=workspace)
    folder = workspace / "build" / "review" / "materials"
    folder.mkdir(parents=True, exist_ok=True)
    for rel, text in materials:  # 材料全文按仓库相对路径落盘，评审方在只读沙箱里读得到
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    listing = "\n".join(f"- `{rel}`" for rel, _ in materials) or "-（没有收集到任何材料）"
    gone = "\n".join(f"- `{item}`" for item in missing) or "-（无）"
    pr_text = (
        f"# 拆分评审：{plan_rel}\n\n"
        "这不是 PR 评审，而是任务拆分评审：对照 docs/task-splitting.md 的拆分评审检查项（完整性、正确性、"
        "可验证、可完成、一致性），依据下面的主材料与关联材料评审这份拆分能否一并提交。\n\n"
        f"## 主材料：{plan_rel} 全文\n\n{plan_text}\n\n"
        f"## 关联材料（{len(materials)} 份，全文在 build/review/materials/ 下按仓库相对路径存放）\n\n{listing}\n\n"
        f"## 缺失的链接目标（未参与评审）\n\n{gone}\n"
    )
    write_materials(workspace, base, pr_text, "", "（拆分评审没有 CI 运行）")
    verdict, model, seconds = run_reviewer(runner, workspace, load_rules().get("review", {}).get(
        "timeout_minutes", 30) * 60)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_plan_report(plan_rel, materials, missing, verdict, runner.name, model, seconds),
                      encoding="utf-8")
    if verdict.failure:
        print(f"拆分评审：评审方失败（{verdict.failure}），报告已写入 {output}")
        return 1
    print(f"拆分评审：{verdict.verdict}（{runner.name}），报告已写入 {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """review-plan 子命令（engine/cli.py COMMANDS 注册表路由）：任务拆分评审，报告写本地 markdown。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plan_doc", type=Path, help="设计/计划文档路径（相对仓库根）")
    parser.add_argument("--output", type=Path, default=Path("build/review/plan-verdict.md"),
                        help="报告路径（相对仓库根，缺省 build/review/plan-verdict.md）")
    args = parser.parse_args(argv)
    doc = args.plan_doc if args.plan_doc.is_absolute() else ROOT / args.plan_doc
    out = args.output if args.output.is_absolute() else ROOT / args.output
    return review_plan(doc, out)
