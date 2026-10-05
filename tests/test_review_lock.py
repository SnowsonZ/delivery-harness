"""评审互斥与评审方回归（T708，Defect: E128-R1——逃逸 #128）：同一评审工作区的并发评审串行化、
flock 持锁/超时与创建窗口、各评审入口持锁（calibrate 按样本持锁；jobs>1 时并行工作区各用各的锁
文件、互不阻塞，任务书修订记录 2），以及 Codex 评审关闭子代理（B81）。锁为 fcntl.flock 内核文件
锁＋inode 核对（任务书修订记录 3，#134 评审发现①②：空内容不得被当成可抢、回收不得删他人的新锁）。

夹具为匿名临时 git 仓库（bare 远端 + refs/pull/<n>/head）、假 gh 与假评审方，不碰真实库/PR；
并发编排只等待「条件成立」（标记文件、调用计数），不依赖固定 sleep 时序；临界区内的小睡只为
放大缺陷的可观测窗口，断言本身不依赖时长。

评审锁模块在函数内按需导入（模块顶层不导入 engine.agents.review_lock）：修复证据与回放检查会把
E128-R1 的修复整体退回（review_lock.py 随之消失），引用该编号的测试必须仍能加载并以断言失败
（而非 ImportError 出错）结束，才证明它检查的是互斥本身而不是模块的存在（规范 §2「修复提交」）。
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import plan_review, review, review_calibration
from engine.core import events_db

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
VERDICT_LINE = '{"verdict": "需用户验收", "summary": "互斥探针", "findings": []}'


def git(*args: str, cwd: Path) -> str:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "HARNESS_", "GITHUB_"))}
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            env={**env, **GIT_ENV}, check=True)
    return result.stdout.rstrip("\n")


def make_project(tmp: Path) -> Path:
    """匿名最小项目：bare origin + main 上一个提交，返回 work 目录（评审工作区会建在它旁边）。"""
    origin = tmp / "origin.git"
    git("init", "--bare", "-q", "-b", "main", str(origin), cwd=tmp)
    work = tmp / "work"
    git("init", "-b", "main", "-q", str(work), cwd=tmp)
    (work / "app.txt").write_text("v1\n", encoding="utf-8")
    git("add", "app.txt", cwd=work)
    git("commit", "-qm", "c1", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return work


def push_pr(work: Path, number: int, change: tuple[str, str]) -> tuple[str, dict]:
    """从 main 建一个 PR head（一个提交推到 refs/pull/<n>/head），返回 (head, pr view 应答)。"""
    name, text = change
    (work / name).write_text(text, encoding="utf-8")
    git("add", name, cwd=work)
    git("commit", "-qm", f"pr{number}", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work)
    git("push", "-q", "origin", f"HEAD:refs/pull/{number}/head", cwd=work)
    git("reset", "-q", "--hard", "main", cwd=work)
    return head, {"title": f"T900：PR{number} 夹具", "body": "描述正文", "headRefName": f"task/pr-{number}",
                  "headRefOid": head, "baseRefName": "main", "state": "OPEN", "mergeCommit": None}


def wait_until(predicate, timeout: float = 120.0, what: str = "条件") -> None:
    """等待条件成立（轮询条件，不假设任何固定时长足够）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"等待超时：{what}")


class ReviewGitHub:
    """review_pr 用 gh 桩：pr view / pr checks 按工厂应答；评论按发布顺序记入 order。"""

    def __init__(self, prs: dict[int, dict], order: list[tuple] | None = None):
        self.prs, self.order, self.comments, self.calls = prs, order if order is not None else [], [], []

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        joined = " ".join(argv)
        self.calls.append(joined)
        if joined.startswith("gh pr view"):
            return json.dumps(self.prs[int(argv[argv.index("view") + 1])])
        if joined.startswith("gh pr checks"):
            return json.dumps([{"name": "harness", "state": "SUCCESS", "link": "https://ci.example.invalid/1"}])
        if joined.startswith("gh pr edit") and "--remove-label" in joined:
            return ""
        raise AssertionError(f"未预期的 gh 调用：{joined}")

    def comment(self, pr, body, label=None):
        self.comments.append((pr, body))
        if self.order is not None:
            self.order.append(("comment", pr))
        return f"https://example.invalid/pull/{pr}#issuecomment-{len(self.comments)}"


