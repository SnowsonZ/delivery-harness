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
各步骤的实际结果另由 engine/agents/dispatch_observation.py 写观察事件（B46 T105）：观察是旁路，
不改变本文件的判定、预算与返回码。

    bin/dispatch run docs/plans/task-005-x.md [--resume] [--model M]
    bin/dispatch status
    bin/dispatch stop --all
    bin/dispatch review <PR> [--reviewer opencode|pi|codex|claude-code]   独立评审（engine/agents/review.py）
    bin/dispatch review --pending | --watch [--interval 5]   评审全部待评审的 PR（后台常驻用 --watch）
    bin/dispatch review-calibrate [--reviewer 名称] [--output 路径]   评审校准打分（不评论 PR）
    bin/dispatch signoff <PR> --verdict 通过|不通过 --mutations N --caught M --body-file <md> [--designer 席位]   设计方复核（engine/agents/signoff.py）
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

from engine.agents import dispatch_host, run_timeline
from engine.agents import dispatch_observation as observation

# T707 拆分：槽位管理与 PR/升级文本移出到子模块，这里重新导出，engine.agents.dispatch.X 的既有用法不变。
from engine.agents.dispatch_slots import (  # noqa: F401  重新导出（T707）
    _registered_worktrees,
    _salvage_and_remove,
    acquire_slot,
    prepare_slot,
    reclaim_stale_slots,
    release_slot,
    return_slot,
    update_slot,
)
from engine.agents.dispatch_text import (  # noqa: F401  重新导出（T707）
    _manual_section,
    _pr_title,
    _title,
    escalation_body,
    pr_body,
)
from engine.agents.github import GitHub
from engine.checks import taskbook
from engine.core import alerts
from engine.core.common import ENGINE_DIR, ROOT, git, load_rules, setting
from engine.routing import signals

PROMPT_TEMPLATE = ENGINE_DIR / "prompts" / "dispatch_prompt.md"
ESCALATION_FILE = "build/dispatch/escalation.md"


def agent_identity() -> tuple[str, str]:
    """执行方的提交身份（checks.toml [identity]）。邮箱缺省时用 GitHub 的 noreply 形式 `<login>@users.noreply.github.com`。"""
    login = os.environ.get("AGENT_LOGIN") or setting("identity", "agent_login", required=True)
    email = os.environ.get("AGENT_EMAIL") or setting("identity", "agent_email", f"{login}@users.noreply.github.com")
    return login, email


