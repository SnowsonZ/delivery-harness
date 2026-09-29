"""安装与升级：把引擎以内置副本的形式装进业务仓库，并写锁文件（只能从引擎仓库的检出运行）。

    python3 <引擎仓库>/engine/cli.py install --target <业务仓库>    首次安装：引擎、锁文件、配置骨架与模板
    python3 <引擎仓库>/engine/cli.py upgrade --target <业务仓库>    升级：只整体替换引擎与锁文件

模板（bin/、.githooks/、各 Agent 的守卫钩子、.harness/ 的配置骨架）只在目标文件不存在时写入，已有文件一律不覆盖，
结束时列出跳过的文件，由接入方按需合并。锁文件记录版本、引擎仓库提交与目录树哈希，verify 的 integrity 检查据此
拒绝任何绕过升级流程的引擎改动。安装与升级的结果都应经 PR 合并（R3，由用户批准）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from engine import __version__
from engine.checks.integrity import tree_hash, write_lock
from engine.core.common import ENGINE_DIR

TEMPLATES = ENGINE_DIR.parent / "templates"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def engine_commit(allow_dirty: bool) -> str:
    """引擎仓库当前提交；engine/ 有未提交改动时拒绝（锁文件必须对应一个真实提交）。"""
    repo = ENGINE_DIR.parent
    head = _git("rev-parse", "HEAD", cwd=repo)
    if head.returncode != 0:
        raise SystemExit("只能从引擎仓库的 git 检出运行 install/upgrade")
    dirty = _git("status", "--porcelain", "--", "engine", "templates", cwd=repo).stdout.strip()
    if dirty and not allow_dirty:
        raise SystemExit("引擎仓库的 engine/ 或 templates/ 有未提交改动：先提交或检出一个发布 tag")
    return head.stdout.strip() + ("-dirty" if dirty else "")


def not_on_origin_main() -> str:
    """引擎仓库当前提交不在本地 origin/main 上时返回原因（守卫的可信来源是合并到 main 的代码）；否则空串。"""
    repo = ENGINE_DIR.parent
    if _git("rev-parse", "--verify", "-q", "origin/main", cwd=repo).returncode != 0:
        return "引擎仓库没有 origin/main 引用，无法确认提交已合并"
    if _git("merge-base", "--is-ancestor", "HEAD", "origin/main", cwd=repo).returncode != 0:
        return "引擎仓库当前提交不在 origin/main 上：先 `git checkout main && git pull`"
    return ""


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def migration_notes(previous_version: str) -> list[str]:
    """CHANGELOG 中晚于 previous_version 的版本段（含 Unreleased）里带 `**Migration:**` 的条目。previous_version 为空时不提示。"""
    changelog = ENGINE_DIR.parent / "CHANGELOG.md"
    if not previous_version or not changelog.exists():
        return []
    notes, applies = [], False
    for line in changelog.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            version = re.search(r"\d+\.\d+\.\d+", line)
            applies = "Unreleased" in line or (version is not None and _version_tuple(version.group()) > _version_tuple(previous_version))
        elif applies and line.lstrip("- ").startswith("**Migration:**"):
            notes.append(line.lstrip("- ").strip())
    return notes


def copy_engine(target: Path) -> Path:
    destination = target / ".harness" / "engine"
    if destination.resolve() == ENGINE_DIR.resolve():
        raise SystemExit("不能把引擎装到它自己所在的位置")
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ENGINE_DIR, destination, ignore=IGNORE)
    return destination


def copy_templates(target: Path) -> tuple[list[str], list[str]]:
    """返回 (新写入, 已存在而跳过)。可执行位随模板保留。"""
    written, skipped = [], []
    for source in sorted(p for p in TEMPLATES.rglob("*") if p.is_file()):
        rel = source.relative_to(TEMPLATES)
        if "__pycache__" in rel.parts:
            continue
        destination = target / rel
        if destination.exists():
            skipped.append(rel.as_posix())
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        written.append(rel.as_posix())
    return written, skipped


def install(target: Path, templates: bool, allow_dirty: bool) -> int:
    if not (target / ".git").exists():
        raise SystemExit(f"{target} 不是 git 仓库的根目录")
    if not allow_dirty and (reason := not_on_origin_main()):
        raise SystemExit(reason + "（开发调试可加 --allow-dirty）")
    lock = target / ".harness" / "engine.lock"
    previous = lock.read_text(encoding="utf-8") if lock.exists() else ""
    commit = engine_commit(allow_dirty)
    destination = copy_engine(target)
    write_lock(lock, __version__, commit, tree_hash(destination))
    print(f"引擎 {__version__}（{commit[:12]}）已装到 {destination.relative_to(target)}，锁文件已写入")
    if previous:
        print("  升级前的锁文件：" + " ".join(previous.split()))
        notes = migration_notes(json.loads(previous).get("version", ""))
        if notes:
            print("升级后需要你手工处理的迁移项（CHANGELOG）：")
            for note in notes:
                print(f"  ! {note}")
        print("升级后：bin/verify --full，经 PR 合并（R3，由用户批准）；流程见引擎仓库 docs/upgrading.md")
    if templates:
        written, skipped = copy_templates(target)
        for rel in written:
            print(f"  + {rel}")
        if skipped:
            print("已存在、未覆盖（按需手工合并）：")
            for rel in skipped:
                print(f"  = {rel}")
        print("下一步：填 .harness/config/checks.toml 的 [identity] 与 [sources]、[verify]；"
              "bin/harness guard-git install；bin/harness verify；经 PR 合并（R3，由用户批准）")
    return 0


def main(argv: list[str] | None = None, command: str = "install") -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, help="业务仓库根目录")
    parser.add_argument("--allow-dirty", action="store_true", help="允许引擎仓库有未提交改动、提交不在 origin/main 上（仅限开发调试，锁文件会标 -dirty）")
    args = parser.parse_args(argv)
    return install(Path(args.target).resolve(), templates=command == "install", allow_dirty=args.allow_dirty)


def upgrade_main(argv: list[str] | None = None) -> int:
    return main(argv, command="upgrade")


if __name__ == "__main__":
    raise SystemExit(main())
