"""独立评审：R2 及以上的 PR 由非任务设计方的 Agent 评审，结论写回 PR 供用户参考（目标态设计 8.3，决定 5）。

分离规则：评审方不是该任务的设计方（任务书头部 designer；没有任务书时按提交的 Co-Authored-By 判断）；
也不看执行方轨迹，只看任务书、PR 描述、diff 与证据包。材料里的任务书在既有探测（run_check.scope）
未命中时，从 PR 标题与正文的 `docs/plans/task-*.md` 引用回退（B68），探测命中时行为不变。
默认评审方与各评审方的模型由 rules.toml [review]
指定（不配模型时沿用评审方工具自己的默认设置）；评审方与执行方同一宿主时在评论中标出，供用户判断独立性。

结构上保证只读：评审在单独的 worktree（<仓库名>-review）里进行，Pi 只开放 read、grep、find、ls 且不加载
项目文件，Codex 以 `-s read-only` 沙箱运行，Claude Code 只开放 Read、Grep、Glob；评审方的环境没有 GitHub
凭据，结论由本脚本以 Agent 身份评论到 PR。
试行期内评审结论只是给用户的输入，不替代用户审批。

校准（设计 8.3）有两个入口：`calibrate` 用已知有问题的改动（由事故回放用例在 main 上重新注入）与
已知良好的已合并 PR，统计抓住问题的比例（TPR）与放过好改动的比例（TNR），结果写入 evals/review/results/；
`review_calibrate`（B59）对 docs/review/calibration/samples.json 里的真实历史样本（pr + head + 期望结论）逐个重跑
独立评审，统计 TPR/TNR 与逐样本偏差，报告写本地 markdown，不评论任何 GitHub PR。

    bin/dispatch review <PR> [--reviewer pi|codex|claude-code]
    bin/dispatch review-calibrate [--reviewer 名称] [--output 路径]   评审校准打分（B59，不评论 PR）
    bin/harness review calibrate --reviewer pi [--limit N] [--jobs N] [--resume evals/review/results/<文件>]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import ClassVar

from engine.agents import dispatch, dispatch_host, review_lock, review_pack_io
from engine.agents import dispatch_observation as observation

# T707 拆分：B59 真实历史样本校准段移出到子模块，这里重新导出，engine.agents.review.X 的既有用法不变。
from engine.agents.review_calibration import (  # noqa: F401  重新导出（T707）
    _cell,
    load_samples,
    prepare_head_sample,
    render_calibration_report,
    review_calibrate,
    sample_deviates,
    score_samples,
)
from engine.core import alerts
from engine.core.common import ENGINE_DIR, ROOT, changed_files, commit_field, git, load_rules, setting
from engine.routing import run_check, signals

PROMPT = ENGINE_DIR / "prompts" / "review_prompt.md"
EVALS = ROOT / "evals" / "review"
CALIBRATION_SAMPLES = ROOT / "docs" / "review" / "calibration" / "samples.json"
VERDICTS = ("通过", "不通过", "需用户验收")
OTHER = {"claude-code": "codex", "codex": "claude-code"}
REVIEWERS = ("opencode", "pi", "codex", "claude-code")
LABEL = "needs-independent-review"


@dataclass
class Verdict:
    verdict: str
    findings: list[dict] = field(default_factory=list)
    summary: str = ""
    parsed: bool = True
    failure: str = ""  # 评审方本身失败（报错退出、超时、额度用尽）：不是结论，不计入校准，不评论为结论
    error_kind: str = ""  # 失败的机器类别（timeout / reviewer_exit / no_output），供评审观察事件
    audit_model: str | None = None  # 审计摘要用的模型（明确报告优先，其次显式请求；两者皆无为 None）
    model_basis: str = "unknown"  # reported / explicit_request / unknown（共用合同 C6）

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
    model_name: str | None = None  # 显式请求的模型（审计摘要用；展示名可带档位等修饰）
    env: ClassVar[dict[str, str]] = {}  # 评审方专用的环境变量（如 OpenCode 的权限配置）

    def argv(self, prompt: str, workspace: Path, output: Path) -> list[str]:
        raise NotImplementedError

    def read(self, stdout: str, output: Path) -> tuple[str, str, str]:
        """返回 (最终文本, 展示用模型, model_basis：reported / explicit_request / unknown)。"""
        raise NotImplementedError


class CodexReviewer(Reviewer):
    name = "codex"

    def __init__(self, model: str | None, effort: str | None = None):
        self.model, self.effort = model, effort
        self.model_name = model

    def argv(self, prompt, workspace, output):
        # 没配置模型时沿用 Codex 自己的默认设置。
        model = ["-m", self.model] if self.model else []
        effort = ["-c", f'model_reasoning_effort="{self.effort}"'] if self.effort else []
        # B81：关闭子代理（实测每次评审另派 2 个子代理，占评审输入 token 的 53%，而 9 条严重发现
        # 里主会话自己查到 8 条；关掉后输入从平均 3.8M 降到 0.96M，同样查出严重问题，2026-10-04）。
        # 插在推理强度之后、-C 之前：第 0–5 位不变（消费方契约测试断言 argv[5] 是模型名）。
        no_subagents = ["-c", "agents.enabled=false"]
        return ["codex", "exec", "-s", "read-only", *model, *effort, *no_subagents, "-C", str(workspace),
                "--skip-git-repo-check", "-o", str(output), prompt]

    def read(self, stdout, output):
        base = self.model or "codex 默认模型"
        label = f"{base} {self.effort}" if self.effort else base
        basis = "explicit_request" if self.model else "unknown"
        return (output.read_text(encoding="utf-8") if output.exists() else stdout), label, basis


class ClaudeReviewer(Reviewer):
    name = "claude-code"

    def __init__(self, model: str | None = None):
        self.model = model
        self.model_name = model

    def argv(self, prompt, workspace, output):
        command = ["claude", "-p", "--allowedTools", "Read,Grep,Glob", "--output-format", "json"]
        if self.model:
            command += ["--model", self.model]
        return command + [prompt]

    def read(self, stdout, output):
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout, self.model or "", "explicit_request" if self.model else "unknown"
        reported = next(iter(data.get("modelUsage") or {}), "")
        model = reported or self.model or ""
        basis = "reported" if reported else ("explicit_request" if self.model else "unknown")
        return str(data.get("result", "")), model, basis


class PiReviewer(Reviewer):
    name = "pi"

    def __init__(self, model: str | None = None):
        self.model = model
        self.model_name = model

    def argv(self, prompt, workspace, output):
        command = ["pi", "-na", "--tools", "read,grep,find,ls", "-p", "--mode", "json", "--no-session"]
        if self.model:
            command += ["--model", self.model]
        return command + [prompt]

    def read(self, stdout, output):
        """取最后一条助手消息的文本与模型（pi 的 json 事件流）。"""
        text, model, reported = "", self.model or "", ""
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
                if message.get("model"):
                    reported = message["model"]
                model = message.get("model", model)
        basis = "reported" if reported else ("explicit_request" if self.model else "unknown")
        return text, model, basis


class OpenCodeReviewer(Reviewer):
    """OpenCode 自带的 plan 代理只读（拒绝编辑与写入）；--pure 不加载插件（含 PR 自带的项目插件）。"""

    name = "opencode"
    # plan 代理只禁止编辑，不禁止 bash（2026-09-28 实测它在评审中运行了 PR 的测试与 gh）：这里关掉命令、
    # 子代理、网页与写入类工具，只留读取与搜索。
    env: ClassVar[dict[str, str]] = {"OPENCODE_CONFIG_CONTENT": json.dumps({
        "permission": {"bash": "deny", "edit": "deny", "webfetch": "deny"},
        "tools": {"bash": False, "task": False, "webfetch": False, "write": False, "edit": False, "patch": False},
    })}

    def __init__(self, model: str | None = None):
        self.model = model
        self.model_name = model

    def argv(self, prompt, workspace, output):
        command = ["opencode", "run", "--pure", "--agent", "plan", "--format", "json", "--dir", str(workspace)]
        if self.model:
            command += ["-m", self.model]
        return command + [prompt]

    def read(self, stdout, output):
        """取事件流里的全部文本片段（结论 JSON 在最后）；模型取配置值（事件里不报模型名）。"""
        texts = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            part = event.get("part") or {}
            if event.get("type") == "text" and part.get("type") == "text":
                texts.append(part.get("text", ""))
        return "\n".join(texts), self.model or "", "explicit_request" if self.model else "unknown"


def make_reviewer(name: str) -> Reviewer:
    config = load_rules().get("review", {})
    if name == "opencode":
        return OpenCodeReviewer(config.get("opencode_model"))
    if name == "pi":
        return PiReviewer(config.get("pi_model"))
    if name == "codex":
        return CodexReviewer(config.get("codex_model"), config.get("codex_reasoning_effort"))
    if name == "claude-code":
        return ClaudeReviewer(config.get("claude_model"))
    raise SystemExit(f"未知的评审方：{name}")


def run_reviewer(reviewer: Reviewer, workspace: Path, timeout: float) -> tuple[Verdict, str, float]:
    prompt = PROMPT.read_text(encoding="utf-8")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="review-") as tmp:
        output = Path(tmp) / "last.txt"
        env = dispatch_host.executor_env(dict(os.environ), {}, Path(tmp) / "gh")  # 没有 GitHub 凭据
        env.update(reviewer.env)
        (Path(tmp) / "gh").mkdir()
        try:
            # 标准输入必须关闭：opencode run 在标准输入是管道时会一直等它结束（2026-09-28 实测卡住的原因）。
            result = subprocess.run(reviewer.argv(prompt, workspace, output), cwd=workspace, env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                                    check=False)
            text, model, basis = reviewer.read(result.stdout, output)
            failure = "" if result.returncode == 0 else _failure_line(result.stdout + result.stderr, result.returncode)
            error_kind = "" if result.returncode == 0 else "reviewer_exit"
        except subprocess.TimeoutExpired:
            text, model, basis = "", "", "unknown"
            failure, error_kind = f"超过 {int(timeout / 60)} 分钟未完成", "timeout"
    verdict = parse_output(text)
    if failure or not text.strip():
        verdict = Verdict("评审失败", [], failure or "评审方没有输出", parsed=False, failure=failure or "没有输出",
                          error_kind=error_kind or "no_output")
    # C6：模型明确报告优先；其次实际命令显式指定的请求模型；两者皆无不把配置默认值当已调用模型。
    verdict.audit_model = model if basis == "reported" else (
        reviewer.model_name if basis == "explicit_request" else None)
    verdict.model_basis = basis
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
    git("clean", "-ffdxq", *[arg for path in dispatch.preserved_paths() for arg in ("-e", path)], cwd=workspace)


def review_base(pr: dict, workspace: Path) -> str:
    """评审的比较基点。已合并的 PR 用合并提交的第一个父提交：此时 head 已在 main 上，与 main 的合并基点
    就是 head 本身，diff 为空（2026-09-28 对 #62 的首次试行即因此拿到空材料，被评审方指出）。"""
    merge = (pr.get("mergeCommit") or {}).get("oid")
    if pr.get("state") == "MERGED" and merge:
        return git("rev-parse", f"{merge}^1", cwd=workspace)
    return git("merge-base", "origin/main", "HEAD", cwd=workspace)


