r"""Swift 语言插件（只用标准库的轻量解析）。

函数体量与嵌套深度：先把注释（含嵌套块注释）与字符串字面量（普通、多行三引号、带 # 的原始字符串）抹成空格，
再配对大括号。字符串插值 `\(...)` 里的代码连同内嵌的字符串字面量一起抹平，不让内层引号提前终止外层字符串。
「函数」指 func、init、deinit 与计算属性（`var x: T {`，SwiftUI 的 body 即此类）；协议成员只有声明要求、
没有实现体（如 `var x: T { get }`），不计入。函数体行数从 `{` 所在行数到匹配的 `}` 所在行，嵌套深度以函数体
自身为 1，闭包与控制流都计入。解析不求完美（如 #if 分支、正则字面量不处理），只需对同一份代码稳定。
签名按声明文本比对，重载按多个签名计；新增 import 即算新依赖。
"""

from __future__ import annotations

import re

SUFFIXES = (".swift",)

FUNC = re.compile(
    r"\bfunc\s+(?P<name>[A-Za-z_]\w*)\s*(?P<generic><[^>{]*>)?\s*\((?P<params>[^)]*)\)"
    r"(?P<tail>[^{\n]*)"
)
IMPORT = re.compile(r"^\s*(?:@\w+\s+)?import\s+(\w+)")

_STRING_START = re.compile(r'(#*)("""|")')
_SWIFT_DECL = re.compile(
    r"(?<![.\w])(?:func\s+(?P<func>[^\s(<]+)|(?P<init>init|deinit)\b[?!]?|var\s+(?P<var>\w+)\s*:\s*[^=\n{};]+?\{)"
)
_DECL_KEYWORD = re.compile(r"(?<![.\w])(?:func|var|let|init|case|struct|class|enum|protocol|extension)\b")
_PROTOCOL = re.compile(r"(?<![.\w])protocol\s+\w+")


def _string_end(source: str, start: int, hashes: str, quote: str) -> int:
    r"""从 opening 之后扫描字符串字面量，返回结束位置（closing 之后）。

    识别插值 `\(...)` / `\#(...)`：插值内的嵌套字符串递归配对，不让内层引号提前终止外层字符串。
    单行字符串未闭合时吞到行尾或文件尾（与既有行为一致）。
    """
    closing = quote + hashes
    multiline = quote == '"""'
    n = len(source)
    j = start
    while j < n:
        ch = source[j]
        if ch == "\\":
            if source.startswith(hashes + "(", j + 1):
                j = _interp_end(source, j + 2 + len(hashes), hashes, multiline)
                continue
            if not hashes:  # 普通字符串的转义 `\x`；原始字符串没有转义
                j += 2
                continue
        if source.startswith(closing, j):
            return j + len(closing)
        if ch == "\n" and not multiline:
            return j
        j += 1
    return n


def _interp_end(source: str, start: int, hashes: str, multiline: bool) -> int:
    r"""扫描插值表达式，返回配对 `)` 之后的位置；插值内是代码上下文。

    圆括号计深度；字符串字面量（含插值，可递归）整体配对后跳过；单行字符串的插值遇到
    换行视为未闭合，停在该换行处。
    """
    n = len(source)
    depth = 1
    j = start
    while j < n:
        ch = source[j]
        if ch == '"':
            match = _STRING_START.match(source, j)
            if match:
                j = _string_end(source, match.end(), match.group(1), match.group(2))
                continue
        elif ch == "#":
            match = _STRING_START.match(source, j)
            if match and match.group(1):  # 插值里的原始字符串 `#"…"#`
                j = _string_end(source, match.end(), match.group(1), match.group(2))
                continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        elif ch == "\n" and not multiline:
            return j
        j += 1
    return n


def strip(source: str) -> str:
    """把注释与字符串字面量抹成空格（保留换行，行号不变），其余字符原样保留。"""
    out: list[str] = []
    i, n = 0, len(source)
    block_depth = 0
    while i < n:
        ch = source[i]
        if block_depth:  # Swift 的块注释可以嵌套
            if source.startswith("/*", i) or source.startswith("*/", i):
                block_depth += 1 if source[i] == "/" else -1
                out.append("  ")
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        if source.startswith("/*", i):
            block_depth = 1
            out.append("  ")
            i += 2
            continue
        match = _STRING_START.match(source, i)
        if match and (match.group(1) or ch == '"'):
            j = _string_end(source, match.end(), match.group(1), match.group(2))
            out.append("".join("\n" if c == "\n" else " " for c in source[i:j]))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _body_open(text: str, index: int) -> int | None:
    """从声明名之后找函数体的 `{`：跳过参数表与泛型；先遇到 `}`、`=` 或下一个声明说明没有函数体（协议要求）。"""
    parens = 0
    while index < len(text):
        ch = text[index]
        if ch in "([":
            parens += 1
        elif ch in ")]":
            parens -= 1
        elif parens == 0:
            if ch == "{":
                return index
            if ch in "};=" or _DECL_KEYWORD.match(text, index):
                return None
        index += 1
    return None


def functions(source: str) -> list[tuple[str, int, int, int]]:
    """返回 (名字, 声明行号, 函数体行数, 最大嵌套深度)。协议成员只有要求、没有实现体，不计入。"""
    text = strip(source)
    closing: dict[int, int] = {}
    stack: list[int] = []
    for index, ch in enumerate(text):
        if ch == "{":
            stack.append(index)
        elif ch == "}" and stack:
            closing[stack.pop()] = index
    protocols: list[tuple[int, int]] = []
    for match in _PROTOCOL.finditer(text):
        open_at = _body_open(text, match.end())
        if open_at is not None and open_at in closing:
            protocols.append((open_at, closing[open_at]))
    functions = []
    for decl in _SWIFT_DECL.finditer(text):
        open_at = decl.end() - 1 if decl.group("var") else _body_open(text, decl.end())
        if open_at is None or open_at not in closing:
            continue
        if any(lo <= open_at <= hi for lo, hi in protocols):
            continue
        close_at = closing[open_at]
        depth = deepest = 0
        for ch in text[open_at : close_at + 1]:
            if ch == "{":
                depth += 1
                deepest = max(deepest, depth)
            elif ch == "}":
                depth -= 1
        name = decl.group("func") or decl.group("init") or decl.group("var")
        body_lines = text.count("\n", open_at, close_at) + 1
        functions.append((name, text.count("\n", 0, decl.start()) + 1, body_lines, deepest))
    return functions


def signatures(source: str) -> dict[str, list[str]]:
    """函数名 → 该名下全部声明（重载按多个签名计）。"""
    result: dict[str, list[str]] = {}
    for match in FUNC.finditer(source):
        text = " ".join(f"{match['generic'] or ''}({match['params']}){match['tail']}".split())
        result.setdefault(match["name"], []).append(text)
    return {name: sorted(items) for name, items in result.items()}


def new_dependencies(path: str, lines: list[str], local: set[str]) -> list[str]:
    return [f"`{path}` 新增了 `import {match[1]}`" for text in lines if (match := IMPORT.match(text))]
