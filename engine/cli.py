"""delivery-harness 唯一入口：python3 <引擎目录>/cli.py <子命令> [参数…]

CI、git 钩子、Agent 宿主钩子与引擎内部的子进程调用都经这里启动，各子命令的参数见对应模块的说明。
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

# 让 `engine` 包可导入：引擎目录的上一级（业务仓库的 .harness/，或引擎仓库根）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COMMANDS = {
    # 判定器
    "verify": "engine.checks.verify",
    "hygiene": "engine.checks.hygiene",
    "acceptance": "engine.checks.acceptance",
    "taskbook": "engine.checks.taskbook",
    "docs": "engine.checks.docs_check",
    "base-tests": "engine.checks.base_tests",
    "evidence": "engine.checks.evidence",
    "r1": "engine.checks.r1_checks",
    "replay": "engine.checks.replay",
    "mutate": "engine.checks.mutate",
    "quality": "engine.checks.quality",
    "release-check": "engine.checks.release_check",
    "integrity": "engine.checks.integrity",
    # 判级与合并路由
    "risk": "engine.routing.risk",
    "policy": "engine.routing.policy",
    "run-check": "engine.routing.run_check",
    # 护栏
    "guard-command": "engine.guards.command_guard",
    "guard-git": "engine.guards.git_guard",
    # 派发与评审
    "dispatch": "engine.agents.dispatch",
    "review": "engine.agents.review",
    "review-pack": "engine.agents.review_pack",
    "review-plan": "engine.agents.plan_review",
    "identity": "engine.agents.identity",
    # 报告
    "metrics": "engine.reports.metrics",
    "weekly": "engine.reports.weekly",
    # 可观测性查询（同一模块两入口；不追加自身事件，见 QUIET_COMMANDS）
    "events": "engine.reports.trace:events_main",
    "trace": "engine.reports.trace:trace_main",
    # 审计引用复原与哈希核对（不追加自身事件；观察事件属 T404）
    "audit": "engine.reports.audit",
    # 安装与升级（只能从引擎仓库的检出运行）
    "install": "engine.core.install",
    "upgrade": "engine.core.install:upgrade_main",
}

# 入口事件的阶段映射（共用合同 C1）：判定入口归 verify，判级与路由归 route，派发归 dispatch，
# 评审归 review；表外的其余检查、报告与安装升级归 ci。新增入口必须落进这张表或登记例外。
COMMAND_STAGE = {
    "verify": "verify",
    "integrity": "verify",
    "risk": "route",
    "policy": "route",
    "dispatch": "dispatch",
    "review": "review",
    "review-pack": "review",
    "review-plan": "review",
}
# 不记通用入口事件的命令：guard 只记拒绝（T104，放行不逐条记，设计 3.6）；events/trace 查询、
# audit/alert 发布各自记事件，避免递归（C1）。
QUIET_COMMANDS = frozenset({"guard-command", "guard-git", "events", "trace", "audit", "alert"})


def usage() -> str:
    return "用法：cli.py <子命令> [参数…]\n子命令：" + "、".join(COMMANDS)


def _record_dispatch(name: str, started: float, outcome) -> None:
    """分派边界的观察旁路：一条 cli.<命令> 事件（时长、返回码或异常类别）；永不影响原行为。

    观察失败（含 events 导入失败，如引擎副本损坏）静默丢弃：原 SystemExit/业务异常/返回码
    与 stderr 逐字不变（共用合同 C0 三态等价）。
    """
    if name in QUIET_COMMANDS:
        return
    try:
        from engine.core import events  # 延迟导入：钩子等高频路径不付不必要的导入成本

        if isinstance(outcome, SystemExit):
            code = outcome.code if isinstance(outcome.code, int) else (0 if outcome.code is None else 1)
            status = "ok" if code == 0 else "fail"
            error = {"kind": "SystemExit"} if code else None
        elif isinstance(outcome, BaseException):
            code, status, error = None, "error", {"kind": type(outcome).__name__}
        else:
            code, status, error = outcome, ("ok" if outcome == 0 else "fail"), None
        events.emit(stage=COMMAND_STAGE.get(name, "ci"), step=f"cli.{name}", status=status,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    outputs={"code": code}, error=error)
    except Exception:  # noqa: BLE001  设计要求：观察失败不得影响原行为，也不得向 stderr 写提示
        return


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in {"-h", "--help"}:
        print(usage())
        return 0 if args else 2
    name, rest = args[0], args[1:]
    if name not in COMMANDS:
        print(f"未知子命令：{name}\n{usage()}", file=sys.stderr)
        return 2
    module_name, _, function = COMMANDS[name].partition(":")
    started = time.monotonic()
    try:
        module = importlib.import_module(module_name)
        sys.argv = [f"cli.py {name}", *rest]
        code = getattr(module, function or "main")(rest)
    except SystemExit as exc:
        _record_dispatch(name, started, exc)  # 仅记录，原样重抛
        raise
    except BaseException as exc:
        _record_dispatch(name, started, exc)  # 业务异常同样记录后原样重抛
        raise
    _record_dispatch(name, started, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