def blocking_script() -> str:
    """阻塞评审方脚本：写 started 标记，等 release 标记出现后输出结论 JSON。"""
    return ("import pathlib, sys, time\n"
            "d = pathlib.Path(sys.argv[1])\n"
            "(d / 'started').write_text('', encoding='utf-8')\n"
            "while not (d / 'release').exists():\n"
            "    time.sleep(0.01)\n"
            f"print({VERDICT_LINE!r})\n")


class ProbeReviewer(review.Reviewer):
    """探针评审方：立即给出「需用户验收」；argv 时记录评审运行期间本工作区的锁文件是否存在。"""

    name = "probe"

    def __init__(self, seen: list | None = None):
        self.seen = seen

    def argv(self, prompt, workspace, output):
        if self.seen is not None:
            self.seen.append((workspace.name, (workspace.parent / f"{workspace.name}.review.lock").exists()))
        return [sys.executable, "-c", f"print({VERDICT_LINE!r})"]

    def read(self, stdout, output):
        return stdout, "probe-model", "explicit_request"


class BlockingReviewer(review.Reviewer):
    """阻塞评审方：blocking 时等 release 标记后才给出结论（只用于持锁的第一个评审）。"""

    name = "probe"

    def __init__(self, markdir: Path, blocking: bool):
        self.markdir, self.blocking = markdir, blocking

    def argv(self, prompt, workspace, output):
        if self.blocking:
            return [sys.executable, "-c", blocking_script(), str(self.markdir)]
        return [sys.executable, "-c", f"print({VERDICT_LINE!r})"]

    def read(self, stdout, output):
        return stdout, "probe-model", "explicit_request"


class BlockingFactory:
    """make_reviewer 桩：第一次调用（必为持锁的第一个评审）返回阻塞评审方，之后立即返回。"""

    def __init__(self, markdir: Path):
        self.markdir, self.first = markdir, True

    def __call__(self, name):
        blocking, self.first = self.first, False
        return BlockingReviewer(self.markdir, blocking)


def pair_script() -> str:
    """并行样本的评审方脚本：把「自己的锁文件是否存在」写进 started，等 ready 出现后输出结论。"""
    return ("import pathlib, sys, time\n"
            "mark, lock = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
            "mark.write_text('1' if lock.exists() else '0', encoding='utf-8')\n"
            "while not (mark.parent / 'ready').exists():\n"
            "    time.sleep(0.01)\n"
            f"print({VERDICT_LINE!r})\n")


class PairBlockingReviewer(review.Reviewer):
    """并行样本评审方：started 内容记录评审启动时自己的锁文件是否存在，等同目录 ready 后输出结论。"""

    name = "probe"

    def __init__(self, markdir: Path):
        self.markdir = markdir

    def argv(self, prompt, workspace, output):
        mark = self.markdir / workspace.name / "started"
        mark.parent.mkdir(parents=True, exist_ok=True)
        return [sys.executable, "-c", pair_script(), str(mark),
                str(workspace.parent / f"{workspace.name}.review.lock")]

    def read(self, stdout, output):
        return stdout, "probe-model", "explicit_request"


