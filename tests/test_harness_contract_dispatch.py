"""消费方 harness 契约测试（B42，T118）：派发契约——成功链写记录开 PR、CI 失败带摘要重试、CI 超预算升级。

蓝本：Agent-Notification main 的 tests/test_harness_dispatch.py（DispatchTest 的三个契约场景），按本仓库
当前引擎逐项核对接口后移植；夹具改用本仓库既有模式（tests/test_events_agents.py）：匿名临时 git 仓库、
隔离 events_db.ROOT、冻结时钟、假执行方宿主与假 gh，派发链路经真实 Dispatcher.run 驱动。本仓库单独
verify 即可运行，不依赖检出消费方仓库。

与蓝本的形状差异（逐条列明；照抄蓝本形状在本仓库引擎上会失败或行为不同）：
1. 导入位置：蓝本从消费方仓库的 `.harness/engine` 导入引擎（安装形态）；本仓库是引擎仓库，从仓库根
   `engine/` 导入。
2. `FakeGitHub.wait_ci` 增加第 4 个形参 `detail`：本仓库引擎以 `wait_ci(branch, sha, timeout, detail)`
   调用，并把结论对应的 Actions 运行 ID 写进 detail（ci_wait 观察事件用）；蓝本只有 3 个形参。
3. `Dispatcher` 显式传 `identity`：夹具项目没有 `.harness/config/checks.toml`，缺省的 agent_identity()
   会因缺少 [identity] agent_login 而报错；蓝本的消费方仓库装了这份配置。
4. setUp 额外隔离 `events_db.ROOT` 并冻结 `events_db._now`：本仓库派发链路经 dispatch_observation 写
   观察事件（B46 T105）、经 run_timeline 在运行记录里组装时间线（T201），事件库须落进夹具仓库的
   git 公共目录（.git/harness/），不碰本仓库真实库；蓝本所对齐的引擎版本还没有这两条链路。
5. 运行记录由本仓库引擎生成，除蓝本断言的字段外还含 trace_id/stages/anchors（T201）与
   missing_context*（T202）等新增字段；蓝本断言原样保留，全部在此通过。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch
from engine.core import events_db

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
FROZEN_TS = "2026-02-03T04:05:06.789Z"

TASKBOOK = """---
task: T005
class: K2
risk: R0
designer: claude-code
size: small
architecture: false
spec_refs: [DR14]
budget:
  wall_clock_min: 5
  ci_rounds: {ci_rounds}
  retries: 2
  tokens: null
rollback: git revert
---

# 任务：补一个测试

## 目标终态

