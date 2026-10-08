"""B122 派发保全与续做（T721 B 部分）。

覆盖：backup_uncommitted 把槽位里未提交的完整工作区状态（已暂存、未暂存、被删除的已跟踪文件、
未跟踪文件、已跟踪但被忽略的文件）备份成 refs/backup/dispatch/<分支>/<UTC 时间> 引用，且不碰
真实索引字节、分支 HEAD 与工作区内容（--no-optional-locks 状态检测），干净槽位不建引用；
恢复命令在新工作树里实测五类状态还原；备份失败抛 RuntimeError 且工作树保留；记录提交被钩子
拒绝时 write_record 抛 Stop（带钩子输出末段），端到端经 _run_dispatch 退出码 2 并已备份；
prepare_slot(resume=True) 按四种情形合并 origin/main（快进/合并提交/已含时不动/冲突 abort 后
Stop），resume=False 不合并。全部走真实临时 git 仓库与 worktree，不碰真实远端。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch, dispatch_host, dispatch_slots
from engine.checks import taskbook
from engine.core import events, events_db

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
AGENT_ENV = {
    "GIT_AUTHOR_NAME": "agent-bot", "GIT_AUTHOR_EMAIL": "agent-bot@example.com",
    "GIT_COMMITTER_NAME": "agent-bot", "GIT_COMMITTER_EMAIL": "agent-bot@example.com",
}
VERIFY_PASS = "print('local verify ok')"
REL = "docs/plans/task-230-salvage.md"


class RecordingHost:
    """假执行方宿主：argv 启动固定脚本；流解析委托真实 PiHost（模块导入时先存真类，免被测试补丁递归）。"""

    name = "fake-pi"
    real_pi = dispatch_host.PiHost

    def __init__(self, script: str):
        self.script = script
        self.pi = self.real_pi()

    def version(self):
        return "9.9.9-fake"

    def argv(self, prompt, guard):
        return [sys.executable, "-c", self.script]

    def parse(self, events_path):
        return self.pi.parse(events_path)

    def parse_observability(self, events_path):
        return self.pi.parse_observability(events_path)

    def parse_context(self, events_path):
        return self.pi.parse_context(events_path)


class FakeGitHub:
    """派发用 gh 桩：push 走真实 git（本地 bare 远端），其余按需应答。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def remote_branch_exists(self, branch):
        self.calls.append(("remote_branch_exists", branch))
        return False

    def push(self, slot, branch):
        self.calls.append(("push", branch))
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        subprocess.run(["git", "push", "-q", "-f", "-u", "origin", branch], cwd=slot,
                       check=True, capture_output=True, env={**env, **GIT_ENV})

    def open_pr(self, slot, branch, title, body):
        return 14

    def wait_ci(self, branch, sha, timeout, detail=None):
        return True, ""


