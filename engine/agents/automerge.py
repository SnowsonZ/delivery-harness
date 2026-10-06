"""原生自动合并的平台子命令与停机撤销（T715，B102）。

automerge platform    把 checks.toml [platform] 的批准方式、environment 与批准/同步 App 的变量、
                      密钥名写成 key=value（有 GITHUB_OUTPUT 时写入），auto-merge 工作流在 main
                      push 时的 sync-waiting job 用它取 environment 与同步 App 的变量/密钥名；
                      取值与 policy 同一套 platform_outputs()，不另造配置读取。
automerge-off         停机与类别回收时撤销 GitHub 上已开启的自动合并：分页列出 auto-merge.yml
                      全部未完成的运行（它们可能还在开启请求），逐个关闭打开 PR 的存量请求，
                      关闭后重新查询请求与未完成运行——只有「未完成运行为零且请求为零」才以 0
                      退出；仍有未完成运行（须先取消或等其结束再重跑本命令）、仍有请求或任一
                      查询失败都列出并以非零退出，不假装成功。--dry-run 只列出不改。

停机顺序（README「Platform setup」、auto-merge.yml 文件头）：先 Disable workflow，再取消或
等待在途运行，然后运行 automerge-off 直到它以 0 退出。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from engine.routing import policy

WORKFLOW = "auto-merge.yml"
# GitHub 文档的 run 状态里除了 completed 都算未完成（waiting/pending/requested 属排队形态）。
INCOMPLETE_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def platform_main(argv: list[str] | None = None) -> int:
    """platform 子命令：platform_outputs() 的每个键写一行 key=value（GITHUB_OUTPUT 在场时追加）。"""
    values = policy.platform_outputs()
    lines = [f"{key}={value}" for key, value in values.items()]
    print("\n".join(lines))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="automerge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("platform", help="输出 checks.toml [platform] 配置（auto-merge 工作流的 push 触发用）")
    args = parser.parse_args(argv)
    if args.command == "platform":
        return platform_main()
    parser.error(f"未知子命令：{args.command}")  # pragma: no cover - argparse required=True 已拦截
    return 2


def incomplete_runs(gh=None) -> list[dict]:
    """分页列出 auto-merge.yml 全部未完成的运行；查询失败向上抛，由调用方按失败处理。"""
    gh = gh or _gh
    raw = gh("api", f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}/runs?per_page=100",
             "--paginate", "--slurp")
    pages = json.loads(raw or "[]")
    runs = [run for page in pages for run in page.get("workflow_runs", [])]
    return [run for run in runs if run.get("status") in INCOMPLETE_STATUSES]


def requested_prs(gh=None) -> list[int]:
    """全部 autoMergeRequest 非空的打开 PR（分页）；查询失败向上抛。"""
    gh = gh or _gh
    raw = gh("pr", "list", "--state", "open", "--json", "number,autoMergeRequest", "--paginate",
             "--jq", ".[] | select(.autoMergeRequest != null) | .number")
    return sorted({int(line) for line in raw.split() if line.strip().isdigit()})


def _describe(runs: list[dict]) -> str:
    return "；".join(f"{run.get('databaseId')}（{run.get('status')}，{run.get('headBranch')}）"
                     for run in runs)


def off_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="automerge-off", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="只列出未完成运行与存量请求，不做任何修改")
    args = parser.parse_args(argv)
    try:
        runs, requests = incomplete_runs(), requested_prs()
    except (RuntimeError, json.JSONDecodeError, OSError) as error:
        print(f"查询失败：{error}", file=sys.stderr)
        return 1
    print(f"未完成的 {WORKFLOW} 运行：{_describe(runs) or '无'}")
    print("已开启自动合并的打开 PR：" + ("、".join(f"#{pr}" for pr in requests) or "无"))
    if args.dry_run:
        return 0
    failures = []
    for pr in requests:
        try:
            _gh("pr", "merge", str(pr), "--disable-auto")
            print(f"PR #{pr} 的自动合并已关闭")
        except (RuntimeError, OSError) as error:
            failures.append(pr)
            print(f"关闭 PR #{pr} 失败：{error}", file=sys.stderr)
    try:
        remaining_runs, remaining_requests = incomplete_runs(), requested_prs()
    except (RuntimeError, json.JSONDecodeError, OSError) as error:
        print(f"复查失败：{error}", file=sys.stderr)
        return 1
    if remaining_runs:
        print(f"复查：仍有未完成的运行（先取消或等其结束，再重跑本命令）：{_describe(remaining_runs)}")
    if remaining_requests:
        print("复查：仍有已开启的请求：" + "、".join(f"#{pr}" for pr in remaining_requests))
    if remaining_runs or remaining_requests or failures:
        return 1
    print("复查通过：未完成运行为零，自动合并请求为零")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
