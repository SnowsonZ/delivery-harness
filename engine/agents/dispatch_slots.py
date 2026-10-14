"""派发的槽位管理（T707 自 dispatch.py 逐字移出；B93 起槽位互斥改用内核文件锁）。

调用原模块的名字（state_dir、_alive、slot_path、_now、Stop、preserved_paths，以及原本同在
一模块的 _registered_worktrees、_salvage_and_remove）时按需在函数体内导入 dispatch 并以
属性访问：保留测试在原模块上的 patch 语义，也避免循环导入。

槽位锁（B93）：`<state>/slots/<n>.json` 的互斥以 fcntl.flock 为准，做法与 review_lock 相同——
O_CREAT|O_RDWR 打开、LOCK_EX|LOCK_NB，拿到后核对 fstat(fd) 与 stat(路径) 的 inode，不一致或
路径已消失就关掉重来（上一持有者按「先删文件再关描述符」释放时，先开好文件的等待方拿到的正是那个
已删除的旧 inode）。描述符由取得者登记并持有到 release_slot（先删锁文件、再关描述符），持有进程
退出时内核自动放锁；锁内容（pid、task、branch、started_at 及 update_slot 追加的字段）只作展示，
不参与判活——两个派发同时回收同一过期槽位时后到者拿不到 flock、各占各的槽位，不再删对方的新锁。
兼容升级前的旧式锁：无人持 flock 但内容 pid 仍存活的锁视为占用，acquire_slot、return_slot 与
reclaim_stale_slots 都不动它；pid 已退出、或内容为空或坏 JSON（持有者在写完之前退出）的锁照常回收。
"""

from __future__ import annotations

import datetime as dt
import fcntl
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

_held: dict[str, int] = {}  # 本进程持有的槽位锁：锁文件路径 → 描述符（flock 保持到 release_slot）


def _claim(lock: Path, *, create: bool) -> int | None:
    """打开锁文件并取内核文件锁，核对 inode 后返回描述符；锁已被别人持有返回 None。

    拿到的可能是上一持有者刚删除（先删文件再关描述符）的旧 inode：stat 与 fstat 不一致或路径已
    消失就关掉重来，不得动别人按同一路径新建的锁。create=False 时不以 O_CREAT 重建（回收路径：
    持有者已正常释放的锁直接跳过）。flock、打开的其他异常向上传播，已打开的描述符不残留。
    """
    while True:
        try:
            fd = os.open(lock, os.O_RDWR | (os.O_CREAT if create else 0), 0o644)
        except FileNotFoundError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
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
        return fd


