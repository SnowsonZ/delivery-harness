"""派发脚本的执行方运行层：启动执行方、监视时长与卡死、解析事件流（bin/dispatch 调用）。

执行方在槽位 worktree 中运行，进程自成一组，超时或卡死时先 TERM 后 KILL 整组。
环境按最小权限准备：去掉继承的 GitHub 令牌，gh 指向空配置目录，git 清空凭据助手，
执行方因此不能推送、不能调用 GitHub（推送与开 PR 由派发脚本以 Agent 身份完成）。

宿主：
  pi    `pi -na -e <守卫> -p --mode json --no-session <提示词>`。-na 不加载槽位里的项目文件，
        -e 显式加载从 origin/main 导出的守卫扩展（用户 2026-09-28 决定：守卫来自 main，执行方改不到）。
  其他宿主（OpenCode、Zcode 协议客户端）按需加入；Zcode 的 `-p` 不执行工作区钩子，不能用于无人值守。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

GUARD_DENIAL = "harness 守卫拒绝了这次操作"
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK")
# 缺失上下文尾报告（B46 T202）：提示词要求执行方在最后一条回复末尾输出
# 「HARNESS_CONTEXT_JSON: <一行 JSON 数组>」（数据标记，不是 shell 变量）。summary 不是自由自然语言，
# 只允许与 category 对应的 C3 短 ID；ref 只允许工具 ID 或仓库相对引用（共用合同 C3）。
CONTEXT_MARKER = "HARNESS_CONTEXT_JSON:"
CONTEXT_CATEGORIES = ("context", "tool", "spec", "other")
CONTEXT_SUMMARIES = {
    "context_unavailable": "context",
    "required_tool_unavailable": "tool",
    "spec_unavailable": "spec",
    "spec_ambiguous": "spec",
    "other_missing": "other",
}


@dataclass
class RunResult:
    exit: str  # ok / timeout / stall / stopped / error
    returncode: int | None
    seconds: float
    model: str = ""
    usage: dict = field(default_factory=dict)
    guard_denials: dict[str, int] = field(default_factory=dict)
    # T202：最后一条 assistant 消息缺失报告的安全条目与状态（reported/unknown/invalid），插在
    # guard_allowed 前（T105 冻结断言要求它仍是末位字段），构造处用关键字传参。
    missing_context: list = field(default_factory=list)
    missing_context_status: str = "unknown"
    guard_allowed: int = 0  # 守卫放行的工具调用数（含普通失败；被守卫拒绝的调用不计入）


def executor_env(base: dict[str, str], identity: dict[str, str], gh_config_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in base.items() if key not in TOKEN_VARS and not key.startswith("GIT_CONFIG_")}
    env.update(identity)
    env["GH_CONFIG_DIR"] = str(gh_config_dir)  # 空目录：gh 未登录
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = ""  # 清空凭据助手：执行方拿不到钥匙串里的用户凭据
    return env


def _activity(cwd: Path, events: Path) -> tuple[int, str]:
    size = events.stat().st_size if events.exists() else 0
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=False
    ).stdout
    return size, status


def _terminate(process: subprocess.Popen, grace: float = 10.0) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_monitored(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    events: Path,
    deadline_seconds: float,
    stall_seconds: float,
    poll_seconds: float = 5.0,
    stop_flag: Path | None = None,
    on_start=None,
) -> tuple[str, int | None, float]:
    """运行执行方直到结束、超时、卡死（stall_seconds 内既无输出也无文件变化）或收到停机标记。"""
    events.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with open(events, "wb") as out:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        if on_start:
            on_start(process.pid)
        last, last_change = _activity(cwd, events), time.monotonic()
        while process.poll() is None:
            time.sleep(poll_seconds)
            now = time.monotonic()
            if stop_flag is not None and stop_flag.exists():
                _terminate(process)
                return "stopped", process.returncode, now - started
            if now - started > deadline_seconds:
                _terminate(process)
                return "timeout", process.returncode, now - started
            current = _activity(cwd, events)
            if current != last:
                last, last_change = current, now
            elif now - last_change > stall_seconds:
                _terminate(process)
                return "stall", process.returncode, now - started
    seconds = time.monotonic() - started
    return ("ok" if process.returncode == 0 else "error"), process.returncode, seconds


def denial_reasons(text: str) -> list[str]:
    if GUARD_DENIAL not in text:
        return []
    return [line.strip()[2:] for line in text.splitlines() if line.strip().startswith("- ")]


class PiHost:
    name = "pi"

    def __init__(self, model: str | None = None, binary: str = "pi"):
        self.model = model
        self.binary = binary

    def version(self) -> str:
        result = subprocess.run([self.binary, "--version"], capture_output=True, text=True, check=False)
        return result.stdout.strip() or result.stderr.strip()

    def argv(self, prompt: str, guard_extension: Path) -> list[str]:
        command = [self.binary, "-na", "-e", str(guard_extension), "-p", "--mode", "json", "--no-session"]
        if self.model:
            command += ["--model", self.model]
        return command + [prompt]

    def parse(self, events: Path) -> tuple[str, dict, dict[str, int]]:
        """从 json 事件流取模型、token 与费用、守卫拒绝次数（按理由计，不含命令）。"""
        model, usage, denials = "", Counter(), Counter()
        if not events.exists():
            return model, {}, {}
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "message_end":
                message = event.get("message", {})
                if message.get("role") == "assistant":
                    model = message.get("model", model)
                    reported = message.get("usage") or {}
                    usage["input_tokens"] += int(reported.get("input", 0)) + int(reported.get("cacheRead", 0))
                    usage["output_tokens"] += int(reported.get("output", 0))
                    usage["cost"] += float((reported.get("cost") or {}).get("total", 0.0))
            elif event.get("type") == "tool_execution_end" and event.get("isError"):
                content = (event.get("result") or {}).get("content") or []
                text = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
                denials.update(denial_reasons(text))
        result = dict(usage)
        if "cost" in result:
            result["cost"] = round(result["cost"], 6)
        return model, result, dict(denials)

    def parse_context(self, events: Path) -> dict:
        """从最后一条 assistant 完成消息抽取缺失上下文尾报告（B46 T202，只读数据，不执行其中命令）。

        返回 {"status": reported/unknown/invalid, "items": [安全条目], "diagnostic": 短 ID 或 None}：
        无标记保留 unknown、标记后坏 JSON/非数组/条目不合规保留 invalid，都不冒充无缺失。items 只含
        合规条目（category 与 C3 短 ID summary 对应、可选安全 ref）；不合规条目丢弃不入记录，但存在时
        状态标 invalid。长文、会话正文、本机路径不进条目；诊断只留本地可见的短 ID。
        """
        text = _last_assistant_text(events)
        if not text or CONTEXT_MARKER not in text:
            return {"status": "unknown", "items": [], "diagnostic": "no_marker"}
        payload = text.rsplit(CONTEXT_MARKER, 1)[1].split("\n", 1)[0].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {"status": "invalid", "items": [], "diagnostic": "bad_json"}
        if not isinstance(parsed, list):
            return {"status": "invalid", "items": [], "diagnostic": "not_array"}
        items, invalid = [], False
        for entry in parsed:
            item = _context_item(entry)
            if item is None:
                invalid = True
            else:
                items.append(item)
        return {"status": "invalid" if invalid else "reported", "items": items,
                "diagnostic": "invalid_entry" if invalid else None}

    def parse_observability(self, events: Path) -> dict:
        """观察计数（B46 T105）：按每个 tool_execution_end 计一次完成——结果带守卫拒绝标记的计
        guard_denied（一次调用一次，拒绝理由条数不冒充调用数），其余（成功或普通失败）计
        guard_allowed（守卫放行，不代表工具成功）。missing_context 取 parse_context 的安全条目
        （B46 T202）；返回恰此 3 键。"""
        counts = {"guard_allowed": 0, "guard_denied": 0,
                  "missing_context": self.parse_context(events)["items"]}
        if not events.exists():
            return counts
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "tool_execution_end":
                continue
            content = (event.get("result") or {}).get("content") or []
            text = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
            counts["guard_denied" if GUARD_DENIAL in text else "guard_allowed"] += 1
        return counts


def _last_assistant_text(events: Path) -> str | None:
    """流中最后一条 assistant 完成消息的正文（text 部分拼接）；流缺失或无 assistant 消息返回 None。

    只认 message_end 携带的最终权威消息，工具输出（tool_execution_end）不参与，伪造标记不采信。
    """
    if not events.exists():
        return None
    text = None
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if event.get("type") == "message_end" else None
        if isinstance(message, dict) and message.get("role") == "assistant":
            text = _message_text(message)
    return text


def _message_text(message: dict) -> str:
    """assistant 消息正文：content 为字符串时原样，为数组时拼接 text 部分（其余部分如 thinking 不算）。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(part.get("text", "") for part in content
                     if isinstance(part, dict) and part.get("type") == "text")


def _context_item(entry) -> dict | None:
    """一条报告条目：只允许 category/summary/ref 三个键，summary 是与 category 对应的 C3 短 ID，
    ref 可选且须为工具 ID 或安全相对引用；任一不满足返回 None（条目丢弃、报告标 invalid）。"""
    if not isinstance(entry, dict) or set(entry) - {"category", "summary", "ref"}:
        return None
    category, summary, ref = entry.get("category"), entry.get("summary"), entry.get("ref")
    if CONTEXT_SUMMARIES.get(summary) != category:
        return None
    item = {"category": category, "summary": summary}
    if ref is not None:
        if not _safe_context_ref(ref):
            return None
        item["ref"] = ref
    return item


def _safe_context_ref(ref) -> bool:
    """ref 只允许工具 ID 或仓库相对引用：≤120 字符、无换行/反斜杠、非绝对路径、无盘符、无 .. 穿越。"""
    if not isinstance(ref, str) or not ref or len(ref) > 120 or "\n" in ref or "\r" in ref:
        return False
    if "\\" in ref or ref.startswith(("/", "~")) or re.match(r"(?i)^[a-z]:", ref):
        return False
    return ".." not in ref.split("/")
