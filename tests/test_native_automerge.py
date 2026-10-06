"""T715 原生自动合并（B102）：merge-app 开启前再判定→批准→开启→退回、抽审议题在任何合并命令之前
幂等登记、main push 时同步等待中的 PR、request-review 关闭自动合并、再判定拒绝时撤销存量请求。

夹具沿用 tests/test_signoff.py 的模式：模板解析与步骤重放（gh 桩未登记的子命令直接失败，一般 7）；
不碰真实库/PR，不检出、不执行 PR 代码。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch, review, signoff
from engine.core import alerts, common, events_db
from engine.routing import signals
from tests.test_ci_events_workflows import parse_workflow
from tests.test_dispatch_alerts import (
    GIT_ENV as DISPATCH_GIT_ENV,
)
from tests.test_dispatch_alerts import (
    VERIFY_PASS,
    RecordingHost,
    executor_script,
    stream,
)
from tests.test_dispatch_alerts import FakeGitHub as DispatchFakeGitHub
from tests.test_signoff import (
    BRANCH,
    CHECKS_TOML,
    GIT_ENV,
    LOGIN,
    PR,
    RULES_TOML,
    TASKBOOK,
    FakeReviewer,
    MergeReplay,
    ReviewRetriggerGitHub,
    replay_merge_app,
    verdict_script,
    write_gh_shim,
)

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


class SequenceReviewGitHub(ReviewRetriggerGitHub):
    """在 ReviewRetriggerGitHub 之上记录 comment 与 disable 的先后（T715：评论之后才关闭）。"""

    def __init__(self, pr_json: dict, runs: dict | None = None):
        super().__init__(pr_json, runs)
        self.sequence: list[str] = []

    def comment(self, pr, body, label=None):
        self.sequence.append("comment")
        return super().comment(pr, body, label)

    def disable_auto_merge(self, pr):
        self.sequence.append("disable")
        return super().disable_auto_merge(pr)


class NegativeSignalsTest(unittest.TestCase):
    """T715 验收第 4 行：否决信号关闭自动合并（引擎侧），判据与 signals 共用同一纯函数。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-negative-signals-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        (self.repo / "docs/plans").mkdir(parents=True)
        (self.repo / "docs/plans/task-902-signoff.md").write_text(TASKBOOK, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        origin = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", ".", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("fetch", "-q", "origin")
        config = self.tmp / "config"
        config.mkdir()
        (config / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (config / "checks.toml").write_text(CHECKS_TOML, encoding="utf-8")
        for name, target in (("CONFIG_DIR", config), ("RULES_PATH", config / "rules.toml")):
            patch = mock.patch.object(common, name, target)
            patch.start()
            self.addCleanup(patch.stop)
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)

    # ---------- 夹具 ----------

    def git(self, *args, check=True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_", "GITHUB_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def pr_head(self) -> tuple[str, dict]:
        """任务分支夹具：任务书已在 main 上，分支上一个实现提交，推为 origin 的任务分支与 PR head。"""
        self.git("checkout", "-q", "-b", BRANCH)
        (self.repo / "engine").mkdir(exist_ok=True)
        (self.repo / "engine" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "实现\n\nTask: T902")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}", f"HEAD:refs/pull/{PR}/head")
        self.git("checkout", "-q", "main")
        return head, {"title": "T902：夹具", "body": "描述", "headRefOid": head,
                      "headRefName": BRANCH, "baseRefName": "main", "comments": []}

    def run_review(self, pr_json: dict, fake: FakeReviewer) -> SequenceReviewGitHub:
        gh = SequenceReviewGitHub(pr_json)
        with mock.patch.object(review, "make_reviewer", lambda name: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review.review_pr(PR, "opencode", root=self.repo, github=gh), 0)
        return gh

    # ---------- 验收第 4 行：评审否决各形态关闭自动合并，全部通过不调用 ----------

    def test_review_negative_verdicts_disable_auto_merge(self):
        _head, pr_json = self.pr_head()
        # 不通过：评论之后关闭一次
        gh = self.run_review(pr_json, FakeReviewer(verdict_script("不通过", "[]")))
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(gh.disabled, [PR])
        self.assertEqual(gh.sequence, ["comment", "disable"])
        # 「需用户验收」（评审输出没有可解析的结论）：同样关闭
        gh = self.run_review(pr_json, FakeReviewer("print('评审输出没有结论 JSON')"))
        self.assertEqual(gh.disabled, [PR])
        self.assertEqual(gh.sequence, ["comment", "disable"])
        # 「通过」但带严重发现（flagged）：同样关闭
        findings = json.dumps([{"severity": "严重", "location": "engine/app.py",
                                "problem": "问题", "fix": "修"}], ensure_ascii=False)
        gh = self.run_review(pr_json, FakeReviewer(verdict_script("通过", findings)))
        self.assertEqual(gh.disabled, [PR])
        # 评审与复核判据共用：关闭与否与 signals.review_status 对同一标记的判定一致
        body = gh.comments[0][1]
        marker = json.loads(body.split(review.REVIEW_MARK, 1)[1].split(" -->", 1)[0])
        self.assertEqual(signals.review_marker_status(marker)[0], "fail")
        # 全部通过：不调用
        gh = self.run_review(pr_json, FakeReviewer(verdict_script("通过", "[]")))
        self.assertEqual(gh.disabled, [])
        self.assertEqual(gh.sequence, ["comment"])

    # ---------- 验收第 4 行：复核否决关闭；矛盾「通过」仍拒绝且零调用 ----------

    def test_signoff_negative_verdict_disables_auto_merge(self):
        _head, pr_view = self.pr_head()
        body_file = self.tmp / "signoff-body.md"
        body_file.write_text("复核正文。\n", encoding="utf-8")
        # 不通过：评论之后关闭一次
        gh = SequenceReviewGitHub(pr_view)
        argv = [str(PR), "--verdict", "不通过", "--mutations", "0", "--caught", "0",
                "--body-file", str(body_file)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(signoff.main(argv, root=self.repo, github=gh), 0)
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(gh.disabled, [PR])
        self.assertEqual(gh.sequence, ["comment", "disable"])
        # 通过且证据一致：不调用（重判路径照旧，不在此断言）
        pr_view_with_comments = {**pr_view, "comments": gh._comments()}
        gh = SequenceReviewGitHub(pr_view_with_comments,
                                  runs={"pull_request": [{"databaseId": 1, "status": "completed"}]})
        argv = [str(PR), "--verdict", "通过", "--mutations", "2", "--caught", "2",
                "--body-file", str(body_file)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(signoff.main(argv, root=self.repo, github=gh), 0)
        self.assertEqual(gh.disabled, [])
        # 矛盾的「通过」：拒绝发布，零评论、零关闭、零 gh 调用（既有拒绝行为不变）
        gh = SequenceReviewGitHub(pr_view)
        argv = [str(PR), "--verdict", "通过", "--mutations", "0", "--caught", "0",
                "--body-file", str(body_file)]
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(signoff.main(argv, root=self.repo, github=gh), 2)
        self.assertEqual(gh.comments, [])
        self.assertEqual(gh.disabled, [])
        self.assertEqual(gh.calls, [])

    # ---------- 验收第 4 行：判据纯函数覆盖全部非法标记 ----------

    def test_marker_status_pure_functions(self):
        # 评审：结论不是「通过」、flagged 非 false 都是 fail；通过且 flagged 恰为 false 才是 ok
        for data, expect in (({"verdict": "不通过", "flagged": True}, "fail"),
                             ({"verdict": "需用户验收", "flagged": False}, "fail"),
                             ({"verdict": "通过", "flagged": True}, "fail"),
                             ({"verdict": "通过"}, "fail"),
                             ({"verdict": "通过", "flagged": None}, "fail"),
                             ({"verdict": "通过", "flagged": "false"}, "fail"),
                             ({"verdict": "通过", "flagged": False}, "ok")):
            with self.subTest(review=data):
                self.assertEqual(signals.review_marker_status(data)[0], expect)
        # 复核：结论不通过、mutations 缺失或 < 1、caught != mutations 都是 fail
        for data, expect in (({"verdict": "不通过", "mutations": 2, "caught": 2}, "fail"),
                             ({"verdict": "通过"}, "fail"),
                             ({"verdict": "通过", "mutations": 2}, "fail"),
                             ({"verdict": "通过", "mutations": 0, "caught": 0}, "fail"),
                             ({"verdict": "通过", "mutations": 2, "caught": 1}, "fail"),
                             ({"verdict": "通过", "mutations": True, "caught": True}, "fail"),
                             ({"verdict": "通过", "mutations": 1, "caught": 1}, "ok")):
            with self.subTest(signoff=data):
                self.assertEqual(signals.signoff_marker_status(data)[0], expect)
        # 状态函数与纯函数同一判据：对同一条标记数据，结论一致
        head, _ = self.pr_head()  # 标记的 head 需要指向当前 head
        data = {"verdict": "通过", "head": head, "designer": "codex", "mutations": 1, "caught": 0}
        comment = {"author": {"login": LOGIN},
                   "body": f"正文\n{signals.SIGNOFF_MARK}{json.dumps(data, ensure_ascii=False)} -->\n"}
        self.assertEqual(signals.signoff_status([comment], LOGIN, head, "origin/main", self.repo)[0],
                         signals.signoff_marker_status(data)[0])

    # ---------- 验收第 4 行：打 budget-exceeded 标签之后关闭 ----------

    def test_budget_exceeded_disables_auto_merge(self):
        header = {"task": "T915B", "class": "K7", "risk": "R3", "designer": "codex", "size": "small",
                  "spec_refs": [],
                  "budget": {"wall_clock_min": 5, "retries": 0, "ci_rounds": 1, "tokens": None}}
        rel = "docs/plans/task-915-budget.md"
        budget = header["budget"]
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(
            "---\n"
            f"task: {header['task']}\nclass: {header['class']}\nrisk: {header['risk']}\n"
            "designer: codex\nsize: small\narchitecture: false\nspec_refs: []\n"
            "no_spec_reason: 测试夹具\nbudget:\n"
            f"  wall_clock_min: {budget['wall_clock_min']}\n  retries: {budget['retries']}\n"
            f"  ci_rounds: {budget['ci_rounds']}\n  tokens: null\n"
            "rollback: git revert\n---\n\n# 任务：夹具\n", encoding="utf-8")
        from engine.checks import taskbook
        reports = [taskbook.Report(rel, header=header)]
        for patcher in (mock.patch.object(taskbook, "check_all", lambda *args, **kwargs: reports),
                        mock.patch.object(taskbook, "on_main", lambda *args, **kwargs: None),
                        mock.patch.object(taskbook, "load_exempt", lambda *args, **kwargs: {}),
                        mock.patch.object(alerts, "load_rules", return_value={}),
                        mock.patch.object(dispatch, "prepare_guard",
                                          return_value=(Path("guard.ts"), "abc123"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        gh = DispatchFakeGitHub(ci=(False, "CI 未通过：见摘要", [5]))
        config = dispatch.Config(slots=2, stall_seconds=120, poll_seconds=0.05, ci_timeout_seconds=5,
                                 verify=[sys.executable, "-c", VERIFY_PASS])
        host = RecordingHost(executor_script(stream("done")))
        with contextlib.redirect_stdout(io.StringIO()):
            code = dispatch.Dispatcher(self.repo, config, gh, host,
                                       identity=dict(DISPATCH_GIT_ENV)).run(rel)
        self.assertEqual(code, 1)  # 预算超限有序退出
        calls = gh.calls
        self.assertIn(("add_label", 14, "budget-exceeded"), calls)
        self.assertIn(("disable_auto_merge", 14), calls)
        self.assertGreater(calls.index(("disable_auto_merge", 14)),
                           calls.index(("add_label", 14, "budget-exceeded")))  # 标签之后


if __name__ == "__main__":
    unittest.main()
