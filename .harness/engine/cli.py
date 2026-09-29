"""delivery-harness 唯一入口：python3 <引擎目录>/cli.py <子命令> [参数…]

CI、git 钩子、Agent 宿主钩子与引擎内部的子进程调用都经这里启动，各子命令的参数见对应模块的说明。
"""

from __future__ import annotations

import importlib
import sys
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
    "identity": "engine.agents.identity",
    # 报告
    "metrics": "engine.reports.metrics",
    "weekly": "engine.reports.weekly",
    # 安装与升级（只能从引擎仓库的检出运行）
    "install": "engine.core.install",
    "upgrade": "engine.core.install:upgrade_main",
}


def usage() -> str:
    return "用法：cli.py <子命令> [参数…]\n子命令：" + "、".join(COMMANDS)


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
    module = importlib.import_module(module_name)
    sys.argv = [f"cli.py {name}", *rest]
    return getattr(module, function or "main")(rest)


if __name__ == "__main__":
    sys.exit(main())
