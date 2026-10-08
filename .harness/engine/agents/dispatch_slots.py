"""派发的槽位管理（T707 自 dispatch.py 逐字移出，行为不变）。

调用原模块的名字（state_dir、_alive、slot_path、_now、Stop、preserved_paths，以及原本同在
一模块的 _registered_worktrees、_salvage_and_remove）时按需在函数体内导入 dispatch 并以
属性访问：保留测试在原模块上的 patch 语义，也避免循环导入。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
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


def backup_uncommitted(slot: Path, branch: str) -> str | None:
    """槽位有未提交改动时先备份成引用再返回引用名，干净时返回 None（B122）。

    备份树反映工作区完整状态：已暂存与未暂存改动、未跟踪文件、被删除的已跟踪文件；遵守
    .gitignore（被忽略的未跟踪文件不进备份），已跟踪但匹配忽略规则的文件照常保留。实现只用
    临时索引（GIT_INDEX_FILE 指向临时文件，先 read-tree HEAD 再 add -A）+ commit-tree +
    update-ref，不碰真实索引与分支；引用在主仓库（worktree 共享）。状态检测必须用
    `git --no-optional-locks status`：普通 status 会刷新并写回真实索引的 stat 缓存。备份失败抛
    RuntimeError，调用方（return_slot / 回收路径）按「归还未完成（工作树保留）」处理，不删工作树。
    """
    status = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain"],
                            cwd=slot, capture_output=True, text=True, check=False)
    if status.returncode != 0:
        raise RuntimeError(f"读取槽位状态失败：{status.stderr.strip()}")
    if not status.stdout.strip():
        return None

    def run(args: list[str], env: dict[str, str]) -> str:
        try:
            done = subprocess.run(args, cwd=slot, env=env, capture_output=True, text=True, check=False)
        except subprocess.CalledProcessError as error:  # 注入或环境异常时也归一为 RuntimeError（B122）
            raise RuntimeError(f"备份失败（git {' '.join(args)}）：{error}") from error
        if done.returncode != 0:
            raise RuntimeError(f"备份失败（git {' '.join(args)}）：{done.stderr.strip()}")
        return done.stdout.strip()

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    ref = f"refs/backup/dispatch/{(branch or 'detached').replace('/', '-')}/{stamp}"
    with tempfile.TemporaryDirectory(prefix="dispatch-backup-") as folder:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(folder) / "index")}
        run(["git", "read-tree", "HEAD"], env)
        run(["git", "add", "-A"], env)
        tree = run(["git", "write-tree"], env)
        commit = run(["git", "-c", "user.name=harness-backup", "-c", "user.email=harness-backup@localhost",
                      "commit-tree", tree, "-p", "HEAD",
                      "-m", f"dispatch backup：{branch} 槽位未提交改动（{stamp}）"], env)
        run(["git", "update-ref", ref, commit], env)
    print(f"已把未提交改动备份到 {ref}", file=sys.stderr)
    print(f"查看差异：git diff --name-status HEAD {ref}（D 项是执行方删除的文件）", file=sys.stderr)
    print(f"整体还原：git restore --source={ref} --worktree --staged :/", file=sys.stderr)
    return ref


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
    backup_uncommitted(slot, branch)  # B122：删除前先把未提交改动备份成引用；失败则不删（RuntimeError 上抛）
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


def _merge_main_into_branch(slot: Path, env: dict[str, str] | None) -> None:
    """续做前把 origin/main 合进任务分支（B122）：只落后时快进，分叉时产生合并提交，已含时不动。

    祖先判定用 subprocess.run 直接调 git：common.git() 不接受 env，且 check=False 会把
    merge-base --is-ancestor 的 0、1、128 返回码都压成空字符串。冲突时 merge --abort 并抛 Stop。
    合并提交身份取调用方传入的 env（Dispatcher.run 传 {**os.environ, **self.identity}）。
    """
    from engine.agents import dispatch  # Stop 留在原模块，按需导入（T707）

    probe = subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
                           cwd=slot, capture_output=True, text=True, check=False)
    if probe.returncode == 0:
        return
    if probe.returncode != 1:
        raise RuntimeError(f"无法判定 origin/main 与 HEAD 的祖先关系：{probe.stderr.strip()}")
    merged = subprocess.run(["git", "merge", "--no-edit", "origin/main"], cwd=slot,
                            env={**os.environ, **(env or {})}, capture_output=True, text=True, check=False)
    if merged.returncode == 0:
        return
    subprocess.run(["git", "merge", "--abort"], cwd=slot, capture_output=True, text=True, check=False)
    raise dispatch.Stop("续做前合并 origin/main 冲突，请设计方先解决冲突："
                        + (merged.stdout + merged.stderr).strip()[-600:])


def prepare_slot(root: Path, slot: Path, branch: str, resume: bool,
                 env: dict[str, str] | None = None) -> None:
    from engine.agents import dispatch  # preserved_paths 留在原模块，按需导入（T707）

    if not slot.exists():
        git("worktree", "add", "--detach", str(slot), "origin/main", cwd=root)
    git("fetch", "--quiet", "origin", cwd=slot)
    start = f"origin/{branch}" if resume else "origin/main"
    git("checkout", "--quiet", "--force", "-B", branch, start, cwd=slot)
    if resume:  # B122：任务书修订合进 main 后，续做先把默认分支合进来，避免读到过期任务书
        _merge_main_into_branch(slot, env)
    keep = dispatch.preserved_paths()
    git("clean", "-ffdxq", *[arg for path in keep for arg in ("-e", path)], cwd=slot)
    for path in keep:
        source = root / path
        if source.exists() and not (slot / path).exists():
            (slot / path).parent.mkdir(parents=True, exist_ok=True)
            (slot / path).symlink_to(source)