class GitFixture(unittest.TestCase):
    """匿名临时 git 仓库（含 bare 远端）与 worktree 夹具。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-salvage-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def git(self, *args, check=True, cwd=None):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_", "GITHUB_"))}
        done = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=False)
        if check and done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout.strip()

    def init_repo(self, files: dict[str, str], *, origin: bool = True):
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        for name, content in files.items():
            self.write(name, content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        if origin:
            origin_path = self.tmp / "origin.git"
            self.git("clone", "-q", "--bare", ".", str(origin_path))
            self.git("remote", "add", "origin", str(origin_path))
            self.git("fetch", "-q", "origin")
            self.git("push", "-q", "origin", "main")

    def write(self, name: str, content: str, cwd: Path | None = None):
        path = (cwd or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def make_slot(self, branch: str, start: str = "origin/main") -> Path:
        """干净槽位：独立 worktree，检出 branch（本地新建，自 start）。"""
        slot = self.tmp / f"slot-{branch.replace('/', '-')}"
        self.git("worktree", "add", "-q", "-b", branch, str(slot), start)
        return slot

    def index_path(self, slot: Path) -> Path:
        return Path(os.path.abspath(self.git("rev-parse", "--git-path", "index", cwd=slot)))

    def refs(self, pattern="refs/backup") -> list[str]:
        return [line.split()[-1] for line in self.git("for-each-ref", pattern,
                                                      "--format=%(refname)").splitlines()]

    def dirty_slot(self, slot: Path) -> None:
        """覆盖验收要求的全部状态：已暂存、未暂存、删除、未跟踪、被忽略、已跟踪但被忽略。"""
        self.write("tracked-modified.txt", "modified\n", cwd=slot)                       # 未暂存改动
        self.write("tracked-staged.txt", "staged\n", cwd=slot)                           # 已暂存改动
        self.git("add", "tracked-staged.txt", "staged-new.txt", cwd=slot)
        self.write("staged-new.txt", "new\n", cwd=slot)                                  # 新增并已暂存
        (slot / "deleted.txt").unlink()                                                  # 已跟踪文件被删除（未暂存）
        self.write("untracked.txt", "untracked\n", cwd=slot)                             # 未跟踪
        self.write("ignored.log", "ignored\n", cwd=slot)                                 # 被忽略的未跟踪
        self.write("keep-ignored.txt", "keep-modified\n", cwd=slot)                      # 已跟踪但匹配忽略规则


class BackupTest(GitFixture):
    def setUp(self):
        super().setUp()
        self.init_repo({
            "README.md": "# app\n",
            ".gitignore": "ignored.log\nkeep-ignored.txt\n",
            "tracked-modified.txt": "base\n",
            "tracked-staged.txt": "base\n",
            "deleted.txt": "doomed\n",
            "staged-new.txt": "",
            "keep-ignored.txt": "keep\n",
        })
        self.git("add", "-f", "keep-ignored.txt")  # 已跟踪但匹配忽略规则
        self.git("commit", "-q", "--amend", "--no-edit")
        self.git("push", "-q", "-f", "origin", "main")
        self.slot = self.make_slot("task/salvage")
        self.dirty_slot(self.slot)
        self.head_before = self.git("rev-parse", "HEAD", cwd=self.slot)
        self.index_before = self.index_path(self.slot).read_bytes()

    def test_backup_captures_full_worktree_state(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ref = dispatch_slots.backup_uncommitted(self.slot, "task/salvage")
        self.assertRegex(ref, r"^refs/backup/dispatch/task-salvage/\d{8}T\d{6}$")
        # 备份树：已暂存与未暂存改动、未跟踪、已跟踪但被忽略的文件都在；被删除与被忽略的不在
        tree = set(self.git("ls-tree", "-r", "--name-only", ref).splitlines())
        self.assertEqual(tree, {"README.md", ".gitignore", "tracked-modified.txt", "tracked-staged.txt",
                                "staged-new.txt", "untracked.txt", "keep-ignored.txt"})
        self.assertEqual(self.git("show", f"{ref}:tracked-modified.txt"), "modified")
        self.assertEqual(self.git("show", f"{ref}:tracked-staged.txt"), "staged")
        self.assertEqual(self.git("show", f"{ref}:untracked.txt"), "untracked")
        self.assertEqual(self.git("show", f"{ref}:keep-ignored.txt"), "keep-modified")
        self.assertNotIn("deleted.txt", tree)
        self.assertNotIn("ignored.log", tree)
        # 提示里带恢复方法
        self.assertIn(ref, err.getvalue())
        self.assertIn(f"git restore --source={ref} --worktree --staged :/", err.getvalue())

    def test_backup_leaves_head_index_and_worktree_untouched(self):
        status_before = self.git("status", "--porcelain", cwd=self.slot)
        with contextlib.redirect_stderr(io.StringIO()):
            ref = dispatch_slots.backup_uncommitted(self.slot, "task/salvage")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.slot), self.head_before)
        self.assertEqual(self.index_path(self.slot).read_bytes(), self.index_before)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.slot), status_before)
        self.assertEqual(self.git("show", f"{ref}:tracked-modified.txt"), "modified")

    def test_timestamp_only_change_with_untracked_still_leaves_index_untouched(self):
        # 普通git status 会刷新并写回真实索引的 stat 缓存；--no-optional-locks 不会（B122 验收点）
        os.utime(self.slot / "tracked-modified.txt", (1234567890, 1234567890))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNotNone(dispatch_slots.backup_uncommitted(self.slot, "task/salvage"))
        self.assertEqual(self.index_path(self.slot).read_bytes(), self.index_before)

    def test_clean_slot_returns_none_and_creates_no_ref(self):
        slot = self.make_slot("task/clean")
        self.assertIsNone(dispatch_slots.backup_uncommitted(slot, "task/clean"))
        self.assertEqual(self.refs(), [])

    def test_restore_command_reproduces_all_five_states(self):
        with contextlib.redirect_stderr(io.StringIO()):
            ref = dispatch_slots.backup_uncommitted(self.slot, "task/salvage")
        restore = self.tmp / "slot-restore"
        self.git("worktree", "add", "-q", "--detach", str(restore), self.head_before)
        self.git("restore", f"--source={ref}", "--worktree", "--staged", ":/", cwd=restore)
        status = self.git("status", "--porcelain", cwd=restore)
        self.assertFalse((restore / "deleted.txt").exists())                # 删除被还原
        self.assertEqual((restore / "tracked-modified.txt").read_text(), "modified\n")
        self.assertEqual((restore / "staged-new.txt").read_text(), "new\n")  # 暂存的新增在
        # 未跟踪也在（备份树含它，还原后为已暂存新增）
        self.assertEqual((restore / "untracked.txt").read_text(), "untracked\n")
        self.assertIn("A  untracked.txt", status)
        self.assertIn("M  tracked-modified.txt", status)
        self.assertEqual((restore / "keep-ignored.txt").read_text(), "keep-modified\n")

    def test_salvage_path_removes_dir_but_keeps_ref_with_deletion_diff(self):
        with contextlib.redirect_stderr(io.StringIO()):
            dispatch_slots._salvage_and_remove(self.repo, self.slot, lambda slot, branch: None)
        self.assertFalse(self.slot.exists())
        refs = self.refs("refs/backup/dispatch")
        self.assertEqual(len(refs), 1)
        diff = self.git("diff", "--name-status", "HEAD", refs[0])
        self.assertIn("D\tdeleted.txt", diff)


class BackupFailureKeepsWorktreeTest(GitFixture):
    def test_failed_backup_raises_and_return_slot_keeps_worktree(self):
        self.init_repo({"README.md": "# app\n"})
        self.slot = self.tmp / "app-slot-1"
        self.git("worktree", "add", "-q", "-b", "task/x", str(self.slot), "main")
        self.write("note.txt", "work\n", cwd=self.slot)
        real_run = subprocess.run

        def failing_run(args, **kwargs):
            if "update-ref" in args:
                raise subprocess.CalledProcessError(1, args)
            return real_run(args, **kwargs)

        config = dispatch.Config(slots=2, slot_root=self.tmp)
        with mock.patch.object(dispatch_slots.subprocess, "run", side_effect=failing_run):
            with self.assertRaises(RuntimeError):
                dispatch_slots.backup_uncommitted(self.slot, "task/x")
            self.assertEqual(self.refs(), [])  # 备份引用没建成
            self.assertTrue(self.slot.exists())  # 工作树还在

            # 归还路径：备份失败 → 不删工作树，只提示
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                dispatch_slots.return_slot(self.repo, config, 1, lambda slot, branch: None)
        self.assertIn("归还未完成（工作树保留", err.getvalue())
        self.assertTrue(self.slot.exists())
        self.assertEqual(self.refs(), [])


class RecordCommitRejectedTest(GitFixture):
    HEADER = """---
