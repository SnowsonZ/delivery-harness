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
import stat
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
    # T205：本轮被守卫拒绝的工具调用数（一次调用多个拒绝理由算一次），同样插在 guard_allowed 前。
    guard_denied: int = 0
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


def _activity(cwd: Path, events: Path) -> tuple[int, str, tuple]:
    """活动采样（B128）：事件流字节数、git 状态原文、状态涉及文件的元数据指纹。

    状态文字在「持续改写同一已改文件」时不变（同大小重写、`AM` 一直如此），单靠它会
    误判 stall，所以补采每个状态路径的 (ino, size, mtime_ns)：只 lstat 不读正文、不跟随
    链接——链接条目只采自身元数据，目标（哪怕在忽略目录、.git 内或仓库外）变化不算
    活动；采样子路径前先核对全部中间目录分量都是真实目录，任一分量是符号链接就整条
    跳过。路径来自 `git status --porcelain -z -uall`（-z 不转义路径，含空格原样；-uall
    展开未跟踪目录），天然不含 .git 与忽略目录。单个条目消失或不可读按缺失记，不崩。
    """
    try:
        size = events.stat().st_size
    except OSError:
        size = 0
    probe = subprocess.run(
        ["git", "status", "--porcelain", "-z", "-uall"], cwd=cwd, capture_output=True, check=False
    )
    status = probe.stdout.decode("utf-8", "replace")
    return size, status, _fingerprints(cwd, status)


def _fingerprints(cwd: Path, status: str) -> tuple:
    """每个状态路径一条 (路径, 指纹)；porcelain 路径相对仓库根，先定位根再采样。"""
    paths = _status_paths(status)
    if not paths:
        return ()
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, check=False
    )
    if top.returncode != 0:  # 不在 git 仓库里：没有可采样的工作区，退回只看输出与状态文字
        return ()
    # B129：按字节取输出，只去掉 Git 协议在末尾追加的那一个记录分隔符 \n。目录名里
    # 真实的末尾空白（空格、制表符、换行、回车）属于路径本身，必须原样保留——text=True
    # 的通用换行转换会改写名字里的 \r，strip() 会把末尾空白截掉，采样随之落到被截短
    # 的同名路径上（存在就采到别的仓库，不存在就全按缺失记，安静执行方都会被误判 stall）。
    raw = top.stdout
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    root = Path(raw.decode("utf-8", "replace"))
    return tuple((path, _fingerprint(root, path)) for path in paths)


def _status_paths(status: str) -> list[str]:
    """porcelain v1 -z 的路径段：`XY 路径`；重命名/复制条目在 -z 下再跟一段来源路径，跳过。"""
    paths: list[str] = []
    skip_origin = False
    for entry in status.split("\0"):
        if not entry:
            continue
        if skip_origin:
            skip_origin = False
            continue
        if len(entry) < 4 or entry[2] != " ":
            continue
        paths.append(entry[3:])
        skip_origin = "R" in entry[:2] or "C" in entry[:2]
    return paths


def _fingerprint(root: Path, rel: str) -> tuple[int, int, int] | None:
    """单条路径的 lstat 指纹 (ino, size, mtime_ns)；缺失或被跳过统一 None，采样不抛错。"""
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    branch = root
    for part in parts[:-1]:
        try:
            if not stat.S_ISDIR(branch.joinpath(part).lstat().st_mode):
                return None  # 中间分量是符号链接等占位：它指向的树不是本仓库的工作区
        except OSError:
            return None
        branch = branch / part
    try:
        info = branch.joinpath(parts[-1]).lstat()
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns)


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
    """运行执行方直到结束、超时、卡死（stall_seconds 内输出、git 状态与状态涉及文件的
    元数据皆无变化，见 _activity 的采样边界：链接目标与链接经过的目录树不算）或收到停机标记。

    绝对 deadline_seconds 与 stop_flag 优先于活动判定，持续活动不能绕过；自然退出仍按
    退出码给 ok/error。"""
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
        """从 json 事件流取模型、token 与费用、守卫拒绝次数（按理由计，不含命令）。

        B130：顶层非对象 JSON 值（null/数组/布尔/数字/字符串）不是协议事件，整行忽略，
        不展开、不二次解析、不采信其中伪造的事件或上下文标记；其余对象事件照常处理。
        """
        model, usage, denials = "", Counter(), Counter()
        if not events.exists():
            return model, {}, {}
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):  # B130：整行忽略，继续后面的对象事件
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
        顶层非对象 JSON 行不是事件，不参与、不覆盖最后一条合法 assistant 消息（B130）。
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
        （B46 T202）；返回恰此 3 键。顶层非对象 JSON 行不是事件，不计入工具完成（B130）。"""
        counts = {"guard_allowed": 0, "guard_denied": 0,
                  "missing_context": self.parse_context(events)["items"]}
        if not events.exists():
            return counts
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):  # B130：整行忽略，不计入工具完成
                continue
            if event.get("type") != "tool_execution_end":
                continue
            content = (event.get("result") or {}).get("content") or []
            text = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
            counts["guard_denied" if GUARD_DENIAL in text else "guard_allowed"] += 1
        return counts


def _last_assistant_text(events: Path) -> str | None:
    """流中最后一条 assistant 完成消息的正文（text 部分拼接）；流缺失或无 assistant 消息返回 None。

    只认 message_end 携带的最终权威消息，工具输出（tool_execution_end）不参与，伪造标记不采信；
    顶层非对象 JSON 行不是事件，整行忽略，最后的合法消息不被后续噪声覆盖（B130）。
    """
    if not events.exists():
        return None
    text = None
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):  # B130：整行忽略，权威消息保持不变
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
