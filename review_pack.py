"""评审证据包：把评审需要的机器结论汇成一页，评审方的时间花在清单中机器判定不了的部分。

    python3 harness/review_pack.py --base origin/main [--head HEAD] [--output build/review/pack.md] [--no-run]

内容：改动概要、风险等级（risk.py）、修复证据（evidence.py）、本地 verify 汇总（仅当与当前 head 一致）、
本次改动涉及的验收编号及其中的人工项、评审清单入口（docs/templates/review-checklist.md）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance
import evidence
import risk
from common import ROOT, added_lines, git

VERIFY_SUMMARY = ROOT / "build" / "verify" / "summary.json"


def touched_acceptance(base: str, head: str, cwd: Path = ROOT) -> list[acceptance.Item]:
    """新增行里出现的验收编号（规格表新增或修改、测试里新标注的编号）。"""
    items, _ = acceptance.check(cwd)
    by_id = {item.id: item for item in items}
    found: dict[str, acceptance.Item] = {}
    for path, lines in added_lines(base, head, cwd).items():
        if not path.startswith(("docs/specs/", "tests/")):
            continue
        for _, text in lines:
            for token in re.findall(r"(?<![\w-])([A-Z]{1,3}\d+)(?![\w-])", text):
                if token in by_id:
                    found[token] = by_id[token]
    return sorted(found.values(), key=lambda item: (item.spec, item.line))


def verify_section(head_sha: str, summary_path: Path = VERIFY_SUMMARY) -> list[str]:
    if not summary_path.exists():
        return ["本地没有 verify 汇总；以 CI 上当前 head 的运行为准。"]
    summary = json.loads(summary_path.read_text())
    if summary.get("head") != head_sha or summary.get("dirty"):
        return [f"本地 verify 汇总对应 `{str(summary.get('head'))[:12]}`（或工作区有改动），与当前 head 不符；以 CI 为准。"]
    icon = {"pass": "✅", "fail": "❌", "skip": "⏭️"}
    lines = [f"本地 `bin/verify`（{summary.get('tier')} 档，{summary.get('platform')}）@ `{head_sha[:12]}`：", ""]
    lines += ["| 检查 | 结果 | 说明 |", "|---|---|---|"]
    lines += [f"| {r['name']} | {icon[r['status']]} | {r.get('note', '')} |" for r in summary["results"]]
    return lines


def build(base: str, head: str = "HEAD", run_tests: bool = True, cwd: Path = ROOT) -> str:
    head_sha = git("rev-parse", head, cwd=cwd)
    commits = git("rev-list", "--count", f"{base}..{head}", cwd=cwd)
    files = git("diff", "--shortstat", f"{base}...{head}", cwd=cwd).strip()
    parts = [
        f"# 评审证据包 `{git('rev-parse', '--short', base, cwd=cwd)}` → `{head_sha[:7]}`",
        "",
        f"{commits} 个提交；{files or '无改动'}。评审清单见 `docs/templates/review-checklist.md`。",
        "",
        risk.render_markdown(risk.classify(base, head, cwd)),
        evidence.render_markdown(evidence.analyse(base, head, run_tests=run_tests, cwd=cwd), base, head, cwd),
        "### 验证",
        "",
        *verify_section(head_sha),
        "",
        "### 涉及的验收编号",
        "",
    ]
    touched = touched_acceptance(base, head, cwd)
    if not touched:
        parts.append("本次改动的新增行没有涉及验收编号。")
    else:
        parts += ["| 编号 | 验收内容 | 证据类型 | 需人工验收 |", "|---|---|---|---|"]
        parts += [
            f"| {item.id} | {item.text} | {'、'.join(item.kinds)} | {'是' if item.manual else ''} |" for item in touched
        ]
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", help="同时写入该文件")
    parser.add_argument("--no-run", action="store_true", help="修复证据不运行测试")
    args = parser.parse_args(argv)
    text = build(args.base, args.head, run_tests=not args.no_run)
    print(text)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
