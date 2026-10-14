"""试点任务 TASK-20261015-01（B93）复现测试：派发槽位锁的竞争。

现状（engine/agents/dispatch_slots.py）：槽位锁是 <state>/slots/<n>.json，acquire_slot 读内容里的 pid，
持有者已退出就 unlink 后 O_EXCL 重建。两个派发同时回收同一个过期槽位时，后到者会删掉先到者刚建的
新锁，两个派发占同一槽位；锁内容是坏 JSON（写到一半）时直接抛 ValueError；持有者在写入前退出留下
的空锁按 pid 0 处理，而 _alive(0) 为真（os.kill(0, 0) 发给本进程组），该槽位永远回收不了。

并发编排只等待条件成立（事件），在 dispatch._now / dispatch._alive 这两个按模块属性调用的接缝上
暂停某个线程，不依赖固定时长。夹具为匿名临时 git 仓库，不碰真实库、PR 或槽位。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch


def make_root(tmp: Path) -> Path:
    root = tmp / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    return root


def make_task(name: str) -> dispatch.Task:
    return dispatch.Task(f"docs/plans/task-{name}.md", name, "bugfix", "R1", {}, [], f"task/{name}")


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class SlotLockRaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dh-b93-")
        self.tmp = Path(self._tmp.name)
        self.root = make_root(self.tmp)
        self.config = dispatch.Config(slots=2, slot_root=self.tmp)
        self.locks = dispatch.state_dir(self.root) / "slots"
        self.locks.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for index in (1, 2):
            dispatch.release_slot(self.root, index)
        self._tmp.cleanup()

    def held(self, index: int) -> dict:
        return json.loads((self.locks / f"{index}.json").read_text())

    def pause_first_call(self, name: str, in_thread: list):
        """把 dispatch.<name> 换成：在 in_thread[0] 线程里第一次调用时置 paused、等 resume 后再调真实函数。"""
        real = getattr(dispatch, name)
        paused, resume = threading.Event(), threading.Event()

        def wrapper(*args, **kwargs):
            if threading.get_ident() == in_thread[0] and not paused.is_set():
                paused.set()
                resume.wait(30)
            return real(*args, **kwargs)

        return mock.patch.object(dispatch, name, wrapper), paused, resume

    def run_in_thread(self, task: dispatch.Task, ident: list, results: dict) -> threading.Thread:
        def run():
            ident[0] = threading.get_ident()
            try:
                results[task.id] = dispatch.acquire_slot(self.root, self.config, task)[0]
            except Exception as error:  # noqa: BLE001 — 线程内异常记为结果，由主线程断言
                results[task.id] = error

        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def test_concurrent_reclaim_of_stale_slot_gets_distinct_slots(self):
        """槽位 1 的锁已过期（持有进程已退出）：派发 C 判活途中，派发 B 完成回收并占用；C 继续时
        不得删掉 B 的新锁再占同一槽位——两者分到不同槽位，各自的锁内容是自己的任务，不抛异常。"""
        (self.locks / "1.json").write_text(json.dumps({"pid": dead_pid(), "task": "OLD", "branch": "task/old"}))
        ident = [None]
        patcher, paused, resume = self.pause_first_call("_alive", ident)
        results: dict = {}
        with patcher:
            thread = self.run_in_thread(make_task("C"), ident, results)
            try:
                while not (paused.wait(0.01) or not thread.is_alive()):
                    pass
                results["B"] = dispatch.acquire_slot(self.root, self.config, make_task("B"))[0]
            finally:
                resume.set()
                thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results.get("C"), int, results)
        self.assertEqual(sorted([results["B"], results["C"]]), [1, 2])
        self.assertEqual(self.held(results["B"])["task"], "B")
        self.assertEqual(self.held(results["C"])["task"], "C")

    def test_unheld_empty_or_corrupt_lock_is_reclaimed(self):
        """持有者在写完之前退出留下的空锁或坏 JSON 锁、且无人持有：acquire_slot 照常回收槽位 1，
        写入自己的持有信息，不抛异常，也不跳到下一个槽位。"""
        for content in ("", '{"pid": 12'):
            with self.subTest(content=content):
                (self.locks / "1.json").write_text(content)
                index, _ = dispatch.acquire_slot(self.root, self.config, make_task("N"))
                self.assertEqual(index, 1)
                self.assertEqual(self.held(1)["task"], "N")
                self.assertEqual(self.held(1)["pid"], os.getpid())
                dispatch.release_slot(self.root, 1)
                self.assertFalse((self.locks / "1.json").exists())

    def test_lock_being_created_is_not_taken_or_reclaimed(self):
        """护栏：派发 A 已建锁、尚未写入持有信息（暂停在取时间戳处）时，reclaim_stale_slots 不回收它，
        另一个派发分到别的槽位；A 继续后两把锁各是自己的任务。"""
        ident = [None]
        patcher, paused, resume = self.pause_first_call("_now", ident)
        results: dict = {}

        def no_push(worktree, branch):
            raise AssertionError(f"不应回收并推送：{branch}")

        with patcher:
            thread = self.run_in_thread(make_task("A"), ident, results)
            try:
                while not (paused.wait(0.01) or not thread.is_alive()):
                    pass
                self.assertEqual(dispatch.reclaim_stale_slots(self.root, self.config, no_push), [])
                results["B"] = dispatch.acquire_slot(self.root, self.config, make_task("B"))[0]
            finally:
                resume.set()
                thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results.get("A"), int, results)
        self.assertEqual(sorted([results["A"], results["B"]]), [1, 2])
        self.assertEqual(self.held(results["A"])["task"], "A")
        self.assertEqual(self.held(results["B"])["task"], "B")


if __name__ == "__main__":
    unittest.main()
