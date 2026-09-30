"""覆盖变量判定的 shell 结构解析（B60）：引号内文字与 heredoc 正文一律当数据。

覆盖变量（HARNESS_*、*_ALLOW_MAIN/TAG/REWRITE、_SKIP_VERIFY 及拼接形式如 ${P}_ALLOW_TAG）
只供人使用，Agent 不能自行设置；但只看真正的赋值位置：命令前缀赋值（VAR=… cmd）、
`env VAR=…`、`export VAR=…`，以及递归展开后同样的位置（`bash -c`、`eval`、`$(...)`、反引号）。
heredoc（`<<`、`<<-`）与 here-string（`<<<`）正文、引号内文字、普通参数（如 `grep HARNESS_ …`）
一律当数据，不参与覆盖变量判定。

结构展开委托给 engine.core.shell_structure（把命令拆成简单命令并递归处理嵌套结构），
这里只取其中覆盖变量的判定结果；无法解析（引号不配对）时保守地整段扫描原文（宁可误报）。
"""

from __future__ import annotations

import re

from engine.core import shell_structure as structure

# 出现变量名或其后半截即视为覆盖变量（拼接如 ${P}_ALLOW_TAG 也能命中；评审 PR7-R6）。
PATTERN = re.compile(r"HARNESS_|_ALLOW_(MAIN|TAG|REWRITE)\b|_SKIP_VERIFY\b")


def check(command: str) -> list[str]:
    """命令在真赋值位置设置覆盖变量时返回拒绝理由；数据里的文字（heredoc 正文、引号内）不拒绝。"""
    try:
        found = structure.check(command, lambda _text: [], "designer")
    except ValueError:  # 引号不配对等无法解析：退回整段扫描（宁可误报）
        return [structure.OVERRIDE] if PATTERN.search(command) else []
    return [reason for reason in found if reason == structure.OVERRIDE]
