"""引擎完整性：业务仓库里的 .harness/engine/ 必须与 .harness/engine.lock 记录的发布版本逐字节一致。

引擎以内置副本的形式装进业务仓库（经用户批准合并到 main，守卫与规则都从 main 读取）。
锁文件记录版本、引擎仓库提交与目录树哈希；任何人（包括执行方）改了引擎文件而没有走 upgrade（在引擎仓库的检出中运行），
哈希就对不上，verify 失败。升级引擎只能由 upgrade 同时改引擎与锁文件，属于 R3，由用户批准。

    bin/harness integrity            核对（verify 各档运行）
    bin/harness integrity --print    打印当前目录树哈希

核对本身是一条观察事件（设计 3.3）：lock 引用、目录摘要、文件计数与失败签名，写入失败不影响原判定。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from engine.core import events
from engine.core.common import ENGINE_DIR, HARNESS_DIR, git

LOCK_FILE = HARNESS_DIR / "engine.lock"
IGNORED_PARTS = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _scan(engine_dir: Path) -> tuple[str, dict[str, str]]:
    """一次遍历同时得到目录树哈希与逐文件哈希（同一过滤：忽略字节码缓存）。

    哈希算法与既有实现逐字节一致：按相对路径排序，对「路径 NUL 内容哈希」逐行求 sha256。
    """
    digest = hashlib.sha256()
    hashes: dict[str, str] = {}
    for path in sorted(p for p in engine_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(engine_dir)
        if IGNORED_PARTS & set(rel.parts) or path.suffix in IGNORED_SUFFIXES:
            continue
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(rel.as_posix().encode() + b"\0" + file_hash.encode() + b"\n")
        hashes[rel.as_posix()] = file_hash
    return f"sha256:{digest.hexdigest()}", hashes


def tree_hash(engine_dir: Path = ENGINE_DIR) -> str:
    """目录树哈希（install 写锁与 integrity 核对共用同一算法）。"""
    return _scan(engine_dir)[0]


def read_lock(path: Path = LOCK_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_lock(path: Path, version: str, commit: str, tree: str) -> None:
    data = {"engine": "delivery-harness", "version": version, "commit": commit, "tree": tree}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check(engine_dir: Path | None = None, lock_file: Path | None = None) -> tuple[bool, str]:
    """核对引擎目录与锁文件；参数缺省取当前安装位置（延迟绑定，便于隔离测试与工具复用）。"""
    engine_dir = ENGINE_DIR if engine_dir is None else engine_dir
    lock_file = LOCK_FILE if lock_file is None else lock_file
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


def _changed_count(engine_dir: Path) -> int | None:
    """引擎目录里未提交的变更文件数（含未跟踪）。

    锁文件只有整体摘要，无法逐文件回溯；未提交改动是「改了引擎而没走 upgrade」的主要形态，
    以 git HEAD 为基线给出被改文件数（不含内容）。git 不可用时返回 None。
    """
    try:
        out = git("status", "--porcelain", "--untracked-files=all", "--", ".",
                  cwd=engine_dir, check=True, isolate=True)
    except RuntimeError:
        return None
    return sum(1 for line in out.splitlines() if line.strip())


def _metadata() -> tuple[list[dict], dict, str | None]:
    """事件的 lock 引用、目录摘要、文件计数与失败签名；各项准备失败只让对应字段缺失。"""
    inputs: list[dict] = []
    try:
        inputs.append(events.file_ref("lock", LOCK_FILE))
    except OSError:
        pass
    signature = None if LOCK_FILE.exists() else "lock_missing"
    outputs: dict = {}
    try:
        tree, hashes = _scan(ENGINE_DIR)
    except OSError:
        return inputs, outputs, signature
    outputs["tree"] = tree
    outputs["files"] = len(hashes)
    changed = _changed_count(ENGINE_DIR)
    if changed is not None:
        outputs["changed"] = changed
    try:
        lock = read_lock(LOCK_FILE)
    except Exception:  # noqa: BLE001  缺锁或坏锁：事件只缺对应字段，不改变原判定
        lock = {}
    if lock.get("tree"):
        outputs["lock.tree"] = lock["tree"]
        if signature is None and lock["tree"] != tree:
            signature = "tree_mismatch"
    return inputs, outputs, signature


def _record(ok: bool) -> None:
    """观察旁路：一条 integrity 内容事件（stage=verify）；准备失败不得影响原判定与输出。"""
    try:
        if ENGINE_DIR.parent != LOCK_FILE.parent:  # 引擎仓库自身：核对不适用
            events.emit(stage="verify", step="integrity", status="skip")
            return
        inputs, outputs, signature = _metadata()
        events.emit(stage="verify", step="integrity", status="ok" if ok else "fail",
                    inputs=inputs or None, outputs=outputs or None,
                    error=None if ok else {"kind": "integrity_mismatch", "signature": signature})
    except Exception:  # noqa: BLE001  观察失败不得影响调用方
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="打印当前目录树哈希")
    args = parser.parse_args(argv)
    if args.print:
        print(tree_hash())
        return 0
    ok, message = check()
    _record(ok)  # 观察：lock 引用、目录摘要、变更文件计数；失败 error.kind=integrity_mismatch
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
