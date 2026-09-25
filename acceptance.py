"""验收映射检查：docs/specs 中每条验收编号都要有证据类型；可自动化的要有真实存在的测试。

规格里的验收表以表头含「证据类型」与「覆盖」两列识别，第一列是编号（全局唯一）：

    | 编号 | 验收内容 | 证据类型 | 覆盖 |
    | U4 | 规范名 … | 单测 | `test_model_names.CanonicalTest` |

证据类型（可多选，用「、」或「+」分隔）：
  可自动化  单测、夹具、性质、快照、架构、模拟协议、真实 PTY、CI 断言  → 「覆盖」列至少一个有效引用
  人工      真实数据、真机 UI、人工                    → 进入人工验收清单（--manual）

「覆盖」列中用反引号写引用：
  `test_mod`、`test_mod.Class`、`test_mod.Class.test_x`   tests/ 下的 Python 测试（按 AST 核对存在）
  `路径#片段`                                              文件存在且包含该片段（Swift 断言、CI 步骤）

暂时没有测试的可自动化条目登记在 harness/acceptance-gaps.txt（带原因）；补上测试后必须从清单删除，
清单只能缩减。

    python3 harness/acceptance.py            检查（verify 默认档运行）
    python3 harness/acceptance.py --manual   列出人工验收清单
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT

SPECS_DIR = ROOT / "docs" / "specs"
GAPS_FILE = ROOT / "harness" / "acceptance-gaps.txt"
# 与 AGENTS.md「验证边界」一致：单元测试、模拟协议、真实 PTY 可自动化；用户 UI 验收属人工。
AUTOMATABLE = {"单测", "夹具", "性质", "快照", "架构", "模拟协议", "真实 PTY", "CI 断言"}
MANUAL = {"真实数据", "真机 UI", "人工"}
ID_RE = re.compile(r"^[A-Z]{1,3}\d+$")


@dataclass
class Item:
    id: str
    spec: str
    line: int
    text: str
    kinds: list[str]
    refs: list[str]
    valid_refs: list[str] = field(default_factory=list)

    @property
    def automatable(self) -> bool:
        return any(kind in AUTOMATABLE for kind in self.kinds)

    @property
    def manual(self) -> bool:
        return any(kind in MANUAL for kind in self.kinds)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_spec(path: Path, root: Path = ROOT) -> list[Item]:
    items = []
    lines = path.read_text(encoding="utf-8").splitlines()
    columns = None
    for number, line in enumerate(lines, 1):
        if not line.lstrip().startswith("|"):
            columns = None
            continue
        cells = _cells(line)
        if "证据类型" in cells and "覆盖" in cells:
            columns = {"kind": cells.index("证据类型"), "cover": cells.index("覆盖")}
            continue
        if columns is None or not ID_RE.match(cells[0]):
            continue
        kinds = [part.strip() for part in re.split(r"[、+＋]", cells[columns["kind"]]) if part.strip()]
        refs = re.findall(r"`([^`]+)`", cells[columns["cover"]])
        text = cells[1] if len(cells) > 1 else ""
        items.append(Item(cells[0], str(path.relative_to(root)), number, text, kinds, refs))
    return items


@cache
def _test_symbols(module: str, tests_dir: Path) -> set[str] | None:
    path = tests_dir / f"{module}.py"
    if not path.exists():
        return None
    symbols = {module}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ClassDef):
            symbols.add(f"{module}.{node.name}")
            for child in node.body:
                if isinstance(child, ast.FunctionDef):
                    symbols.add(f"{module}.{node.name}.{child.name}")
    return symbols


def resolve(ref: str, root: Path = ROOT) -> bool:
    if "#" in ref:
        rel, snippet = ref.split("#", 1)
        target = root / rel
        return target.is_file() and snippet in target.read_text(encoding="utf-8")
    if ref.startswith("test_"):
        symbols = _test_symbols(ref.split(".")[0], root / "tests")
        return symbols is not None and ref in symbols
    return (root / ref).exists()


def load_gaps(path: Path = GAPS_FILE) -> dict[str, str]:
    gaps = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            entry, _, reason = line.partition("#")
            if entry.strip():
                gaps[entry.strip()] = reason.strip()
    return gaps


def check(root: Path = ROOT, gaps: dict[str, str] | None = None) -> tuple[list[Item], list[str]]:
    gaps = load_gaps() if gaps is None else gaps
    items = [item for spec in sorted((root / "docs" / "specs").glob("*.md")) for item in parse_spec(spec, root)]
    errors = []
    seen: dict[str, Item] = {}
    for item in items:
        where = f"{item.spec}:{item.line} {item.id}"
        if item.id in seen:
            errors.append(f"{where}：编号与 {seen[item.id].spec}:{seen[item.id].line} 重复")
        seen[item.id] = item
        unknown = [kind for kind in item.kinds if kind not in AUTOMATABLE | MANUAL]
        if not item.kinds or unknown:
            errors.append(f"{where}：证据类型缺失或不在词表中（{'、'.join(unknown) or '空'}）")
            continue
        broken = [ref for ref in item.refs if not resolve(ref, root)]
        if broken:
            errors.append(f"{where}：覆盖引用不存在（{'、'.join(broken)}）")
        valid = [ref for ref in item.refs if ref not in broken]
        item.valid_refs = valid
        if item.automatable and not valid and item.id not in gaps:
            errors.append(f"{where}：可自动化条目没有测试，也不在 acceptance-gaps.txt 中")
        if item.id in gaps and valid:
            errors.append(f"{where}：已有覆盖（{'、'.join(valid)}），请从 acceptance-gaps.txt 删除")
    for gap in gaps:
        if gap not in seen:
            errors.append(f"acceptance-gaps.txt：{gap} 不是任何规格中的验收编号")
        elif not seen[gap].automatable:
            errors.append(f"acceptance-gaps.txt：{gap} 不含可自动化证据类型，不应登记为缺口")
    return items, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manual", action="store_true", help="列出人工验收清单")
    args = parser.parse_args(argv)
    items, errors = check()
    if args.manual:
        for item in items:
            if item.manual:
                print(f"{item.id:<5} [{'、'.join(k for k in item.kinds if k in MANUAL)}] {item.text}  （{item.spec}）")
        return 0
    gaps = load_gaps()
    automatable = [item for item in items if item.automatable]
    covered = [item for item in automatable if item.valid_refs]
    registered = [item for item in automatable if not item.valid_refs and item.id in gaps]
    print(
        f"验收编号 {len(items)} 条：可自动化 {len(automatable)}（有测试 {len(covered)}，登记缺口 {len(registered)}），"
        f"需人工 {sum(item.manual for item in items)}"
    )
    for error in errors:
        print(f"✗ {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