CI_REASON_LIMIT = 600  # 读不到时写进 ci.md 的原因文本上限（各取尾部），够诊断又不撑爆材料


def _rollup_lines(rollup: list) -> tuple[list[str], int]:
    """statusCheckRollup → (展示行, 跳过的项数)。CheckRun 取 name、conclusion（未完成时取 status）、detailsUrl；
    StatusContext 取 context、state、targetUrl；不是对象或没有名称的项跳过并计数，不静默当作没有检查。"""
    lines: list[str] = []
    skipped = 0
    for item in rollup:
        if not isinstance(item, dict):
            skipped += 1
            continue
        name = item.get("name") or item.get("context")
        if not name:
            skipped += 1
            continue
        if "context" in item and "name" not in item:
            state, link = item.get("state") or "UNKNOWN", item.get("targetUrl") or ""
        else:
            status = item.get("status") or ""
            state = (item.get("conclusion") if status == "COMPLETED" else status) or "UNKNOWN"
            link = item.get("detailsUrl") or ""
        lines.append(f"- {name}：{state}（{link}）")
    return lines, skipped


def ci_summary(number: int, github) -> str:
    """当前 head 的 CI 检查结论与链接（评审方据此核对 PR 描述中「CI 通过」的说法）。

    先用 `gh pr checks --json`（成功时输出格式不变）；它失败或形状不对时回退 `gh pr view --json statusCheckRollup`；
    两条都读不到时写明各自的原因，而不是只写「读不到」——#175 的评审材料里只剩一句「读不到」，事后无从诊断。"""
    reasons: list[str] = []
    try:
        checks = json.loads(github._run(["gh", "pr", "checks", str(number), "--json", "name,state,link"]))
        return "\n".join(f"- {item['name']}：{item['state']}（{item['link']}）" for item in checks) or "没有 CI 检查。"
    except (RuntimeError, json.JSONDecodeError, TypeError, KeyError) as error:
        reasons.append(f"gh pr checks：{error}")
    try:
        data = json.loads(github._run(["gh", "pr", "view", str(number), "--json", "statusCheckRollup"]))
        rollup = data.get("statusCheckRollup") if isinstance(data, dict) else None
        if not isinstance(rollup, list):
            raise TypeError(f"statusCheckRollup 不是列表（{type(rollup).__name__}）")
        lines, skipped = _rollup_lines(rollup)
        if skipped:
            lines.append(f"- （另有 {skipped} 项缺少名称或形状异常，未能列出：这份 CI 结论不完整）")
        return "\n".join(lines) or "没有 CI 检查。"
    except (RuntimeError, json.JSONDecodeError, TypeError, ValueError) as error:
        reasons.append(f"gh pr view：{error}")
    detail = "；".join(reasons)
    return f"读不到 CI 检查结果：{detail[-CI_REASON_LIMIT:]}"


