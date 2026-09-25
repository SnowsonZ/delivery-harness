"""交付度量：统计一次交付（base..head）中机器可数的指标，并与 v0.8.0 基线并列。

    python3 harness/metrics.py --base origin/main [--head HEAD] [--github] [--json]

--github 时用 GITHUB_TOKEN 与 GITHUB_REPOSITORY 查询该分支的 build workflow 运行（CI 轮次、失败轮次）；
没有凭据时标为「不可用」，不猜。评审轮次、自述不实、人工介入由人记录在交付说明中（见
docs/plans/verifiable-delivery.md 的试跑规程）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evidence
import risk
from common import ROOT, added_lines, git
from replay_cases import CASES, DEFERRED, GUARDED

# docs/review/2026-09-25-harness-baseline.md §2
V080_BASELINE = {
    "评审轮次": 5,
    "声称已修但代码未变": 3,
    "自述与事实不符": 5,
    "临时探针 CI 运行": 32,
    "首轮修复净增测试": "16 项修复 / +1",
}


def added_test_functions(base: str, head: str, cwd: Path = ROOT) -> int:
    count = 0
    for path, lines in added_lines(base, head, cwd).items():
        if path.startswith("tests/") and path.endswith(".py"):
            count += sum(1 for _, text in lines if text.lstrip().startswith("def test_"))
    return count


def replay_coverage(defects: list[str]) -> tuple[int, list[str]]:
    covered = {case.defect for case in CASES} | set(GUARDED) | set(DEFERRED)
    missing = [defect for defect in defects if defect not in covered]
    return len(defects) - len(missing), missing


def github_runs(branch: str) -> dict | None:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    query = urllib.parse.urlencode({"branch": branch, "per_page": 100})
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/build.yml/runs?{query}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            runs = json.load(response).get("workflow_runs", [])
    except (OSError, ValueError):
        return None
    return {
        "runs": len(runs),
        "failed": sum(1 for run in runs if run.get("conclusion") == "failure"),
    }


def collect(base: str, head: str = "HEAD", use_github: bool = False, cwd: Path = ROOT) -> dict:
    fixes = evidence.analyse(base, head, run_tests=False, cwd=cwd)
    defects = sorted(fixes)
    replayed, missing = replay_coverage(defects)
    branch = git("rev-parse", "--abbrev-ref", head, cwd=cwd)
    data = {
        "base": git("rev-parse", "--short", base, cwd=cwd),
        "head": git("rev-parse", "--short", head, cwd=cwd),
        "提交数": int(git("rev-list", "--count", f"{base}..{head}", cwd=cwd)),
        "风险等级": risk.classify(base, head, cwd).label,
        "Defect 修复": len(defects),
        "声称已修但代码未变": sum(any("没有代码改动" in p for p in fix.problems) for fix in fixes.values()),
        "修复带回放用例": f"{replayed}/{len(defects)}",
        "缺回放的修复": missing,
        "新增测试函数": added_test_functions(base, head, cwd),
    }
    if use_github:
        # PR 事件检出的是合并提交（HEAD 游离），分支名取 CI 提供的变量。
        runs = github_runs(os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or branch)
        data["CI 运行"] = runs["runs"] if runs else "不可用"
        data["CI 失败轮次"] = runs["failed"] if runs else "不可用"
    return data


def render(data: dict) -> str:
    lines = [f"### 交付度量（`{data['base']}` → `{data['head']}`）", "", "| 指标 | 本次 |", "|---|---|"]
    for key, value in data.items():
        if key in ("base", "head"):
            continue
        shown = "、".join(value) if isinstance(value, list) else value
        lines.append(f"| {key} | {shown if shown != '' else '—'} |")
    lines += ["", "v0.8.0 基线：" + "；".join(f"{key} {value}" for key, value in V080_BASELINE.items()) + "。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", help="追加写入该文件（CI 写 job summary）")
    args = parser.parse_args(argv)
    data = collect(args.base, args.head, args.github)
    text = json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data)
    print(text)
    if args.markdown:
        with open(args.markdown, "a") as handle:
            handle.write(render(data) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
