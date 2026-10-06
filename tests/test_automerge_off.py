"""T715 automerge-off 与 automerge platform：停机撤销存量自动合并请求（五种未完成状态、
关闭后复查、仅两者皆零才 0 退出、--dry-run 不改），platform 子命令写 [platform] 配置输出。

夹具沿用本仓库的模式：打桩 gh 与隔离的 checks 配置目录，不碰真实库/PR。
"""

from __future__ import annotations

import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli
from engine.agents import automerge
from engine.core import common
from engine.routing.policy import platform_outputs


def run_row(run_id: int, status: str, branch: str = "task/1-a") -> dict:
    return {"id": run_id, "status": status, "head_branch": branch}


class FakeGh:
    """automerge 的 gh 桩：runs 与请求查询按预置序列应答（每次查询消费一条），写操作记录。

    runs_pages 的每个元素是一次查询的应答文本值：--slurp 的 JSON 数组（元素是 API 的 page 对象）。
    request_outputs 的每个元素是一次 REST pulls 分页查询的 jq 输出。
    """

    def __init__(self, runs_pages: list, request_outputs: list, fail_on_call: dict[int, str] | None = None):
        self.runs_pages = list(runs_pages)
        self.request_outputs = list(request_outputs)
        self.fail_on_call = dict(fail_on_call or {})  # 第 N 次调用包含该子串即失败（定位初查/复查）
        self.calls: list[str] = []
        self.disabled: list[int] = []

    def __call__(self, *args: str) -> str:
        joined = " ".join(args)
        index = len(self.calls)
        self.calls.append(joined)
        pattern = self.fail_on_call.get(index)
        if pattern and pattern in joined:
            raise RuntimeError(f"gh 失败（{pattern}）")
        if "workflows/auto-merge.yml/runs" in joined:
            return json.dumps(self.runs_pages.pop(0))
        if "/pulls?state=open" in joined:
            return self.request_outputs.pop(0)
        if joined.startswith("pr merge") and "--disable-auto" in joined:
            self.disabled.append(int(args[2]))
            return ""
        raise AssertionError(f"未预期的 gh 调用：{joined}")


