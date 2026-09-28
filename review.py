"""独立评审：R2 及以上的 PR 由非任务设计方的 Agent 评审，结论写回 PR 供用户参考（目标态设计 8.3，决定 5）。

分离规则：评审方不是该任务的设计方（任务书头部 designer；没有任务书时按提交的 Co-Authored-By 判断）；
也不看执行方轨迹，只看任务书、PR 描述、diff 与证据包。默认评审方由 rules.toml [review] reviewer 指定
（用户 2026-09-28 定为 Pi + glm-5.3）；评审方与执行方同一宿主时在评论中标出，供用户判断独立性。

结构上保证只读：评审在单独的 worktree（<仓库名>-review）里进行，Pi 只开放 read、grep、find、ls 且不加载
项目文件，Codex 以 `-s read-only` 沙箱运行，Claude Code 只开放 Read、Grep、Glob；评审方的环境没有 GitHub
凭据，结论由本脚本以 Agent 身份评论到 PR。
试行期内评审结论只是给用户的输入，不替代用户审批。

校准（设计 8.3）：`calibrate` 用已知有问题的改动（由事故回放用例在 main 上重新注入）与已知良好的已合并 PR，
分别统计抓住问题的比例（TPR）与放过好改动的比例（TNR），结果写入 evals/review/results/。

    bin/dispatch review <PR> [--reviewer pi|codex|claude-code]
    python3 harness/review.py calibrate --reviewer pi [--limit N] [--jobs N] [--resume evals/review/results/<文件>]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch
import dispatch_host
import run_check
from common import ROOT, changed_files, commit_field, git, load_rules

PROMPT = Path(__file__).resolve().parent / "review_prompt.md"
EVALS = ROOT / "evals" / "review"
VERDICTS = ("通过", "不通过", "需用户验收")
OTHER = {"claude-code": "codex", "codex": "claude-code"}
REVIEWERS = ("pi", "codex", "claude-code")
LABEL = "needs-independent-review"


@dataclass
class Verdict:
    verdict: str
    findings: list[dict] = field(default_factory=list)
    summary: str = ""
    parsed: bool = True
    failure: str = ""  # 评审方本身失败（报错退出、超时、额度用尽）：不是结论，不计入校准，不评论为结论

    @property
    def flagged(self) -> bool:
        """校准口径：不通过，或有阻断、严重发现，即视为抓住了问题。"""
        return self.verdict == "不通过" or any(item.get("severity") in ("阻断", "严重") for item in self.findings)


def parse_output(text: str) -> Verdict:
    """取最后一个带 verdict 的 JSON 对象；取不到时按「需用户验收」处理并标明未解析。"""
    for line in reversed(text.strip().splitlines()):
        line = line.strip().removeprefix("```json").removeprefix("```").strip()
        if not (line.startswith("{") and '"verdict"' in line):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        verdict = data.get("verdict", "")
        if verdict in VERDICTS:
            findings = [item for item in data.get("findings", []) if isinstance(item, dict)]
            return Verdict(verdict, findings, str(data.get("summary", "")))
    return Verdict("需用户验收", [], "评审输出中没有可解析的结论 JSON", parsed=False)


# ---- 评审方 ----

class Reviewer:
    name = ""

    def argv(self, prompt: str, workspace: Path, output: Path) -> list[str]:
        raise NotImplementedError

    def read(self, stdout: str, output: Path) -> tuple[str, str]:
        """返回 (最终文本, 模型)。"""
        raise NotImplementedError


class CodexReviewer(Reviewer):
    name = "codex"

    def __init__(self, model: str, effort: str | None = None):
        self.model, self.effort = model, effort

    def argv(self, prompt, workspace, output):
        effort = ["-c", f'model_reasoning_effort="{self.effort}"'] if self.effort else []
        return ["codex", "exec", "-s", "read-only", "-m", self.model, *effort, "-C", str(workspace),
                "--skip-git-repo-check", "-o", str(output), prompt]

    def read(self, stdout, output):
        label = f"{self.model} {self.effort}" if self.effort else self.model
        return (output.read_text(encoding="utf-8") if output.exists() else stdout), label


class ClaudeReviewer(Reviewer):
    name = "claude-code"

    def __init__(self, model: str | None = None):
        self.model = model

    def argv(self, prompt, workspace, output):
        command = ["claude", "-p", "--allowedTools", "Read,Grep,Glob", "--output-format", "json"]
        if self.model:
            command += ["--model", self.model]
        return command + [prompt]

    def read(self, stdout, output):
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout, self.model or ""
        model = next(iter(data.get("modelUsage") or {}), self.model or "")
        return str(data.get("result", "")), model


class PiReviewer(Reviewer):
    name = "pi"

    def __init__(self, model: str | None = None):
        self.model = model

    def argv(self, prompt, workspace, output):
        command = ["pi", "-na", "--tools", "read,grep,find,ls", "-p", "--mode", "json", "--no-session"]
        if self.model:
            command += ["--model", self.model]
        return command + [prompt]

    def read(self, stdout, output):
        """取最后一条助手消息的文本与模型（pi 的 json 事件流）。"""
        text, model = "", self.model or ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message") or {}
            if event.get("type") == "message_end" and message.get("role") == "assistant":
                parts = [part.get("text", "") for part in message.get("content", []) if part.get("type") == "text"]
                if any(parts):
                    text = "\n".join(parts)
                model = message.get("model", model)
        return text, model


def make_reviewer(name: str) -> Reviewer:
    config = load_rules().get("review", {})
    if name == "pi":
        return PiReviewer(config.get("pi_model"))
    if name == "codex":
        return CodexReviewer(config.get("codex_model", "gpt-6-sol"), config.get("codex_reasoning_effort"))
    if name == "claude-code":
        return ClaudeReviewer(config.get("claude_model"))
    raise SystemExit(f"未知的评审方：{name}")


def run_reviewer(reviewer: Reviewer, workspace: Path, timeout: float) -> tuple[Verdict, str, float]:
    prompt = PROMPT.read_text(encoding="utf-8")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="review-") as tmp:
        output = Path(tmp) / "last.txt"
        env = dispatch_host.executor_env(dict(os.environ), {}, Path(tmp) / "gh")  # 没有 GitHub 凭据
        (Path(tmp) / "gh").mkdir()
        try:
            result = subprocess.run(reviewer.argv(prompt, workspace, output), cwd=workspace, env=env,
                                    capture_output=True, text=True, timeout=timeout, check=False)
            text, model = reviewer.read(result.stdout, output)
            failure = "" if result.returncode == 0 else _failure_line(result.stdout + result.stderr, result.returncode)
        except subprocess.TimeoutExpired:
            text, model, failure = "", "", f"超过 {int(timeout / 60)} 分钟未完成"
    verdict = parse_output(text)
    if failure or not text.strip():
        verdict = Verdict("评审失败", [], failure or "评审方没有输出", parsed=False, failure=failure or "没有输出")
    return verdict, model, time.monotonic() - started


def _failure_line(output: str, code: int) -> str:
    """评审方报错时取最能说明原因的一行（如额度用尽），不含提示词与材料。"""
    lines = [line.strip() for line in output.splitlines() if line.strip().startswith("ERROR")]
    return (lines[-1] if lines else f"退出码 {code}")[:300]


# ---- 分离规则与材料 ----

def designer_of(base: str, head: str, branch: str, cwd: Path) -> str | None:
    target = run_check.scope(base, head, branch, cwd)
    if target is not None and target.header.get("designer") in OTHER:
        return target.header["designer"]
    trailers = " ".join(value for sha in git("rev-list", "--no-merges", f"{base}..{head}", cwd=cwd).split()
                        for value in commit_field(sha, "Co-Authored-By", cwd))
    if "Claude" in trailers:
        return "claude-code"
    if "Codex" in trailers or "OpenAI" in trailers:
        return "codex"
    return None


def review_workspace(root: Path, index: int = 0) -> Path:
    """评审工作区：第 0 个为 <仓库名>-review，并行校准时另建 <仓库名>-review-<n>。"""
    main = dispatch.main_checkout(root)
    workspace = main.parent / (f"{main.name}-review" + (f"-{index}" if index else ""))
    if not workspace.exists():
        git("worktree", "add", "--detach", str(workspace), "origin/main", cwd=root)
    return workspace


def checkout(workspace: Path, ref: str) -> None:
    git("checkout", "--quiet", "--force", "--detach", ref, cwd=workspace)
    git("clean", "-ffdxq", "-e", dispatch.VENV, cwd=workspace)


def write_materials(workspace: Path, base: str, pr_text: str, task_text: str) -> None:
    folder = workspace / "build" / "review"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pr.md").write_text(pr_text, encoding="utf-8")
    (folder / "task.md").write_text(task_text or "无", encoding="utf-8")
    diff = git("diff", "--no-color", f"{base}...HEAD", cwd=workspace)
    (folder / "diff.patch").write_text(diff[:200_000], encoding="utf-8")
    pack = subprocess.run([sys.executable, str(workspace / "harness" / "review_pack.py"), "--base", base, "--no-run"],
                          cwd=workspace, capture_output=True, text=True, check=False)
    (folder / "pack.md").write_text(pack.stdout or pack.stderr, encoding="utf-8")


def run_check_records(workspace: Path, base: str) -> list[dict]:
    """本 PR 新增的运行记录（用于判断评审方是否与执行方同一宿主）。"""
    records = []
    for status, path in changed_files(base, "HEAD", workspace):
        if status != "D" and run_check.RECORD_PATH.match(path):
            try:
                records.append(json.loads((workspace / path).read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return records


def render_comment(verdict: Verdict, reviewer: str, model: str, designer: str | None, head: str, seconds: float,
                   same_host: bool = False) -> str:
    lines = [
        f"### 独立评审（试行）：{verdict.verdict}", "",
        f"- 评审方：{reviewer}（{model or '模型未报告'}）；设计方：{designer or '未能判定'}；评审的提交：`{head[:12]}`；用时 {seconds / 60:.1f} 分钟",
        "- 只读评审：只看任务书、PR 描述、diff 与证据包，不看执行过程；结论供用户参考，不替代用户审批。",
    ]
    if same_host:
        lines.append(f"- ⚠️ 评审方与本 PR 的执行方同为 {reviewer}：独立性较弱，请结合自己的判断。")
    lines += ["", f"**结论**：{verdict.summary or '（无）'}", ""]
    if verdict.findings:
        lines += ["| 严重度 | 位置 | 问题 | 修复要求 |", "|---|---|---|---|"]
        lines += [f"| {item.get('severity', '')} | {item.get('location', '')} | {item.get('problem', '')} | "
                  f"{item.get('fix', '')} |" for item in verdict.findings]
    else:
        lines.append("没有发现。")
    data = {"verdict": verdict.verdict, "reviewer": reviewer, "model": model, "head": head,
            "findings": len(verdict.findings), "flagged": verdict.flagged, "parsed": verdict.parsed}
    lines += ["", f"<!-- independent-review {json.dumps(data, ensure_ascii=False)} -->"]
    return "\n".join(lines) + "\n"


def review_pr(number: int, reviewer_name: str | None, root: Path = ROOT, github=None) -> int:
    github = github or dispatch.GitHub(root)
    pr = json.loads(github._run(["gh", "pr", "view", str(number), "--json", "title,body,headRefName,headRefOid,baseRefName"]))
    workspace = review_workspace(root)
    git("fetch", "--quiet", "origin", f"pull/{number}/head", "main", cwd=workspace)
    checkout(workspace, pr["headRefOid"])
    base = git("merge-base", "origin/main", "HEAD", cwd=workspace)
    designer = designer_of(base, "HEAD", pr["headRefName"], workspace)
    name = reviewer_name or load_rules().get("review", {}).get("reviewer") or OTHER.get(designer or "")
    if name is None:
        print("未能判定评审方：用 --reviewer 指定（不能是设计方本身）")
        return 2
    if name == designer:
        print(f"评审方 {name} 就是设计方，违反分离规则")
        return 2
    target = run_check.scope(base, "HEAD", pr["headRefName"], workspace)
    task_text = (workspace / target.taskbook).read_text(encoding="utf-8") if target and target.taskbook else ""
    write_materials(workspace, base, f"# {pr['title']}\n\n{pr['body']}", task_text)
    verdict, model, seconds = run_reviewer(make_reviewer(name), workspace, load_rules().get("review", {}).get(
        "timeout_minutes", 30) * 60)
    if verdict.failure:
        print(f"PR #{number}：评审方失败（{verdict.failure}），没有评论，保留待评审标签")
        return 1
    executors = {record.get("gen_ai.agent.name") for record in run_check_records(workspace, base)}
    github.comment(number, render_comment(verdict, name, model, designer, pr["headRefOid"], seconds,
                                          same_host=name in executors))
    try:
        github._run(["gh", "pr", "edit", str(number), "--remove-label", LABEL], agent=True)
    except RuntimeError:
        pass  # 没有该标签
    print(f"PR #{number}：{verdict.verdict}（{name}），已评论")
    return 0


# ---- 校准 ----

def calibration_samples(root: Path = ROOT) -> list[dict]:
    """已知有问题：事故回放用例（在 main 上重新注入）；已知良好：manifest 中列出的已合并 PR。"""
    from replay_cases import CASES

    manifest = json.loads((EVALS / "samples.json").read_text(encoding="utf-8"))
    bad = [{"kind": "bad", "id": f"{case.defect}:{index}", "title": manifest["bad_title"], "file": case.file,
            "find": case.find, "replace": case.replace}
           for index, case in enumerate(CASES) if case.defect not in manifest.get("skip_defects", [])]
    good = [{"kind": "good", "id": f"PR{number}", "pr": number} for number in manifest["good_prs"]]
    return bad + good


def prepare_sample(workspace: Path, sample: dict, main_ref: str, root: Path) -> tuple[str, str]:
    """把工作区准备成「PR 的 head」，返回 (base, PR 文本)。"""
    if sample["kind"] == "bad":
        checkout(workspace, main_ref)
        path = workspace / sample["file"]
        text = path.read_text(encoding="utf-8")
        if text.count(sample["find"]) != 1:
            raise ValueError(f"{sample['id']} 的注入点在 {main_ref[:7]} 上不唯一或已过期")
        path.write_text(text.replace(sample["find"], sample["replace"]), encoding="utf-8")
        # 用底层命令建提交（不经过提交钩子，也不动任何分支）：样本只存在于评审工作区，不推送。
        env = {**os.environ, "GIT_AUTHOR_NAME": "calibration", "GIT_AUTHOR_EMAIL": "calibration@example.com",
               "GIT_COMMITTER_NAME": "calibration", "GIT_COMMITTER_EMAIL": "calibration@example.com"}
        git("add", "--", sample["file"], cwd=workspace)
        tree = git("write-tree", cwd=workspace)
        commit = subprocess.run(["git", "commit-tree", tree, "-p", "HEAD", "-m", sample["title"]], cwd=workspace,
                                env=env, capture_output=True, text=True, check=True).stdout.strip()
        git("checkout", "--quiet", "--detach", commit, cwd=workspace)
        return main_ref, f"# {sample['title']}\n\n整理代码，行为不变。\n"
    merge = git("log", "--merges", "--first-parent", "--format=%H %s", "origin/main", cwd=root)
    sha = next((line.split()[0] for line in merge.splitlines() if f"#{sample['pr']} " in line + " "), None)
    if sha is None:
        raise ValueError(f"找不到 PR #{sample['pr']} 的合并提交")
    checkout(workspace, f"{sha}^2")
    body = git("log", "-1", "--format=%B", sha, cwd=root)
    return git("rev-parse", f"{sha}^1", cwd=root), f"# PR #{sample['pr']}\n\n{body}\n"


MAX_CONSECUTIVE_FAILURES = 3


def calibrate(reviewer_name: str, limit: int | None = None, root: Path = ROOT, resume: Path | None = None,
              reviewer: Reviewer | None = None, jobs: int = 1) -> dict:
    """逐个样本评审，每完成一个就写盘；--resume 跳过已有有效结论的样本；--jobs 并行（每个任务独占一个评审
    工作区）；评审方连续失败 3 次即停（如额度用尽）。"""
    workspace = review_workspace(root)
    git("fetch", "--quiet", "origin", "main", cwd=workspace)
    reviewer = reviewer or make_reviewer(reviewer_name)
    previous = json.loads(resume.read_text(encoding="utf-8")) if resume else {}
    main_ref = previous.get("main") or git("rev-parse", "origin/main", cwd=root)
    done = {item["id"]: item for item in previous.get("samples", []) if item.get("parsed") and "error" not in item}
    record = {"reviewer": reviewer_name, "model": previous.get("model", ""), "main": main_ref,
              "date": previous.get("date") or dt.date.today().isoformat(), "samples": list(done.values())}
    out = resume or EVALS / "results" / f"{record['date']}-{reviewer_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    pending = [sample for sample in calibration_samples(root)[:limit] if sample["id"] not in done]
    # 每个并行任务独占一个评审工作区（git worktree 的创建串行进行，避免锁冲突）。
    workspaces = Queue()
    for index in range(max(1, min(jobs, len(pending)))):
        workspaces.put(workspace if index == 0 else review_workspace(root, index))

    def review_one(sample: dict) -> dict:
        space = workspaces.get()
        try:
            try:
                base, pr_text = prepare_sample(space, sample, main_ref, root)
            except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                return {"kind": sample["kind"], "id": sample["id"], "error": str(error)}
            write_materials(space, base, pr_text, "")
            verdict, model, seconds = run_reviewer(reviewer, space, 30 * 60)
            return {"kind": sample["kind"], "id": sample["id"], "verdict": verdict.verdict, "flagged": verdict.flagged,
                    "parsed": verdict.parsed, "failure": verdict.failure, "seconds": round(seconds), "model": model}
        finally:
            workspaces.put(space)

    failures = 0
    with ThreadPoolExecutor(max_workers=workspaces.qsize()) as pool:
        futures = [pool.submit(review_one, sample) for sample in pending]
        for future in as_completed(futures):
            item = future.result()
            record["model"] = item.pop("model", "") or record["model"]
            record["samples"].append(item)
            record.update(score(record["samples"]))
            out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            failure = item.get("failure", "")
            print(f"{item['id']}：{item.get('verdict', '出错')}{'（抓住）' if item.get('flagged') else ''}"
                  f"{f'：{failure}' if failure else ''}{item.get('error', '')}", flush=True)
            failures = failures + 1 if failure else 0
            if failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"评审方连续失败 {failures} 次，停止；恢复后运行 calibrate --resume {out}")
                for other in futures:
                    other.cancel()
                break
    record.update(score(record["samples"]))
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(score(record["samples"]), ensure_ascii=False))
    return record


def score(results: list[dict]) -> dict:
    """只统计有效结论：样本准备出错与评审方失败都不计入 TPR、TNR（否则失败会被当成「放过」）。"""
    valid = [item for item in results if "error" not in item and item.get("parsed", True)]
    bad = [item for item in valid if item["kind"] == "bad"]
    good = [item for item in valid if item["kind"] == "good"]
    tp = sum(item["flagged"] for item in bad)
    tn = sum(not item["flagged"] for item in good)
    return {
        "bad": len(bad), "good": len(good), "errors": sum("error" in item for item in results),
        "tpr": round(tp / len(bad), 3) if bad else None,
        "tnr": round(tn / len(good), 3) if good else None,
        "unparsed": sum(not item.get("parsed", True) for item in results if "error" not in item),
        "total": len(results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("pr", help="评审一个 PR")
    pr.add_argument("number", type=int)
    pr.add_argument("--reviewer", choices=REVIEWERS)
    cal = sub.add_parser("calibrate", help="在校准集上统计 TPR 与 TNR")
    cal.add_argument("--reviewer", choices=REVIEWERS, required=True)
    cal.add_argument("--limit", type=int)
    cal.add_argument("--resume", type=Path, help="接着上次的结果文件继续（跳过已有有效结论的样本）")
    cal.add_argument("--jobs", type=int, default=1, help="并行评审数（每个占一个评审工作区）")
    args = parser.parse_args(argv)
    if args.command == "pr":
        return review_pr(args.number, args.reviewer)
    calibrate(args.reviewer, args.limit, resume=args.resume, jobs=args.jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