def preserved_paths() -> list[str]:
    """槽位清理未跟踪文件时保留、并以符号链接指向主目录的路径（checks.toml [runtime] preserve，如本地 venv）。"""
    return list(setting("runtime", "preserve", []))


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
    review_after_ci: bool = False  # B77③：CI 通过后由本进程接上独立评审（评审链见 review_with_chain）

    @classmethod
    def load(cls, rules: dict | None = None) -> Config:
        raw = (rules or load_rules()).get("dispatch", {})
        return cls(
            slots=raw.get("slots", 3),
            stall_seconds=raw.get("stall_minutes", 15) * 60,
            ci_timeout_seconds=raw.get("ci_timeout_minutes", 60) * 60,
            review_after_ci=raw.get("review_after_ci", False),
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
    # T202：最后一轮缺失报告的安全条目与状态（reported/unknown/invalid），插在 guard_allowed 前
    # （T105 冻结断言要求它仍是末位字段）。
    missing_context: list = field(default_factory=list)
    missing_context_status: str = "unknown"
    guard_allowed: int = 0  # 守卫放行的工具调用数（含普通失败；被守卫拒绝的调用不计入）


# ---- 1 准入与 2 认领 ----

def admit(path: str, root: Path = ROOT, require_on_main: bool = True) -> Task:
    rel = Path(path).as_posix().removeprefix("./")
    reports = [report for report in taskbook.check_all(root) if report.path == rel]
    if not reports:
        observation.admit(rel, root, problems=[f"{rel} 不是 docs/plans/task-*.md 下的任务书"])
        raise Stop(f"{rel} 不是 docs/plans/task-*.md 下的任务书")
    report = reports[0]
    if rel in taskbook.load_exempt(root / ".harness" / "state" / "taskbook-exempt.txt"):
        problem = f"{rel} 是登记豁免的历史任务书，只检查头部，不能派发"
        observation.admit(rel, root, problems=[problem])
        raise Stop(problem)
    if require_on_main:
        problem = taskbook.on_main(rel, root)
        if problem:
            report.errors.append(problem)
    report.errors += taskbook.headroom_errors(rel, root)  # B119：白名单行数余量只在准入与显式路径检查
    for warning in report.warnings:  # 体量提示（B119）：不阻断，不并入错误
        print(f"提示：{rel}：{warning}", file=sys.stderr)
    if report.errors:
        observation.admit(rel, root, problems=report.errors)
        raise Stop("准入未通过：\n" + "\n".join(f"  - {error}" for error in report.errors))
    header = report.header
    slug = Path(rel).stem.removeprefix("task-")
    task = Task(rel, header["task"], header["class"], header["risk"], header["budget"],
                header.get("spec_refs") or [], f"task/{slug}")
    observation.admit(rel, root, task=task)
    return task


# ---- 3 槽位 ----

def state_dir(root: Path = ROOT) -> Path:
    common = Path(git("rev-parse", "--git-common-dir", cwd=root))
    return (common if common.is_absolute() else root / common).resolve() / "dispatch"


def main_checkout(root: Path) -> Path:
    """主工作目录（git 公共目录的上级）：从任何 worktree 启动都得到同一个仓库名与槽位位置。"""
    return state_dir(root).parent.parent


def slot_path(root: Path, config: Config, index: int) -> Path:
    main = main_checkout(root)
    base = config.slot_root or main.parent
    return base / f"{main.name}-slot-{index}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---- 4 守卫 ----

def prepare_guard(root: Path, trace: str = "") -> tuple[Path, str]:
    """把 origin/main 上的守卫导出到本机目录并确认可运行；返回 (扩展路径, main 的提交)。"""
    ref = git("rev-parse", "origin/main", cwd=root)
    bundle = state_dir(root) / "guard" / ref
    # 守卫与它读取的规则都取自 origin/main（.harness/ 与 .pi/）；main 还是旧布局（harness/ 平铺）时按旧布局导出。
    legacy = not git("ls-tree", "--name-only", ref, ".harness/engine/cli.py", cwd=root, check=False)
    guard_cmd = [str(bundle / "harness" / "command_guard.py")] if legacy \
        else [str(bundle / ".harness" / "engine" / "cli.py"), "guard-command"]
    if not Path(guard_cmd[0]).exists():
        bundle.mkdir(parents=True, exist_ok=True)
        paths = ["harness", ".pi"] if legacy else [".harness", ".pi"]
        archive = subprocess.run(["git", "archive", ref, *paths], cwd=root, capture_output=True, check=False)
        if archive.returncode != 0:
            observation.guard_preflight(trace, ref, False)
            raise Stop(f"无法从 origin/main 导出守卫：{archive.stderr.decode(errors='replace').strip()}")
        subprocess.run(["tar", "-x", "-C", str(bundle)], input=archive.stdout, check=True)
    extension = bundle / ".pi" / "extensions" / "harness-guard.ts"
    probe = subprocess.run(
        [sys.executable, *guard_cmd, "--format", "json", "--role", "implementer"],
        input=json.dumps({"command": "gh issue close 1"}), capture_output=True, text=True, check=False,
    )
    denied = extension.exists() and probe.returncode == 2
    observation.guard_preflight(trace, ref, denied)
    if not denied:
        raise Stop("守卫预检失败：导出的守卫没有拒绝必拒绝的样例，停止派发")
    return extension, ref


# ---- 5 与 6：执行方与本地判定 ----

def render_prompt(task: Task, minutes: int, feedback: str) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    extra = f"\n## 上一轮未通过\n\n{feedback}\n\n修复它，不要重复上一轮的做法。\n" if feedback else ""
    return template.format(task_path=task.path, task_id=task.id, task_class=task.klass, task_risk=task.risk,
                           branch=task.branch, minutes=minutes, feedback=extra)


def _context_report(host, events: Path) -> dict:
    """宿主的缺失上下文报告（B46 T202）：宿主缺 parse_context 或解析失败时按 unknown 处理，
    不改变派发判定、退出码与调用序列（C0 失败隔离）。"""
    parse = getattr(host, "parse_context", None)
    if parse is None:
        return {"status": "unknown", "items": [], "diagnostic": "no_marker"}
    try:
        return parse(events)
    except Exception:  # noqa: BLE001  报告解析失败不改变派发判定
        return {"status": "unknown", "items": [], "diagnostic": "no_marker"}


def verify_signature(slot: Path, config: Config) -> tuple[bool, str, str, str]:
    """运行 verify；返回 (通过, 失败摘要, 失败签名, 完整输出)。签名去掉数字与路径，用于识别打转。"""
    result = subprocess.run(config.verify, cwd=slot, capture_output=True, text=True, check=False)
    log = result.stdout + result.stderr
    if result.returncode == 0:
        return True, "", "", log
    summary_file = slot / "build" / "verify" / "summary.json"
    failed, excerpts = [], []
    if summary_file.exists():
        for item in json.loads(summary_file.read_text()).get("results", []):
            if item.get("status") == "fail":
                failed.append(item["name"])
                log_path = slot / item.get("log", "")
                tail = log_path.read_text(errors="replace").splitlines()[-15:] if log_path.is_file() else []
                excerpts.append(f"### {item['name']}\n" + "\n".join(tail))
    text = "\n\n".join(excerpts) or (result.stdout + result.stderr)[-2000:]
    normalized = re.sub(r"\d+(\.\d+)?", "#", re.sub(r"/\S+/", "/…/", text))
    signature = ",".join(failed) + ":" + hashlib.sha256(normalized.encode()).hexdigest()[:12]
    return False, f"`bin/verify` 未通过：{'、'.join(failed) or '见输出'}\n\n```\n{text[-3000:]}\n```", signature, log


def worktree_problem(slot: Path, base: str) -> str | None:
    if git("status", "--porcelain", "--untracked-files=normal", cwd=slot):
        return "工作区有未提交的改动：所有改动都要提交"
    if not git("rev-list", f"{base}..HEAD", cwd=slot):
        return "没有新的提交"
    return None


class Dispatcher:
    def __init__(self, root: Path, config: Config, github, host, identity: dict[str, str] | None = None):
        self.root, self.config, self.github, self.host = root, config, github, host
        if identity is None:
            login, email = agent_identity()
            identity = {
                "GIT_AUTHOR_NAME": login, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": login, "GIT_COMMITTER_EMAIL": email,
            }
        self.identity = identity
        self.used_seconds = 0.0
        self.acquired: int | None = None  # 已认领的槽位号；归还（return_slot）由派发进程结束路径统一做（B77）
        # CI 通过时记下 (PR, head)：开关 review_after_ci 打开时，run 在归还槽位后接上独立评审（B77③）。
        self.review_target: tuple[int, str] | None = None

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
        counts = observation.tool_counts(self.host, events)
        context = _context_report(self.host, events)
        result = dispatch_host.RunResult(exit_kind, code, seconds, model, usage, denials,
                                         missing_context=context["items"],
                                         missing_context_status=context["status"],
                                         guard_denied=counts["guard_denied"],
                                         guard_allowed=counts["guard_allowed"])
        observation.executor_round(task.branch, slot, prompt, events, result,
                                   {"name": self.host.name, "version": self.host.version()}, counts)
        return result

    def local_rounds(self, task: Task, slot: Path, guard: Path, feedback: str, trace_dir: Path,
                     pr: int | None = None) -> tuple[Attempt, list[str]]:
        """5 与 6：执行方 → verify，失败带摘要重试；返回本次尝试与每轮提示词。"""
        attempt, prompts = Attempt(ok=False, exit="retries"), []
        base = git("rev-parse", "HEAD", cwd=slot)
        retries = task.budget["retries"]
        for round_no in range(retries + 1):
            attempt.retries = round_no
            prompt = render_prompt(task, task.budget["wall_clock_min"], feedback)
            prompts.append(prompt)
            result = self.run_executor(task, slot, guard, prompt, trace_dir / f"round-{round_no + 1}.jsonl")
            alerts.guard_round_alert(task.branch, task.id, result.guard_denied, pr=pr, gh=self.github)
            attempt.executor_seconds += result.seconds
            attempt.model = result.model or attempt.model
            for key, value in result.usage.items():
                attempt.usage[key] = round(attempt.usage.get(key, 0) + value, 6)
            for key, value in result.guard_denials.items():
                attempt.guard_denials[key] = attempt.guard_denials.get(key, 0) + value
            attempt.guard_allowed += result.guard_allowed
            # 最后报告以最后一轮为准：每轮覆盖，抽取只读、不改变判定与退出码
            attempt.missing_context = result.missing_context
            attempt.missing_context_status = result.missing_context_status
            note = slot / ESCALATION_FILE
            if note.exists():
                attempt.executor_note = note.read_text(encoding="utf-8")[:4000]
                observation.clarify(task.branch, note,
                                    context={"status": result.missing_context_status,
                                             "items": result.missing_context})
                attempt.exit = "clarify"
                return attempt, prompts
            if result.exit != "ok":
                attempt.exit = result.exit
                return attempt, prompts
            problem = worktree_problem(slot, base)
            if problem:
                ok, summary, signature, log = False, problem, problem, ""
            else:
                ok, summary, signature, log = verify_signature(slot, self.config)
            repeat = bool(attempt.signatures) and attempt.signatures[-1] == signature
            observation.local_verify(task.branch, slot, base, ok, signature, log, repeat)
            if ok:
                attempt.ok, attempt.exit = True, "ok"
                return attempt, prompts
            attempt.verify_summary = summary
            if repeat:
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
        # B77 落盘前自检：prompt 快照的 CI/runner 工作区路径先占位，sha256 按占位后字节计算。
        prompt_text = run_timeline.placeholder_workspace("\n\n---\n\n".join(prompts))
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
            "missing_context": attempt.missing_context,
            "missing_context_status": attempt.missing_context_status,
            "gen_ai.usage.input_tokens": attempt.usage.get("input_tokens"),
            "gen_ai.usage.output_tokens": attempt.usage.get("output_tokens"),
            "cost": attempt.usage.get("cost"),
            "escalation": None if attempt.ok else attempt.exit,
        }
        # T201 时间线：落盘前取本机链头，stages/anchors 安全摘要写进记录；记录提交后固定 run_record 锚点。
        timeline, head = run_timeline.record_fields(task.branch)
        record.update(timeline)
        run_timeline.fit_record(record)  # B77 自检：记录超 512KB 先截断 stages 并标注，不让卫生守卫在提交步杀派发
        (folder / f"{number}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        paths = [str(folder / f"{number}.json"), str(folder / f"{number}.prompt.md")]
        git("add", "--", *paths, cwd=slot)
        claims_r1 = all("Risk: R1" in git("log", "-1", "--format=%B", sha, cwd=slot)
                        for sha in git("rev-list", "origin/main..HEAD", cwd=slot).split())
        message = f"run：{task.id} 第 {number} 次派发记录（{attempt.exit}）\n\nTask: {task.id}\n"
        if claims_r1 and attempt.ok:
            message += "Risk: R1\n"  # 与执行方的提交一致，否则 R1 声明会被这次记录提交打断
        # B122：钩子拒绝（如 pre-commit 对整个工作区 lint）不再回溯崩溃；finally 里的 return_slot
        # 会先把槽位里的未提交改动备份到 refs/backup/dispatch/…，不丢执行方成果。
        committed = subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=slot,
                                   env={**os.environ, **self.identity}, capture_output=True, text=True,
                                   check=False)
        if committed.returncode != 0:
            tail = (committed.stderr or committed.stdout or "").strip()[-600:]
            raise Stop("派发记录提交被钩子拒绝：" + tail
                       + "；执行方的未提交改动会在归还槽位时备份到 refs/backup/dispatch/…")
        run_timeline.fix_anchor(task.branch, head)
        return record["prompt_sha256"]

    # ---- 主流程 ----

    def run(self, path: str, resume: bool = False) -> int:
        task = admit(path, self.root)
        reclaimed = reclaim_stale_slots(self.root, self.config, self.github.push)
        if resume and task.branch in reclaimed:
            self._chain_hint(task.branch)
        if self.github.remote_branch_exists(task.branch) and not resume:
            observation.claim(task.branch, False)
            raise Stop(f"远端已有 {task.branch}：该任务已被认领（续跑加 --resume）")
        guard, guard_ref = prepare_guard(self.root, task.branch)
        index, slot = acquire_slot(self.root, self.config, task)
        self.acquired = index
        try:
            prepare_slot(self.root, slot, task.branch, resume, {**os.environ, **self.identity})
            observation.slot(task.branch, index, git("rev-parse", "HEAD", cwd=slot),
                             git("rev-parse", "origin/main", cwd=self.root))
            if not resume:
                self.github.push(slot, task.branch)  # 分支即认领
                observation.claim(task.branch, True)
            code = self._loop(task, index, slot, guard, guard_ref)
        finally:
            release_slot(self.root, index)
        # B77③：CI 已通过（_loop 因 CI 通过返回 0）且开关打开时接上独立评审——上面的 finally 已先归还
        # 槽位，评审不占槽位；升级、预算耗尽、本地未完成都返回非 0，不触发评审。
        if code == 0 and self.config.review_after_ci and self.review_target is not None:
            self._review_after_ci()
        return code

    def _chain_hint(self, branch: str) -> None:
        """等待链自愈提示（B77）：上一轮派发进程异常退出（槽位锁 pid 不存活）、分支 head 已保全到远端时，
        提示设计方核对 CI 与评审结论；不自动重跑评审，缺链由设计方手动接链。"""
        print(f"链路自检：上一轮派发异常退出，分支 {branch} 的 head 已在远端；若该 head 没有 CI 结论或"
              "独立评审结论，请设计方手动接链（重跑 CI 或 bin/dispatch review <PR>），评审不会自动重跑。",
              file=sys.stderr)

    def _reviewed(self, pr: int, head: str) -> bool:
        """已有本 Agent 账号写的、指向当前 head 的独立评审结论时为真（B77③，跳过自动评审）。

        用 T701 的 signals.review_status：它核对标记作者（agent_login）。不能用
        review.reviewed_heads——它不核对作者，其他账号贴一条标记就会让派发误以为已评审而漏评
        （拆分评审严重项 4）。评论读取失败按 missing 处理：宁可重复评审，不漏评。
        """
        login = setting("identity", "agent_login")
        try:
            comments = json.loads(self.github._run(["gh", "pr", "view", str(pr), "--json", "comments"]))["comments"]
        except (RuntimeError, ValueError, KeyError):
            comments = []
        status, _reason = signals.review_status(comments, login, head, "origin/main", self.root)
        if status != "missing":
            print(f"PR #{pr}：已有评审结论（{status}），跳过")
            return True
        return False

    def _review_after_ci(self) -> None:
        """CI 通过后由派发进程接上独立评审（B77③）：调用方 run 已先归还槽位，评审不占槽位。

        失败只提示不改变退出码：CI 已通过，任务本身已完成；缺链必须让设计方看见，不沉默。
        """
        pr, head = self.review_target
        try:
            if self._reviewed(pr, head):
                return
            code = review_with_chain(pr, self.root, self.github)
            reason = "" if code == 0 else "评审链全部失败（各评审方结果见上方汇总）"
        except Exception as error:  # noqa: BLE001  评审衔接失败不改变派发判定与退出码
            reason = f"{type(error).__name__}: {error}"
        if reason:
            print(f"评审未能自动接上：{reason}；请设计方运行 bin/dispatch review {pr}", file=sys.stderr)

    def _loop(self, task: Task, index: int, slot: Path, guard: Path, guard_ref: str) -> int:
        runs = state_dir(self.root) / "runs" / task.id
        folder = task.path.split("/")[-1].removesuffix(".md")
        records = slot / "docs" / "runs" / folder
        number = len(list(records.glob("*.json"))) + 1 if records.exists() else 1
        pr, ci_rounds, feedback = None, 0, ""
        while True:
            started = _now()
            update_slot(self.root, index, attempt=number)
            attempt, prompts = self.local_rounds(task, slot, guard, feedback, runs / str(number), pr=pr)
            prompt_sha = self.write_record(task, slot, number, attempt, prompts, guard_ref, started, ci_rounds)
            self.github.push(slot, task.branch)
            if not attempt.ok:
                self.escalate(task, pr, attempt, f"本地未完成（{EXIT_TEXT.get(attempt.exit, attempt.exit)}）")
                return 1
            if pr is None:
                # B70：开新 PR 前先查该分支是否已有开放 PR（resume 时上一轮已开出），有则复用编号，
                # 后续 CI 轮次反馈照旧注入既有 PR；查询为空或适配器没有这个查询（既有桩）才走原新建路径。
                lookup = getattr(self.github, "existing_pr", None)
                if lookup is not None:
                    pr = lookup(task.branch)
            if pr is None:
                body = pr_body(task, attempt, number, prompt_sha, root=self.root)
                pr = self.github.open_pr(slot, task.branch, _pr_title(self.root, task), body)
                observation.push_pr(task.branch, slot, pr, f"docs/runs/{folder}/{number}.json", body)
            ci_rounds += 1
            head = git("rev-parse", "HEAD", cwd=slot)
            detail: dict = {}
            alerts.last_round_alert(task.branch, task.id, ci_rounds, task.budget["ci_rounds"], pr=pr, gh=self.github)
            ok, summary = self.github.wait_ci(task.branch, head, self.config.ci_timeout_seconds, detail)
            observation.ci_wait(task.branch, pr, head, ci_rounds, ok, summary, detail.get("run_ids"))
            if ok:
                print(f"{task.id}：PR #{pr} 的 CI 通过，合并由 auto-merge 判定")
                self.review_target = (pr, head)  # B77③：开关打开时由 run 在归还槽位后接上评审
                return 0
            if ci_rounds >= task.budget["ci_rounds"]:
                self.github.add_label(pr, "budget-exceeded")
                # 预算超限是否决的一种（T715）：关闭可能已开启的自动合并；失败只打印，
                # 不改变升级路径与退出码。
                self.github.disable_auto_merge(pr)
                attempt.verify_summary = summary
                self.escalate(task, pr, attempt, f"CI 第 {ci_rounds} 轮未通过，已达预算 {task.budget['ci_rounds']} 轮")
                return 1
            feedback, number = summary, number + 1

    def escalate(self, task: Task, pr: int | None, attempt: Attempt, reason: str) -> None:
        body = escalation_body(task, attempt, reason, pr=pr)
        target = "pr" if pr is not None else "issue"
        sent = False
        try:
            if pr is not None:
                self.github.comment(pr, body, label="escalation")
            else:
                self.github.create_issue(f"升级：{task.id} {reason}", body, labels=["escalation"])
            sent = True
        finally:
            observation.escalate(task.branch, pr, reason, target, body, sent)  # 评论失败也留事件
        print(f"{task.id}：{reason}，已升级")


