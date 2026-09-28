"""派发脚本：把一份已合并的任务书交给执行方，在槽位 worktree 中跑完并开 PR（目标态设计 6.2、7.x、10.1）。

确定性流程包住执行方，执行方只在「实现」和「按报错修复」两步工作：

  1 准入      taskbook.py 检查任务书，且任务书已在 origin/main 上
  2 认领      远端已有 task/<编号>-<名字> 分支即已被认领，停止；否则建分支并立即推送
  3 备环境    取空闲槽位（仓库同级的固定目录），从 origin/main 切出分支，清理未跟踪文件
  4 守卫预检  从 origin/main 导出守卫（harness/ 与 .pi/），用一条必拒绝的载荷确认守卫可运行
  5 实现      执行方在槽位中运行；超时、卡死、停机即终止
  6 本地判定  在执行方进程之外运行 bin/verify；失败带摘要回到 5，同一失败签名连续两次即打转
  7 推送与 PR 写入并提交运行记录 docs/runs/<任务>/<序号>.json 与提示词，推送，开 PR
  8 等 CI     计 CI 轮次；失败且未超预算带 CI 摘要回到 5，超预算打 budget-exceeded 标签
  9 收尾      归还槽位；合并与否由 auto-merge 的 policy.py 决定

不能完成时升级：已开 PR 的在 PR 上评论，否则开议题，都加 escalation 标签；升级包预填当前状态、
已尝试的方案与证据，执行方写的「可选方案」「需要决定的问题」（build/dispatch/escalation.md）附在后面。
运行记录只存结构化摘要，完整事件流留在本机 <git 公共目录>/dispatch/runs/，不入库。

    bin/dispatch run docs/plans/task-005-x.md [--resume] [--model M]
    bin/dispatch status
    bin/dispatch stop --all
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_host
import taskbook
from common import ROOT, git, load_rules

PROMPT_TEMPLATE = Path(__file__).resolve().parent / "dispatch_prompt.md"
AGENT_LOGIN = os.environ.get("AGENT_LOGIN", "Snowson")
AGENT_EMAIL = os.environ.get("AGENT_EMAIL", "15168246+Snowson@users.noreply.github.com")
VENV = "scratch/iterm-probe-venv"
ESCALATION_FILE = "build/dispatch/escalation.md"


class Stop(Exception):
    """派发在确定性步骤中止（准入、认领、无槽位、守卫预检）。"""


@dataclass
class Config:
    slots: int = 3
    slot_root: Path | None = None  # 默认为仓库的上级目录
    stall_seconds: float = 900
    poll_seconds: float = 5
    ci_timeout_seconds: float = 3600
    verify: list[str] = field(default_factory=lambda: ["bin/verify"])

    @classmethod
    def load(cls, rules: dict | None = None) -> Config:
        raw = (rules or load_rules()).get("dispatch", {})
        return cls(
            slots=raw.get("slots", 3),
            stall_seconds=raw.get("stall_minutes", 15) * 60,
            ci_timeout_seconds=raw.get("ci_timeout_minutes", 60) * 60,
        )


@dataclass
class Task:
    path: str
    id: str
    klass: str
    risk: str
    budget: dict
    spec_refs: list[str]
    branch: str


@dataclass
class Attempt:
    ok: bool
    exit: str  # ok / timeout / stall / stopped / error / loop / retries / clarify
    retries: int = 0
    signatures: list[str] = field(default_factory=list)
    verify_summary: str = ""
    executor_seconds: float = 0.0
    model: str = ""
    usage: dict = field(default_factory=dict)
    guard_denials: dict[str, int] = field(default_factory=dict)
    executor_note: str = ""


# ---- 1 准入与 2 认领 ----

def admit(path: str, root: Path = ROOT, require_on_main: bool = True) -> Task:
    rel = Path(path).as_posix().removeprefix("./")
    reports = [report for report in taskbook.check_all(root) if report.path == rel]
    if not reports:
        raise Stop(f"{rel} 不是 docs/plans/task-*.md 下的任务书")
    report = reports[0]
    if rel in taskbook.load_exempt(root / "harness" / "taskbook-exempt.txt"):
        raise Stop(f"{rel} 是登记豁免的历史任务书，只检查头部，不能派发")
    if require_on_main:
        problem = taskbook.on_main(rel, root)
        if problem:
            report.errors.append(problem)
    if report.errors:
        raise Stop("准入未通过：\n" + "\n".join(f"  - {error}" for error in report.errors))
    header = report.header
    slug = Path(rel).stem.removeprefix("task-")
    return Task(rel, header["task"], header["class"], header["risk"], header["budget"],
                header.get("spec_refs") or [], f"task/{slug}")


# ---- 3 槽位 ----

def state_dir(root: Path = ROOT) -> Path:
    common = Path(git("rev-parse", "--git-common-dir", cwd=root))
    return (common if common.is_absolute() else root / common).resolve() / "dispatch"


def slot_path(root: Path, config: Config, index: int) -> Path:
    base = config.slot_root or root.parent
    return base / f"{root.name}-slot-{index}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_slot(root: Path, config: Config, task: Task) -> tuple[int, Path]:
    locks = state_dir(root) / "slots"
    locks.mkdir(parents=True, exist_ok=True)
    for index in range(1, config.slots + 1):
        lock = locks / f"{index}.json"
        if lock.exists():
            held = json.loads(lock.read_text() or "{}")
            if _alive(held.get("pid", 0)):
                continue
            lock.unlink()  # 持有者已退出：回收
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "task": task.id, "branch": task.branch,
                       "started_at": _now()}, handle)
        return index, slot_path(root, config, index)
    raise Stop(f"{config.slots} 个槽位都在使用中：稍后再派发，或用 bin/dispatch status 查看")


def update_slot(root: Path, index: int, **fields) -> None:
    lock = state_dir(root) / "slots" / f"{index}.json"
    data = json.loads(lock.read_text() or "{}")
    data.update(fields)
    lock.write_text(json.dumps(data))


def release_slot(root: Path, index: int) -> None:
    (state_dir(root) / "slots" / f"{index}.json").unlink(missing_ok=True)


def prepare_slot(root: Path, slot: Path, branch: str, resume: bool) -> None:
    if not slot.exists():
        git("worktree", "add", "--detach", str(slot), "origin/main", cwd=root)
    git("fetch", "--quiet", "origin", cwd=slot)
    start = f"origin/{branch}" if resume else "origin/main"
    git("checkout", "--quiet", "--force", "-B", branch, start, cwd=slot)
    git("clean", "-ffdxq", "-e", VENV, cwd=slot)
    venv = root / VENV
    if venv.exists() and not (slot / VENV).exists():
        (slot / VENV).parent.mkdir(parents=True, exist_ok=True)
        (slot / VENV).symlink_to(venv)


# ---- 4 守卫 ----

def prepare_guard(root: Path) -> tuple[Path, str]:
    """把 origin/main 上的守卫导出到本机目录并确认可运行；返回 (扩展路径, main 的提交)。"""
    ref = git("rev-parse", "origin/main", cwd=root)
    bundle = state_dir(root) / "guard" / ref
    if not (bundle / "harness" / "command_guard.py").exists():
        bundle.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(["git", "archive", ref, "harness", ".pi"], cwd=root, capture_output=True, check=False)
        if archive.returncode != 0:
            raise Stop(f"无法从 origin/main 导出守卫：{archive.stderr.decode(errors='replace').strip()}")
        subprocess.run(["tar", "-x", "-C", str(bundle)], input=archive.stdout, check=True)
    extension = bundle / ".pi" / "extensions" / "harness-guard.ts"
    probe = subprocess.run(
        [sys.executable, str(bundle / "harness" / "command_guard.py"), "--format", "json", "--role", "implementer"],
        input=json.dumps({"command": "gh issue close 1"}), capture_output=True, text=True, check=False,
    )
    if not extension.exists() or probe.returncode != 2:
        raise Stop("守卫预检失败：导出的守卫没有拒绝必拒绝的样例，停止派发")
    return extension, ref


# ---- 5 与 6：执行方与本地判定 ----

def render_prompt(task: Task, minutes: int, feedback: str) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    extra = f"\n## 上一轮未通过\n\n{feedback}\n\n修复它，不要重复上一轮的做法。\n" if feedback else ""
    return template.format(task_path=task.path, task_id=task.id, task_class=task.klass, task_risk=task.risk,
                           branch=task.branch, minutes=minutes, feedback=extra)


def verify_signature(slot: Path, config: Config) -> tuple[bool, str, str]:
    """运行 verify；返回 (通过, 失败摘要, 失败签名)。签名去掉数字与路径，用于识别打转。"""
    result = subprocess.run(config.verify, cwd=slot, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return True, "", ""
    summary_file = slot / "build" / "verify" / "summary.json"
    failed, excerpts = [], []
    if summary_file.exists():
        for item in json.loads(summary_file.read_text()).get("results", []):
            if item.get("status") == "fail":
                failed.append(item["name"])
                log = slot / item.get("log", "")
                tail = log.read_text(errors="replace").splitlines()[-15:] if log.is_file() else []
                excerpts.append(f"### {item['name']}\n" + "\n".join(tail))
    text = "\n\n".join(excerpts) or (result.stdout + result.stderr)[-2000:]
    normalized = re.sub(r"\d+(\.\d+)?", "#", re.sub(r"/\S+/", "/…/", text))
    signature = ",".join(failed) + ":" + hashlib.sha256(normalized.encode()).hexdigest()[:12]
    return False, f"`bin/verify` 未通过：{'、'.join(failed) or '见输出'}\n\n```\n{text[-3000:]}\n```", signature


def worktree_problem(slot: Path, base: str) -> str | None:
    if git("status", "--porcelain", "--untracked-files=normal", cwd=slot):
        return "工作区有未提交的改动：所有改动都要提交"
    if not git("rev-list", f"{base}..HEAD", cwd=slot):
        return "没有新的提交"
    return None


class Dispatcher:
    def __init__(self, root: Path, config: Config, github, host, identity: dict[str, str] | None = None):
        self.root, self.config, self.github, self.host = root, config, github, host
        self.identity = identity or {
            "GIT_AUTHOR_NAME": AGENT_LOGIN, "GIT_AUTHOR_EMAIL": AGENT_EMAIL,
            "GIT_COMMITTER_NAME": AGENT_LOGIN, "GIT_COMMITTER_EMAIL": AGENT_EMAIL,
        }
        self.used_seconds = 0.0

    def run_executor(self, task: Task, slot: Path, guard: Path, prompt: str, events: Path) -> dispatch_host.RunResult:
        budget_seconds = task.budget["wall_clock_min"] * 60
        remaining = max(budget_seconds - self.used_seconds, 0)
        stop_flag = state_dir(self.root) / "stop"
        with tempfile.TemporaryDirectory(prefix="dispatch-gh-") as gh_config:
            env = dispatch_host.executor_env(dict(os.environ), self.identity, Path(gh_config))
            exit_kind, code, seconds = dispatch_host.run_monitored(
                self.host.argv(prompt, guard), slot, env, events, remaining, self.config.stall_seconds,
                self.config.poll_seconds, stop_flag,
            )
        self.used_seconds += seconds
        model, usage, denials = self.host.parse(events)
        return dispatch_host.RunResult(exit_kind, code, seconds, model, usage, denials)

    def local_rounds(self, task: Task, slot: Path, guard: Path, feedback: str, trace_dir: Path) -> tuple[Attempt, list[str]]:
        """5 与 6：执行方 → verify，失败带摘要重试；返回本次尝试与每轮提示词。"""
        attempt, prompts = Attempt(ok=False, exit="retries"), []
        base = git("rev-parse", "HEAD", cwd=slot)
        retries = task.budget["retries"]
        for round_no in range(retries + 1):
            attempt.retries = round_no
            prompt = render_prompt(task, task.budget["wall_clock_min"], feedback)
            prompts.append(prompt)
            result = self.run_executor(task, slot, guard, prompt, trace_dir / f"round-{round_no + 1}.jsonl")
            attempt.executor_seconds += result.seconds
            attempt.model = result.model or attempt.model
            for key, value in result.usage.items():
                attempt.usage[key] = round(attempt.usage.get(key, 0) + value, 6)
            for key, value in result.guard_denials.items():
                attempt.guard_denials[key] = attempt.guard_denials.get(key, 0) + value
            note = slot / ESCALATION_FILE
            if note.exists():
                attempt.executor_note = note.read_text(encoding="utf-8")[:4000]
                attempt.exit = "clarify"
                return attempt, prompts
            if result.exit != "ok":
                attempt.exit = result.exit
                return attempt, prompts
            problem = worktree_problem(slot, base)
            if problem:
                ok, summary, signature = False, problem, problem
            else:
                ok, summary, signature = verify_signature(slot, self.config)
            if ok:
                attempt.ok, attempt.exit = True, "ok"
                return attempt, prompts
            attempt.verify_summary = summary
            if attempt.signatures and attempt.signatures[-1] == signature:
                attempt.signatures.append(signature)
                attempt.exit = "loop"
                return attempt, prompts
            attempt.signatures.append(signature)
            feedback = summary
        return attempt, prompts

    # ---- 7 运行记录 ----

    def write_record(self, task: Task, slot: Path, number: int, attempt: Attempt, prompts: list[str],
                     guard_ref: str, started: str, ci_rounds: int) -> str:
        folder = slot / "docs" / "runs" / task.path.split("/")[-1].removesuffix(".md")
        folder.mkdir(parents=True, exist_ok=True)
        prompt_text = "\n\n---\n\n".join(prompts)
        (folder / f"{number}.prompt.md").write_text(prompt_text, encoding="utf-8")
        record = {
            "task": task.id,
            "class": task.klass,
            "attempt": number,
            "branch": task.branch,
            "gen_ai.agent.name": self.host.name,
            "host_version": self.host.version(),
            "gen_ai.request.model": attempt.model,
            "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
            "prompt_path": f"{folder.relative_to(slot).as_posix()}/{number}.prompt.md",
            "guard_ref": guard_ref,
            "started_at": started,
            "ended_at": _now(),
            "executor_seconds": round(attempt.executor_seconds, 1),
            "exit": attempt.exit,
            "retries": attempt.retries,
            "failure_signatures": attempt.signatures,
            "ci_rounds_before": ci_rounds,
            "guard_denials": attempt.guard_denials,
            "gen_ai.usage.input_tokens": attempt.usage.get("input_tokens"),
            "gen_ai.usage.output_tokens": attempt.usage.get("output_tokens"),
            "cost": attempt.usage.get("cost"),
            "escalation": None if attempt.ok else attempt.exit,
        }
        (folder / f"{number}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        paths = [str(folder / f"{number}.json"), str(folder / f"{number}.prompt.md")]
        git("add", "--", *paths, cwd=slot)
        claims_r1 = all("Risk: R1" in git("log", "-1", "--format=%B", sha, cwd=slot)
                        for sha in git("rev-list", "origin/main..HEAD", cwd=slot).split())
        message = f"run：{task.id} 第 {number} 次派发记录（{attempt.exit}）\n\nTask: {task.id}\n"
        if claims_r1 and attempt.ok:
            message += "Risk: R1\n"  # 与执行方的提交一致，否则 R1 声明会被这次记录提交打断
        subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=slot, env={**os.environ, **self.identity},
                       check=True)
        return record["prompt_sha256"]

    # ---- 主流程 ----

    def run(self, path: str, resume: bool = False) -> int:
        task = admit(path, self.root)
        if self.github.remote_branch_exists(task.branch) and not resume:
            raise Stop(f"远端已有 {task.branch}：该任务已被认领（续跑加 --resume）")
        guard, guard_ref = prepare_guard(self.root)
        index, slot = acquire_slot(self.root, self.config, task)
        try:
            prepare_slot(self.root, slot, task.branch, resume)
            if not resume:
                self.github.push(slot, task.branch)  # 分支即认领
            return self._loop(task, index, slot, guard, guard_ref)
        finally:
            release_slot(self.root, index)

    def _loop(self, task: Task, index: int, slot: Path, guard: Path, guard_ref: str) -> int:
        runs = state_dir(self.root) / "runs" / task.id
        records = slot / "docs" / "runs" / task.path.split("/")[-1].removesuffix(".md")
        number = len(list(records.glob("*.json"))) + 1 if records.exists() else 1
        pr, ci_rounds, feedback = None, 0, ""
        while True:
            started = _now()
            update_slot(self.root, index, attempt=number)
            attempt, prompts = self.local_rounds(task, slot, guard, feedback, runs / str(number))
            prompt_sha = self.write_record(task, slot, number, attempt, prompts, guard_ref, started, ci_rounds)
            self.github.push(slot, task.branch)
            if not attempt.ok:
                self.escalate(task, pr, attempt, f"本地未完成（{EXIT_TEXT.get(attempt.exit, attempt.exit)}）")
                return 1
            if pr is None:
                pr = self.github.open_pr(slot, task.branch, f"{task.id}：{_title(self.root, task)}",
                                         pr_body(task, attempt, number, prompt_sha))
            ci_rounds += 1
            ok, summary = self.github.wait_ci(task.branch, git("rev-parse", "HEAD", cwd=slot),
                                              self.config.ci_timeout_seconds)
            if ok:
                print(f"{task.id}：PR #{pr} 的 CI 通过，合并由 auto-merge 判定")
                return 0
            if ci_rounds >= task.budget["ci_rounds"]:
                self.github.add_label(pr, "budget-exceeded")
                attempt.verify_summary = summary
                self.escalate(task, pr, attempt, f"CI 第 {ci_rounds} 轮未通过，已达预算 {task.budget['ci_rounds']} 轮")
                return 1
            feedback, number = summary, number + 1

    def escalate(self, task: Task, pr: int | None, attempt: Attempt, reason: str) -> None:
        body = escalation_body(task, attempt, reason)
        if pr is not None:
            self.github.comment(pr, body, label="escalation")
        else:
            self.github.create_issue(f"升级：{task.id} {reason}", body, labels=["escalation"])
        print(f"{task.id}：{reason}，已升级")


EXIT_TEXT = {
    "timeout": "超出时长预算", "stall": "卡死：长时间既无输出也无文件变化", "stopped": "被停机终止",
    "error": "执行方异常退出", "loop": "打转：连续两轮同一失败", "retries": "重试次数用尽",
    "clarify": "执行方请求澄清",
}


def _title(root: Path, task: Task) -> str:
    first = (root / task.path).read_text(encoding="utf-8").split("\n# ", 1)
    return first[1].splitlines()[0].removeprefix("任务：").strip() if len(first) > 1 else task.path


def pr_body(task: Task, attempt: Attempt, number: int, prompt_sha: str) -> str:
    usage = attempt.usage
    return "\n".join([
        "## 任务", "",
        f"- 任务书：`{task.path}`（{task.id}，类别 {task.klass}，预期风险 {task.risk}）",
        f"- 对应验收编号：{'、'.join(task.spec_refs) or '不挂规格（见任务书）'}",
        "- 派发：`bin/dispatch`，执行方与模型见下表", "",
        "## 运行记录摘要", "",
        "| 项 | 值 |", "|---|---|",
        f"| 记录 | `docs/runs/{task.path.split('/')[-1].removesuffix('.md')}/{number}.json` |",
        f"| 模型 | {attempt.model or '未报告'} |",
        f"| 执行时长 | {round(attempt.executor_seconds / 60, 1)} 分钟，本地重试 {attempt.retries} 次 |",
        f"| token | 输入 {usage.get('input_tokens', '—')}，输出 {usage.get('output_tokens', '—')} |",
        f"| 守卫拒绝 | {sum(attempt.guard_denials.values())} 次 |",
        f"| 提示词 sha256 | `{prompt_sha[:16]}` |", "",
        "本地 `bin/verify` 由派发脚本在执行方进程之外运行并通过；以本 PR 的 CI 为准。", "",
        "## 证据", "",
        "- CI 运行（当前 head）：见本 PR checks",
        "- 风险等级与修复证据：见 harness job summary（机器生成）", "",
        "## 需要人工验收的部分", "",
        "见任务书验收表中的人工条目（`python3 harness/acceptance.py --manual`）。", "",
        "🤖 Dispatched by bin/dispatch",
    ]) + "\n"


def escalation_body(task: Task, attempt: Attempt, reason: str) -> str:
    lines = [
        f"### 升级：{task.id}（{reason}）", "",
        f"- **当前状态与目标差距**：任务书 `{task.path}`，分支 `{task.branch}`；退出方式：{EXIT_TEXT.get(attempt.exit, attempt.exit)}。",
        f"- **已尝试的方案与结果**：执行方运行 {attempt.retries + 1} 轮，失败签名："
        + ("、".join(f"`{sig}`" for sig in attempt.signatures) or "无"),
        "- **证据**：", "",
        attempt.verify_summary or "（无 verify 输出）", "",
    ]
    if attempt.executor_note:
        lines += ["#### 执行方的说明（`build/dispatch/escalation.md`）", "", attempt.executor_note, ""]
    else:
        lines += ["- **可选方案与推荐**、**需要决定的问题**：执行方未提供，由设计方判断。", ""]
    lines += ["完整事件流在派发机器本地（git 公共目录下 `dispatch/runs/`），不入库。"]
    return "\n".join(lines) + "\n"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


# ---- GitHub（推送与写操作以 Agent 身份经 bin/as-agent） ----

class GitHub:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.as_agent = [str(root / "bin" / "as-agent")]

    def _run(self, argv: list[str], cwd: Path | None = None, agent: bool = False, stdin: str | None = None) -> str:
        result = subprocess.run((self.as_agent if agent else []) + argv, cwd=cwd or self.root, input=stdin,
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:3])} 失败：{result.stderr.strip()[-500:]}")
        return result.stdout.strip()

    def remote_branch_exists(self, branch: str) -> bool:
        return bool(self._run(["git", "ls-remote", "--heads", "origin", branch]))

    def push(self, slot: Path, branch: str) -> None:
        self._run(["git", "push", "--quiet", "-u", "origin", branch], cwd=slot, agent=True)

    # 正文一律经 stdin（--body-file -），不进命令行（规范 §2：不在引号里写 Markdown）。
    def open_pr(self, slot: Path, branch: str, title: str, body: str) -> int:
        url = self._run(["gh", "pr", "create", "--base", "main", "--head", branch, "--title", title,
                         "--body-file", "-"], cwd=slot, agent=True, stdin=body)
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def comment(self, pr: int, body: str, label: str | None = None) -> None:
        self._run(["gh", "pr", "comment", str(pr), "--body-file", "-"], agent=True, stdin=body)
        if label:
            self.add_label(pr, label)

    def _ensure_label(self, label: str) -> None:
        """不存在才创建；不用 --force，已有标签（含登记用标签）不被改写。"""
        try:
            self._run(["gh", "label", "create", label, "--color", "d93f0b"], agent=True)
        except RuntimeError:
            pass  # 已存在

    def add_label(self, pr: int, label: str) -> None:
        self._ensure_label(label)
        self._run(["gh", "pr", "edit", str(pr), "--add-label", label], agent=True)

    def create_issue(self, title: str, body: str, labels: list[str]) -> None:
        for label in labels:
            self._ensure_label(label)
        argv = ["gh", "issue", "create", "--title", title, "--body-file", "-"]
        for label in labels:
            argv += ["--label", label]
        self._run(argv, agent=True, stdin=body)

    def wait_ci(self, branch: str, sha: str, timeout: float) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            runs = json.loads(self._run(["gh", "run", "list", "--workflow", "build", "--branch", branch,
                                         "--json", "headSha,status,conclusion,databaseId,url", "--limit", "10"]))
            run = next((item for item in runs if item["headSha"] == sha), None)
            if run and run["status"] == "completed":
                if run["conclusion"] == "success":
                    return True, ""
                log = subprocess.run(["gh", "run", "view", str(run["databaseId"]), "--log-failed"], cwd=self.root,
                                     capture_output=True, text=True, check=False).stdout
                return False, f"CI 未通过：{run['url']}\n\n```\n{log[-3000:]}\n```"
            time.sleep(30)
        return False, f"等待 CI 超过 {int(timeout / 60)} 分钟"


# ---- 状态与停机 ----

def status(root: Path = ROOT) -> int:
    locks = sorted((state_dir(root) / "slots").glob("*.json"))
    if not locks:
        print("没有正在运行的派发")
    for lock in locks:
        data = json.loads(lock.read_text() or "{}")
        state = "运行中" if _alive(data.get("pid", 0)) else "已退出（锁待回收）"
        print(f"槽位 {lock.stem}：{data.get('task')} {data.get('branch')} 第 {data.get('attempt', 1)} 次，{state}，"
              f"开始于 {data.get('started_at')}")
    return 0


def stop_all(root: Path = ROOT, wait_seconds: float = 30) -> int:
    """停机（规范 §8）：写停机标记，各派发进程在下一次轮询时终止执行方并升级；标记在全部退出后删除。"""
    flag = state_dir(root) / "stop"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(_now())
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        running = [lock for lock in (state_dir(root) / "slots").glob("*.json")
                   if _alive(json.loads(lock.read_text() or "{}").get("pid", 0))]
        if not running:
            flag.unlink(missing_ok=True)
            print("所有派发已停止")
            return 0
        time.sleep(2)
    print("仍有派发未退出：停机标记保留，新的派发会立即终止；确认后删除 " + str(flag))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="派发一份任务书")
    run.add_argument("taskbook")
    run.add_argument("--resume", action="store_true", help="从远端已有的任务分支继续")
    run.add_argument("--model", help="执行方模型（默认用 Pi 当前设置）")
    sub.add_parser("status", help="查看槽位")
    stop = sub.add_parser("stop", help="停机：终止所有正在运行的执行方")
    stop.add_argument("--all", action="store_true", required=True)
    args = parser.parse_args(argv)
    if args.command == "status":
        return status()
    if args.command == "stop":
        return stop_all()
    if (state_dir() / "stop").exists():
        print("停机标记存在（bin/dispatch stop），不派发；恢复前删除该标记")
        return 1
    try:
        return Dispatcher(ROOT, Config.load(), GitHub(), dispatch_host.PiHost(args.model)).run(args.taskbook, args.resume)
    except Stop as stop:
        print(f"未派发：{stop}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
