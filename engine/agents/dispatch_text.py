"""派发的 PR 与升级文本组装（T707 自 dispatch.py 逐字移出，行为不变）。

调用原模块的名字（EXIT_TEXT，以及原本同一模块的 _title、_manual_section）时按需在函数体内
导入 dispatch 并以属性访问：保留测试在原模块上的 patch 语义，也避免循环导入。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from engine.checks import acceptance
from engine.core import alerts
from engine.core.common import ROOT

if TYPE_CHECKING:
    from engine.agents.dispatch import Attempt, Task


def _title(root: Path, task: Task) -> str:
    first = (root / task.path).read_text(encoding="utf-8").split("\n# ", 1)
    return first[1].splitlines()[0].removeprefix("任务：").strip() if len(first) > 1 else task.path


def _pr_title(root: Path, task: Task) -> str:
    """PR 标题：任务书标题已带「TXXX：」前缀时不再重复拼接（B65）。"""
    from engine.agents import dispatch  # _title 留在原模块，按需导入（B89）

    title = dispatch._title(root, task)
    return title if title.startswith(f"{task.id}：") else f"{task.id}：{title}"


def _manual_section(root: Path, task: Task) -> str:
    """人工验收一节正文：验收表存在「人工」类证据行时给指引，否则写「无」（B57）。

    判定与 `bin/harness acceptance --manual` 同源（acceptance.Item.manual）。
    """
    items = acceptance.parse_spec(root / task.path, root)
    if any(item.manual for item in items):
        return "见任务书验收表中的人工条目（`bin/harness acceptance --manual`）。"
    return "无"


def pr_body(task: Task, attempt: Attempt, number: int, prompt_sha: str, root: Path = ROOT) -> str:
    from engine.agents import dispatch  # _manual_section 留在原模块，按需导入（B89）

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
        dispatch._manual_section(root, task), "",
        "🤖 Dispatched by bin/dispatch",
    ]) + "\n"


def escalation_body(task: Task, attempt: Attempt, reason: str, pr: int | None = None) -> str:
    from engine.agents import dispatch  # EXIT_TEXT 留在原模块，按需导入（T707）

    lines = [
        f"### 升级：{task.id}（{reason}）", "",
        f"- **当前状态与目标差距**：任务书 `{task.path}`，分支 `{task.branch}`；退出方式：{dispatch.EXIT_TEXT.get(attempt.exit, attempt.exit)}。",
        f"- **已尝试的方案与结果**：执行方运行 {attempt.retries + 1} 轮，失败签名："
        + ("、".join(f"`{sig}`" for sig in attempt.signatures) or "无"),
        "- **证据**：", "",
        attempt.verify_summary or "（无 verify 输出）", "",
    ]
    if attempt.executor_note:
        lines += ["#### 执行方的说明（`build/dispatch/escalation.md`）", "", attempt.executor_note, ""]
    else:
        lines += ["- **可选方案与推荐**、**需要决定的问题**：执行方未提供，由设计方判断。", ""]
    lines += alerts.timeline_lines(task.branch, pr)  # T205：trace、安全阶段摘要与时间线入口
    lines += ["完整事件流在派发机器本地（git 公共目录下 `dispatch/runs/`），不入库。"]
    return "\n".join(lines) + "\n"