task: T230
class: K7
risk: R3
designer: codex
size: small
architecture: false
spec_refs: []
no_spec_reason: 测试
budget:
  wall_clock_min: 5
  retries: 0
  ci_rounds: 2
  tokens: null
rollback: git revert
---

# 任务：夹具
"""

    def setUp(self):
        super().setUp()
        self.init_repo({"README.md": "# app\n"})
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", return_value="2026-02-03T04:05:06.789Z")
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI"):
            os.environ.pop(key, None)
        for key, value in GIT_ENV.items():
            os.environ[key] = value
        os.environ["AGENT_LOGIN"] = "agent-bot"
        os.environ["AGENT_EMAIL"] = "agent-bot@example.com"
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)
        self.config = dispatch.Config(slots=2, stall_seconds=120, poll_seconds=0.05,
                                      ci_timeout_seconds=5, verify=[sys.executable, "-c", VERIFY_PASS])
        self.commit_guards()

    def commit_guards(self):
        """origin/main 上的守卫夹具：必拒载荷退出 2。"""
        for path, content in {
            "harness/command_guard.py": "import sys\nsys.exit(2)\n",
            ".pi/extensions/harness-guard.ts": "// guard fixture\n",
        }.items():
            self.write(path, content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.git("push", "-q", "origin", "main")

    def install_rejecting_hook(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'lint failed (fixture)' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

    def stub_taskbook(self):
        header = {
            "task": "T230", "class": "K7", "risk": "R3", "designer": "codex", "size": "small",
            "spec_refs": [],
            "budget": {"wall_clock_min": 5, "retries": 0, "ci_rounds": 2, "tokens": None},
        }
        reports = [taskbook.Report(REL, header=header)]
        for patcher in (
            mock.patch.object(taskbook, "check_all", lambda *args, **kwargs: reports),
            mock.patch.object(taskbook, "on_main", lambda *args, **kwargs: None),
            mock.patch.object(taskbook, "load_exempt", lambda *args, **kwargs: {}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_write_record_raises_stop_with_hook_tail(self):
        self.install_rejecting_hook()
        slot = self.make_slot("task/230-a")
        runner = dispatch.Dispatcher(self.repo, self.config, FakeGitHub(), dispatch_host.PiHost(),
                                     identity=dict(GIT_ENV))
        task = dispatch.Task(REL, "T230", "K7", "R3",
                             {"wall_clock_min": 5, "retries": 0, "ci_rounds": 2, "tokens": None},
                             [], "task/230-a")
        attempt = dispatch.Attempt(ok=False, exit="timeout")
        with self.assertRaises(dispatch.Stop) as caught:
            runner.write_record(task, slot, 1, attempt, ["prompt"], "guardref", "2026-02-03T04:05:06", 0)
        message = str(caught.exception)
        self.assertIn("派发记录提交被钩子拒绝", message)
        self.assertIn("lint failed (fixture)", message)  # 钩子输出末段的片段
        self.assertIn("refs/backup/dispatch/", message)

    def test_end_to_end_exit_two_and_uncommitted_work_backed_up(self):
        self.install_rejecting_hook()
        self.stub_taskbook()
        script = ("import json, pathlib\n"
                  "print(json.dumps({'type': 'session'}), flush=True)\n"
                  "pathlib.Path('note.txt').write_text('work\\n')\n")  # 执行方留下未提交改动
        gh = FakeGitHub()
        out, err = io.StringIO(), io.StringIO()
        patches = (
            mock.patch.object(dispatch, "ROOT", self.repo),
            mock.patch.object(dispatch.Config, "load", classmethod(lambda cls: self.config)),
            mock.patch.object(dispatch, "GitHub", lambda: gh),
            mock.patch.object(dispatch.dispatch_host, "PiHost", lambda model=None: RecordingHost(script)),
        )
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            code = dispatch._run_dispatch(argparse.Namespace(taskbook=REL, resume=False, model=None))
        self.assertEqual(code, 2)
        self.assertIn("派发记录提交被钩子拒绝", out.getvalue())
        self.assertIn("refs/backup/dispatch/", err.getvalue())  # 备份提示
        slot = self.tmp / "app-slot-1"
        self.assertFalse(slot.exists())  # 槽位已归还
        refs = self.refs("refs/backup/dispatch")
        self.assertEqual(len(refs), 1)
        tree = set(self.git("ls-tree", "-r", "--name-only", refs[0]).splitlines())
        self.assertIn("note.txt", tree)  # 执行方未提交的成果在备份里
        self.assertIn("docs/runs/task-230-salvage/1.json", tree)  # 已暂存的运行记录也在


class ResumeMergeTest(GitFixture):
    def setUp(self):
        super().setUp()
        self.init_repo({"README.md": "# app\n", "shared.txt": "base\n", "other.txt": "base\n"})

    def add_branch_commit(self, branch: str, base: str, edits: dict[str, str], message: str) -> str:
        """在临时 worktree 里以 base 为起点造分支提交，推到远端分支（不在本地留检出）。"""
        scratch = self.tmp / f"scratch-{branch.replace('/', '-')}"
        self.git("worktree", "add", "-q", "--detach", str(scratch), base)
        try:
            for name, content in edits.items():
                self.write(name, content, cwd=scratch)
            self.git("add", "-A", cwd=scratch)
            self.git("commit", "-q", "-m", message, cwd=scratch)
            sha = self.git("rev-parse", "HEAD", cwd=scratch)
            self.git("push", "-q", "origin", f"{sha}:refs/heads/{branch}", cwd=scratch)
        finally:
            self.git("worktree", "remove", "--force", str(scratch))
        return sha

    def advance_main(self, edits: dict[str, str], message: str) -> str:
        for name, content in edits.items():
            self.write(name, content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        sha = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "main")
        return sha

    def prepare(self, branch: str, *, resume=True, env=None):
        """prepare_slot 并监视 git merge 调用；返回 (槽位路径, merge 调用列表)。"""
        slot = self.tmp / f"slot-{branch.replace('/', '-')}"
        real_run = subprocess.run
        calls: list[list[str]] = []

        def spy(args, **kwargs):
            calls.append(list(args))
            return real_run(args, **kwargs)

        with mock.patch.object(dispatch_slots.subprocess, "run", side_effect=spy):
            dispatch_slots.prepare_slot(self.repo, slot, branch, resume, env)
        merges = [args for args in calls if args[:2] == ["git", "merge"]]
        return slot, merges

    def test_fast_forward_when_branch_only_behind(self):
        base = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", f"{base}:refs/heads/task/x")
        main_sha = self.advance_main({"other.txt": "advanced\n"}, "chore：main 前进")
        slot, merges = self.prepare("task/x")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=slot), main_sha)  # 快进到 origin/main
        self.assertEqual(self.git("rev-list", "--count", "origin/main..HEAD", cwd=slot), "0")
        self.assertEqual(len(merges), 1)  # 合并执行了，但只是快进

    def test_diverged_creates_merge_commit_with_env_identity(self):
        base = self.git("rev-parse", "HEAD")
        branch_sha = self.add_branch_commit("task/x", base, {"feature.txt": "feature\n"},
                                            "feat：分支自己的提交")
        main_sha = self.advance_main({"other.txt": "advanced\n"}, "chore：main 前进")
        slot, merges = self.prepare("task/x", env={**os.environ, **AGENT_ENV})
        self.assertEqual(len(merges), 1)
        head = self.git("rev-parse", "HEAD", cwd=slot)
        parents = self.git("rev-list", "--parents", "-n", "1", "HEAD", cwd=slot).split()[1:]
        self.assertEqual(sorted(parents), sorted([branch_sha, main_sha]))  # 合并提交的两个父节点
        self.assertEqual(self.git("log", "-1", "--format=%an <%ae> %cn <%ce>", cwd=slot),
                         "agent-bot <agent-bot@example.com> agent-bot <agent-bot@example.com>")
        self.assertNotEqual(head, branch_sha)

    def test_already_contains_main_skips_merge(self):
        main_sha = self.advance_main({"other.txt": "advanced\n"}, "chore：main 前进")
        branch_sha = self.add_branch_commit("task/x", main_sha, {"feature.txt": "feature\n"},
                                            "feat：分支自己的提交")
        slot, merges = self.prepare("task/x")
        self.assertEqual(merges, [])  # 已包含 origin/main，没有调用 git merge
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=slot), branch_sha)

    def test_conflict_aborts_and_keeps_branch_clean(self):
        base = self.git("rev-parse", "HEAD")
        branch_sha = self.add_branch_commit("task/x", base, {"shared.txt": "branch\n"},
                                            "feat：分支改同一行")
        self.advance_main({"shared.txt": "main\n"}, "chore：main 改同一行")
        slot = self.tmp / "slot-task-x"
        with self.assertRaises(dispatch.Stop) as caught:
            dispatch_slots.prepare_slot(self.repo, slot, "task/x", True)
        self.assertIn("续做前合并 origin/main 冲突", str(caught.exception))
        self.assertEqual(self.git("status", "--porcelain", cwd=slot), "")  # 工作树干净
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=slot), branch_sha)  # 分支未被破坏
        self.assertEqual(self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=slot, check=False),
                         "")  # 没有残留合并状态

    def test_no_merge_without_resume(self):
        self.advance_main({"other.txt": "advanced\n"}, "chore：main 前进")
        slot, merges = self.prepare("task/fresh", resume=False)
        self.assertEqual(merges, [])  # 非续做路径不合并
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=slot),
                         self.git("rev-parse", "origin/main"))


if __name__ == "__main__":
    unittest.main()
