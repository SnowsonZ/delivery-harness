"""T715 原生自动合并（B102）：merge-app 开启前再判定→批准→开启→退回、抽审议题在任何合并命令之前
幂等登记、main push 时同步等待中的 PR、request-review 关闭自动合并、再判定拒绝时撤销存量请求。

夹具沿用 tests/test_signoff.py 的模式：模板解析与步骤重放（gh 桩未登记的子命令直接失败，一般 7）；
不碰真实库/PR，不检出、不执行 PR 代码。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_ci_events_workflows import parse_workflow
from tests.test_signoff import GIT_ENV, MergeReplay, replay_merge_app, write_gh_shim

ENGINE_REPO = Path(__file__).resolve().parents[1]
WORKFLOW = ENGINE_REPO / "templates/.github/workflows/auto-merge.yml"

# sync-waiting 重放桩：按 GH_SHIM_RESPONSES（JSON [子串, 应答] 列表，按序首个命中）应答，
# 登记过的写命令静默成功，未登记的子命令直接失败（T715 验收：回放桩不得把没见过的调用当成功）。
SYNC_GH_SHIM = '''#!/usr/bin/env python3
import json
import os
import sys

argv = " ".join(sys.argv[1:])
with open(os.environ["GH_SHIM_LOG"], "a", encoding="utf-8") as fh:
    fh.write("CALL " + argv + " [token=" + str(os.environ.get("GH_TOKEN") or "none") + "]\\n")
for pattern, output in json.loads(os.environ.get("GH_SHIM_RESPONSES", "[]")):
    if pattern in argv:
        print(output)
        sys.exit(0)
if any(flag in argv for flag in ("update-branch", "pr comment", "--add-label", "--disable-auto",
                                 "pr merge", "label create", "pr edit")):
    sys.exit(0)
sys.stderr.write("gh shim: 未登记的调用: " + argv + "\\n")
sys.exit(1)
'''

# pr list 的应答（jq 计算后的行）：21、22、23 都打开且目标默认分支；eligibility 由脚本对每个 PR
# 再查 isCrossRepository/autoMergeRequest 判定——21 已开启自动合并（等同步），22 未开启，23 来自 fork。
# 后两者必须被脚本跳过：不查 mergeable、不同步。
WAITING_PRS = "21 " + "b" * 40 + "\n22 " + "c" * 40 + "\n23 " + "d" * 40
ELIGIBLE = [[f"pr view {n} --repo owner/repo --json isCrossRepository", verdict]
            for n, verdict in ((21, "yes"), (22, "no"), (23, "no"))]


class NativeAutomergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-native-automerge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---------- 夹具 ----------

    def template(self) -> dict:
        return parse_workflow(WORKFLOW.read_text(encoding="utf-8"))

    def base_env(self, project: Path, log: Path) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "GITHUB_", "HARNESS_")) and key != "CI"}
        env.update(GIT_ENV)
        env["GITHUB_WORKSPACE"] = str(project)
        env["GITHUB_REPOSITORY"] = "owner/repo"
        env["RUNNER_TEMP"] = str(self.tmp / "runner-temp")
        env["GH_SHIM_LOG"] = str(log)
        env["DEFAULT_BRANCH"] = "main"
        return env

    def context(self, **extra: str) -> dict[str, str]:
        base = {"github.event.workflow_run.pull_requests[0].number": "14",
                "github.event.workflow_run.head_sha": "a" * 40,
                "github.event.workflow_run.head_branch": "task/715-native-automerge",
                "github.event.repository.default_branch": "main",
                "github.repository_owner": "owner",
                "github.token": "job-token", "github.repository": "owner/repo",
                "needs.judge.outputs.risk": "R2", "needs.judge.outputs.class": "K5",
                "needs.judge.outputs.audit": "false", "needs.judge.outputs.pending": "",
                "steps.app.outputs.token": "app-token-1", "steps.sync-app.outputs.token": "",
                "vars[needs.judge.outputs.sync_app_client_id_var]": "",
                "vars[needs.platform.outputs.sync_app_client_id_var]": "",
                "needs.platform.outputs.sync_app_client_id_var": "HARNESS_SYNC_APP_CLIENT_ID",
                "needs.platform.outputs.sync_app_private_key_secret": "HARNESS_SYNC_APP_PRIVATE_KEY",
                "needs.platform.outputs.environment": "harness-auto-merge"}
        base.update(extra)
        return base

    def replay_job(self, job: str, context: dict[str, str], *, responses: list | None = None,
                   sync_token: str = "", retry_seconds: str = "0") -> tuple[MergeReplay, str]:
        """重放一个 job 的 run 步骤（uses 步骤记账跳过）。responses 非空时用表驱动的 gh 桩。"""
        steps = [step for step in self.template()["jobs"][job]["steps"] if "uses" not in step]
        shim = self.tmp / f"shim-{job}-{len(list(self.tmp.glob('shim-*')))}"
        shim.mkdir()
        (shim / "gh").write_text(SYNC_GH_SHIM, encoding="utf-8")
        (shim / "gh").chmod(0o755)
        log = self.tmp / f"calls-{job}-{len(list(self.tmp.glob('calls-*')))}.log"
        log.write_text("", encoding="utf-8")
        project = Path(tempfile.mkdtemp(prefix=f"proj-{job}-", dir=self.tmp))
        env = self.base_env(project, log)
        env["PATH"] = f"{shim}:{env['PATH']}"
        if responses is not None:
            env["GH_SHIM_RESPONSES"] = json.dumps(responses)
            env["MERGEABLE_RETRY_SECONDS"] = retry_seconds
        if sync_token:
            context = {**context, "steps.sync-app.outputs.token": sync_token}
        replay = MergeReplay(project, env, context)
        replay.run(steps, stub=project / "unused-stub")
        return replay, log.read_text(encoding="utf-8")

    def replay_shimmed_job(self, job: str, context: dict[str, str], **shim_env: str) -> tuple[MergeReplay, str]:
        """用 tests.test_signoff 的 gh 桩重放一个 job；shim_env 直接注入桩环境（GH_SHIM_FAIL 等）。"""
        shim, log_path = write_gh_shim(self.tmp, "0", "MERGEABLE")
        steps = [step for step in self.template()["jobs"][job]["steps"] if "uses" not in step]
        project = Path(tempfile.mkdtemp(prefix=f"proj-{job}-", dir=self.tmp))
        env = self.base_env(project, log_path)
        env["PATH"] = f"{shim}:{env['PATH']}"
        env.update(shim_env)
        replay = MergeReplay(project, env, context)
        replay.run(steps, stub=project / "unused-stub")
        return replay, log_path.read_text(encoding="utf-8")

    # ---------- 验收第 1 行：再判定最先、批准之后用同步令牌开启、不出现无 --auto 的合并 ----------

    def test_merge_app_rechecks_then_enables(self):
        # 结构断言（步骤顺序、再判定命令与 judge 逐字相同、令牌拆分、并发组、actions:read）
        # 在 tests.test_signoff.merge_app_steps 里，replay_merge_app 每次都会执行；这里回放行为。
        # 再判定通过：批准之后用同步令牌执行 --auto 开启；开启成功不出现退回合并。
        _replay, log = replay_merge_app(self.tmp, sync_token="sync-token-1")
        lines = log.splitlines()
        approve = next(index for index, line in enumerate(lines) if "pulls/14/reviews" in line)
        merges = [index for index, line in enumerate(lines) if "pr merge 14" in line]
        self.assertEqual(len(merges), 1)  # 开启成功：没有退回的不带 --auto 的合并
        self.assertGreater(merges[0], approve)  # 先批准后开启
        self.assertIn("--auto --merge --match-head-commit", lines[merges[0]])
        self.assertIn("a" * 40, lines[merges[0]])  # 绑定评估过的 head
        self.assertIn("[token=sync-token-1]", lines[merges[0]])  # 同步令牌开启（第 5 轮评审严重 2）
        # 未配置同步 App：开启用工作流令牌，批准令牌只出现在批准步骤
        _replay, log = replay_merge_app(self.tmp)
        lines = log.splitlines()
        merge_line = next(line for line in lines if "pr merge 14" in line)
        self.assertIn("[token=job-token]", merge_line)
        approve_line = next(line for line in lines if "pulls/14/reviews" in line)
        self.assertIn("[token=app-token-1]", approve_line)
        self.assertEqual([line for line in lines if "[token=app-token-1]" in line], [approve_line])

    # ---------- 验收第 2 行：退回立即合并与抽审登记 ----------

    def test_fallback_and_audit_registration(self):
        # 开启失败且 autoMergeRequest 为空：评论一次开启失败原因与设置指引，退回立即合并
        # （命令与原步骤逐字相同，令牌与开启一致），抽审议题在任何合并命令之前登记。
        replay, log = replay_merge_app(self.tmp, sync_token="sync-token-1", audit="true",
                                       merge_fail="--auto --merge", auto_merge_request="")
        self.assertEqual(replay.conclusion, "success")
        lines = log.splitlines()
        issue = next(index for index, line in enumerate(lines) if "issue create" in line)
        approve = next(index for index, line in enumerate(lines) if "pulls/14/reviews" in line)
        enable = next(index for index, line in enumerate(lines) if "--auto" in line)
        view = next(index for index, line in enumerate(lines) if "autoMergeRequest" in line)
        comment = next(index for index, line in enumerate(lines) if "pr comment 14" in line)
        fallback = next(index for index, line in enumerate(lines)
                        if "pr merge 14" in line and "--auto" not in line)
        self.assertLess(issue, approve)  # 抽审登记在批准（更在任何合并命令）之前
        self.assertLess(issue, enable)
        self.assertLess(enable, view)  # 开启失败后才查 autoMergeRequest
        self.assertLess(view, comment)
        self.assertLess(comment, fallback)
        self.assertEqual(log.count("pr comment 14"), 1)  # 只评论一次
        self.assertIn("Allow auto-merge", log)
        self.assertIn("--merge --match-head-commit", lines[fallback])  # 退回立即合并（逐字命令）
        self.assertIn("[token=sync-token-1]", lines[fallback])  # 令牌与开启一致
        # 被抽中的 PR 走开启（开启成功）也有议题，同样在合并命令之前
        _replay, log = replay_merge_app(self.tmp, sync_token="sync-token-1", audit="true")
        lines = log.splitlines()
        issue = next(index for index, line in enumerate(lines) if "issue create" in line)
        merge = next(index for index, line in enumerate(lines) if "pr merge 14" in line)
        self.assertLess(issue, merge)
        # 重复运行不重复开题：议题查询命中（jq 计数 = 1）时不再 issue create，批准与开启照常
        _replay, log = replay_merge_app(self.tmp, sync_token="sync-token-1", audit="true", issue_count="1")
        self.assertNotIn("issue create", log)
        self.assertIn("Label and approve", _replay.ran)
        self.assertIn("Enable native auto-merge", _replay.ran)
        # 开启失败但 autoMergeRequest 已非空（如上一次运行已开启）：视为成功，不退回、不评论
        replay, log = replay_merge_app(self.tmp, sync_token="sync-token-1", audit="true",
                                       merge_fail="--auto --merge", auto_merge_request="true", issue_count="1")
        self.assertEqual(replay.conclusion, "success")
        self.assertNotIn("pr comment", log)
        self.assertEqual([line for line in log.splitlines() if "pr merge 14" in line and "--auto" not in line],
                         [])  # 已开启过：不退回立即合并
        # merge-direct 同样先开启、失败再退回并评论一次；抽审议题同样先行；单账号全程工作流令牌
        context = {**self.context(), "needs.judge.outputs.audit": "true"}
        replay, log = self.replay_shimmed_job("merge-direct", context,
                                              GH_SHIM_FAIL="--auto --merge", AUTO_MERGE_REQUEST="")
        self.assertEqual(replay.conclusion, "success")
        lines = log.splitlines()
        issue = next(index for index, line in enumerate(lines) if "issue create" in line)
        enable = next(index for index, line in enumerate(lines) if "--auto" in line)
        fallback = next(index for index, line in enumerate(lines)
                        if "pr merge 14" in line and "--auto" not in line)
        self.assertLess(issue, enable)
        self.assertLess(enable, fallback)
        self.assertEqual(log.count("pr comment 14"), 1)
        self.assertIn("[token=job-token]", lines[fallback])
        self.assertIn("[token=job-token]", lines[enable])
        self.assertEqual([line for line in lines if "[token=app-token-1]" in line], [])  # 无批准 App

    # ---------- 验收第 3 行：main push 时同步等待中的 PR ----------

    def test_push_syncs_waiting_prs(self):
        # 静态断言：alert 补 workflow_run 事件条件；push 下 judge 等 job 不运行；新 job 不检出 PR head。
        auto = self.template()
        self.assertEqual(sorted(auto["on"]["push"]["branches"]), ["main"])
        self.assertIn("github.event.workflow_run", str(auto["jobs"]["judge"]["if"]))
        for name in ("merge-app", "merge-direct", "request-review"):
            self.assertIn("judge", auto["jobs"][name]["needs"], name)  # judge 跳过它们也跳过
        self.assertEqual(auto["jobs"]["alert"]["if"],
                         "${{ always() && github.event_name == 'workflow_run' }}")
        platform = auto["jobs"]["platform"]
        self.assertEqual(platform["if"], "github.event_name == 'push'")
        self.assertEqual(set(platform["permissions"].values()), {"read"})
        self.assertEqual(platform["steps"][0]["with"]["ref"],
                         "${{ github.event.repository.default_branch }}")  # 检出默认分支，不是 PR head
        cfg = next(step for step in platform["steps"] if "run" in step)
        self.assertIn("automerge platform", cfg["run"])
        self.assertEqual(sorted(platform["outputs"]),
                         ["environment", "sync_app_client_id_var", "sync_app_private_key_secret"])
        sync = auto["jobs"]["sync-waiting"]
        self.assertEqual(sync["if"], "github.event_name == 'push'")
        self.assertEqual(sync["needs"], "platform")
        self.assertEqual(sync["environment"], "${{ needs.platform.outputs.environment }}")
        self.assertEqual(set(sync["permissions"].values()), {"write"})
        self.assertEqual(sync["concurrency"], {"group": "auto-merge-push-sync", "cancel-in-progress": False})
        self.assertFalse([step for step in sync["steps"] if "checkout" in str(step.get("uses", ""))])
        for step in sync["steps"]:
            run = str(step.get("run", ""))
            for forbidden in ("git checkout", "git switch", "git clone"):
                self.assertNotIn(forbidden, run, step.get("name"))  # 不检出、不执行 PR 代码

        # 回放：落后且不冲突的等待中 PR 用同步令牌 update-branch；fork 与未开自动合并的没进候选。
        responses = [["pr list", WAITING_PRS], *ELIGIBLE,
                     ["pr view 21 --repo owner/repo --json mergeable", "MERGEABLE"],
                     [f"compare/main...{'b' * 40}", "3"]]
        _replay, log = self.replay_job("sync-waiting", self.context(), responses=responses,
                                       sync_token="sync-token-1")
        self.assertIn("pr update-branch 21", log)
        self.assertIn("[token=sync-token-1]", log.split("update-branch 21")[1].splitlines()[0])
        self.assertNotIn("pr view 22 mergeable", log)  # 未开启的没进候选
        self.assertNotIn("pr view 23 mergeable", log)  # fork 的没进候选
        self.assertNotIn("pr view 22", "".join(line for line in log.splitlines()
                                               if "isCrossRepository" not in line))

        # 冲突的等待中 PR：评论、打 escalation、关闭自动合并，不同步
        responses = [["pr list", "31 " + "e" * 40],
                     ["pr view 31 --repo owner/repo --json isCrossRepository", "yes"],
                     ["pr view 31 --repo owner/repo --json mergeable", "CONFLICTING"]]
        _replay, log = self.replay_job("sync-waiting", self.context(), responses=responses,
                                       sync_token="sync-token-1")
        self.assertIn("pr comment 31", log)
        self.assertIn("--add-label escalation", log)
        self.assertIn("pr merge 31 --repo owner/repo --disable-auto", log)
        self.assertNotIn("update-branch", log)
        disable_line = next(line for line in log.splitlines() if "--disable-auto" in line)
        self.assertIn("[token=job-token]", disable_line)  # 关闭用工作流令牌

        # mergeable 重试后仍未知：打印并跳过（重试 3 次，间隔经 MERGEABLE_RETRY_SECONDS 压缩）
        responses = [["pr list", "41 " + "f" * 40],
                     ["pr view 41 --repo owner/repo --json isCrossRepository", "yes"],
                     ["pr view 41 --repo owner/repo --json mergeable", "UNKNOWN"]]
        _replay, log = self.replay_job("sync-waiting", self.context(), responses=responses,
                                       sync_token="sync-token-1")
        self.assertEqual(log.count("pr view 41 --repo owner/repo --json mergeable"), 3)
        self.assertNotIn("update-branch", log)
        self.assertNotIn("disable-auto", log)

        # 没有等待中的 PR：只查询一次列表，无写调用
        _replay, log = self.replay_job("sync-waiting", self.context(), responses=[["pr list", ""]],
                                       sync_token="sync-token-1")
        self.assertEqual([line for line in log.splitlines() if "pr list" in line], log.splitlines())
        # 同步 App 未配置：整体跳过，没有任何 gh 调用
        _replay, log = self.replay_job("sync-waiting", self.context(), responses=[["pr list", WAITING_PRS]],
                                       sync_token="")
        self.assertEqual(log, "")

    # ---------- 验收第 5 行：judge 判「不自动合并」时 request-review 关闭自动合并 ----------

    def test_judge_reject_disables_auto_merge(self):
        job = self.template()["jobs"]["request-review"]
        self.assertEqual(job["if"], "needs.judge.outputs.auto_merge == 'false'")  # 判合并时不进该 job
        disable = 'gh pr merge "$PR" --repo "$GITHUB_REPOSITORY" --disable-auto || true'
        self.assertIn(disable, job["steps"][0]["run"])
        replay, log = self.replay_shimmed_job("request-review", self.context())
        self.assertEqual(replay.conclusion, "success")
        lines = log.splitlines()
        self.assertIn("pr merge 14 --repo owner/repo --disable-auto", lines[0])  # 关闭在最前
        self.assertIn("--add-label class:K5", log)
        self.assertIn("--add-reviewer owner", log)

    # ---------- 验收第 6 行：再判定拒绝时撤销存量请求，不批准、不开启 ----------

    def test_recheck_reject_revokes_existing_request(self):
        # judge 通过进入 merge-app，再判定拒绝（如逃逸预算新近超限）、旧请求仍在（autoMergeRequest 非空）：
        # 先撤销请求，再跳过批准与开启，job 以成功结束
        replay, log = replay_merge_app(self.tmp, auto_merge="false", auto_merge_request="true")
        self.assertIn("Revoke stale auto-merge request (recheck rejected)", replay.ran)
        self.assertNotIn("Label and approve", replay.ran)
        self.assertNotIn("Enable native auto-merge", replay.ran)
        self.assertNotIn("Register audit issue before any merge command", replay.ran)
        self.assertEqual(replay.conclusion, "success")
        self.assertIn("pr merge 14 --repo owner/repo --disable-auto", log)  # 先撤销
        self.assertNotIn("pulls/14/reviews", log)  # 不批准
        self.assertNotIn("--auto", log)  # 不开启
        # merge-app 权限含 actions:read（删除即失败）、再判定环境无 App 令牌：
        # 结构断言在 tests.test_signoff.merge_app_steps（replay_merge_app 每次执行）。
        # 再判定拒绝但请求已不存在：撤销失败被 || true 吞掉；之后只剩同步预检查的两条只读查询
        # （落后与冲突预检查保持原样，无条件运行），没有任何写调用
        _replay, log = replay_merge_app(self.tmp, auto_merge="false")
        lines = log.splitlines()
        disable = next(index for index, line in enumerate(lines) if "--disable-auto" in line)
        self.assertEqual([line for line in lines[disable + 1:]
                          if not ("compare/" in line or "--json mergeable" in line)], [])


if __name__ == "__main__":
    unittest.main()
