"""评审工作区互斥（T708，修复逃逸 #128）：所有评审入口共用同一批评审工作区（<仓库名>-review[-n]），
并发评审会互相 checkout、clean 并覆盖材料（#119 的补审与 #120 的评审同时运行时，#120 读到的全是
T707 的代码，见 #127）。本模块提供 workspace_lock：锁文件放在评审工作区旁边，
`<工作区目录>.review.lock`（`workspace.parent / f"{workspace.name}.review.lock"`），路径只由工作区
决定、不依赖 git（两套校准都以非 git 临时目录作 root 的测试场景也要能加锁，任务书修订记录 2）。

互斥用内核文件锁 fcntl.flock（任务书修订记录 3，#134 评审发现①：O_EXCL 创建＋按 pid 判活在
「创建与写入之间文件为空」与「多个等待方同时回收」两个窗口上都会放两方进临界区）。锁文件以
O_CREAT|O_RDWR 打开，flock(LOCK_EX|LOCK_NB) 轮询到超时；等待期间描述符保持打开，持锁方释放
（先删文件再关描述符）后，本描述符会在已删除的旧 inode 上 flock 成功，所以拿到锁后必须核对
fstat(fd) 与 stat(路径) 的 inode，不一致或路径已消失就关掉重来；核对通过才截断并写入
{"pid", "started_at", "purpose"}——内容只用于超时提示，不参与判活。释放时仍持锁先删文件、再关
描述符，等待方靠 inode 核对识别重来（#134 评审发现②：后到的回收方不得删掉先到者刚建的新锁）。
持有进程退出时内核自动放锁，无需按 pid 判活回收；`_now` 经 `dispatch.<名字>` 调用，保留在原模块
上 patch 的语义（与 dispatch_slots 的按需导入相同）。
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _holder(lock: Path) -> dict:
    """读锁文件里的持有信息（只用于超时提示）：读不到或不是 JSON 对象就当未知。"""
    try:
        held = json.loads(lock.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


@contextmanager
def workspace_lock(root: Path, workspace: Path, timeout_seconds: float, purpose: str = "review",
                   *, poll_seconds: float = 2.0) -> Iterator[Path]:
    """串行化对同一评审工作区的并发使用（E128-R1）；退出时（正常或异常）先删锁文件再关描述符。"""
    from engine.agents import dispatch  # _now 留在原模块，按需导入（T708 patch 语义）

    lock = workspace.parent / f"{workspace.name}.review.lock"
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    try:
        while True:
            if fd is None:  # 陈旧 inode 的描述符已关：按同一路径重新打开（不存在则创建）
                fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    held = _holder(lock)
                    pid = held.get("pid")
                    who = (f"pid {pid}（{held.get('purpose') or '用途未写'}）"
                           if isinstance(pid, int) and pid > 0 else "未知持有者（尚未写入持有信息）")
                    raise TimeoutError(
                        f"评审工作区 {workspace.name} 正被{who}持有，"
                        f"等待 {timeout_seconds:.0f} 秒未释放") from None
                time.sleep(poll_seconds)  # 描述符保持打开（见模块说明）：旧 inode 上的成功靠下方核对重来
                continue
            try:
                stale = os.stat(lock).st_ino != os.fstat(fd).st_ino
            except FileNotFoundError:
                stale = True
            if stale:  # 拿到的是前一持有者刚删除（或已被换新）的旧 inode：关掉重来（修订记录 3）
                os.close(fd)
                fd = None
                continue
            os.ftruncate(fd, 0)  # 核对通过：截断并写入持有信息（内容只用于超时提示，不参与判活）
            os.lseek(fd, 0, os.SEEK_SET)
            payload = memoryview(json.dumps(
                {"pid": os.getpid(), "started_at": dispatch._now(), "purpose": purpose},
                ensure_ascii=False).encode("utf-8"))
            while payload:
                payload = payload[os.write(fd, payload):]
            break
        try:
            yield lock
        finally:
            # 释放：先删文件（仍持锁时删除，等待方靠 inode 核对重来），再关描述符（内核随之放锁）
            lock.unlink(missing_ok=True)
    finally:
        if fd is not None:
            os.close(fd)
