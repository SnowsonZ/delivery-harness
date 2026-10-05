"""设计方复核（T702，自治试验设计第 5 节第 3–5 步）：发布带标记的复核评论，条件齐备后重跑判定。

设计方逐行验收与定向变异复核后运行 `bin/dispatch signoff <PR>`：评论正文取 --body-file，末尾追加
复核标记（engine/routing/signals.py 的合同，policy 的合同制路径只认这个标记）。前后矛盾的「通过」
（mutations < 1 或 caught != mutations）直接拒绝，一条评论都不发——复核结论必须与证据一致。

发布后调用 retrigger_if_ready：该 PR 的独立评审与复核都已通过时，重跑该 head 最近一次由
pull_request 触发的 harness 运行。auto-merge 只由 harness 的 workflow_run 触发，而评审与复核都
发生在 CI 结束之后，不重跑判定就不会再有人看这个 PR（设计第 5 节第 4 步）。复核先到、评审后到时，
由 engine/agents/review.py 在评审评论发布后调用同一个函数补上这一环。

    bin/dispatch signoff <PR> --verdict 通过|不通过 --mutations N --caught M --body-file <md> [--designer 席位]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engine.agents import dispatch
from engine.checks import taskbook
from engine.core.common import ROOT, git, setting
from engine.routing import run_check, signals

VERDICTS = ("通过", "不通过")


def build_parser(prog: str = "bin/dispatch signoff", add_help: bool = True) -> argparse.ArgumentParser:
    """参数定义（dispatch 的子命令注册复用同一份，两处不漂移）。"""
    parser = argparse.ArgumentParser(prog=prog, add_help=add_help, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pr", type=int, help="要复核的 PR 编号")
    parser.add_argument("--verdict", choices=VERDICTS, required=True, help="复核结论")
    parser.add_argument("--mutations", type=int, required=True, help="定向变异数")
    parser.add_argument("--caught", type=int, required=True, help="被对应断言抓住的变异数")
    parser.add_argument("--body-file", type=Path, required=True, help="复核正文（Markdown 文件）")
    parser.add_argument("--designer", choices=sorted(taskbook.DESIGNERS),
                        help="任务书取不到 designer 时的席位（缺省取任务书头部）")
    return parser


def designer_of_pr(base: str, head: str, branch: str, cwd: Path, explicit: str | None) -> tuple[str | None, str]:
    """复核人席位：PR 所指任务书头部的 designer 优先；取不到时用 --designer；都没有或非法时返回原因。"""
    target = run_check.scope(base, head, branch, cwd)
    designer = target.header.get("designer") if target and target.taskbook else None
    designer = designer if designer in taskbook.DESIGNERS else explicit
    if designer not in taskbook.DESIGNERS:
        return None, (f"designer 取不到或非法（{designer!r}）：任务书头部没有 designer 时必须用 --designer "
                      f"显式给出，取值 {'、'.join(sorted(taskbook.DESIGNERS))}")
    return designer, ""


def retrigger_if_ready(pr: int, root: Path = ROOT, github=None) -> None:
    """评审与复核都 ok 时，重跑该 head 最近一次由 pull_request 触发的 harness 运行（设计第 5 节第 4 步）。

    限定 pull_request 事件：auto-merge 的 judge 只接受这类运行，重跑 workflow_dispatch 运行进不了判定
    （拆分评审严重项 5）。失败不抛异常、只打印原因——评审/复核本体已经完成，这里的网络问题不能
    改变调用方的退出码；需要时可以手动 gh run rerun。
    """
    try:
        github = github or dispatch.GitHub(root)
        pr_data = json.loads(github._run(["gh", "pr", "view", str(pr),
                                          "--json", "headRefOid,headRefName,baseRefName,comments"]))
        head, branch = pr_data["headRefOid"], pr_data["headRefName"]
        comments = pr_data.get("comments") or []
        git("fetch", "--quiet", "origin", head, pr_data["baseRefName"], cwd=root)
        base = f"origin/{pr_data['baseRefName']}"
        login = setting("identity", "agent_login")
        review = signals.review_status(comments, login, head, base, root)
        signoff = signals.signoff_status(comments, login, head, base, root)
        if review[0] != "ok" or signoff[0] != "ok":
            print(f"PR #{pr}：不重判——独立评审 {review[0]}（{review[1]}），设计方复核 {signoff[0]}（{signoff[1]}）")
            return
        runs = json.loads(github._run(["gh", "run", "list", "--workflow", "harness", "--commit", head,
                                       "--event", "pull_request", "--branch", branch,
                                       "--json", "databaseId,status", "--limit", "1"]))
        if not runs:
            print(f"PR #{pr}：没有该 head 由 pull_request 触发的 harness 运行，不重判")
            return
        run = runs[0]
        if run.get("status") != "completed":
            print(f"PR #{pr}：harness 运行 {run['databaseId']} 还在进行，结束时自动触发 auto-merge，无需重判")
            return
        github._run(["gh", "run", "rerun", str(run["databaseId"])])
        print(f"PR #{pr}：已重跑 harness 运行 {run['databaseId']}（head {head[:7]}），结束后由 auto-merge 判定")
    except Exception as error:  # noqa: BLE001  重判失败不影响已完成的本体（评审/复核评论已发布）
        print(f"重判未触发：{error}；可手动 gh run rerun", file=sys.stderr)


def main(argv: list[str] | None = None, root: Path = ROOT, github=None) -> int:
    args = build_parser().parse_args(argv)
    if args.verdict == "通过" and (args.mutations < 1 or args.caught != args.mutations):
        print("矛盾的「通过」：mutations ≥ 1 且 caught == mutations 才能发布，未发任何评论", file=sys.stderr)
        return 2
    try:
        body = args.body_file.read_text(encoding="utf-8")
    except OSError as error:
        print(f"读不到 --body-file：{error}", file=sys.stderr)
        return 2
    github = github or dispatch.GitHub(root)
    try:
        pr_data = json.loads(github._run(["gh", "pr", "view", str(args.pr),
                                          "--json", "headRefOid,headRefName,baseRefName"]))
        git("fetch", "--quiet", "origin", pr_data["headRefOid"], pr_data["baseRefName"], cwd=root)
        designer, problem = designer_of_pr(f"origin/{pr_data['baseRefName']}", pr_data["headRefOid"],
                                           pr_data["headRefName"], root, args.designer)
    except (RuntimeError, OSError, json.JSONDecodeError, KeyError) as error:
        print(f"复核未发布：读不到 PR 或任务书（{error}）", file=sys.stderr)
        return 1
    if designer is None:
        print(problem, file=sys.stderr)
        return 2
    data = {"verdict": args.verdict, "head": pr_data["headRefOid"], "designer": designer,
            "mutations": args.mutations, "caught": args.caught}
    text = body if not body or body.endswith("\n") else body + "\n"
    try:
        github.comment(args.pr, text + f"{signals.SIGNOFF_MARK}{json.dumps(data, ensure_ascii=False)} -->\n")
    except RuntimeError as error:
        print(f"复核未发布：{error}", file=sys.stderr)
        return 1
    print(f"PR #{args.pr}：复核已评论（{args.verdict}，designer {designer}，定向变异 {args.caught}/{args.mutations}）")
    retrigger_if_ready(args.pr, root, github)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
