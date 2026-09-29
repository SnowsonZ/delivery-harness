"""引擎完整性：业务仓库里的 .harness/engine/ 必须与 .harness/engine.lock 记录的发布版本逐字节一致。

引擎以内置副本的形式装进业务仓库（经用户批准合并到 main，守卫与规则都从 main 读取）。
锁文件记录版本、引擎仓库提交与目录树哈希；任何人（包括执行方）改了引擎文件而没有走 upgrade（在引擎仓库的检出中运行），
哈希就对不上，verify 失败。升级引擎只能由 upgrade 同时改引擎与锁文件，属于 R3，由用户批准。

    bin/harness integrity            核对（verify 各档运行）
    bin/harness integrity --print    打印当前目录树哈希
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from engine.core.common import ENGINE_DIR, HARNESS_DIR

LOCK_FILE = HARNESS_DIR / "engine.lock"
IGNORED_PARTS = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def tree_hash(engine_dir: Path = ENGINE_DIR) -> str:
    """按相对路径排序，对「路径 NUL 内容哈希」逐行求 sha256；忽略字节码缓存。"""
    digest = hashlib.sha256()
    for path in sorted(p for p in engine_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(engine_dir)
        if IGNORED_PARTS & set(rel.parts) or path.suffix in IGNORED_SUFFIXES:
            continue
        digest.update(rel.as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).hexdigest().encode() + b"\n")
    return f"sha256:{digest.hexdigest()}"


def read_lock(path: Path = LOCK_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_lock(path: Path, version: str, commit: str, tree: str) -> None:
    data = {"engine": "delivery-harness", "version": version, "commit": commit, "tree": tree}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check(engine_dir: Path = ENGINE_DIR, lock_file: Path = LOCK_FILE) -> tuple[bool, str]:
    if engine_dir.parent != lock_file.parent:
        return True, "引擎不在业务仓库的 .harness/ 下（引擎仓库自身），无需核对"
    if not lock_file.exists():
        return False, f"缺少 {lock_file.name}：在引擎仓库的检出中运行 `python3 engine/cli.py upgrade --target <本仓库>`，不要手工复制"
    lock = read_lock(lock_file)
    actual = tree_hash(engine_dir)
    if actual != lock.get("tree"):
        return False, (
            f"引擎文件与锁定的 {lock.get('version')}（{str(lock.get('commit', ''))[:12]}）不一致：\n"
            f"  锁定 {lock.get('tree')}\n  实际 {actual}\n"
            "引擎只能在引擎仓库的检出中用 `python3 engine/cli.py upgrade --target <本仓库>` 整体替换（R3，由用户批准）；本地修改请撤销"
        )
    return True, f"引擎 {lock.get('version')}（{str(lock.get('commit', ''))[:12]}）与锁文件一致 ✓"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="打印当前目录树哈希")
    args = parser.parse_args(argv)
    if args.print:
        print(tree_hash())
        return 0
    ok, message = check()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