DR14 有测试。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或验证步骤） |
|---|---|---|---|
| DR14 | 合并取较大值 | 单测 | `test_mod.Case` |
"""

SPEC = "| 编号 | 内容 | 证据类型 | 覆盖 |\n|---|---|---|---|\n| DR14 | 合并 | 单测 | `test_mod` |\n"

# 假执行方：按模式在槽位里做事。
FAKE = textwrap.dedent('''
    import subprocess, sys, time
    from pathlib import Path
    mode = sys.argv[1]
    def commit(name, text):
        Path(name).write_text(text)
        subprocess.run(["git", "add", name], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "step\\n\\nTask: T005"], check=True)
    print('{"type": "session"}', flush=True)
    if mode == "commit":
        n = len(list(Path(".").glob("tests_new*.txt")))
        commit(f"tests_new{n}.txt", "ok")
    elif mode == "bad":
        n = len(list(Path(".").glob("bad*.txt")))
        commit(f"bad{n}.txt", "bad")
    elif mode == "sleep":
        while True:
            print("{}", flush=True); time.sleep(0.2)
    elif mode == "silent":
        time.sleep(60)
    elif mode == "note":
        Path("build/dispatch").mkdir(parents=True, exist_ok=True)
        Path("build/dispatch/escalation.md").write_text("- 可选方案与推荐：A\\n- 需要决定的问题：Q")
''')

# 假 verify：有 bad*.txt 即以同样的理由失败。
VERIFY = textwrap.dedent('''
    import sys
    from pathlib import Path
    if list(Path(".").glob("bad*.txt")):
        print("FAIL: test_mod.Case 断言失败 1 != 2"); sys.exit(1)
''')


class FakeHost:
    name = "fake"

    def __init__(self, script: Path, modes: list[str]):
        self.script, self.modes, self.prompts = script, list(modes), []

    def version(self):
        return "fake 1.0"

    def argv(self, prompt, guard):
        self.prompts.append(prompt)
        mode = self.modes.pop(0) if len(self.modes) > 1 else self.modes[0]
        return [sys.executable, str(self.script), mode]

    def parse(self, events):
        return "fake-model", {"input_tokens": 10, "output_tokens": 2}, {}


class FakeGitHub:
    def __init__(self, claimed=(), ci=(True,)):
        self.claimed, self.ci = set(claimed), list(ci)
        self.pushes, self.prs, self.comments, self.labels, self.issues = [], [], [], [], []

    def remote_branch_exists(self, branch):
        return branch in self.claimed

    def push(self, slot, branch):
        subprocess.run(["git", "push", "-q", "-u", "origin", branch], cwd=slot, check=True, capture_output=True)
        self.pushes.append(branch)

    def open_pr(self, slot, branch, title, body):
        self.prs.append((branch, title, body))
        return 7

    def comment(self, pr, body, label=None):
        self.comments.append((pr, body, label))

    def add_label(self, pr, label):
        self.labels.append((pr, label))

    def create_issue(self, title, body, labels):
        self.issues.append((title, body, labels))

    def wait_ci(self, branch, sha, timeout, detail=None):
        """差异 2：本仓库引擎多传一个 detail 字典（写入 run_ids 供观察事件），蓝本只有 3 个形参。"""
        ok = self.ci.pop(0) if len(self.ci) > 1 else self.ci[0]
        if detail is not None:
            detail["run_ids"] = []
        return ok, "" if ok else "CI 未通过：test_mod.Case"


class DispatchContractTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.root = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.root)], check=True, capture_output=True)
        # 差异 4：观察事件与时间线落在夹具仓库的 git 公共目录，不碰本仓库真实事件库；时钟冻结。
        root_patch = mock.patch.object(events_db, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", return_value=FROZEN_TS)
        clock.start()
        self.addCleanup(clock.stop)
        self.write_task(ci_rounds=3)
        (self.root / "docs/specs").mkdir(parents=True)
        (self.root / "docs/specs/daily-report.md").write_text(SPEC)
        (self.root / "fake.py").write_text(FAKE)
        (self.root / "verify.py").write_text(VERIFY)
        (self.root / ".gitignore").write_text("build/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("push", "-q", "origin", "HEAD:main")
        self.git("fetch", "-q", "origin")
        self.config = dispatch.Config(slots=2, slot_root=self.tmp / "slots", stall_seconds=30, poll_seconds=0.1,
                                      verify=[sys.executable, "verify.py"])
        (self.tmp / "slots").mkdir()
        guard = mock.patch.object(dispatch, "prepare_guard", return_value=(Path("guard.ts"), "abc123"))
        guard.start()
        self.addCleanup(guard.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, check=True, capture_output=True, text=True).stdout

    def write_task(self, ci_rounds):
        (self.root / "docs/plans").mkdir(parents=True, exist_ok=True)
        (self.root / "docs/plans/task-005-new-test.md").write_text(TASKBOOK.format(ci_rounds=ci_rounds))

    def dispatcher(self, modes, github=None):
        # 差异 3：显式传夹具身份，缺省的 agent_identity() 需要消费方的 checks.toml。
        host = FakeHost(self.root / "fake.py", modes)
        return dispatch.Dispatcher(self.root, self.config, github or FakeGitHub(), host,
                                   identity=dict(GIT_ENV)), host

    def run_task(self, modes, github=None):
        runner, host = self.dispatcher(modes, github)
        code = runner.run("docs/plans/task-005-new-test.md")
        return code, runner, host

    def slot(self):
        return dispatch.slot_path(self.root, self.config, 1)

    def record(self, number=1):
        return json.loads((self.slot() / f"docs/runs/task-005-new-test/{number}.json").read_text())

    def test_success_writes_record_opens_pr_and_leaves_main_untouched(self):
        github = FakeGitHub()
        code, _, host = self.run_task(["commit"], github)
        self.assertEqual(code, 0)
        record = self.record()
        self.assertEqual((record["task"], record["exit"], record["gen_ai.request.model"]), ("T005", "ok", "fake-model"))
        self.assertEqual(record["gen_ai.usage.input_tokens"], 10)
        self.assertEqual(record["guard_ref"], "abc123")
        self.assertTrue((self.slot() / record["prompt_path"]).exists())
        self.assertEqual(len(github.prs), 1)
        self.assertIn("docs/runs/task-005-new-test/1.json", github.prs[0][2])
        self.assertEqual(github.pushes, ["task/005-new-test", "task/005-new-test"])  # 认领 + 结果
        self.assertIn("Task: T005", self.git("log", "-1", "--format=%B", cwd=self.slot()))
        self.assertEqual(self.git("status", "--porcelain"), "")  # 主目录不变
        self.assertIn("docs/plans/task-005-new-test.md", host.prompts[0])
        self.assertFalse(list((dispatch.state_dir(self.root) / "slots").glob("*.json")))  # 槽位已归还

    def test_ci_failure_retries_with_ci_summary_then_passes(self):
        github = FakeGitHub(ci=[False, True])
        code, _, host = self.run_task(["commit", "commit"], github)
        self.assertEqual(code, 0)
        self.assertIn("CI 未通过", host.prompts[1])  # 第二次派发带上 CI 失败摘要
        self.assertEqual((self.record(1)["exit"], self.record(2)["exit"]), ("ok", "ok"))
        self.assertEqual(self.record(2)["ci_rounds_before"], 1)
        self.assertEqual(len(github.prs), 1)  # 同一个 PR 上继续
        self.assertEqual(github.labels, [])

    def test_ci_over_budget_labels_and_escalates(self):
        self.write_task(ci_rounds=1)
        self.git("commit", "-q", "-am", "budget 1")
        self.git("push", "-q", "origin", "HEAD:main")
        self.git("fetch", "-q", "origin")
        github = FakeGitHub(ci=[False])
        code, _, _ = self.run_task(["commit"], github)
        self.assertEqual(code, 1)
        self.assertEqual(github.labels, [(7, "budget-exceeded")])
        self.assertEqual(github.comments[0][2], "escalation")
        self.assertIn("已达预算 1 轮", github.comments[0][1])


if __name__ == "__main__":
    unittest.main()
