"""仓库根路径末尾的空白属于真实目录名，监测必须采样原工作区。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agents import dispatch_host


class RootPathActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo "
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        self.file = self.repo / "active.txt"
        self.file.write_text("0000")
        self.events = self.root / "events.jsonl"

    def rename_repo(self, name):
        self.repo = self.repo.rename(self.root / name)
        self.file = self.repo / "active.txt"

    def run_case(self, change=None, *, deadline=50, stall=1.5, finish=6, code=0):
        # 只替换时钟和外部进程边界：真实活动采样、git 状态、文件读取/元数据不替换。
        clock = [0]
        process = mock.Mock()
        process.returncode = None

        def poll():
            if process.returncode is None and clock[0] >= finish:
                process.returncode = code
            return process.returncode

        def sleep(_seconds):
            clock[0] += 1
            if change:
                change(clock[0])

        def terminate(child):
            self.assertIs(child, process)
            child.returncode = -15

        process.poll.side_effect = poll
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            if argv == ["anonymous-executor"]:
                return process
            return real_popen(argv, *args, **kwargs)

        with mock.patch.object(dispatch_host.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(dispatch_host.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(dispatch_host.time, "sleep", side_effect=sleep), \
                mock.patch.object(dispatch_host, "_terminate", side_effect=terminate) as stopped:
            result = dispatch_host.run_monitored(["anonymous-executor"], self.repo, {}, self.events,
                deadline_seconds=deadline, stall_seconds=stall, poll_seconds=1,
                stop_flag=self.root / "stop")
        return result, stopped.call_count

    def rewrite(self, tick):
        self.file.write_text(f"{tick:04d}")
        timestamp = 1_700_000_000_000_000_000 + tick * 1_000_000
        os.utime(self.file, ns=(timestamp, timestamp))

    def assert_active(self):
        before = subprocess.check_output(["git", "status", "--porcelain", "-z", "-uall"], cwd=self.repo)
        result, stopped = self.run_case(self.rewrite)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)
        self.assertEqual(before, subprocess.check_output(
            ["git", "status", "--porcelain", "-z", "-uall"], cwd=self.repo))

    def test_root_trailing_space_is_preserved(self):
        self.assert_active()

    def test_root_trailing_tab_is_preserved(self):
        self.rename_repo("repo\t")
        self.assert_active()

    def test_root_trailing_newline_is_preserved(self):
        self.rename_repo("repo\n")
        self.assert_active()

    def test_root_carriage_return_is_preserved(self):
        self.rename_repo("repo\r")
        self.assert_active()

    def test_nested_cwd_still_resolves_exact_root(self):
        folder = self.repo / "nested"
        folder.mkdir()
        self.file = self.file.rename(folder / "active.txt")
        self.repo = folder
        self.assert_active()

    def test_trimmed_sibling_changes_do_not_count_as_activity(self):
        sibling = self.root / "repo"
        sibling.mkdir()
        other = sibling / "active.txt"
        other.write_text("0000")
        def change(tick):
            other.write_text(f"{tick:04d}")
            timestamp = 1_700_000_000_000_000_000 + tick * 1_000_000
            os.utime(other, ns=(timestamp, timestamp))
        result, stopped = self.run_case(change)
        self.assertEqual(result[:2], ("stall", -15))
        self.assertEqual(stopped, 1)
        self.assertEqual(self.file.read_text(), "0000")

    def test_unchanged_root_with_whitespace_still_stalls(self):
        result, stopped = self.run_case()
        self.assertEqual(result[:2], ("stall", -15))
        self.assertEqual(stopped, 1)

    def test_activity_cannot_bypass_deadline(self):
        result, stopped = self.run_case(self.rewrite, deadline=3.5, stall=100)
        self.assertEqual(result[:2], ("timeout", -15))
        self.assertEqual(stopped, 1)

    def test_activity_cannot_bypass_stop(self):
        def change(tick):
            self.rewrite(tick)
            if tick == 2:
                (self.root / "stop").touch()
        result, stopped = self.run_case(change, stall=100)
        self.assertEqual(result[:2], ("stopped", -15))
        self.assertEqual(stopped, 1)

    def test_natural_nonzero_exit_is_preserved(self):
        result, stopped = self.run_case(finish=1, code=7)
        self.assertEqual(result[:2], ("error", 7))
        self.assertEqual(stopped, 0)
