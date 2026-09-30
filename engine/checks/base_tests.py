"""已有判定器以 base 为准：用 base 版本的 tests/ 在 head 的代码上运行（评审 PR7-R2）。

执行者往已有测试文件末尾追加几行（例如 monkeypatch 把某个测试替换成空函数），或新增一个测试文件在
导入时篡改别的测试，都能让 head 上的测试套件「全绿」而判定形同虚设。这里把 tests/ 整体换回 base 版本
（head 新增的测试文件一并移除）再运行，追加和新增的代码都不会参与。

- PR 没有改动或删除已有测试、也没有改黄金快照时：base 测试必须全部通过，否则失败。
- PR 有这类改动（risk.py 已标为 R2，交评审）时：base 测试预期可能失败，只报告结果。
- 测试 import 的是 head 的产品代码，产品模块可在 import 时篡改 unittest（PR7-R7）：测试经
  `base_tests_runner.py` 运行，框架被篡改时一律失败，不论是否有意改动了测试。
- 测试读取的验收数据（`docs/specs/`、`.harness/state/acceptance-gaps.txt`）一并换回 base 版本（H0926-4）：
  head 在验收表里引用新测试是补测试的正常做法，与 base 测试放在一起会被误判为失败。

    bin/harness base-tests --base origin/main [--head HEAD] [--repo 路径]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from engine.core import events
from engine.core.common import ROOT, clean_git_env, git
from engine.routing import risk

INTENDED = ("改动已有测试", "删除已有测试", "黄金快照改动")
RUNNER = Path(__file__).resolve().parent / "base_tests_runner.py"
# 判定器的输入整体按 base 版本：测试本身，以及测试读取的验收数据。只还原 tests/ 时，
# head 在规格验收表里引用新测试（补测试的正常做法）会让 base 的验收映射测试失败（H0926-4）。
JUDGE_INPUTS = ("tests", "docs/specs", ".harness/state/acceptance-gaps.txt")


def _restore_base(judged: str, base: str, worktree: Path, cwd: Path) -> None:
    """worktree 中的 judged（目录或文件）换成 base 版本：head 新增的删除，其余检出 base。"""
    base_files = set(git("ls-tree", "-r", "--name-only", base, "--", judged, cwd=cwd).splitlines())
    target = worktree / judged
    candidates = sorted(target.rglob("*")) if target.is_dir() else [target]
    for path in candidates:
        if path.is_file() and path.relative_to(worktree).as_posix() not in base_files:
            path.unlink()
    if base_files:
        git("checkout", base, "--", judged, cwd=worktree, isolate=True)
TAMPERED = 3


def run(base: str, head: str = "HEAD", cwd: Path = ROOT) -> tuple[bool, bool, str]:
    """返回 (base 测试是否通过, 是否强制要求通过, 摘要)。测试框架被篡改时强制要求通过。"""
    if not git("ls-tree", "-r", "--name-only", base, "--", "tests", cwd=cwd).strip():
        return True, False, "base 没有 tests/，无已有测试可回放"
    intended = [flag for flag in risk.classify(base, head, cwd).flags if flag.startswith(INTENDED)]
    temp = Path(tempfile.mkdtemp(prefix="base-tests-"))
    worktree = temp / "wt"
    try:
        git("worktree", "add", "--detach", str(worktree), head, cwd=cwd, isolate=True)
        for judged in JUDGE_INPUTS:
            _restore_base(judged, base, worktree, cwd)
        completed = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", str(RUNNER)],
            cwd=worktree,
            capture_output=True,
            text=True,
            env=clean_git_env({"PYTHONDONTWRITEBYTECODE": "1"}),
            check=False,
        )
    finally:
        git("worktree", "remove", "--force", str(worktree), cwd=cwd, check=False, isolate=True)
        shutil.rmtree(temp, ignore_errors=True)
    lines = completed.stderr.strip().splitlines()
    summary = lines[-1] if lines else ""
    return completed.returncode == 0, not intended or completed.returncode == TAMPERED, summary


def _record(base: str, head: str, ok: bool, enforced: bool, summary: str) -> None:
    """观察旁路：一条 base_tests 事件（是否通过、是否强制、安全摘要行），失败不影响原判定。"""
    try:
        events.emit(stage="ci", step="base_tests", status="ok" if ok else "fail",
                    inputs=[events.ref("rev", base), events.ref("rev", head)],
                    outputs={"ok": ok, "enforced": enforced, "summary": summary or None})
    except Exception:  # noqa: BLE001  设计要求：事件失败不得影响调用方
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", type=Path, default=ROOT, help="要检查的仓库（默认本仓库）")
    args = parser.parse_args(argv)
    ok, enforced, summary = run(args.base, args.head, cwd=args.repo)
    _record(args.base, args.head, ok, enforced, summary)  # 观察：结果、强制与摘要行
    if ok:
        print(f"✓ base 版本的已有测试在 head 代码上全部通过（{summary}）")
        return 0
    if not enforced:
        print(f"- base 版本的已有测试在 head 上失败（{summary}）；本 PR 改动了已有测试或黄金快照，已按 R2 交评审，只报告")
        return 0
    print(f"✗ base 版本的已有测试在 head 代码上失败（{summary}）：已有判定器被绕过，或产品行为变了却没有更新测试")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
