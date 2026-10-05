"""派发的槽位管理（T707 自 dispatch.py 逐字移出，行为不变）。

调用原模块的名字（state_dir、_alive、slot_path、_now、Stop、preserved_paths，以及原本同在
一模块的 _registered_worktrees、_salvage_and_remove）时按需在函数体内导入 dispatch 并以
属性访问：保留测试在原模块上的 patch 语义，也避免循环导入。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from engine.agents import dispatch_observation as observation
from engine.core.common import git

if TYPE_CHECKING:
    from engine.agents.dispatch import Config, Task


def acquire_slot(root: Path, config: Config, task: Task) -> tuple[int, Path]:
    from engine.agents import dispatch  # state_dir 等留在原模块，按需导入（T707）

    locks = dispatch.state_dir(root) / "slots"
    locks.mkdir(parents=True, exist_ok=True)
    for index in range(1, config.slots + 1):
        lock = locks / f"{index}.json"
        if lock.exists():
            held = json.loads(lock.read_text() or "{}")
            if dispatch._alive(held.get("pid", 0)):
                continue
            lock.unlink()  # 持有者已退出：回收
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "task": task.id, "branch": task.branch,
                       "started_at": dispatch._now()}, handle)
        return index, dispatch.slot_path(root, config, index)
    observation.slot(task.branch, None, problem=f"{config.slots} 个槽位都在使用中")
    raise dispatch.Stop(f"{config.slots} 个槽位都在使用中：稍后再派发，或用 bin/dispatch status 查看")


def update_slot(root: Path, index: int, **fields) -> None:
    from engine.agents import dispatch  # state_dir 留在原模块，按需导入（T707）

    lock = dispatch.state_dir(root) / "slots" / f"{index}.json"
    data = json.loads(lock.read_text() or "{}")
    data.update(fields)
    lock.write_text(json.dumps(data))


def release_slot(root: Path, index: int) -> None:
    from engine.agents import dispatch  # state_dir 留在原模块，按需导入（T707）

    (dispatch.state_dir(root) / "slots" / f"{index}.json").unlink(missing_ok=True)


def _registered_worktrees(root: Path) -> set[str]:
    """登记在案的工作树绝对路径（porcelain 输出是规范化路径，按 resolve 后比对，免符号路径误差）。"""
    out = git("worktree", "list", "--porcelain", cwd=root)
    return {str(Path(line[len("worktree "):]).resolve())
            for line in out.splitlines() if line.startswith("worktree ")}


def _salvage_and_remove(root: Path, slot: Path, push) -> None:
    """归还一个槽位工作树（B77）：分支有未推提交先推送保全，再 worktree remove。

    不是登记在案的工作树（普通残留目录）不动；推送/取远端状态失败原样抛出，由调用方决定
    停止派发（回收路径）或提示后继续（结束路径）。
    """
    from engine.agents import dispatch  # _registered_worktrees 留在原模块，按需导入（B89）

    if not slot.exists() or str(slot.resolve()) not in dispatch._registered_worktrees(root):
        return
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=slot, check=False)
    if branch and branch != "HEAD":
        # fetch 失败（远端无该分支——认领推送前崩溃/推送静默失败）不拦回收：直接尝试推送保全
        try:
            git("fetch", "--quiet", "origin", branch, cwd=slot)
        except RuntimeError:
            pass
        pushed = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}",
                     cwd=slot, check=False)
        if not pushed or git("rev-list", f"origin/{branch}..{branch}", cwd=slot).split():
            push(slot, branch)
    git("worktree", "remove", "--force", str(slot), cwd=root)


def reclaim_stale_slots(root: Path, config: Config, push) -> list[str]:
    """崩溃路径无法归还时的槽位回收（B77）：锁文件 pid 不存活的槽在下次派发前强制回收。

    槽内分支有未推提交先推送保全再删工作树；推送失败停止派发（保数据优先，不静默丢提交），
    现场保留待人工处理。返回被回收槽位曾认领的分支名（链路自愈提示用）。
    """
    from engine.agents import dispatch  # state_dir 等留在原模块，按需导入（T707）

    locks = dispatch.state_dir(root) / "slots"
    if not locks.is_dir():
        return []
    reclaimed: list[str] = []
    for lock in sorted(locks.glob("*.json"), key=lambda path: int(path.stem) if path.stem.isdigit() else 0):
        if not lock.stem.isdigit():
            continue
        try:
            data = json.loads(lock.read_text() or "{}")
        except ValueError:
            data = {}
        if dispatch._alive(data.get("pid", 0)):
            continue
        try:
            dispatch._salvage_and_remove(root, dispatch.slot_path(root, config, int(lock.stem)), push)
        except (RuntimeError, subprocess.CalledProcessError) as error:
            raise dispatch.Stop(f"槽位 {lock.stem} 回收失败（分支提交保全未完成，工作树保留待人工处理）：{error}") from error
        reclaimed.append(str(data.get("branch") or ""))
        lock.unlink(missing_ok=True)
    return reclaimed


def return_slot(root: Path, config: Config, index: int, push) -> None:
    """派发结束路径统一归还槽位（B77）：worktree remove + 锁清理。

    run 结束时锁已先释放；归还前先重新独占槽位锁——拿不到说明已有下一次派发认领该槽，
    不删它正准备使用的工作树。删除失败只提示，残留交给下次派发的回收路径。
    """
    from engine.agents import dispatch  # state_dir 等留在原模块，按需导入（T707）

    lock = dispatch.state_dir(root) / "slots" / f"{index}.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as handle:
        json.dump({"pid": os.getpid(), "task": "return", "started_at": dispatch._now()}, handle)
    try:
        dispatch._salvage_and_remove(root, dispatch.slot_path(root, config, index), push)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"槽位 {index} 归还未完成（工作树保留，下次派发会回收）：{error}", file=sys.stderr)
    finally:
        release_slot(root, index)


def prepare_slot(root: Path, slot: Path, branch: str, resume: bool) -> None:
    from engine.agents import dispatch  # preserved_paths 留在原模块，按需导入（T707）

    if not slot.exists():
        git("worktree", "add", "--detach", str(slot), "origin/main", cwd=root)
    git("fetch", "--quiet", "origin", cwd=slot)
    start = f"origin/{branch}" if resume else "origin/main"
    git("checkout", "--quiet", "--force", "-B", branch, start, cwd=slot)
    keep = dispatch.preserved_paths()
    git("clean", "-ffdxq", *[arg for path in keep for arg in ("-e", path)], cwd=slot)
    for path in keep:
        source = root / path
        if source.exists() and not (slot / path).exists():
            (slot / path).parent.mkdir(parents=True, exist_ok=True)
            (slot / path).symlink_to(source)