class AutomergeOffTest(unittest.TestCase):
    def setUp(self):
        self._seq = itertools.count()
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-automerge-off-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---------- 撤销与复查 ----------

    def test_five_incomplete_statuses_are_listed_and_completed_ignored(self):
        # 五种未完成状态都算在途（可能还在开启请求）；completed 与未知状态不算
        gh = FakeGh(
            [[{"total_count": 6, "workflow_runs": [
                run_row(1, "queued"), run_row(2, "in_progress"), run_row(3, "waiting"),
                run_row(4, "pending"), run_row(5, "requested"), run_row(6, "completed")]}]],
            [""])
        runs = automerge.incomplete_runs(gh)
        self.assertEqual([run["id"] for run in runs], [1, 2, 3, 4, 5])

    def test_off_revokes_requests_and_exits_zero_after_recheck(self):
        # 初始：一次在途运行、两个存量请求；关闭后复查为零 → 0 退出，每个 PR 恰好关闭一次
        gh = FakeGh(
            [[{"workflow_runs": [run_row(1, "in_progress")]}], []],
            ["7\n9\n", ""])
        out = io.StringIO()
        with mock.patch.object(automerge, "_gh", gh), redirect_stdout(out):
            self.assertEqual(automerge.off_main([]), 0)
        self.assertEqual(gh.disabled, [7, 9])
        text = out.getvalue()
        self.assertIn("PR #7 的自动合并已关闭", text)
        self.assertIn("PR #9 的自动合并已关闭", text)
        self.assertIn("复查通过", text)
        # 关闭之后确有复查（两个查询各两轮）
        self.assertEqual(len([call for call in gh.calls if "workflows/auto-merge.yml/runs" in call]), 2)
        self.assertEqual(len([call for call in gh.calls if "/pulls?state=open" in call and "--paginate" in call]), 2)

    def test_off_fails_while_a_run_is_still_in_flight(self):
        # 复查时请求为零但旧运行未结束（queued）：非零退出并列出该运行，不假装成功
        gh = FakeGh(
            [[{"workflow_runs": [run_row(1, "queued")]}],
             [{"workflow_runs": [run_row(1, "queued")]}]],
            ["7\n", ""])
        out = io.StringIO()
        with mock.patch.object(automerge, "_gh", gh), redirect_stdout(out):
            self.assertEqual(automerge.off_main([]), 1)
        self.assertIn("1（queued", out.getvalue())
        self.assertIn("先取消或等其结束", out.getvalue())

    def test_off_fails_when_requests_remain_after_disable(self):
        gh = FakeGh([[], []], ["7\n", "7\n"])  # 复查时请求仍在
        out = io.StringIO()
        with mock.patch.object(automerge, "_gh", gh), redirect_stdout(out):
            self.assertEqual(automerge.off_main([]), 1)
        self.assertIn("仍有已开启的请求：#7", out.getvalue())

    def test_off_query_failure_is_non_zero(self):
        # 初始查询失败：非零退出，不触碰任何请求
        gh = FakeGh([[]], [""], fail_on_call={0: "workflows/auto-merge.yml/runs"})
        with mock.patch.object(automerge, "_gh", gh):
            self.assertEqual(automerge.off_main([]), 1)
        self.assertEqual(gh.disabled, [])
        # 复查失败（第 4 次调用是复查的 pr list）：同样非零退出
        gh = FakeGh([[], []], ["7\n", ""], fail_on_call={4: "pulls?state=open"})
        errors = io.StringIO()
        with mock.patch.object(automerge, "_gh", gh), mock.patch("sys.stderr", errors):
            self.assertEqual(automerge.off_main([]), 1)
        self.assertIn("复查失败", errors.getvalue())

    def test_dry_run_lists_without_changes(self):
        gh = FakeGh(
            [[{"workflow_runs": [run_row(3, "waiting")]}],
             [{"workflow_runs": [run_row(3, "waiting")]}]],
            ["7\n", "7\n"])
        out = io.StringIO()
        with mock.patch.object(automerge, "_gh", gh), redirect_stdout(out):
            self.assertEqual(automerge.off_main(["--dry-run"]), 0)
        self.assertEqual(gh.disabled, [])  # 只列出不改
        self.assertIn("3（waiting", out.getvalue())
        self.assertIn("#7", out.getvalue())

    def test_off_disable_failure_is_reported_and_non_zero(self):
        # 关闭调用失败：计入失败，复查后仍非零退出
        gh = FakeGh([[], []], ["7\n", "7\n"], fail_on_call={2: "pr merge 7"})
        errors = io.StringIO()
        with mock.patch.object(automerge, "_gh", gh), mock.patch("sys.stderr", errors):
            self.assertEqual(automerge.off_main([]), 1)
        self.assertIn("关闭 PR #7 失败", errors.getvalue())

    # ---------- platform 子命令 ----------

    def config_dir(self, checks_text: str | None) -> Path:
        """指向新的临时配置目录（_load_toml 按路径缓存，目录不复用、避免读到旧缓存）。"""
        folder = self.tmp / f"config-{next(self._seq)}"
        folder.mkdir()
        if checks_text is not None:
            (folder / "checks.toml").write_text(checks_text, encoding="utf-8")
        patch = mock.patch.object(common, "CONFIG_DIR", folder)
        patch.start()
        self.addCleanup(patch.stop)
        return folder

    def test_platform_writes_outputs_from_the_same_setting(self):
        # platform 子命令输出（并写入 GITHUB_OUTPUT）的键值与 policy.platform_outputs 同一套
        self.config_dir('[platform]\nsync_app_client_id_var = "SYNC_ID"\n')
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(automerge.platform_main(), 0)
        expected = platform_outputs()
        for line in out.getvalue().strip().splitlines():
            key, _, value = line.partition("=")
            self.assertEqual(value, expected[key], line)
        output = self.tmp / "github-output"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), redirect_stdout(io.StringIO()):
            self.assertEqual(automerge.platform_main(), 0)
        written = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        self.assertEqual(written, expected)

    def test_commands_are_registered_in_cli(self):
        self.assertIn("automerge", cli.COMMANDS)
        self.assertIn("automerge-off", cli.COMMANDS)
        module_name, _, function = cli.COMMANDS["automerge-off"].partition(":")
        module = __import__(module_name, fromlist=[function])
        self.assertTrue(callable(getattr(module, function)))


if __name__ == "__main__":
    unittest.main()