def taskbook_from_pr_text(base: str, title: str, body: str, workspace: Path) -> tuple[str, str]:
    """B68 回退：既有任务书探测（run_check.scope）未命中时，从 PR 标题与正文提取 `docs/plans/task-*.md`
    引用；引用文件在 base 上存在且 head 工作区仍可读时，返回 (相对路径, 全文)（与探测成功同一读取方式），
    否则返回 ("", "")（task.md 维持「无」）。只读 base 与工作区，不影响 designer_of 与评审只读性。"""
    for rel in dict.fromkeys(re.findall(r"docs/plans/task-[\w.-]+\.md", f"{title}\n{body}")):
        if not git("show", f"{base}:{rel}", cwd=workspace, check=False):
            continue  # base 上没有这份任务书
        try:
            return rel, (workspace / rel).read_text(encoding="utf-8")
        except OSError:
            continue  # head 上已被本 PR 删除：task_v1 无法按 路径@head 复取，视为未命中
    return "", ""


def write_materials(workspace: Path, base: str, pr_text: str, task_text: str, ci_text: str = "") -> None:
    folder = workspace / "build" / "review"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pr.md").write_text(pr_text, encoding="utf-8")
    (folder / "task.md").write_text(task_text or "无", encoding="utf-8")
    (folder / "ci.md").write_text(ci_text or "（校准样本没有 CI 运行）", encoding="utf-8")
    review_pack_io.write_diff(workspace, base)  # T124：diff 不再 200K 截断，超 2MB 按文件边界分片
    # 评审材料用被评审提交自己的引擎生成；迁移前的旧布局提交里引擎在 harness/ 平铺。
    entry = workspace / ".harness" / "engine" / "cli.py"
    command = [str(entry), "review-pack"] if entry.exists() else [str(workspace / "harness" / "review_pack.py")]
    pack = subprocess.run([sys.executable, *command, "--base", base, "--no-run"],
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


def _escape(text) -> str:
    """评审方输出的纵深防御（T702）：`<!--` 一律转义成 `&lt;!--`，模型输出不能在评论里写出 HTML
    注释。T701 的防混入（标记合计恰好一次、独占一行、位置固定）是第一道防线，这里是第二道；
    引擎自己生成的两个标记行不经这里，原样保留。"""
    return str(text).replace("<!--", "&lt;!--")


def render_comment(verdict: Verdict, reviewer: str, model: str, designer: str | None, head: str, seconds: float,
                   same_host: bool = False, audit: dict | None = None) -> str:
    lines = [
        f"### 独立评审（试行）：{verdict.verdict}", "",
        f"- 评审方：{reviewer}（{model or '模型未报告'}）；设计方：{designer or '未能判定'}；评审的提交：`{head[:12]}`；用时 {seconds / 60:.1f} 分钟",
        "- 只读评审：只看任务书、PR 描述、diff 与证据包，不看执行过程；结论供用户参考，不替代用户审批。",
    ]
    if same_host:
        lines.append(f"- ⚠️ 评审方与本 PR 的执行方同为 {reviewer}：独立性较弱，请结合自己的判断。")
    lines += ["", f"**结论**：{_escape(verdict.summary) or '（无）'}", ""]
    if verdict.findings:
        lines += ["| 严重度 | 位置 | 问题 | 修复要求 |", "|---|---|---|---|"]
        lines += [f"| {_escape(item.get('severity'))} | {_escape(item.get('location'))} | "
                  f"{_escape(item.get('problem'))} | "
                  f"{_escape(item.get('fix'))} |" for item in verdict.findings]
    else:
        lines.append("没有发现。")
    data = {"verdict": verdict.verdict, "reviewer": reviewer, "model": model, "head": head,
            "findings": len(verdict.findings), "flagged": verdict.flagged, "parsed": verdict.parsed}
    lines += ["", f"<!-- independent-review {json.dumps(data, ensure_ascii=False)} -->"]
    if audit is not None:
        # C6 审计摘要：与旧标记并列，单行 JSON，字段与材料编码见共用合同 C6；T305 只解析这个新标记。
        lines.append(f"<!-- harness-review-audit {json.dumps(audit, ensure_ascii=False)} -->")
    return "\n".join(lines) + "\n"


def review_pr(number: int, reviewer_name: str | None, root: Path = ROOT, github=None) -> int:
    github = github or dispatch.GitHub(root)
    pr = json.loads(github._run(["gh", "pr", "view", str(number), "--json",
                                 "title,body,headRefName,headRefOid,baseRefName,state,mergeCommit"]))
    trace = pr["headRefName"]  # 评审事件的显式 trace：PR 的 headRefName
    head = pr["headRefOid"]
    workspace = review_workspace(root)
    # E128-R1（逃逸 #128）：所有评审入口共用这批工作区，从 checkout 到评论发布整段持锁，
    # 并发评审串行进行（#119 补审与 #120 评审同时运行时材料被覆盖）。等待上限取评审超时的两倍。
    timeout = load_rules().get("review", {}).get("timeout_minutes", 30) * 60 * 2
    with review_lock.workspace_lock(root, workspace, timeout, purpose=f"review_pr #{number}"):
        git("fetch", "--quiet", "origin", f"pull/{number}/head", "main", cwd=workspace)
        checkout(workspace, head)
        base = review_base(pr, workspace)
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
        taskbook_rel = target.taskbook if target and target.taskbook else ""
        if not taskbook_rel:  # 探测未命中才回退，命中时行为逐字不变
            taskbook_rel, task_text = taskbook_from_pr_text(base, pr["title"], pr["body"] or "", workspace)
        write_materials(workspace, base, f"# {pr['title']}\n\n{pr['body']}", task_text, ci_summary(number, github))
        materials = observation.review_materials(workspace, base, head, taskbook_rel or None, number)
        executors = {record.get("gen_ai.agent.name") for record in run_check_records(workspace, base)}
        independent = None if designer is None else name != designer
        verdict, model, seconds = run_reviewer(make_reviewer(name), workspace, load_rules().get("review", {}).get(
            "timeout_minutes", 30) * 60)
        if verdict.failure:
            observation.review(trace=trace, head=head, reviewer=name, verdict=verdict.verdict,
                               duration_ms=int(seconds * 1000), findings=verdict.findings, materials=materials,
                               designer=designer, model=verdict.audit_model, model_basis=verdict.model_basis,
                               parsed=verdict.parsed, independent=independent, same_host=name in executors,
                               failure=verdict.failure, error_kind=verdict.error_kind)
            alerts.publish(trace, "review_error", pr=number, gh=github)  # 评审方自身失败也复用发布（D067）
            print(f"PR #{number}：评审方失败（{verdict.failure}），没有评论，保留待评审标签")
            return 1
        same_host = name in executors
        audit = observation.review_audit(trace_id=trace, head=head, base=base, reviewer=name,
                                         model=verdict.audit_model, model_basis=verdict.model_basis,
                                         designer=designer,
                                         implementers=sorted(item for item in executors if item),
                                         independent=independent, same_host=same_host, parsed=verdict.parsed,
                                         verdict=verdict.verdict, duration_ms=int(seconds * 1000),
                                         findings=verdict.findings, materials=materials)
        body = render_comment(verdict, name, model, designer, head, seconds, same_host, audit)
        url = github.comment(number, body)
        # 事件在评论实际发布后写：URL 与评论字节哈希只有此刻可知（C6：摘要不预写 URL、不含自身哈希）。
        observation.review(trace=trace, head=head, reviewer=name, verdict=verdict.verdict,
                           duration_ms=int(seconds * 1000), findings=verdict.findings, materials=materials,
                           designer=designer, model=verdict.audit_model, model_basis=verdict.model_basis,
                           parsed=verdict.parsed, independent=independent, same_host=same_host,
                           comment=url, comment_body=body)
        # 否决或带严重发现（判据与 signals.review_status 共用同一纯函数，T715）：关闭可能已开启的
        # 自动合并；disable_auto_merge 失败只打印，不改变评审结论与退出码。
        if signals.review_marker_status({"verdict": verdict.verdict, "flagged": verdict.flagged})[0] == "fail":
            github.disable_auto_merge(number)
        try:
            github._run(["gh", "pr", "edit", str(number), "--remove-label", LABEL], agent=True)
        except RuntimeError:
            pass  # 没有该标签
        if verdict.verdict == "不通过":  # D067：评审否决在已有评论发布后补标签/告警，不重跑评审
            alerts.publish(trace, "review_rejected", pr=number, gh=github)
        if verdict.verdict == "通过" and not verdict.flagged:
            # T702：复核先到、评审后到时由这一步触发重判（两样都齐了才轮到 auto-merge，而它只由
            # harness 的 workflow_run 触发）。按需导入：signoff 依赖 dispatch，与本模块同层。
            from engine.agents import signoff

            signoff.retrigger_if_ready(number, root, github)
        print(f"PR #{number}：{verdict.verdict}（{name}），已评论")
        return 0


# ---- 后台自动评审 ----

REVIEW_MARK = "<!-- independent-review "
DONE_CHECKS = {"SUCCESS", "SKIPPED", "NEUTRAL"}


def reviewed_heads(comments: list[dict]) -> set[str]:
    heads = set()
    for comment in comments:
        body = comment.get("body", "")
        if REVIEW_MARK in body:
            try:
                heads.add(json.loads(body.split(REVIEW_MARK, 1)[1].split(" -->", 1)[0])["head"])
            except (ValueError, KeyError):
                continue
    return heads


def pending_prs(github, root: Path = ROOT) -> list[int]:
    """待评审：开着、带 needs-independent-review、CI 已全部完成且通过、当前 head 还没有可信的独立评审结论。

    「已评审」用 signals.review_status 判，只认 checks.toml [identity] agent_login 写的标记（T702）；
    reviewed_heads 不核对作者，第三方贴一条标记就会让 PR 永远不被评审（拆分评审严重项 4），不再用它判。
    """
    login = setting("identity", "agent_login")
    found = json.loads(github._run(["gh", "pr", "list", "--state", "open", "--label", LABEL, "--limit", "50",
                                    "--json", "number,headRefOid,comments"]))
    pending = []
    for pr in sorted(found, key=lambda item: item["number"]):
        if signals.review_status(pr.get("comments") or [], login, pr["headRefOid"],
                                 "origin/main", root)[0] != "missing":
            continue
        try:
            checks = json.loads(github._run(["gh", "pr", "checks", str(pr["number"]), "--json", "state"]))
        except (RuntimeError, json.JSONDecodeError):
            continue  # 检查未完成时 gh 以非零退出：下一轮再看
        if checks and all(check.get("state") in DONE_CHECKS for check in checks):
            pending.append(pr["number"])
    return pending


def review_pending(reviewer_name: str | None = None, root: Path = ROOT, github=None, review=None) -> list[int]:
    """逐个评审（多个 OpenCode 同时运行会互相冲突，2026-09-28 实测）。返回评审完成的 PR。

    注入 review（现有测试）时行为逐字不变。生产路径上没有指定评审方时走 dispatch.review_with_chain
    （T702，与派发用同一条评审链）；指定了评审方就只用该评审方。只有返回 0 的 PR 才算「已评审」，
    评审没成的 PR 另起一行打印「评审未完成：#<PR>」，不再被报告成已评审（拆分评审第五轮一般项 3）。
    """
    github = github or dispatch.GitHub(root)
    injected = review is not None
    review = review or review_pr
    done = []
    for number in pending_prs(github, root):
        if injected:
            review(number, reviewer_name, root, github)
            done.append(number)
            continue
        code = (dispatch.review_with_chain(number, root, github) if reviewer_name is None
                else review_pr(number, reviewer_name, root, github))
        if code == 0:
            done.append(number)
        else:
            print(f"评审未完成：#{number}", flush=True)
    return done


def watch(interval_minutes: float = 5, reviewer_name: str | None = None, root: Path = ROOT, github=None,
          review=None, sleep=time.sleep, rounds: int | None = None) -> int:
    """后台常驻：每隔 interval 分钟评审一轮待评审的 PR；派发的停机标记存在时退出（规范 §8）。"""
    stop_flag = dispatch.state_dir(root) / "stop"
    count = 0
    while rounds is None or count < rounds:
        if stop_flag.exists():
            print("停机标记存在，停止后台评审")
            return 0
        try:
            reviewed = review_pending(reviewer_name, root, github, review)
            if reviewed:
                print(f"已评审：{'、'.join(f'#{number}' for number in reviewed)}", flush=True)
        except RuntimeError as error:
            print(f"本轮查询失败（{error}），下一轮重试", flush=True)
        count += 1
        if rounds is None or count < rounds:
            sleep(interval_minutes * 60)
    return 0


# ---- 校准 ----

def calibration_samples(root: Path = ROOT) -> list[dict]:
    """已知有问题：事故回放用例（在 main 上重新注入）；已知良好：manifest 中列出的已合并 PR。"""
    from engine.core.cases import load_project_cases

    CASES = load_project_cases().CASES
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
    # E128-R1（逃逸 #128）：按样本持锁——从占用工作区（prepare_sample）到评审方结果落盘为一段，样本之间
    # 释放（修订记录 2）；index 0 的工作区与 review_pr 等入口互斥，index 大于 0 的并行工作区各用各的锁文件。
    lock_timeout = load_rules().get("review", {}).get("timeout_minutes", 30) * 60 * 2

    def review_one(sample: dict) -> dict:
        space = workspaces.get()
        try:
            with review_lock.workspace_lock(root, space, lock_timeout, purpose=f"calibrate {sample['id']}"):
                try:
                    base, pr_text = prepare_sample(space, sample, main_ref, root)
                except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                    return {"kind": sample["kind"], "id": sample["id"], "error": str(error)}
                write_materials(space, base, pr_text, "")
                verdict, model, seconds = run_reviewer(reviewer, space, 30 * 60)
                return {"kind": sample["kind"], "id": sample["id"], "verdict": verdict.verdict,
                        "flagged": verdict.flagged,
                        "parsed": verdict.parsed, "failure": verdict.failure, "seconds": round(seconds),
                        "model": model}
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
