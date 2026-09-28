"""熵治理：产品代码的复杂度与体量只降不升（报告 11.4；He 等 MSR 2026：复杂度持续上升是后期变慢的主因）。

指标（scripts/ 与 native/）：
  complex_functions   ruff C901 超标函数数（Python，圈复杂度 > 10）          只降不升
  files_over_800      超过 800 行的源文件数                                 只降不升
  largest_file_lines  最大源文件行数                                        只报告（往大文件里加小修复不应被拦）
  swift_files_over_500   native/ 下超过 500 行的 Swift 文件数               只降不升
  swift_long_functions   函数体超过 60 行的 Swift 函数数                     只降不升
  swift_deep_functions   大括号嵌套深度超过 6 的 Swift 函数数               只降不升

Swift 没有 ruff 这样的现成工具，这里用轻量解析（只用标准库）：先把注释（含嵌套块注释）与字符串字面量
（普通、多行三引号、带 # 的原始字符串）抹成空格，再配对大括号。「函数」指 func、init、deinit 与计算属性
（`var x: T {`，SwiftUI 的 body 即此类）；函数体行数从 `{` 所在行数到匹配的 `}` 所在行，嵌套深度以函数体
自身为 1，闭包与控制流都计入。解析不求完美（如 #if 分支、正则字面量不处理），只需对同一份代码稳定。

阈值取自 2026-09-28 native/ 的分布（332 个函数）：函数体行数中位 6、P90 29、P95 48、P99 88，
取 60 行约为前 3%；嵌套深度 1–4 占 92%，SwiftUI 视图常见 5–6 层，超过 6 层只剩 13 个，都是可以
拆出子视图的大 body。文件 500 行：现有 Swift 文件多在 400 行上下，超过 500 行的只有 2 个。

    python3 harness/quality.py            打印指标并与 harness/quality-baseline.json 比较，棘轮指标上升即失败
    python3 harness/quality.py --update   指标下降后写回基线（上调基线须直接改文件，属 R3，由评审确认）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT

BASELINE = ROOT / "harness" / "quality-baseline.json"
SOURCES = [("scripts", "*.py"), ("native", "**/*.swift")]
LONG_FILE = 800
SWIFT_LONG_FILE = 500
SWIFT_LONG_FUNCTION = 60
SWIFT_DEEP_NESTING = 6
RATCHETED = (
    "complex_functions",
    "files_over_800",
    "swift_files_over_500",
    "swift_long_functions",
    "swift_deep_functions",
)

_STRING_START = re.compile(r'(#*)("""|")')
_SWIFT_DECL = re.compile(
    r"(?<![.\w])(?:func\s+(?P<func>[^\s(<]+)|(?P<init>init|deinit)\b[?!]?|var\s+(?P<var>\w+)\s*:\s*[^=\n{};]+?\{)"
)
_DECL_KEYWORD = re.compile(r"(?<![.\w])(?:func|var|let|init|case|struct|class|enum|protocol|extension)\b")


def strip_swift(source: str) -> str:
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
            hashes, quote = match.group(1), match.group(2)
            closing = quote + hashes
            j = match.end()
            while j < n:
                if not hashes and source[j] == "\\":
                    j += 2
                    continue
                if source.startswith(closing, j):
                    j += len(closing)
                    break
                if quote == '"' and source[j] == "\n":
                    break
                j += 1
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


def swift_functions(source: str) -> list[tuple[str, int, int, int]]:
    """返回 (名字, 声明行号, 函数体行数, 最大嵌套深度)。"""
    text = strip_swift(source)
    closing: dict[int, int] = {}
    stack: list[int] = []
    for index, ch in enumerate(text):
        if ch == "{":
            stack.append(index)
        elif ch == "}" and stack:
            closing[stack.pop()] = index
    functions = []
    for decl in _SWIFT_DECL.finditer(text):
        open_at = decl.end() - 1 if decl.group("var") else _body_open(text, decl.end())
        if open_at is None or open_at not in closing:
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


def measure_swift(root: Path = ROOT) -> dict[str, int]:
    files = long_functions = deep_functions = 0
    for path in sorted((root / "native").glob("**/*.swift")):
        source = path.read_text(encoding="utf-8")
        files += len(source.splitlines()) > SWIFT_LONG_FILE
        for _name, _line, body_lines, deepest in swift_functions(source):
            long_functions += body_lines > SWIFT_LONG_FUNCTION
            deep_functions += deepest > SWIFT_DEEP_NESTING
    return {
        "swift_files_over_500": files,
        "swift_long_functions": long_functions,
        "swift_deep_functions": deep_functions,
    }


def measure(root: Path = ROOT) -> dict[str, int]:
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "scripts", "--select", "C901", "--output-format", "concise", "--no-cache"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    complex_functions = sum(1 for line in completed.stdout.splitlines() if " C901 " in line)
    sizes = []
    for directory, pattern in SOURCES:
        for path in (root / directory).glob(pattern):
            sizes.append(len(path.read_text(encoding="utf-8").splitlines()))
    return {
        "complex_functions": complex_functions,
        "largest_file_lines": max(sizes, default=0),
        "files_over_800": sum(size > LONG_FILE for size in sizes),
        **measure_swift(root),
    }


def compare(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    return [
        f"{name} 从 {baseline[name]} 升到 {current[name]}"
        for name in RATCHETED
        if name in baseline and current[name] > baseline[name]
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="指标不高于基线时写回基线")
    args = parser.parse_args(argv)
    current = measure()
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    for name, value in current.items():
        kind = "棘轮" if name in RATCHETED else "报告"
        print(f"{name:<20} {value:>6}（基线 {baseline.get(name, '—')}，{kind}）")
    regressions = compare(current, baseline)
    if args.update:
        if regressions:
            print("✗ 指标高于基线，不能用 --update 上调：" + "；".join(regressions))
            return 1
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"基线已写入 {BASELINE.relative_to(ROOT)}")
        return 0
    for regression in regressions:
        print(f"✗ {regression}：先拆分或简化；确需上调基线请改 harness/quality-baseline.json 并说明理由（R3）")
    return 1 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