def _content(lock: Path) -> dict:
    """读锁文件里的持有信息（只作展示与旧式判活兼容）：读不到或不是 JSON 对象就当空。"""
    try:
        held = json.loads(lock.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def _holder_alive(data: dict) -> bool:
    """旧式锁兼容：内容 pid 仍存活视为占用（升级前启动的派发，或只写文件的持有者）。

    新机制不依赖它——持有者退出时内核已放锁；pid 缺失、非正整数或已退出都算无人。
    """
    from engine.agents import dispatch  # _alive 留在原模块，按需导入（T707 patch 语义）

    pid = data.get("pid")
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and dispatch._alive(pid)


def _write_holder(fd: int, fields: dict) -> None:
    """截断并重写持有信息（调用方已持 flock）：写到一半进程退出留下坏内容的锁，由下次派发照常回收。"""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    payload = memoryview(json.dumps(fields, ensure_ascii=False).encode("utf-8"))
    while payload:
        payload = payload[os.write(fd, payload):]


def acquire_slot(root: Path, config: Config, task: Task) -> tuple[int, Path]:
    from engine.agents import dispatch  # state_dir 等留在原模块，按需导入（T707）

    locks = dispatch.state_dir(root) / "slots"
    locks.mkdir(parents=True, exist_ok=True)
    for index in range(1, config.slots + 1):
        lock = locks / f"{index}.json"
        fd = _claim(lock, create=True)
        if fd is None:  # 别的派发正持着这把锁（含正在回收、尚未写完的）：跳过
            continue
        try:
            if _holder_alive(_content(lock)):
                os.close(fd)  # 旧式存活锁（升级前的派发，或只写文件的持有者）：视为占用
                continue
            _write_holder(fd, {"pid": os.getpid(), "task": task.id, "branch": task.branch,
                               "started_at": dispatch._now()})
        except BaseException:
            os.close(fd)  # 持有信息尚未登记：不残留内核锁，槽位留给下次派发
            raise
        _held[str(lock)] = fd
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
    fd = _held.pop(str(lock), None)  # 先撤销登记：unlink 失败也不给已关闭的描述符留登记
    try:
        lock.unlink(missing_ok=True)  # 仍持锁时先删文件（等待方靠 inode 核对重来）；失败原异常上抛
    finally:
        if fd is not None:
            os.close(fd)  # 描述符总要关闭：内核随之放锁，因 unlink 失败而保留的文件不再被占用


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
    """崩溃路径无法归还时的槽位回收（B77）：无人持 flock 且旧 pid 不存活的槽在下次派发前强制回收。

    互斥同样以 flock 为准：列举后打开即被持有（flock 拿不到，含正在重建的锁）或已消失（持有者正常
    释放）的槽位直接跳过——不重建锁文件、不调用其工作树保全或删除；旧式存活 pid 的锁（升级前的
    派发）也不动。槽内分支有未推提交先推送保全再删工作树；推送失败停止派发（保数据优先，不静默
    丢提交），现场保留待人工处理。返回被回收槽位曾认领的分支名（链路自愈提示用）。
    """
    from engine.agents import dispatch  # state_dir 等留在原模块，按需导入（T707）

    locks = dispatch.state_dir(root) / "slots"
    if not locks.is_dir():
        return []
    reclaimed: list[str] = []
    for lock in sorted(locks.glob("*.json"), key=lambda path: int(path.stem) if path.stem.isdigit() else 0):
        if not lock.stem.isdigit():
            continue
        fd = _claim(lock, create=False)
        if fd is None:  # 已被持有或持有者已正常释放：不归本进程回收
            continue
        try:
            data = _content(lock)
            if _holder_alive(data):
                continue  # 旧式存活锁：不动
            try:
                dispatch._salvage_and_remove(root, dispatch.slot_path(root, config, int(lock.stem)), push)
            except (RuntimeError, subprocess.CalledProcessError) as error:
                raise dispatch.Stop(
                    f"槽位 {lock.stem} 回收失败（分支提交保全未完成，工作树保留待人工处理）：{error}") from error
            reclaimed.append(str(data.get("branch") or ""))
            lock.unlink(missing_ok=True)  # 仍持锁时删除；描述符由 finally 关闭
        finally:
            os.close(fd)
    return reclaimed


def return_slot(root: Path, config: Config, index: int, push) -> None:
    """派发结束路径统一归还槽位（B77）：worktree remove + 锁清理。

    run 结束时锁已先释放；归还前先按同一套 flock 机制重新独占槽位锁——拿不到（已有下一次派发
    认领）或旧式存活锁仍占用时，不动它正准备使用的工作树。删除失败只提示，残留交给下次派发的
    回收路径。
    """
    from engine.agents import dispatch  # state_dir 等留在原模块，按需导入（T707）

    lock = dispatch.state_dir(root) / "slots" / f"{index}.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = _claim(lock, create=True)
    if fd is None:
        return
    try:
        if _holder_alive(_content(lock)):
            os.close(fd)  # 旧式存活锁：下一次派发还占着，不动它的现场
            return
        _write_holder(fd, {"pid": os.getpid(), "task": "return", "started_at": dispatch._now()})
    except BaseException:
        os.close(fd)  # 登记前失败：不残留内核锁，异常原样上抛
        raise
    _held[str(lock)] = fd
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
