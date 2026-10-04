"""评审互斥与评审方回归（T708，Defect: E128-R1——逃逸 #128）：同一评审工作区的并发评审串行化、
死锁回收与超时、各评审入口持锁，以及 Codex 评审关闭子代理（B81）。

夹具为匿名临时 git 仓库（bare 远端 + refs/pull/<n>/head）、假 gh 与假评审方，不碰真实库/PR；
并发编排只等待「条件成立」（标记文件、调用计数），不依赖固定 sleep 时序。
calibrate（另一套校准）的加锁因 tests/test_harness_contract_review.py 用非 git 目录当 root、
与锁的 state_dir 冲突而暂缓，待设计方决定后补上（见派发升级记录与 PR 说明）。
"""

from __future__ import annotations

import contextlib
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

from engine.agents import dispatch, plan_review, review, review_calibration, review_lock
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

    def __init__(self, locks_dir: Path | None = None, seen: list | None = None):
        self.locks_dir, self.seen = locks_dir, seen

    def argv(self, prompt, workspace, output):
        if self.seen is not None:
            self.seen.append((workspace.name, (self.locks_dir / f"{workspace.name}.json").exists()))
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


class ReviewLockTest(unittest.TestCase):
    def test_concurrent_reviews_serialized(self):
        """两个线程同时对同一工作区 review_pr：第二个在第一个发布评论前不 checkout、不写材料（E128-R1）。"""
        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            work = make_project(Path(tmp))
            head1, pr1 = push_pr(work, 7, ("a.txt", "one\n"))
            head2, pr2 = push_pr(work, 8, ("b.txt", "two\n"))
            markdir = Path(tmp) / "marks"
            markdir.mkdir()
            order: list[tuple] = []
            attempts: list[str] = []
            real_checkout, real_materials, real_lock = review.checkout, review.write_materials, \
                review_lock.workspace_lock

            def rec_checkout(workspace, ref):
                order.append(("checkout", ref))
                return real_checkout(workspace, ref)

            def rec_materials(workspace, base, pr_text, task_text, ci_text=""):
                order.append(("materials", 7 if "PR7" in pr_text else 8))
                return real_materials(workspace, base, pr_text, task_text, ci_text)

            def rec_lock(root, workspace, timeout_seconds, *args, **kwargs):
                attempts.append(workspace.name)
                return real_lock(root, workspace, timeout_seconds, *args, **kwargs)

            github = ReviewGitHub({7: pr1, 8: pr2}, order)
            results: dict[int, int] = {}

            def run(number: int):
                with contextlib.redirect_stdout(io.StringIO()):
                    results[number] = review.review_pr(number, "probe", root=work, github=github)

            with mock.patch.object(events_db, "ROOT", work), \
                    mock.patch.object(review, "make_reviewer", BlockingFactory(markdir)), \
                    mock.patch.object(review, "checkout", rec_checkout), \
                    mock.patch.object(review, "write_materials", rec_materials), \
                    mock.patch.object(review_lock, "workspace_lock", rec_lock):
                first = threading.Thread(target=run, args=(7,))
                first.start()
                wait_until((markdir / "started").exists, what="第一个评审进入评审方")
                second = threading.Thread(target=run, args=(8,))
                second.start()
                wait_until(lambda: len(attempts) >= 2, what="第二个评审到达工作区锁")
                # 第二个评审被锁挡住：此刻只有第一个评审的 checkout 与材料，也还没有任何评论
                self.assertEqual([item[0] for item in order], ["checkout", "materials"])
                self.assertEqual(order[0], ("checkout", head1))
                self.assertEqual(order[1], ("materials", 7))
                self.assertEqual(github.comments, [])
                (markdir / "release").write_text("", encoding="utf-8")  # 放行第一个评审方
                first.join(120)
                second.join(120)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(results, {7: 0, 8: 0})
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
            self.assertFalse((dispatch.state_dir(work) / "review-locks" / f"{work.name}-review.json").exists())

    def test_stale_lock_reclaimed_and_timeout(self):
        """持有进程已不存在的锁被回收；持有者仍存活时等到超时抛 TimeoutError（信息含 pid 与用途）；
        正常退出、异常退出都删除锁文件。"""
        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            root, state = Path(tmp), Path(tmp) / "state"
            workspace = root / "app-review"
            lock = state / "review-locks" / "app-review.json"
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            with mock.patch.object(dispatch, "state_dir", return_value=state):
                # 死锁回收：持有进程已退出，直接拿回并覆盖为自己的持有信息
                lock.parent.mkdir(parents=True)
                lock.write_text(json.dumps({"pid": dead.pid, "started_at": "t0", "purpose": "旧评审"}),
                                encoding="utf-8")
                with review_lock.workspace_lock(root, workspace, 5, "探针") as acquired:
                    self.assertEqual(acquired, lock)
                    held = json.loads(lock.read_text(encoding="utf-8"))
                    self.assertEqual(held["pid"], os.getpid())
                    self.assertEqual(held["purpose"], "探针")
                    self.assertIn("started_at", held)
                self.assertFalse(lock.exists())  # 正常退出删除锁文件
                # 异常退出同样删除锁文件
                with self.assertRaises(RuntimeError), review_lock.workspace_lock(root, workspace, 5, "探针"):
                    raise RuntimeError("评审中断")
                self.assertFalse(lock.exists())
                # 持有者仍存活（本进程）：等到超时抛 TimeoutError，信息含持有者 pid 与用途；
                # 等待方不删别人还持有的锁
                lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "t1", "purpose": "别的评审"}),
                                encoding="utf-8")
                with self.assertRaises(TimeoutError) as caught, \
                        review_lock.workspace_lock(root, workspace, 0.05, "探针", poll_seconds=0.01):
                    pass
                self.assertIn(str(os.getpid()), str(caught.exception))
                self.assertIn("别的评审", str(caught.exception))
                self.assertEqual(json.loads(lock.read_text(encoding="utf-8"))["purpose"], "别的评审")

    def test_all_entrypoints_use_lock(self):
        """review_pr、review_calibrate、plan_review 都经同一把锁（同一工作区同一锁文件）；
        评审方运行期间锁文件存在、入口返回后删除。"""
        with tempfile.TemporaryDirectory(prefix="dh-review-lock-") as tmp:
            work = make_project(Path(tmp))
            head1, pr1 = push_pr(work, 7, ("a.txt", "one\n"))
            locks_dir = dispatch.state_dir(work) / "review-locks"
            lock_file = locks_dir / f"{work.name}-review.json"
            seen: list[tuple[str, bool]] = []
            probe = ProbeReviewer(locks_dir, seen)
            with mock.patch.object(events_db, "ROOT", work), \
                    mock.patch.object(review, "make_reviewer", lambda name: probe), \
                    contextlib.redirect_stdout(io.StringIO()):
                # review_pr
                self.assertEqual(review.review_pr(7, "probe", root=work,
                                                  github=ReviewGitHub({7: pr1})), 0)
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
            self.assertGreaterEqual(seen.count((f"{work.name}-review", True)), 3)


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
