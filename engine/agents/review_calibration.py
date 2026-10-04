"""B59 评审校准：真实历史样本的清单装载、逐样本重跑与 TPR/TNR 报告（T707 自 review.py 逐字移出，行为不变）。

调用原模块的名字（VERDICTS、checkout、load_rules、make_reviewer、review_workspace、write_materials、
run_reviewer、git）时按需在函数体内导入 review 并以属性访问：保留测试在原模块上的 patch 语义，
也避免循环导入。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from engine.core.common import ROOT

if TYPE_CHECKING:
    from engine.agents.review import Reviewer


def load_samples(samples_path: Path) -> list[dict]:
    """装载校准样本清单：每条必须有 pr（正整数）、head（40 位提交号）、expected（合法结论）与一句 reason。"""
    from engine.agents import review  # VERDICTS 留在原模块，按需导入（T707）

    try:
        entries = json.loads(samples_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"校准样本清单读不到或不是合法 JSON：{samples_path}（{error}）") from error
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"校准样本清单须是非空的样本数组：{samples_path}")
    for index, entry in enumerate(entries, 1):
        where = f"校准样本第 {index} 条"
        if not isinstance(entry, dict):
            raise TypeError(f"{where} 不是对象")
        pr, head = entry.get("pr"), entry.get("head")
        expected, reason = entry.get("expected"), entry.get("reason")
        if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
            raise ValueError(f"{where} 缺少合法的 pr 号（正整数），得到 {pr!r}")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ValueError(f"{where}（PR #{pr}）的 head 必须是 40 位十六进制提交号，得到 {head!r}")
        if expected not in review.VERDICTS:
            raise ValueError(f"{where}（PR #{pr}）的 expected 只能是 {'、'.join(review.VERDICTS)}，得到 {expected!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{where}（PR #{pr}）缺少一句裁决依据（reason）")
    return entries


def prepare_head_sample(workspace: Path, sample: dict) -> tuple[str, str]:
    """把评审工作区检出到样本 head，返回 (base, PR 文本)，材料组装与 review_pr 同一套（write_materials）。

    样本 head 不在 main 上时（中间轮次的 head）从 pull/<pr>/head 取，对象不可得即报错（该样本进 errors）。
    base 与 review_base 同口径：已合并的样本用合并提交的第一父提交，未合并的退回与 origin/main 的合并基。
    """
    from engine.agents import review  # git、checkout 留在原模块，按需导入（T707）

    pr, head = sample["pr"], sample["head"]
    review.git("fetch", "--quiet", "origin", f"pull/{pr}/head", cwd=workspace, check=False)
    if not review.git("rev-parse", "--verify", "--quiet", f"{head}^{{commit}}", cwd=workspace, check=False):
        raise ValueError(f"样本 head {head[:12]} 检出失败：origin/main 与 pull/{pr}/head 里都没有这个提交")
    review.checkout(workspace, head)
    merge = review.git("log", "--merges", "--first-parent", "--format=%H %s", "origin/main", cwd=workspace)
    sha = next((line.split()[0] for line in merge.splitlines() if f"#{pr} " in f"{line} "), None)
    if sha:
        base = review.git("rev-parse", f"{sha}^1", cwd=workspace)
        body = review.git("log", "-1", "--format=%B", sha, cwd=workspace)
    else:
        base = review.git("merge-base", "origin/main", "HEAD", cwd=workspace)
        body = review.git("log", "-1", "--format=%B", head, cwd=workspace)
    return base, f"# PR #{pr}\n\n{body}\n"


def sample_deviates(item: dict) -> bool:
    """与期望不一致：期望不通过却放行、期望通过却抓中；期望需用户验收时，给了无阻断/严重发现的明确通过也算偏差。"""
    if item["expected"] == "不通过":
        return not item["flagged"]
    if item["expected"] == "通过":
        return item["flagged"]
    return item["verdict"] != "需用户验收" and not item["flagged"]


def score_samples(results: list[dict]) -> dict:
    """TPR＝期望不通过的样本里抓中的比例，TNR＝期望通过的样本里放行的比例。样本检出失败（error）与评审方
    失败（failure）都进 errors、不进分母；有输出但没有可用结论（unparsed）同样不进分母，单独计数。"""
    scored = [item for item in results if "error" not in item and not item.get("failure") and item.get("parsed", True)]
    bad = [item for item in scored if item["expected"] == "不通过"]
    good = [item for item in scored if item["expected"] == "通过"]
    return {
        "total": len(results), "bad": len(bad), "good": len(good),
        "caught": sum(item["flagged"] for item in bad),
        "released": sum(not item["flagged"] for item in good),
        "deviations": sum(sample_deviates(item) for item in scored),
        "errors": sum(1 for item in results if "error" in item or item.get("failure")),
        "unparsed": sum("error" not in item and not item.get("failure") and not item.get("parsed", True)
                        for item in results),
        "tpr": round(sum(item["flagged"] for item in bad) / len(bad), 3) if bad else None,
        "tnr": round(sum(not item["flagged"] for item in good) / len(good), 3) if good else None,
    }


def _cell(text: str) -> str:
    """表格单元格转义：竖线与换行会破坏 markdown 表格。"""
    return text.replace("|", "\\|").replace("\n", " ")


def render_calibration_report(samples_path: Path, results: list[dict], summary: dict, reviewer: str,
                              model: str) -> str:
    def ratio(part: int, whole: int) -> str:
        return f"{part / whole:.3f}（{part}/{whole}）" if whole else "n/a（分母 0）"

    lines = [
        "# 评审校准报告", "",
        f"- 样本清单：{samples_path}",
        f"- 评审方：{reviewer}{'（' + model + '）' if model else ''}；日期：{dt.date.today().isoformat()}",
        f"- TPR（期望不通过的抓中率）：{ratio(summary['caught'], summary['bad'])}",
        f"- TNR（期望通过的放行率）：{ratio(summary['released'], summary['good'])}",
        f"- 偏差 {summary['deviations']} 条；errors {summary['errors']} 条、unparsed {summary['unparsed']} 条不进分母",
        "", "## 逐样本结果", "",
        "| PR | head | 期望 | 实际结论 | 偏差 | 裁决依据 |", "|---|---|---|---|---|---|",
    ]
    for item in results:
        if "error" in item:
            actual, deviation = f"样本错误：{_cell(item['error'])}", "未计分"
        elif item.get("failure"):
            actual, deviation = f"评审方失败：{_cell(item['failure'])}", "未计分"
        else:
            actual = _cell(item["verdict"]) + ("（抓住）" if item["flagged"] else "")
            deviation = "是" if sample_deviates(item) else "—"
        lines.append(f"| #{item['pr']} | {item['head'][:12]} | {item['expected']} | {actual} | {deviation} | "
                     f"{_cell(item['reason'])} |")
    lines += ["", "## 结论", "",
              ("- 校准是测量工具：本报告只给出 TPR/TNR 与逐样本偏差，不改原判定、不评论任何 PR；"
               "换评审方或模型后应在同一清单上重跑再比较。")]
    return "\n".join(lines) + "\n"


def review_calibrate(review_name: str | None, samples_path: Path, output: Path, *, root: Path = ROOT,
                     reviewer: Reviewer | None = None) -> int:
    """B59 评审校准：对真实历史样本（pr + head + 期望结论）逐个重跑独立评审，统计 TPR/TNR 与逐样本偏差，
    报告写本地 markdown（不评论到任何 GitHub PR）。样本检出失败与评审方失败进 errors，不进 TPR/TNR 分母。"""
    from engine.agents import review  # make_reviewer 等留在原模块，按需导入（T707）

    samples = load_samples(samples_path)
    name = review_name or review.load_rules().get("review", {}).get("reviewer")
    runner = reviewer or (review.make_reviewer(name) if name else None)
    if runner is None:
        print("未能判定评审方：用 --reviewer 指定，或在 rules.toml [review] 配置 reviewer")
        return 2
    from engine.agents import review_lock  # E128-R1：与其他评审入口共用工作区，同样持锁串行

    timeout = review.load_rules().get("review", {}).get("timeout_minutes", 30) * 60 * 2
    with review_lock.workspace_lock(root, review.review_workspace(root), timeout, purpose="review_calibrate"):
        workspace = review.review_workspace(root)
        review.git("fetch", "--quiet", "origin", "main", cwd=workspace)
        results = []
        for sample in samples:
            item = {"pr": sample["pr"], "head": sample["head"], "expected": sample["expected"],
                    "reason": sample["reason"]}
            try:
                base, pr_text = prepare_head_sample(workspace, sample)
                review.write_materials(workspace, base, pr_text, "")
            except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                results.append({**item, "error": str(error)})
                print(f"PR #{sample['pr']} @ {sample['head'][:12]}：样本错误（{error}）", flush=True)
                continue
            verdict, model, seconds = review.run_reviewer(runner, workspace, review.load_rules().get("review", {}).get(
                "timeout_minutes", 30) * 60)
            results.append({**item, "verdict": verdict.verdict, "flagged": verdict.flagged,
                            "parsed": verdict.parsed, "failure": verdict.failure, "model": model,
                            "seconds": round(seconds)})
            print(f"PR #{sample['pr']} @ {sample['head'][:12]}：{verdict.verdict}"
                  f"{'（抓住）' if verdict.flagged else ''}"
                  f"{'：' + verdict.failure if verdict.failure else ''}", flush=True)
        summary = score_samples(results)
        model = next((item["model"] for item in results if item.get("model")), "")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_calibration_report(samples_path, results, summary, runner.name, model),
                          encoding="utf-8")
    print(f"校准完成：TPR {summary['tpr']}、TNR {summary['tnr']}、偏差 {summary['deviations']} 条、"
          f"errors {summary['errors']} 条；报告已写入 {output}")
    return 0
