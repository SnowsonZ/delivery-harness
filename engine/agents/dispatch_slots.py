"""派发的槽位管理（T707 自 dispatch.py 逐字移出；B93 起槽位锁改内核文件锁）。

槽位锁仍是 <state>/slots/<n>.json，互斥由 fcntl.flock 负责（B93，做法与 review_lock 相同）：
打开锁文件（不存在则创建）后先取非阻塞独占 flock，拿到后核对 fstat(fd) 与 stat(路径) 的
inode，不一致或路径已消失就关掉重来；派发进程持有该描述符直到 release_slot（先删锁文件、
再关描述符），持有进程退出时内核自动放锁。锁文件内容（pid、task、branch、started_at，以及
update_slot 追加的字段）用于展示及旧式存活 pid 锁的兼容保护：内容里的 pid 仍存活、但没有人
持 flock 的锁（升级前启动的派发，或只写文件的持有者）仍视为占用，acquire_slot、return_slot、
reclaim_stale_slots 都不动它；pid 已退出的照常回收。内容为空或不是合法 JSON（写到一半、持有者
在写完之前退出）都算「未知持有者」，是否有人持有以 flock 为准，无人持 flock 即可回收——不再
按 _alive(0) 判活（os.kill(0, 0) 发给本进程组，空文件会被误判为占用）。

调用原模块的名字（state_dir、_alive、slot_path、_now、Stop、preserved_paths，以及原本同在
一模块的 _registered_worktrees、_salvage_and_remove）时按需在函数体内导入 dispatch 并以
属性访问：保留测试在原模块上的 patch 语义，也避免循环导入。
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from engine.agents import dispatch_observation as observation
from engine.core.common import git

if TYPE_CHECKING:
    from engine.agents.dispatch import Config, Task

# 本进程持有 flock 的锁文件描述符（锁路径 → fd），acquire_slot/return_slot 登记，release_slot 撤销。
_registered_fds: dict[str, int] = {}


def _holder_info(lock: Path) -> dict:
    """读锁内容里的持有信息：空内容、坏 JSON 或不是对象都算未知（是否有人持有以 flock 为准）。

    PermissionError、EIO 等非预期读取失败原样上抛，不得吞成空字典（B93⑪）；ValueError（含
    UnicodeDecodeError）是写到一半之类的内容问题，归为未知。
    """
    try:
        held = json.loads(lock.read_text(encoding="utf-8") or "{}")
    except ValueError:
        return {}
    return held if isinstance(held, dict) else {}


def _lock_holder(lock: Path, *, create: bool) -> tuple[int, dict] | None:
    """独占打开锁文件并读出持有信息，返回（描述符，内容）；他人已持有返回 None。

    flock 负责互斥（B93①）：非阻塞取独占锁，拿到后核对 fstat 与 stat 的 inode，不一致或路径
    已消失（上一持有者先删后关）就关掉重来；他人已持有（BlockingIOError）不等待。create=False
    的调用方（回收路径）在路径消失时直接返回 None，不以 O_CREAT 重建文件。flock、inode 核对或
    读取抛出非预期异常时，先关闭描述符再让异常向上传播，不残留内核锁（B93⑧）。
    """
    while True:
        try:
            fd = os.open(lock, os.O_RDWR | (os.O_CREAT if create else 0), 0o644)
        except FileNotFoundError:
            return None  # 持有者已在列举与打开之间正常删除锁文件：不重建
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        try:
            stale = os.stat(lock).st_ino != os.fstat(fd).st_ino
        except FileNotFoundError:
            stale = True
        except BaseException:
            os.close(fd)
            raise
        if stale:
            os.close(fd)
            continue
        try:
            return fd, _holder_info(lock)
        except BaseException:
            os.close(fd)
            raise


def _legacy_holder(data: dict) -> bool:
    """旧式存活 pid 锁：内容写着仍存活的正 pid、但没有人持 flock（升级前持有者）——视为占用。"""
    from engine.agents import dispatch  # _alive 留在原模块，按需导入（T707 patch 语义）

    pid = data.get("pid")
    return isinstance(pid, int) and pid > 0 and dispatch._alive(pid)


def _write_holder(fd: int, holder: dict) -> None:
    """截断后在持锁描述符上写入持有信息，保证内容在锁内完整落盘（与 review_lock 相同）。"""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    payload = memoryview(json.dumps(holder, ensure_ascii=False).encode("utf-8"))
    while payload:
        payload = payload[os.write(fd, payload):]


def acquire_slot(root: Path, config: Config, task: Task) -> tuple[int, Path]:
    from engine.agents import dispatch  # state_dir 等留在原模块，按需导入（T707）

    locks = dispatch.state_dir(root) / "slots"
    locks.mkdir(parents=True, exist_ok=True)
    for index in range(1, config.slots + 1):
        lock = locks / f"{index}.json"
        held = _lock_holder(lock, create=True)
        if held is None:
            continue  # 他人已持有 flock：包括正在创建、尚未写入持有信息的锁（B93④）
        fd, data = held
        if _legacy_holder(data):
            os.close(fd)  # pid 仍存活、无人持 flock 的旧式锁：视为占用，不动（B93⑤）
            continue
        try:
            _write_holder(fd, {"pid": os.getpid(), "task": task.id, "branch": task.branch,
                               "started_at": dispatch._now()})
        except BaseException:
            with suppress(OSError):  # 登记前失败：删掉本进程刚占的锁文件，不残留内核锁（B93⑧）
                lock.unlink(missing_ok=True)
            os.close(fd)
            raise
        _registered_fds[str(lock)] = fd
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

    lock = dispatch.state_dir(root) / "slots" / f"{index}.json"
    fd = _registered_fds.pop(str(lock), None)  # 先撤销登记：unlink 失败也不再持有（B93⑧）
    try:
        lock.unlink(missing_ok=True)
    finally:
        if fd is not None:
            os.close(fd)  # 即使删除失败也关闭描述符：内核锁随之释放，原异常继续向上传播


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
    """崩溃路径无法归还时的槽位回收（B77）：无人持 flock、内容 pid 也不存活的槽在下次派发前强制回收。

    是否有人持有以 flock 为准（B93④）：正在创建、尚未写入持有信息的锁被持有者占着，不回收；
    列举后持有者已正常删除的锁文件直接跳过，不重建、不回收（B93⑨）。内容里的 pid 仍存活的
    旧式锁不动（B93⑤）。槽内分支有未推提交先推送保全再删工作树；保全或删除失败遵循现有异常
    语义（RuntimeError/CalledProcessError 转 Stop）与现场保留规则（锁文件保留），所有退出路径
    释放临时描述符。返回被回收槽位曾认领的分支名（链路自愈提示用）。
    """
    from engine.agents import dispatch  # state_dir 等留在原模块，按需导入（T707）

    locks = dispatch.state_dir(root) / "slots"
    if not locks.is_dir():
        return []
    reclaimed: list[str] = []
    for lock in sorted(locks.glob("*.json"), key=lambda path: int(path.stem) if path.stem.isdigit() else 0):
        if not lock.stem.isdigit():
            continue
        held = _lock_holder(lock, create=False)
        if held is None:
            continue  # 他人已持有 flock，或持有者已正常删除锁文件：跳过（B93⑨）
        fd, data = held
        try:
            if _legacy_holder(data):
                continue  # pid 仍存活的旧式锁：内容与文件都原样保留（B93⑤），finally 关描述符
            try:
                dispatch._salvage_and_remove(root, dispatch.slot_path(root, config, int(lock.stem)), push)
            except (RuntimeError, subprocess.CalledProcessError) as error:
                raise dispatch.Stop(
                    f"槽位 {lock.stem} 回收失败（分支提交保全未完成，工作树保留待人工处理）：{error}") from error
            reclaimed.append(str(data.get("branch") or ""))
            lock.unlink(missing_ok=True)
        finally:
            os.close(fd)  # 所有退出路径（含 Stop 与意外异常）都释放本函数的临时描述符（B93⑧）
    return reclaimed


def return_slot(root: Path, config: Config, index: int, push) -> None:
    """派发结束路径统一归还槽位（B77）：worktree remove + 锁清理。

    run 结束时锁已先释放；归还前用同一锁机制重新独占槽位——拿不到 flock 说明已有下一次派发
    认领该槽（含正在创建、尚未写入持有信息的锁），不动它正准备使用的工作树；内容里的 pid 仍
    存活的旧式锁同样不动（B93⑤）。删除失败只提示，残留交给下次派发的回收路径。
    """
    from engine.agents import dispatch  # state_dir 留在原模块，按需导入（T707）

    lock = dispatch.state_dir(root) / "slots" / f"{index}.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = _lock_holder(lock, create=True)
    if held is None:
        return
    fd, data = held
    if _legacy_holder(data):
        os.close(fd)  # 旧式存活锁：内容与文件原样保留，不回收工作树（B93⑤）
        return
    _registered_fds[str(lock)] = fd
    try:
        _write_holder(fd, {"pid": os.getpid(), "task": "return", "started_at": dispatch._now()})
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
