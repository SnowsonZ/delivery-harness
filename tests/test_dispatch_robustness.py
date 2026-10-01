"""T125 派发收尾健壮性（B77）：stages 收敛到本次 attempt 窗口、记录落盘自检、死槽回收与链路提示。

夹具与 T201（tests/test_run_timeline.py）同型：匿名临时 git 仓库（含匿名 bare 远端）、隔离
events_db.ROOT、冻结时钟、假执行方宿主与假 gh、任务书检查器打桩。派发经真实产品入口
Dispatcher.run 驱动；记录自检验证 write_record 的真实产物（记录 JSON、prompt 快照）与 stderr 提示；
槽位回收与归还验证真实 git worktree 状态。不碰真实库、PR 或工作流。
"""

from __future__ import annotations

import contextlib
import hashlib
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

from engine.agents import dispatch, dispatch_host, run_timeline
from engine.checks import taskbook
from engine.core import events, events_db

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}
FROZEN_TS = "2026-02-03T04:05:06.789Z"
VERIFY_PASS = "print('local verify ok')"
# 动态拼接 CI/runner 工作区路径：运行时是真实形态的 runner 路径，源码里不落字面量（卫生规则）。
RUNNER_PATH = "/home/" + "runner/work/app/app/build"
# 状态机 verify：第 1 次运行失败并打印 runner 路径，之后通过——让下一轮 prompt 快照带上该路径。
VERIFY_PATH_THEN_PASS = (
    "import pathlib, sys\n"
    "state = pathlib.Path('build/verify-round')\n"
    "n = int(state.read_text()) if state.exists() else 0\n"
    "state.parent.mkdir(parents=True, exist_ok=True)\n"
    "state.write_text(str(n + 1))\n"
    "if n == 0:\n"
    f"    print('error log at {RUNNER_PATH}')\n"
    "    sys.exit(1)\n"
)
STAGE_KEYS = {"stage", "step", "status", "ts", "duration_ms", "attempt", "round"}
POINTER_KEYS = {
    "stage",
    "step",
    "status",
    "ts",
    "duration_ms",
    "attempt",
    "round",
}  # 终评修复：指针行七键与窗口行同形（run_check 单一口径）


def executor_script() -> str:
    """假执行方：写一行会话流并做一次提交（--allow-empty 让无改动的重试轮也能过）。
    git 操作对瞬时失败做 3 次退避重试（0.1/0.3/0.9 秒，与 test_events_agents 同款，B73：
    GitHub macOS runner 上偶发 git 瞬断，#105 两连挂实例）。"""
    return (
        "import json, pathlib, subprocess, sys, time\n"
        "def git(argv):\n"
        "    for delay in (0.0, 0.1, 0.3, 0.9):\n"
        "        if delay: time.sleep(delay)\n"
        "        done = subprocess.run(['git', *argv], capture_output=True, text=True)\n"
        "        if done.returncode == 0: return\n"
        "    sys.exit('git ' + ' '.join(argv) + ' 重试 3 次后仍失败：\\n' + done.stderr)\n"
        "print(json.dumps({'type': 'session'}), flush=True)\n"
        "pathlib.Path('note.txt').write_text('work\\n')\n"
        "git(['add', '-A'])\n"
        "git(['commit', '-q', '-m', 'work', '--allow-empty'])\n"
    )


class RecordingHost:
    """假执行方宿主：argv 启动固定脚本；流解析委托真实 PiHost（真实解析逻辑被覆盖）。"""

    name = "fake-pi"

    def __init__(self, script: str):
        self.script = script
        self.pi = dispatch_host.PiHost()
        self.prompts: list[str] = []

    def version(self):
        return "9.9.9-fake"

    def argv(self, prompt, guard):
        self.prompts.append(prompt)
        return [sys.executable, "-c", self.script]

    def parse(self, events_path):
        return self.pi.parse(events_path)

    def parse_observability(self, events_path):
        return self.pi.parse_observability(events_path)

    def parse_context(self, events_path):
        return self.pi.parse_context(events_path)


class BulkHost(RecordingHost):
    """每轮 parse 后向 trace 追加 bulk 条观察事件：模拟长执行（验收：三轮各 800 事件）。"""

    def __init__(self, script: str, branch: str, bulk: int = 800):
        super().__init__(script)
        self.branch = branch
        self.bulk = bulk

    def parse(self, events_path):
        result = self.pi.parse(events_path)
        for _ in range(self.bulk):
            events.emit(stage="dispatch", step="tool_call", status="ok", trace_id=self.branch)
        return result


