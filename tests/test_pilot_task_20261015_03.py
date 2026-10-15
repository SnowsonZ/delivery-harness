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


    def _assert_symlink_target_changes_are_inactive(self, target):
        target.write_text("0000")
        self.file.unlink()
        self.file.symlink_to(target)
        before = self.file.lstat()
        def target_changes(tick):
            target.write_text(f"{tick:04d}")
            timestamp = 1_700_000_000_000_000_000 + tick * 1_000_000
            os.utime(target, ns=(timestamp, timestamp))
        result, stopped = self.run_case(target_changes)
        after = self.file.lstat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        self.assertEqual(result[:2], ("stall", -15))
        self.assertEqual(stopped, 1)

    def test_ignored_symlink_target_updates_do_not_count_as_activity(self):
        (self.repo / ".gitignore").write_text("ignored/\n")
        folder = self.repo / "ignored"
        folder.mkdir()
        self._assert_symlink_target_changes_are_inactive(folder / "data.txt")

    def test_git_internal_symlink_target_updates_do_not_count_as_activity(self):
        self._assert_symlink_target_changes_are_inactive(self.repo / ".git" / "probe-data.txt")

    def test_external_symlink_target_updates_do_not_count_as_activity(self):
        self._assert_symlink_target_changes_are_inactive(self.root / "outside.txt")

    def test_untracked_subdirectory_rewrites_are_activity(self):
        self.file.unlink()
        folder = self.repo / "new-dir"
        folder.mkdir()
        self.file = folder / "active.txt"
        self.file.write_text("0000")
        result, stopped = self.run_case(self.rewrite)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)


    def test_symlink_ancestor_target_updates_do_not_count_as_activity(self):
        # 每个子情形独立匿名仓库，覆盖直接父目录和更上层祖先，避免只检查一层。
        for location in ("outside", "ignored", "git"):
            for depth in (1, 2):
                with self.subTest(location=location, depth=depth):
                    case = QuietFileActivityTest()
                    case.setUp()
                    try:
                        folder = case.repo / "tracked-parent"
                        rel = Path("inner/active.txt") if depth == 2 else Path("active.txt")
                        original = folder / rel
                        original.parent.mkdir(parents=True)
                        original.write_text("0000")
                        subprocess.run(["git", "add", str(original.relative_to(case.repo))],
                                       cwd=case.repo, check=True)
                        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                                        "-c", "user.email=t@example.com", "commit", "-q", "-m", "fixture"],
                                       cwd=case.repo, check=True)
                        if location == "outside":
                            target_root = case.root / "outside"
                        elif location == "ignored":
                            (case.repo / ".gitignore").write_text("ignored/\n")
                            target_root = case.repo / "ignored"
                        else:
                            target_root = case.repo / ".git" / "probe-target"
                        target = target_root / rel
                        target.parent.mkdir(parents=True)
                        target.write_text("0000")
                        original.unlink()
                        if depth == 2:
                            original.parent.rmdir()
                        folder.rmdir()
                        folder.symlink_to(target_root, target_is_directory=True)
                        case.file.unlink()
                        def change(tick, target=target):
                            target.write_text(f"{tick:04d}")
                            timestamp = 1_700_000_000_000_000_000 + tick * 1_000_000
                            os.utime(target, ns=(timestamp, timestamp))
                        result, stopped = case.run_case(change)
                        self.assertEqual(result[:2], ("stall", -15))
                        self.assertEqual(stopped, 1)
                    finally:
                        case.doCleanups()

    def test_symlink_itself_changes_still_count_as_activity(self):
        first, second = self.root / "target-a", self.root / "target-b"
        first.write_text("0000")
        second.write_text("0000")
        self.file.unlink()
        self.file.symlink_to(first)
        def change(tick):
            self.file.unlink()
            self.file.symlink_to(second if tick % 2 else first)
            timestamp = 1_700_000_000_000_000_000 + tick * 1_000_000
            os.utime(self.file, ns=(timestamp, timestamp), follow_symlinks=False)
        result, stopped = self.run_case(change)
        self.assertEqual(result[:2], ("ok", 0))
        self.assertEqual(stopped, 0)


if __name__ == "__main__":
    unittest.main()