EXIT_TEXT = {
    "timeout": "超出时长预算", "stall": "卡死：长时间既无输出也无文件变化", "stopped": "被停机终止",
    "error": "执行方异常退出", "loop": "打转：连续两轮同一失败", "retries": "重试次数用尽",
    "clarify": "执行方请求澄清",
}


def review_with_chain(pr: int, root: Path = ROOT, github=None, review=None) -> int:
    """按 rules.toml [review] chain 依次评审 PR，直到一家给出结论（B77③；T702 的 watch 复用）。

    chain 缺省为 [reviewer]，即原来的单一评审方。review_pr 返回 0（通过或否决都算评审完成）即停；
    返回 1（评审方失败，如额度用尽，已发 review_error 告警）、返回 2（配置不可用，如该家就是设计方，
    无告警）或抛异常（如评审程序不存在的 FileNotFoundError）都换下一家。全部失败返回 1，
    stderr 汇总每一家的结果。review 参数供调用方注入评审模块（测试与 T702）。
    """
    if review is None:
        from engine.agents import review as review_module  # review 依赖本模块，按需导入，沿用 main 的写法
        review = review_module
    review_config = load_rules().get("review", {})
    reviewers = review_config.get("chain") or [review_config.get("reviewer")]
    outcomes: list[str] = []
    for name in reviewers:
        try:
            code = review.review_pr(pr, name, root, github)
        except Exception as error:  # noqa: BLE001  单家异常换下一家，不让链断在这里
            print(f"评审链：{name} 异常（{error}），换下一家", file=sys.stderr)
            outcomes.append(f"{name}：异常 {error}")
            continue
        if code == 0:
            return 0
        if code == 2:
            print(f"评审链：{name} 配置不可用（如该家就是设计方），换下一家", file=sys.stderr)
        outcomes.append(f"{name}：{'评审方失败' if code == 1 else f'返回 {code}'}")
    print(f"PR #{pr}：评审链全部失败（{'；'.join(outcomes) or '没有配置评审方'}）", file=sys.stderr)
    return 1


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


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


