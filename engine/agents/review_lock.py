"""评审工作区互斥（T708，修复逃逸 #128）：所有评审入口共用同一批评审工作区（<仓库名>-review[-n]），
并发评审会互相 checkout、clean 并覆盖材料（#119 的补审与 #120 的评审同时运行时，#120 读到的全是
T707 的代码，见 #127）。本模块提供与槽位锁同形的文件锁：锁文件放在评审工作区旁边，
`<工作区目录>.review.lock`（`workspace.parent / f"{workspace.name}.review.lock"`），路径只由工作区
决定、不依赖 git（两套校准都以非 git 临时目录作 root 的测试场景也要能加锁，任务书修订记录 2）；
`os.open(O_CREAT|O_EXCL)` 创建，内容为 {"pid", "started_at", "purpose"}。持有进程已退出（或锁文件里
没有合法 pid）时回收；仍存活则每 2 秒轮询一次，等到超时抛 TimeoutError（信息含持有者 pid 与用途）。
`_alive` 与 `_now` 经 `dispatch.<名字>` 调用，保留在原模块上 patch 的语义（与 dispatch_slots 的按需导入相同）。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def workspace_lock(root: Path, workspace: Path, timeout_seconds: float, purpose: str = "review",
                   *, poll_seconds: float = 2.0) -> Iterator[Path]:
    """串行化对同一评审工作区的并发使用（E128-R1）；退出时（正常或异常）删除锁文件。"""
    from engine.agents import dispatch  # _alive、_now 留在原模块，按需导入（T708）

    lock = workspace.parent / f"{workspace.name}.review.lock"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(lock.read_text(encoding="utf-8") or "{}")
            except (OSError, ValueError):
                held = {}
            pid = held.get("pid")
            if isinstance(pid, int) and pid > 0 and dispatch._alive(pid):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"评审工作区 {workspace.name} 正被 pid {pid}（{held.get('purpose') or '用途未写'}）持有，"
                        f"等待 {timeout_seconds:.0f} 秒未释放") from None
                time.sleep(poll_seconds)
                continue
            lock.unlink(missing_ok=True)  # 持有者已退出（或锁内容没有合法 pid）：回收后重试
            continue
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "started_at": dispatch._now(), "purpose": purpose}, handle)
        break
    try:
        yield lock
    finally:
        lock.unlink(missing_ok=True)
