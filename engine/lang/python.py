"""Python 语言插件：按 AST 取函数签名；新增 import 只能是标准库或仓库内模块。"""

from __future__ import annotations

import ast
import re
import sys

SUFFIXES = (".py",)
IMPORT = re.compile(r"^\s*(?:from\s+(?P<from>[\w.]+)\s+import\b|import\s+(?P<names>[\w., ]+))")


def signatures(source: str) -> dict[str, str]:
    """模块与类中的函数 → 参数与返回注解的文本。语法错误时返回空（由测试与 lint 拦住）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    result: dict[str, str] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                kind = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
                result[prefix + node.name] = f"{kind}({ast.unparse(node.args)}){returns}"
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body, "")
    return result


def new_dependencies(path: str, lines: list[str], local: set[str]) -> list[str]:
    found = []
    for text in lines:
        match = IMPORT.match(text)
        if not match:
            continue
        modules = [match["from"]] if match["from"] else [part.split()[0] for part in match["names"].split(",") if part.strip()]
        for module in modules:
            top = module.split(".")[0]
            if top and top not in sys.stdlib_module_names and top not in local and top != "__future__":
                found.append(f"`{path}` 新增了对 `{top}` 的依赖")
    return found