def review_calibrate_command(args) -> int:
    """review-calibrate 子命令：在真实历史样本上重跑独立评审并统计 TPR/TNR（不评论 PR）。"""
    from engine.agents import review as review_module  # review 依赖本模块，按需导入

    target = args.output if args.output.is_absolute() else ROOT / args.output
    try:
        return review_module.review_calibrate(args.reviewer, review_module.CALIBRATION_SAMPLES, target)
    except (TypeError, ValueError) as error:
        print(f"校准样本清单有问题：{error}")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="派发一份任务书")
    run.add_argument("taskbook")
    run.add_argument("--resume", action="store_true", help="从远端已有的任务分支继续")
    run.add_argument("--model", help="执行方模型（默认用 Pi 当前设置）")
    review = sub.add_parser("review", help="独立评审一个 PR（非设计方，只读）")
    review.add_argument("pr", type=int, nargs="?", help="评审这个 PR；不给时配合 --pending 或 --watch")
    review.add_argument("--reviewer", choices=["opencode", "pi", "codex", "claude-code"])
    review.add_argument("--pending", action="store_true", help="评审全部待评审且 CI 已通过的 PR，然后退出")
    review.add_argument("--watch", action="store_true", help="后台常驻：每隔 --interval 分钟评审一轮")
    review.add_argument("--interval", type=float, default=5, help="--watch 的间隔（分钟）")
    cal = sub.add_parser("review-calibrate", help="评审校准：在真实历史样本上统计 TPR/TNR（不评论 PR）")
    cal.add_argument("--reviewer", choices=["opencode", "pi", "codex", "claude-code"])
    cal.add_argument("--output", type=Path, default=Path("build/review/calibration-report.md"),
                     help="报告路径（相对仓库根，缺省 build/review/calibration-report.md）")
    from engine.agents import signoff  # T702：按需导入（signoff 依赖本模块，顶层导入会循环）

    # 参数定义只有 signoff.build_parser 一份，这里注册进子命令后原样转给 signoff.main。
    sub.add_parser("signoff", help="设计方复核：发布复核评论，评审复核齐备后重跑判定（engine/agents/signoff.py）",
                   parents=[signoff.build_parser(add_help=False)])
    sub.add_parser("status", help="查看槽位")
    stop = sub.add_parser("stop", help="停机：终止所有正在运行的执行方")
    stop.add_argument("--all", action="store_true", required=True)
    args = parser.parse_args(argv)
    if args.command == "status":
        return status()
    if args.command == "review":
        from engine.agents import review as review_module  # review 依赖本模块，按需导入

        if args.watch:
            return review_module.watch(args.interval, args.reviewer)
        if args.pending:
            reviewed = review_module.review_pending(args.reviewer)
            print(f"已评审 {len(reviewed)} 个 PR" + (f"：{'、'.join(f'#{n}' for n in reviewed)}" if reviewed else ""))
            return 0
        if args.pr is None:
            parser.error("review 需要 PR 编号，或 --pending、--watch")
        return review_module.review_pr(args.pr, args.reviewer)
    if args.command == "review-calibrate":
        return review_calibrate_command(args)
    if args.command == "signoff":
        rest = sys.argv[1:] if argv is None else argv
        return signoff.main(rest[1:])
    if args.command == "stop":
        return stop_all()
    if (state_dir() / "stop").exists():
        print("停机标记存在（bin/dispatch stop），不派发；恢复前删除该标记")
        return 1
    return _run_dispatch(args)


def _run_dispatch(args) -> int:
    """run 子命令：派发并在结束路径统一归还槽位（B77）——成功、升级、预算耗尽与异常都覆盖。"""
    runner = Dispatcher(ROOT, Config.load(), GitHub(), dispatch_host.PiHost(args.model))
    try:
        return runner.run(args.taskbook, args.resume)
    except Stop as stop:
        print(f"未派发：{stop}")
        return 2
    finally:
        if runner.acquired is not None:
            return_slot(ROOT, runner.config, runner.acquired, runner.github.push)


if __name__ == "__main__":
    raise SystemExit(main())
