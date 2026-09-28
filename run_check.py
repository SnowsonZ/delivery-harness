"""运行记录与任务归属的 CI 侧复核（目标态设计 P5、7.3、10.1）。

派发脚本（bin/dispatch）会写运行记录、给提交带 `Task:`、按预算停在 CI 轮次上限；这里由 Agent 之外的
机器再核对一遍，即使绕过派发脚本手工派发，缺记录或超预算也会被发现：

  适用     分支是 task/<名字> 且 docs/plans/task-<名字>.md 存在，或任一提交带 `Task:`（实现某份任务书的 PR）
  归属     范围内每个非合并提交都带 `Task: <该任务书的编号>`
  运行记录 新增了 docs/runs/<任务书名>/<序号>.json；序号最大的一份格式完整、task/class/branch 与任务书一致、
           exit 为 ok，提示词文件存在且 sha256 与记录一致
  CI 轮次  该分支已完成的 build 轮次（按不同的 head 提交计）不超过任务书 budget.ci_rounds

权威判定在 auto-merge 的 policy.py（main 上的代码，只读 PR 数据）；build 的 harness job 只把结果写进 summary。

    python3 harness/run_check.py --base origin/main [--head HEAD] [--branch task/005-x] [--github]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import taskbook
from common import ROOT, changed_files, commit_field, git

RECORD_FIELDS = (
    "task", "class", "attempt", "branch", "gen_ai.agent.name", "host_version", "gen_ai.request.model",
    "prompt_sha256", "prompt_path", "guard_ref", "started_at", "ended_at", "exit", "retries",
    "failure_signatures", "guard_denials",
)
RECORD_PATH = re.compile(r"^docs/runs/(?P<stem>task-[^/]+)/(?P<number>\d+)\.json$")


@dataclass
class Finding:
    name: str
    ok: bool
    reason: str


@dataclass
class Scope:
    taskbook: str  # docs/plans/task-<名字>.md
    header: dict


def scope(base: str, head: str, branch: str, cwd: Path = ROOT) -> Scope | None:
    """这个 PR 是否在实现某份任务书；任务书从 cwd（auto-merge 中是 main）读取。"""
    shas = git("rev-list", "--no-merges", f"{base}..{head}", cwd=cwd).split()
    ids = {value.strip() for sha in shas for value in commit_field(sha, "Task", cwd)}
    candidates = []
    if branch.startswith("task/"):
        candidates.append(f"docs/plans/task-{branch.removeprefix('task/')}.md")
    for task_id in sorted(ids):
        match = re.fullmatch(r"T(\d+)", task_id)
        if match:
            candidates += sorted(p.relative_to(cwd).as_posix()
                                 for p in (cwd / "docs" / "plans").glob(f"task-{int(match[1]):03d}-*.md"))
    for rel in candidates:
        path = cwd / rel
        if path.is_file():
            try:
                header, _ = taskbook.parse_header(path.read_text(encoding="utf-8"))
            except taskbook.HeaderError:
                continue
            return Scope(rel, header)
    if ids:  # 带了 Task: 却找不到任务书：按适用处理，归属检查会报出来
        return Scope("", {"task": min(ids)})
    return None


def check_trailers(base: str, head: str, task_id: str, cwd: Path) -> Finding:
    missing = [
        sha[:7] for sha in git("rev-list", "--no-merges", f"{base}..{head}", cwd=cwd).split()
        if task_id not in [value.strip() for value in commit_field(sha, "Task", cwd)]
    ]
    if missing:
        return Finding("归属", False, f"提交 {'、'.join(missing)} 没有 `Task: {task_id}`")
    return Finding("归属", True, f"全部提交带 `Task: {task_id}`")


def check_record(base: str, head: str, target: Scope, branch: str, cwd: Path) -> Finding:
    stem = Path(target.taskbook).stem
    records = sorted(
        (int(match["number"]), path)
        for status, path in changed_files(base, head, cwd)
        if status != "D" and (match := RECORD_PATH.match(path)) and match["stem"] == stem
    )
    if not records:
        return Finding("运行记录", False, f"缺运行记录：没有新增 `docs/runs/{stem}/<序号>.json`（未经 bin/dispatch？）")
    number, path = records[-1]
    try:
        record = json.loads(git("show", f"{head}:{path}", cwd=cwd))
    except (json.JSONDecodeError, RuntimeError) as error:
        return Finding("运行记录", False, f"`{path}` 不是有效的 JSON（{error}）")
    missing = [key for key in RECORD_FIELDS if key not in record]
    if missing:
        return Finding("运行记录", False, f"`{path}` 缺字段：{'、'.join(missing)}")
    header = target.header
    expected = {"task": header.get("task"), "class": header.get("class"), "attempt": number}
    if branch.startswith("task/"):
        expected["branch"] = branch
    wrong = [f"{key}={record.get(key)!r}（应为 {value!r}）" for key, value in expected.items() if record.get(key) != value]
    if wrong:
        return Finding("运行记录", False, f"`{path}` 与任务书不一致：{'、'.join(wrong)}")
    if record["exit"] != "ok":
        return Finding("运行记录", False, f"`{path}` 的退出方式为 {record['exit']}，不是 ok")
    # 按原始字节比对（common.git 会去掉结尾换行）。
    prompt = subprocess.run(["git", "show", f"{head}:{record['prompt_path']}"], cwd=cwd, capture_output=True,
                            check=False)
    if prompt.returncode != 0 or hashlib.sha256(prompt.stdout).hexdigest() != record["prompt_sha256"]:
        return Finding("运行记录", False, f"提示词 `{record['prompt_path']}` 缺失或 sha256 与记录不一致")
    return Finding("运行记录", True, f"`{path}`：{record['gen_ai.agent.name']} {record['gen_ai.request.model']}，"
                                     f"重试 {record['retries']} 次")


def ci_rounds(runs: list[dict]) -> int:
    """已完成的 build 轮次：按不同的 head 提交计（同一提交的重跑不算新一轮）。"""
    return len({run.get("headSha") or run.get("head_sha") for run in runs
                if run.get("status") == "completed" and (run.get("headSha") or run.get("head_sha"))})


def check_ci(rounds: int | None, header: dict) -> Finding:
    limit = (header.get("budget") or {}).get("ci_rounds")
    if rounds is None:
        return Finding("CI 轮次", False, "读不到该分支的 build 运行，按超预算处理")
    if not isinstance(limit, int):
        return Finding("CI 轮次", False, "任务书没有 budget.ci_rounds")
    ok = rounds <= limit
    return Finding("CI 轮次", ok, f"已完成 {rounds} 轮，预算 {limit} 轮" + ("" if ok else "：超出预算"))


def check(base: str, head: str, branch: str, cwd: Path = ROOT, rounds: int | None = None,
          with_ci: bool = True) -> list[Finding] | None:
    """不适用（不是在实现任务书）时返回 None。"""
    target = scope(base, head, branch, cwd)
    if target is None:
        return None
    task_id = target.header.get("task", "")
    findings = [check_trailers(base, head, task_id, cwd)]
    if not target.taskbook:
        findings.append(Finding("任务书", False, f"`Task: {task_id}` 在 {cwd.name} 上找不到对应的任务书"))
        return findings
    findings.append(check_record(base, head, target, branch, cwd))
    if with_ci:
        findings.append(check_ci(rounds, target.header))
    return findings


def render(findings: list[Finding] | None) -> str:
    if findings is None:
        return "### 运行记录复核\n\n不适用：不是在实现某份任务书的分支。\n"
    lines = ["### 运行记录复核", "", "| 检查 | 结果 | 理由 |", "|---|---|---|"]
    lines += [f"| {item.name} | {'✅' if item.ok else '❌'} | {item.reason} |" for item in findings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--branch", help="PR 的分支名（默认取 GITHUB_HEAD_REF 或当前分支）")
    parser.add_argument("--github", action="store_true", help="写 GITHUB_STEP_SUMMARY（只报告，不失败）")
    args = parser.parse_args(argv)
    branch = args.branch or os.environ.get("GITHUB_HEAD_REF") or git("rev-parse", "--abbrev-ref", args.head)
    text = render(check(args.base, args.head, branch, with_ci=False))
    print(text)
    if args.github and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(text + "（CI 轮次与合并与否由 auto-merge 的合并路由判定。）\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
