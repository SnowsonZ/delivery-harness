"""熵治理：产品代码的复杂度与体量只降不升（报告 11.4；He 等 MSR 2026：复杂度持续上升是后期变慢的主因）。

指标（scripts/ 与 native/）：
  complex_functions   ruff C901 超标函数数（Python，圈复杂度 > 10）          只降不升
  files_over_800      超过 800 行的源文件数                                 只降不升
  largest_file_lines  最大源文件行数                                        只报告（往大文件里加小修复不应被拦）

    python3 harness/quality.py            打印指标并与 harness/quality-baseline.json 比较，棘轮指标上升即失败
    python3 harness/quality.py --update   指标下降后写回基线（上调基线须直接改文件，属 R3，由评审确认）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT

BASELINE = ROOT / "harness" / "quality-baseline.json"
SOURCES = [("scripts", "*.py"), ("native", "**/*.swift")]
LONG_FILE = 800
RATCHETED = ("complex_functions", "files_over_800")


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
