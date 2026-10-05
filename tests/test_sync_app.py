"""T714 同步 App 测试：platform_outputs 的同步键与 judge outputs 的透传、批准与同步两个 App 令牌的分工
（落后分支用同步令牌更新；同步 App 未配置时评论提示、不批准不合并；令牌权限拆分的静态断言）。

夹具沿用 tests/test_signoff.py 的模式：匿名临时目录、隔离的 checks 配置、模板解析与步骤重放；
不碰真实库/PR。
"""

from __future__ import annotations

import itertools
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.core import common
from engine.routing.policy import platform_outputs
from tests.test_ci_events_workflows import parse_workflow
from tests.test_signoff import merge_app_steps, replay_merge_app

ENGINE_REPO = Path(__file__).resolve().parents[1]
WORKFLOW = ENGINE_REPO / "templates/.github/workflows/auto-merge.yml"


class SyncAppTest(unittest.TestCase):
    def setUp(self):
        self._seq = itertools.count()
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-sync-app-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---------- 夹具 ----------

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

    def template(self) -> dict:
        return parse_workflow(WORKFLOW.read_text(encoding="utf-8"))

    # ---------- 验收 1：platform_outputs 的同步键与 judge outputs ----------

    def test_platform_outputs_and_judge_outputs(self):
        # 缺省：两个同步键与批准键一样取通用默认名，不含任何使用者自己的名字
        with self.config_dir(None):
            out = platform_outputs()
        self.assertEqual(out["sync_app_client_id_var"], "HARNESS_SYNC_APP_CLIENT_ID")
        self.assertEqual(out["sync_app_private_key_secret"], "HARNESS_SYNC_APP_PRIVATE_KEY")
        # 配置后为配置值
        configured = ('[platform]\nsync_app_client_id_var = "SYNC_ID"\n'
                      'sync_app_private_key_secret = "SYNC_KEY"\n')
        with self.config_dir(configured):
            out = platform_outputs()
        self.assertEqual((out["sync_app_client_id_var"], out["sync_app_private_key_secret"]),
                         ("SYNC_ID", "SYNC_KEY"))
        # judge 任务把 platform_outputs 的每个键都列进 outputs：merge-app 用 vars/secrets 按这些名字取值，
        # 少列一个，工作流就读不到同步 App 的变量名（缺省回落为空、同步被静默跳过）
        outputs = self.template()["jobs"]["judge"]["outputs"]
        with self.config_dir(None):
            expected = platform_outputs()
        for key in expected:
            self.assertEqual(outputs.get(key), f"${{{{ steps.policy.outputs.{key} }}}}", key)

    # ---------- 验收 4：令牌权限拆分的静态断言 ----------

    def test_token_permissions_split(self):
        # 批准 App 的令牌步骤只申请写 PR（不再持有写代码权限）；同步令牌步骤仅在同步 App 的
        # 变量非空时执行，并申请写内容与写 PR（两个 App 不能合一：ruleset 要求最后一次推送
        # 由推送者以外的人批准，批准 App 同步后成了最后推送者，它的批准不再满足这条规则）
        app, sync_app, _sync, _merge = merge_app_steps(self.template())
        self.assertEqual(app["name"], "App token (approve)")
        self.assertEqual(app["with"]["permission-pull-requests"], "write")
        self.assertNotIn("permission-contents", app["with"])
        self.assertEqual(sync_app["name"], "Sync App token")
        self.assertIn("vars[needs.judge.outputs.sync_app_client_id_var] != ''", str(sync_app.get("if")))
        self.assertEqual(sync_app["with"]["permission-contents"], "write")
        self.assertEqual(sync_app["with"]["permission-pull-requests"], "write")
        self.assertEqual(sync_app["with"]["client-id"], "${{ vars[needs.judge.outputs.sync_app_client_id_var] }}")
        self.assertEqual(sync_app["with"]["private-key"], "${{ secrets[needs.judge.outputs.sync_app_private_key_secret] }}")

    # ---------- 验收 3：同步 App 未配置时评论提示、不同步 ----------

    def test_behind_without_sync_app_comments(self):
        # 落后且同步 App 未配置：不调用 update-branch，评论提示一次，不批准、不合并，步骙成功结束
        replay, log = replay_merge_app(self.tmp, behind_by="2", mergeable="MERGEABLE")
        self.assertIn("Sync behind branch before approving", replay.ran)
        self.assertNotIn("Sync App token", replay.ran)  # 令牌步骙按「变量非空」条件跳过
        self.assertNotIn("Label, approve and merge", replay.ran)  # 批准步骙被拦下
        self.assertEqual(replay.conclusion, "success")
        self.assertEqual(log.count("pr comment 14"), 1)  # 只评论一次
        self.assertIn("分支落后于 main，未配置同步 App，请手动同步后重判", log)
        self.assertNotIn("update-branch", log)  # 没有退回用批准 App 同步
        self.assertNotIn("pulls/14/reviews", log)  # 没有批准
        self.assertNotIn("pr merge", log)  # 没有合并
        self.assertNotIn("app-token-1", log)  # 批准令牌全程未被使用


if __name__ == "__main__":
    unittest.main()
