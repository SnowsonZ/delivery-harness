"""安静执行方持续改写同一文件时，不应因 git 状态文字不变而被误杀。"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agents import dispatch_host


class QuietFileActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        self.file = self.repo / "active.txt"
        self.file.write_text("base")
        self.events = self.root / "events.jsonl"

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

    def test_untracked_file_growth_is_activity(self):
        def grow(tick):
            with self.file.open("a") as output:
                output.write(str(tick))
        result, stopped = self.run_case(grow)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)

    def test_same_size_rewrites_of_indexed_modified_file_are_activity(self):
        # 文件纳入索引后再改写，状态一直为 AM；匿名夹具无需创建任何 commit。
        subprocess.run(["git", "add", "active.txt"], cwd=self.repo, check=True)
        self.file.write_text("0000")
        result, stopped = self.run_case(self.rewrite)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)

    def test_same_size_rewrites_with_spaces_in_name_are_activity(self):
        self.file = self.file.rename(self.repo / "active file.txt")
        result, stopped = self.run_case(self.rewrite)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)

    def test_unchanged_workspace_still_stalls(self):
        result, stopped = self.run_case()
        self.assertEqual(result[:2], ("stall", -15))
        self.assertEqual(stopped, 1)

    def test_output_growth_still_counts_as_activity(self):
        def output(_tick):
            with self.events.open("a") as stream:
                stream.write("event\n")
        result, stopped = self.run_case(output)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)

    def test_file_activity_does_not_bypass_absolute_deadline(self):
        result, stopped = self.run_case(self.rewrite, deadline=3.5, stall=100)
        self.assertEqual(result[:2], ("timeout", -15))
        self.assertEqual(stopped, 1)

    def test_file_activity_does_not_bypass_stop_flag(self):
        def stop(tick):
            self.rewrite(tick)
            if tick == 2:
                (self.root / "stop").touch()
        result, stopped = self.run_case(stop, stall=100)
        self.assertEqual(result[:2], ("stopped", -15))
        self.assertEqual(stopped, 1)

    def test_quick_quiet_success_is_unchanged(self):
        result, stopped = self.run_case(finish=1)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)

    def test_natural_nonzero_exit_is_unchanged(self):
        result, stopped = self.run_case(finish=1, code=7)
        self.assertEqual(result[:2], ("error", 7))
        self.assertEqual(stopped, 0)

    def test_deleted_file_during_sampling_does_not_crash(self):
        subprocess.run(["git", "add", "active.txt"], cwd=self.repo, check=True)
        def delete(_tick):
            self.file.unlink(missing_ok=True)
        result, stopped = self.run_case(delete, stall=100)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)


if __name__ == "__main__":
    unittest.main()