class ReviewLockTest(unittest.TestCase):
    def test_concurrent_reviews_serialized(self):
        """两个线程同时对同一工作区 review_pr：第二个在第一个发布评论前不 checkout、不写材料（E128-R1）。"""
        try:  # 锁模块随修复才存在；修复被退回（回放/修复证据会删掉整个 review_lock.py）时不接锁桩，
            from engine.agents import review_lock  # 让互斥断言自己失败，而不是 ImportError 出错
        except ImportError:
            review_lock = None
        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            work = make_project(Path(tmp))
            head1, pr1 = push_pr(work, 7, ("a.txt", "one\n"))
            head2, pr2 = push_pr(work, 8, ("b.txt", "two\n"))
            markdir = Path(tmp) / "marks"
            markdir.mkdir()
            order: list[tuple] = []
            attempts: list[str] = []
            real_checkout, real_materials = review.checkout, review.write_materials

            def rec_checkout(workspace, ref):
                order.append(("checkout", ref))
                return real_checkout(workspace, ref)

            def rec_materials(workspace, base, pr_text, task_text, ci_text=""):
                order.append(("materials", 7 if "PR7" in pr_text else 8))
                return real_materials(workspace, base, pr_text, task_text, ci_text)

            github = ReviewGitHub({7: pr1, 8: pr2}, order)
            results: dict[int, int] = {}

            def run(number: int):
                with contextlib.redirect_stdout(io.StringIO()):
                    results[number] = review.review_pr(number, "probe", root=work, github=github)

            with contextlib.ExitStack() as stack:
                for patcher in (mock.patch.object(events_db, "ROOT", work),
                                mock.patch.object(review, "make_reviewer", BlockingFactory(markdir)),
                                mock.patch.object(review, "checkout", rec_checkout),
                                mock.patch.object(review, "write_materials", rec_materials)):
                    stack.enter_context(patcher)
                if review_lock is not None:
                    real_lock = review_lock.workspace_lock

                    def rec_lock(root, workspace, timeout_seconds, *args, **kwargs):
                        attempts.append(workspace.name)
                        return real_lock(root, workspace, timeout_seconds, *args, **kwargs)

                    stack.enter_context(mock.patch.object(review_lock, "workspace_lock", rec_lock))
                first = threading.Thread(target=run, args=(7,))
                first.start()
                wait_until((markdir / "started").exists, what="第一个评审进入评审方")
                second = threading.Thread(target=run, args=(8,))
                second.start()
                try:
                    # 第二个评审要么到达工作区锁（被挡住，attempts 到 2），要么已越过互斥完成了
                    # 自己的 checkout 与材料（缺陷在，order 涨到 4 或已发评论）：两种结局都可确定观测
                    wait_until(lambda: len(attempts) >= 2 or len(order) >= 4 or github.comments,
                               what="第二个评审到达工作区锁或越过互斥")
                    # 第二个评审被锁挡住：此刻只有第一个评审的 checkout 与材料，也还没有任何评论
                    self.assertEqual([item[0] for item in order], ["checkout", "materials"])
                    self.assertEqual(order[0], ("checkout", head1))
                    self.assertEqual(order[1], ("materials", 7))
                    self.assertEqual(github.comments, [])
                    (markdir / "release").write_text("", encoding="utf-8")  # 放行第一个评审方
                finally:  # 断言失败也要放行阻塞的评审方并等两个线程结束，不留悬挂
                    (markdir / "release").write_text("", encoding="utf-8")
                    first.join(120)
                    second.join(120)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(results, {7: 0, 8: 0})
            # 两个评审都经过同一把锁（同一工作区名到达两次）；无锁时 attempts 为空，这里同样失败
            self.assertEqual(attempts, [f"{work.name}-review"] * 2)
            # 顺序：第一个 checkout → 材料 → 评论，之后第二个才开始 checkout → 材料 → 评论
            self.assertEqual([item[0] for item in order], ["checkout", "materials", "comment"] * 2)
            self.assertEqual(order[2], ("comment", 7))
            self.assertEqual(order[3], ("checkout", head2))
            self.assertEqual(order[5], ("comment", 8))
            # 两条评论各自绑定自己的 head，材料不串
            body7 = next(body for number, body in github.comments if number == 7)
            body8 = next(body for number, body in github.comments if number == 8)
            self.assertIn(head1[:12], body7)
            self.assertIn(head2[:12], body8)
            self.assertNotIn(head2[:12], body7)
            pr_md = work.parent / f"{work.name}-review" / "build" / "review" / "pr.md"
            self.assertIn("PR8", pr_md.read_text(encoding="utf-8"))  # 工作区最终是第二个评审的材料
            # 评审结束锁已删除
            self.assertFalse((work.parent / f"{work.name}-review.review.lock").exists())

    def test_stale_lock_reclaimed_and_timeout(self):
        """锁文件残留但无人持有（持有进程已退出，flock 已被内核放掉）时直接拿到锁并覆盖持有信息；
        另一个描述符持有 flock 时等到超时抛 TimeoutError（信息含持有者 pid 与用途），且不删除、
        不改写对方的锁文件；正常退出、异常退出都删除锁文件（任务书修订记录 3）。"""
        from engine.agents import review_lock  # 按需导入：模块顶层导入会让修复退回后的并发测试连坐出错

        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            workspace = Path(tmp) / "app-review"
            workspace.mkdir()
            lock = workspace.parent / f"{workspace.name}.review.lock"
            # 残留锁：内容写着早已退出的 pid，但 flock 已随进程退出被内核放掉——直接拿到并覆盖
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            lock.write_text(json.dumps({"pid": dead.pid, "started_at": "t0", "purpose": "旧评审"}),
                            encoding="utf-8")
            with review_lock.workspace_lock(Path(tmp), workspace, 5, "探针") as acquired:
                self.assertEqual(acquired, lock)
                held = json.loads(lock.read_text(encoding="utf-8"))
                self.assertEqual(held["pid"], os.getpid())
                self.assertEqual(held["purpose"], "探针")
                self.assertIn("started_at", held)
            self.assertFalse(lock.exists())  # 正常退出删除锁文件
            # 异常退出同样删除锁文件
            with self.assertRaises(RuntimeError), review_lock.workspace_lock(Path(tmp), workspace, 5, "探针"):
                raise RuntimeError("评审中断")
            self.assertFalse(lock.exists())
            # 另一个描述符持有 flock：等到超时抛 TimeoutError，信息含持有者 pid 与用途；
            # 等待方不删别人还持有的锁文件，也不改写其内容
            holder = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(holder, json.dumps(
                    {"pid": os.getpid(), "started_at": "t1", "purpose": "别的评审"}).encode("utf-8"))
                with self.assertRaises(TimeoutError) as caught, \
                        review_lock.workspace_lock(Path(tmp), workspace, 0.05, "探针", poll_seconds=0.01):
                    pass
                self.assertIn(str(os.getpid()), str(caught.exception))
                self.assertIn("别的评审", str(caught.exception))
                self.assertEqual(json.loads(lock.read_text(encoding="utf-8"))["purpose"], "别的评审")
            finally:
                os.close(holder)
                lock.unlink(missing_ok=True)

    def test_lock_held_before_info_written(self):
        """创建窗口（#134 评审发现①）：持有方已拿到 flock 但锁文件仍为空（尚未写入持有信息）时，
        等待方不得进入、不得删除锁文件（空内容不得被当成可抢的残留锁）。"""
        from engine.agents import review_lock

        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            workspace = Path(tmp) / "app-review"
            workspace.mkdir()
            lock = workspace.parent / f"{workspace.name}.review.lock"
            holder = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)  # 已创建文件并拿到 flock，尚未写内容
            try:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # 若空内容被当成可抢，等待方会进入而不抛超时
                with self.assertRaises(TimeoutError), \
                        review_lock.workspace_lock(Path(tmp), workspace, 0.05, "探针",
                                                    poll_seconds=0.01):
                    pass
                self.assertEqual(lock.read_text(encoding="utf-8"), "")  # 锁文件原样保留：未删、未改
            finally:
                os.close(holder)
                lock.unlink(missing_ok=True)

    def test_many_waiters_never_overlap(self):
        """多等待方（#134 评审发现②）：至少 4 个线程经同一把闸门出发、反复争用同一把锁，计数器断言
        临界区从不重叠；每次释放都是「先删文件再关描述符」，等待方在旧 inode 上 flock 成功后必须靠
        inode 核对重来（回收/释放不得删掉他人的新锁）；全部结束后锁文件不存在（修订记录 3）。"""
        from engine.agents import review_lock

        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            workspace = Path(tmp) / "app-review"
            workspace.mkdir()
            lock = workspace.parent / f"{workspace.name}.review.lock"
            worker_count, rounds = 5, 10
            guard = threading.Lock()
            gate = threading.Event()  # 所有线程同点出发，保证反复正面争用而不是排队鱼贯
            counter = {"inside": 0, "peak": 0, "done": 0}

            def churn() -> None:
                gate.wait()
                for _ in range(rounds):
                    with review_lock.workspace_lock(Path(tmp), workspace, 60, "争用",
                                                    poll_seconds=0.002):
                        with guard:  # 临界区计数：任何时刻至多 1 个线程在内，重叠即缺陷
                            counter["inside"] += 1
                            counter["done"] += 1
                            counter["peak"] = max(counter["peak"], counter["inside"])
                        time.sleep(0.001)  # 只放大缺陷可观测窗口，断言不依赖此时长
                        with guard:
                            counter["inside"] -= 1

            threads = [threading.Thread(target=churn, daemon=True) for _ in range(worker_count)]
            for thread in threads:
                thread.start()
                wait_until(thread.is_alive, what="争用线程启动")
            gate.set()
            for thread in threads:
                thread.join(120)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(counter["done"], worker_count * rounds)  # 没有人因争用掉队
            self.assertEqual(counter["peak"], 1)  # 临界区从不重叠
            self.assertFalse(lock.exists())  # 全部结束后锁文件不存在

    def test_all_entrypoints_use_lock(self):
        """review_pr、两套校准（calibrate、review_calibrate）、plan_review 都经同一把锁（同一工作区同一
        锁文件）；评审方运行期间锁文件存在、入口返回后删除。"""
        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            work = make_project(Path(tmp))
            head1, pr1 = push_pr(work, 7, ("a.txt", "one\n"))
            lock_file = work.parent / f"{work.name}-review.review.lock"
            seen: list[tuple[str, bool]] = []
            probe = ProbeReviewer(seen)
            with mock.patch.object(events_db, "ROOT", work), \
                    mock.patch.object(review, "make_reviewer", lambda name: probe), \
                    contextlib.redirect_stdout(io.StringIO()):
                # review_pr
                self.assertEqual(review.review_pr(7, "probe", root=work,
                                                  github=ReviewGitHub({7: pr1})), 0)
                self.assertIn((f"{work.name}-review", True), seen)
                self.assertFalse(lock_file.exists())
                # calibrate（较早那套校准，按样本持锁；jobs=1 时用 index 0 工作区，与 review_pr 互斥）。
                # bad 样本＝事故回放：在 main_ref 上把 find 替换为 replace 后建提交，不推送。
                samples = [{"kind": "bad", "id": "S1", "title": "夹具样本", "file": "app.txt",
                            "find": "v1\n", "replace": "v2\n"}]
                with mock.patch.object(review, "calibration_samples", return_value=samples), \
                        mock.patch.object(review, "EVALS", Path(tmp)):
                    record = review.calibrate("probe", root=work, reviewer=probe)
                self.assertEqual(record["samples"][0]["id"], "S1")
                self.assertIn((f"{work.name}-review", True), seen)
                self.assertFalse(lock_file.exists())
                # review_calibrate（B59 真实历史样本校准）
                manifest = Path(tmp) / "samples.json"
                manifest.write_text(json.dumps(
                    [{"pr": 7, "head": head1, "expected": "通过", "reason": "互斥探针"}], ensure_ascii=False),
                    encoding="utf-8")
                self.assertEqual(review_calibration.review_calibrate(
                    "probe", manifest, Path(tmp) / "calibrate.md", root=work, reviewer=probe), 0)
                self.assertIn((f"{work.name}-review", True), seen)
                self.assertFalse(lock_file.exists())
                # plan_review（任务拆分评审）
                plan = work / "docs/plans/design.md"
                plan.parent.mkdir(parents=True)
                plan.write_text("# 设计\n\n见 [规格](spec.md)。\n", encoding="utf-8")
                self.assertEqual(plan_review.review_plan(Path("docs/plans/design.md"),
                                                         Path(tmp) / "plan.md", root=work,
                                                         reviewer=probe), 0)
                self.assertIn((f"{work.name}-review", True), seen)
                self.assertFalse(lock_file.exists())
            self.assertGreaterEqual(seen.count((f"{work.name}-review", True)), 4)

    def test_calibrate_parallel_workspaces_do_not_block(self):
        """jobs=2 时 index 0 与 index 1 的工作区各用各的锁文件：两个样本同时进入评审方、互不阻塞；
        评审启动时两把锁都在，结束后都删除（任务书修订记录 2）。"""
        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            work = make_project(Path(tmp))
            markdir = Path(tmp) / "marks"
            markdir.mkdir()
            names = [f"{work.name}-review", f"{work.name}-review-1"]
            samples = [{"kind": "bad", "id": f"S{n}", "title": f"夹具样本 {n}", "file": "app.txt",
                        "find": "v1\n", "replace": f"v{n}\n"} for n in (1, 2)]
            done: dict = {}

            def run() -> None:
                with contextlib.redirect_stdout(io.StringIO()):
                    done["record"] = review.calibrate("probe", root=work,
                                                      reviewer=PairBlockingReviewer(markdir), jobs=2)

            with mock.patch.object(events_db, "ROOT", work), \
                    mock.patch.object(review, "calibration_samples", return_value=samples), \
                    mock.patch.object(review, "EVALS", Path(tmp)):
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                try:
                    for name in names:  # 两个样本都进入评审方：若共用一把锁，第二个会卡在等锁上
                        wait_until((markdir / name / "started").exists, timeout=30,
                                   what=f"{name} 的评审方启动")
                        self.assertEqual((markdir / name / "started").read_text(encoding="utf-8"), "1")
                    for name in names:  # 各自的锁文件都在评审方运行期间存在
                        self.assertTrue((work.parent / f"{name}.review.lock").exists())
                finally:  # 失败路径也要放行已启动的评审方，让线程能结束
                    for name in names:
                        (markdir / name / "ready").write_text("", encoding="utf-8")
                    thread.join(120)
            self.assertFalse(thread.is_alive())
            self.assertEqual(sorted(item["id"] for item in done["record"]["samples"]), ["S1", "S2"])
            for name in names:
                self.assertFalse((work.parent / f"{name}.review.lock").exists())


    def test_codex_reviewer_disables_subagents(self):
        """Codex 评审关闭子代理（B81）：-c agents.enabled=false 位于推理强度之后、-C 之前；
        第 0–5 位与原来一致（Agent-Notification 契约断言 argv[5] 是模型名）。"""
        argv = review.CodexReviewer("host/model-a", "high").argv("p", Path("/w"), Path("/o"))
        agents = argv.index("agents.enabled=false")
        self.assertEqual(argv[agents - 1], "-c")  # 作为 -c 的配置项传入
        self.assertLess(argv.index('model_reasoning_effort="high"'), agents)  # 在推理强度之后
        self.assertLess(agents, argv.index("-C"))  # 在 -C 之前
        self.assertEqual(argv[:6], ["codex", "exec", "-s", "read-only", "-m", "host/model-a"])
        # 只配置模型、不配置推理强度时同样关闭，且第 0–5 位不变
        plain = review.CodexReviewer("host/model-a").argv("p", Path("/w"), Path("/o"))
        self.assertIn("agents.enabled=false", plain)
        self.assertLess(plain.index("agents.enabled=false"), plain.index("-C"))
        self.assertEqual(plain[:6], ["codex", "exec", "-s", "read-only", "-m", "host/model-a"])


if __name__ == "__main__":
    unittest.main()