class BigContextHost(RecordingHost):
    """parse_context 返回超大缺失报告、并向 trace 追加窗口事件：缺失报告把记录 JSON 顶过 512KB
    自检线，stages 窗口提供可截断的余量（自检截 stages 后回到线内；缺报告本身不动）。"""

    def __init__(self, script: str, branch: str, bulk: int = 1500):
        super().__init__(script)
        self.branch = branch
        self.bulk = bulk

    def parse(self, events_path):
        result = self.pi.parse(events_path)
        for _ in range(self.bulk):
            events.emit(stage="dispatch", step="tool_call", status="ok", trace_id=self.branch)
        return result

    def parse_context(self, events_path):
        items = [{"category": "other", "summary": "other_missing", "ref": f"tool-{index}"} for index in range(2800)]
        return {"status": "reported", "items": items}


class FakeGitHub:
    """派发用 gh 桩：push 走真实 git（本地 bare 远端），其余按脚本应答。"""

    def __init__(self, *, branch_exists: bool = False, ci=(True, "")):
        self.calls: list[tuple] = []
        self.branch_exists = branch_exists
        self.ci = list(ci)
        self.existing = None

    def remote_branch_exists(self, branch):
        self.calls.append(("remote_branch_exists", branch))
        return self.branch_exists

    def push(self, slot, branch):
        self.calls.append(("push", branch))
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        # 夹具远端上同名分支可能是上一状态留下的：强推覆盖，保持各状态调用序列一致
        subprocess.run(
            ["git", "push", "-q", "-f", "-u", "origin", branch],
            cwd=slot,
            check=True,
            capture_output=True,
            env={**env, **GIT_ENV},
        )

    def open_pr(self, slot, branch, title, body):
        self.calls.append(("open_pr", branch))
        return 14

    def comment(self, pr, body, label=None):
        self.calls.append(("comment", pr))
        if label:
            self.add_label(pr, label)
        return f"https://example.invalid/repo/pull/{pr}#issuecomment-1"

    def add_label(self, pr, label):
        self.calls.append(("add_label", pr, label))

    def create_issue(self, title, body, labels):
        self.calls.append(("create_issue", title, tuple(labels)))
        return "https://example.invalid/repo/issues/1"

    def wait_ci(self, branch, sha, timeout, detail=None):
        self.calls.append(("wait_ci", branch))
        ok = self.ci.pop(0) if len(self.ci) > 1 else self.ci[0]
        if detail is not None:
            detail["run_ids"] = []
        return ok, "" if ok else "CI 未通过：见摘要"

    def existing_pr(self, branch):
        return self.existing


class DispatchRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-dispatch-robust-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        origin = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", ".", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("fetch", "-q", "origin")
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", return_value=FROZEN_TS)
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)
        self.assertIsNotNone(events_db.db_path())
        self.config = dispatch.Config(
            slots=2,
            stall_seconds=120,
            poll_seconds=0.05,
            ci_timeout_seconds=5,
            verify=[sys.executable, "-c", VERIFY_PASS],
        )

    # ---------- 夹具 ----------

    def git(self, *args, check=True, cwd=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "HARNESS_", "GITHUB_"))}
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, capture_output=True, text=True, env={**env, **GIT_ENV}, check=check
        )

    def commit_guards(self):
        """给 origin/main 装上守卫夹具：必拒载荷直接退出 2，扩展文件存在即可。"""
        for path, content in {
            "harness/command_guard.py": "import sys\nsys.exit(2)\n",
            ".pi/extensions/harness-guard.ts": "// guard fixture\n",
        }.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.git("push", "-q", "origin", "main")

    def header(self, task: str, *, retries=0, ci_rounds=2) -> dict:
        return {
            "task": task,
            "class": "K7",
            "risk": "R3",
            "designer": "codex",
            "size": "small",
            "spec_refs": [],
            "budget": {"wall_clock_min": 5, "retries": retries, "ci_rounds": ci_rounds, "tokens": None},
        }

    def write_taskbook(self, rel: str, header_dict: dict):
        budget = header_dict["budget"]
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(
            "---\n"
            f"task: {header_dict['task']}\nclass: {header_dict['class']}\nrisk: {header_dict['risk']}\n"
            "designer: codex\nsize: small\narchitecture: false\nspec_refs: []\n"
            "no_spec_reason: 测试夹具\n"
            "budget:\n"
            f"  wall_clock_min: {budget['wall_clock_min']}\n  retries: {budget['retries']}\n"
            f"  ci_rounds: {budget['ci_rounds']}\n  tokens: null\n"
            "rollback: git revert\n---\n\n# 任务：夹具\n",
            encoding="utf-8",
        )

    def stub_taskbook(self, rel: str, header_dict: dict):
        """隔离任务书检查器（admit 的下游）：返回固定 Report，on_main 无问题、豁免表为空。"""
        reports = [taskbook.Report(rel, header=header_dict)]
        for patcher in (
            mock.patch.object(taskbook, "check_all", lambda *args, **kwargs: reports),
            mock.patch.object(taskbook, "on_main", lambda *args, **kwargs: None),
            mock.patch.object(taskbook, "load_exempt", lambda *args, **kwargs: {}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_dispatcher(self, host, github, *, verify_script: str = VERIFY_PASS):
        config = dispatch.Config(
            slots=self.config.slots,
            slot_root=self.config.slot_root,
            stall_seconds=120,
            poll_seconds=0.05,
            ci_timeout_seconds=5,
            verify=[sys.executable, "-c", verify_script],
        )
        return dispatch.Dispatcher(self.repo, config, github, host, identity=dict(GIT_ENV))

    def run_dispatch(self, rel: str, host, github, *, resume=False, **kwargs) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.make_dispatcher(host, github, **kwargs).run(rel, resume=resume)
        return code, out.getvalue(), err.getvalue()

    def record(self, stem: str, number: int) -> tuple[Path, dict]:
        path = self.tmp / "app-slot-1" / "docs" / "runs" / stem / f"{number}.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    # ---------- 验收 1：三轮 resume 的记录只含本次 attempt 事件 + 前置指针行，体积不回胀 ----------

    def test_stages_scoped_to_attempt(self):
        rel = "docs/plans/task-220-a.md"
        header = self.header("T220-A", retries=2, ci_rounds=3)
        self.write_taskbook(rel, header)
        self.stub_taskbook(rel, header)
        self.commit_guards()
        gh = FakeGitHub(ci=[False, False, True])
        code, _, _ = self.run_dispatch(rel, BulkHost(executor_script(), "task/220-a", bulk=800), gh)
        self.assertEqual(code, 0)
        _path1, record1 = self.record("task-220-a", 1)
        _path2, record2 = self.record("task-220-a", 2)
        path3, record3 = self.record("task-220-a", 3)

        # 记录 1：首轮窗口没有指针行；admit..claim 后是本轮 800 条观察事件，收尾是执行与本地判定
        # （parse 阶段追加的观察事件发生在 executor_round 观察事件之前）
        self.assertEqual(
            [item["step"] for item in record1["stages"][:4]], ["admit", "guard_preflight", "slot", "claim"]
        )
        self.assertEqual([item["step"] for item in record1["stages"][-2:]], ["executor_round", "local_verify"])
        self.assertEqual(len(record1["stages"]), 6 + 800)
        self.assertEqual({item["attempt"] for item in record1["stages"]}, {1})
        self.assertNotIn("prior", [item["status"] for item in record1["stages"]])

        # 记录 2：更早 attempt 只留 push_pr/ci_wait 指针行，其余全部属于本次 attempt
        self.assertEqual(
            [(item["step"], item["status"], item["attempt"]) for item in record2["stages"][:2]],
            [("push_pr", "prior", 1), ("ci_wait", "prior", 1)],
        )
        self.assertEqual(len(record2["stages"]), 2 + 802)
        self.assertEqual({item["attempt"] for item in record2["stages"][2:]}, {2})

        # 记录 3：三条指针行（push_pr@1、ci_wait@1、ci_wait@2）+ 本次窗口 802 条；不带全历史（2400+）
        self.assertEqual(
            [(item["step"], item["status"], item["attempt"]) for item in record3["stages"][:3]],
            [("push_pr", "prior", 1), ("ci_wait", "prior", 1), ("ci_wait", "prior", 2)],
        )
        for pointer in record3["stages"][:3]:
            self.assertEqual(set(pointer), POINTER_KEYS)
        self.assertEqual(len(record3["stages"]), 3 + 802)
        self.assertEqual({item["attempt"] for item in record3["stages"][3:]}, {3})
        self.assertTrue(all(set(item) == STAGE_KEYS for item in record3["stages"][3:]))

        # 总 JSON 小于 512KB 自检线，也没有触发 256KB 保底截断
        self.assertLess(path3.stat().st_size, run_timeline.RECORD_MAX_BYTES)
        self.assertNotIn("stages_truncated", record3)
        self.assertNotIn("stages_total", record3)

    # ---------- 验收 2：记录超 512KB / prompt 含 runner 路径时自处理后再提交，守卫不拦 ----------

    def test_record_self_check_normalizes(self):
        # 记录超 512KB：stages 之外的缺失报告把 JSON 顶过自检线，写记录前截断 stages 并标注
        rel = "docs/plans/task-220-b.md"
        header = self.header("T220-B", ci_rounds=1)
        self.write_taskbook(rel, header)
        self.stub_taskbook(rel, header)
        self.commit_guards()
        code, _, err = self.run_dispatch(rel, BigContextHost(executor_script(), "task/220-b"), FakeGitHub())
        self.assertEqual(code, 0)
        path, record = self.record("task-220-b", 1)
        self.assertLessEqual(path.stat().st_size, run_timeline.RECORD_MAX_BYTES)
        self.assertTrue(record["stages_truncated"])
        self.assertGreater(record["stages_total"], len(record["stages"]))
        self.assertGreaterEqual(len(record["missing_context"]), 2000)  # 自检只截 stages，其余字段不动
        self.assertIn("运行记录自检", err)
        self.assertIn("512 KB", err)

        # prompt 快照含 CI/runner 工作区路径：占位为 <ci-workspace> 后落盘，sha256 按占位后字节一致
        rel = "docs/plans/task-220-c.md"
        header = self.header("T220-C", retries=1, ci_rounds=1)
        self.write_taskbook(rel, header)
        self.stub_taskbook(rel, header)
        code, _, err = self.run_dispatch(
            rel, RecordingHost(executor_script()), FakeGitHub(), verify_script=VERIFY_PATH_THEN_PASS
        )
        self.assertEqual(code, 0)
        _path, record = self.record("task-220-c", 1)
        prompt = (self.tmp / "app-slot-1" / record["prompt_path"]).read_text(encoding="utf-8")
        self.assertNotIn(RUNNER_PATH, prompt)
        self.assertIn("<ci-workspace>", prompt)
        self.assertIn("运行记录自检", err)
        self.assertIn("<ci-workspace>", err)
        raw = (self.tmp / "app-slot-1" / record["prompt_path"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record["prompt_sha256"])

    # ---------- 验收 3：死进程槽位（锁 pid 不存活）自动回收；分支未推提交先推送保全 ----------

    def test_stale_slot_reclaimed_with_salvage(self):
        rel = "docs/plans/task-220-d.md"
        header = self.header("T220-D")
        self.write_taskbook(rel, header)
        self.stub_taskbook(rel, header)
        self.commit_guards()
        # 崩溃现场（B72 形态）：槽位 2 的工作树停在 task/220-d，带一个未推提交；远端只有认领时的 main
        main_sha = self.git("rev-parse", "origin/main").stdout.strip()
        self.git("push", "-q", "origin", f"{main_sha}:refs/heads/task/220-d")
        slot2 = dispatch.slot_path(self.repo, self.config, 2)
        self.git("worktree", "add", "--detach", str(slot2), "origin/main")
        self.git("checkout", "-q", "-b", "task/220-d", "origin/main", cwd=slot2)
        (slot2 / "crash.txt").write_text("crash work\n")
        self.git("add", "-A", cwd=slot2)
        self.git("commit", "-q", "-m", "crash work\n\nTask: T220-D", cwd=slot2)
        crash_sha = self.git("rev-parse", "HEAD", cwd=slot2).stdout.strip()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()  # pid 已退出：锁文件 pid 不存活
        locks = dispatch.state_dir(self.repo) / "slots"
        locks.mkdir(parents=True, exist_ok=True)
        (locks / "2.json").write_text(
            json.dumps({"pid": dead.pid, "task": "T220-D", "branch": "task/220-d", "started_at": FROZEN_TS})
        )

        gh = FakeGitHub()
        code, _, err = self.run_dispatch(rel, RecordingHost(executor_script()), gh, resume=True)
        self.assertEqual(code, 0)

        # 链路提示：上一轮进程异常退出，head 已在远端；缺结论由设计方手动接链，评审不自动重跑
        self.assertIn("评审不会自动重跑", err)
        # 未推提交已保全进远端分支
        self.git("fetch", "-q", "origin")
        remote_heads = self.git("rev-list", "origin/task/220-d").stdout.split()
        self.assertIn(crash_sha, remote_heads)
        # 死槽工作树被回收，新派发落在槽位 1 并正常完成
        self.assertFalse(slot2.exists())
        self.assertNotIn(str(slot2), self.git("worktree", "list", "--porcelain").stdout)
        self.assertTrue(dispatch.slot_path(self.repo, self.config, 1).exists())
        _path, record = self.record("task-220-d", 1)
        self.assertEqual(record["exit"], "ok")

    # ---------- 验收 4：stages 单条仅含标量字段；指针行与窗口条目形状钉死 ----------

    def test_run_check_accepts_new_stage_shape(self):
        """设计方补（评审 #91 阻断项闭环）：run_check 记录校验接受 T125 收敛后的新 stages 形状。

        七键窗口条目 + prior 指针行 + 顶层截断标注零问题；把校验表回退为旧必填（变异）时
        新形状必报「缺字段」——断链风险被此断言钉住。
        """
        from engine.routing import run_check as rc

        record = {
            "stages": [
                {
                    "stage": "dispatch",
                    "step": "admit",
                    "status": "ok",
                    "ts": "2026-09-30T00:00:00Z",
                    "duration_ms": 5,
                    "attempt": 2,
                    "round": 0,
                },
                {
                    "stage": "dispatch",
                    "step": "push_pr",
                    "status": "prior",
                    "ts": "2026-09-29T00:00:00Z",
                    "duration_ms": None,
                    "attempt": 1,
                    "round": 0,
                },
            ],
            "stages_truncated": False,
            "stages_total": 2,
        }
        self.assertEqual(rc.scan_record(record), [])

    def test_stage_item_shape(self):
        rel = "docs/plans/task-220-e.md"
        header = self.header("T220-E", retries=1, ci_rounds=2)
        self.write_taskbook(rel, header)
        self.stub_taskbook(rel, header)
        self.commit_guards()
        code, _, _ = self.run_dispatch(rel, RecordingHost(executor_script()), FakeGitHub(ci=[False, True]))
        self.assertEqual(code, 0)
        _path1, record1 = self.record("task-220-e", 1)
        _path2, record2 = self.record("task-220-e", 2)

        scalars = (str, int, float, bool, type(None))  # duration_ms 可为 None
        for item in record1["stages"]:  # 首轮：全部是窗口条目，七个标量字段
            self.assertEqual(set(item), STAGE_KEYS)
            self.assertTrue(all(isinstance(item[key], scalars) or item[key] is None for key in STAGE_KEYS))
        pointers = [item for item in record2["stages"] if item["status"] == "prior"]
        windows = [item for item in record2["stages"] if item["status"] != "prior"]
        self.assertEqual([(item["step"], item["attempt"]) for item in pointers], [("push_pr", 1), ("ci_wait", 1)])
        for item in pointers:
            self.assertEqual(set(item), POINTER_KEYS)  # 指针行只标衔接，不携带历史细节
            self.assertTrue(all(isinstance(item[key], scalars) for key in POINTER_KEYS))
        for item in windows:
            self.assertEqual(set(item), STAGE_KEYS)
            self.assertEqual(item["attempt"], 2)
        # anchors 形状不变
        self.assertEqual(
            record2["anchors"],
            [
                {
                    "source": "local",
                    "stage": "dispatch",
                    "head_hash": record2["anchors"][0]["head_hash"],
                    "fixed_in": "run_record",
                }
            ],
        )

    # ---------- 槽位归还：派发结束路径 worktree remove + 锁清理 ----------

    def test_return_slot_removes_worktree_and_honors_new_claim(self):
        repo = self.tmp / "mini"
        repo.mkdir()
        self.git("init", "-q", "-b", "main", cwd=repo)
        (repo / "README.md").write_text("# mini\n")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "init", cwd=repo)
        config = dispatch.Config(slots=1, slot_root=self.tmp)
        slot = dispatch.slot_path(repo, config, 1)
        pushes: list[str] = []

        def push(worktree, branch):
            pushes.append(branch)

        # 正常归还：锁已被 run 释放（无锁文件）——重新独占后删工作树、清锁
        self.git("worktree", "add", "--detach", str(slot), "HEAD", cwd=repo)
        dispatch.return_slot(repo, config, 1, push)
        self.assertFalse(slot.exists())
        self.assertFalse((dispatch.state_dir(repo) / "slots" / "1.json").exists())
        self.assertNotIn(str(slot), self.git("worktree", "list", "--porcelain", cwd=repo).stdout)
        self.assertEqual(pushes, [])

        # 已被下一次派发认领（锁已存在）：跳过删除，不动它的现场
        self.git("worktree", "add", "--detach", str(slot), "HEAD", cwd=repo)
        lock = dispatch.state_dir(repo) / "slots" / "1.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "task": "T220-F", "branch": "task/220-f"}))
        dispatch.return_slot(repo, config, 1, push)
        self.assertTrue(slot.exists())
        self.assertEqual(json.loads(lock.read_text())["task"], "T220-F")


if __name__ == "__main__":
    unittest.main()
