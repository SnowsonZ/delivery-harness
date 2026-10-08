"""T601 可观测性全链与缺陷注入验收：临时匿名 git 项目安装待测引擎（真实 install/guard-git），假 Pi
执行方与受控 bare 远端、FakeGh 平台桩模拟 GitHub，实际调用 dispatch、verify、risk/policy、review、
CI 导出/导入、GitHub 同步、ledger、trace、audit、alert、weekly 的产品入口，形成从 admit 到合并
账本的任务链；分支运行与带 run-name 的判定运行（B117）由假平台按当前 API 形状提供，旧 head 只留
head_mismatch 信息，畸形 head 仍报非信息性的 api；删除本机事件库后从记录/账本/CI 包复原，设计方 PR
不要求派发；缺评审（R2 路径）/路由/记录、改内容、删中间/删尾、锚点不符、引用哈希不符逐项独立注入并
断言稳定 finding（恢复后通过），github: 快照按九字段语义核对；同 reason 告警只一条评论/标签，导出/
账本/summary 不外发 db/WAL/SHM、原始日志与会话内容。

夹具沿用 tests/test_events_agents.py（真实 Dispatcher.run / review.review_pr + 假宿主/假 gh）与
tests/test_audit_reconstruction.py（隔离 events_db.ROOT、递增冻结时钟、FakeGhBase 平台桩）的模式；
事件由真实生产入口写入项目临时库，断言全部来自实际产物/记录，不预写假事件替代生产链。真实平台全链
由设计方 G3 另做（本夹具不声称它已通过）。不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch, dispatch_host, dispatch_observation, review, run_timeline
from engine.checks import r1_checks
from engine.core import alerts, events, events_db, events_io, events_judge
from engine.reports import audit, ci_events, github_events, ledger, trace, weekly, weekly_events
from engine.routing import policy
from tests.gh_fakes import FakeGhBase

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
REPO = "owner/repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
TASK_ID = "T901"
PR = 901
BRANCH = "task/901-observability"
TASKBOOK_REL = "docs/plans/task-901-observability.md"
RECORD_REL = "docs/runs/task-901-observability/1.json"
HARNESS_PATH = ".github/workflows/harness.yml"
AUTOMERGE_PATH = ".github/workflows/auto-merge.yml"
JUDGE_WORKFLOW_ID = 777
BOT_MERGER = {"login": "app/github-actions", "type": "Bot"}
BOT_APPROVAL = {"state": "APPROVED", "user": {"login": "harness-approve[bot]", "type": "Bot"}}
CREATED_AT = "2026-01-02T00:00:00Z"
PR_TITLE = "T901：可观测性夹具任务（测试专用）"

TASKBOOK_TEXT = f"""---
task: {TASK_ID}
class: K2
risk: R0
designer: codex
size: small
architecture: false
spec_refs: []
no_spec_reason: 夹具任务不挂规格（测试专用，不触及产品规格验收编号）
budget:
  wall_clock_min: 5
  retries: 1
  ci_rounds: 2
  tokens: null
rollback: git revert
---

# {PR_TITLE}

## 目标终态

tests/ 下新增一个自洽的测试文件并通过项目检查；本任务只用于驱动可观测性全链夹具。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 夹具 | 夹具测试存在且通过 | 夹具 | `tests.test_greeting.GreetingTest.test_greet` | 断言失败或文件缺失时夹具失败 |

## 步骤与提交顺序

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 新增测试 | `tests/test_greeting.py` | `python3 -m unittest tests.test_greeting -v` | 全部 |
"""

FIXTURE_CHECKS = """schema = 1

[identity]
agent_login = "fixture-agent"
agent_email = "fixture-agent@example.invalid"

[platform]
approval = "app"

[runtime]
preserve = []

[sources]
code = []
ui = []
python_dirs = []
swift_dirs = []

[verify]
pinned_tools = []
order = []

[[verify.checks]]
name = "tests"
tiers = ["default", "full"]
command = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
"""

BROKEN_TEST = """import unittest


class GreetingTest(unittest.TestCase):
    def test_greet(self):
        self.assertEqual("hello", "world")


if __name__ == "__main__":
    unittest.main()
"""

FIXED_TEST = BROKEN_TEST.replace('"world"', '"hello"')

EXECUTOR_STREAM = "\n".join([
    json.dumps({"type": "message_end", "message": {"role": "assistant", "model": "provider/model-fixture",
                                                   "usage": {"input": 10, "output": 5, "cost": {"total": 0.5}}}}),
    json.dumps({"type": "tool_execution_end", "isError": False,
                "result": {"content": [{"type": "text", "text": "done"}]}}),
]) + "\n"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_bytes(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def verdict_script(verdict: str) -> str:
    body = json.dumps({"verdict": verdict, "summary": "结论摘要", "findings": []}, ensure_ascii=False)
    return "import json, sys; print(" + repr(body) + ")"


class _Clock:
    """递增冻结时钟：从真实时刻前 45 分钟起每次调用前进两秒（周报窗口按真实时刻，事件落在窗内；
    子进程事件用真实时钟，落在冻结区间之后，二者不交错）。"""

    def __init__(self):
        self.moment = datetime.now(UTC) - timedelta(minutes=45)

    def __call__(self) -> str:
        self.moment += timedelta(seconds=2)
        return self.moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def iso(self) -> str:
        return self.moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class ExecutorHost:
    """假执行方宿主：argv 启动固定脚本；解析委托真实 PiHost（不依赖本机 pi 可执行文件）。"""

    name = "fake-pi"

    def __init__(self, script: str):
        self.script = script
        self.pi = dispatch_host.PiHost()

    def version(self):
        return "9.9.9-fake"

    def argv(self, prompt, guard):
        return [sys.executable, "-c", self.script]

    def parse(self, events_path):
        return self.pi.parse(events_path)

    def parse_observability(self, events_path):
        return self.pi.parse_observability(events_path)


class DispatchGh:
    """派发用 gh 桩：push 走真实 git（bare 远端），其余按脚本应答并记录（与 T105 夹具同形）。"""

    def __init__(self, pr_number: int, run_ids: list[int]):
        self.pr_number = pr_number
        self.run_ids = run_ids
        self.calls: list[tuple] = []
        self.pr_bodies: list[str] = []
        self.comments: list[str] = []
        self.labels: list[str] = []
        self.issues: list[tuple] = []

    def remote_branch_exists(self, branch):
        self.calls.append(("remote_branch_exists", branch))
        return False

    def push(self, slot, branch):
        self.calls.append(("push", branch))
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        subprocess.run(["git", "push", "-q", "-f", "-u", "origin", branch], cwd=slot, check=True,
                       capture_output=True, env={**env, **GIT_ENV})

    def open_pr(self, slot, branch, title, body):
        self.calls.append(("open_pr", branch))
        self.pr_bodies.append(body)
        return self.pr_number

    def comment(self, pr, body, label=None):
        self.calls.append(("comment", pr))
        self.comments.append(body)
        if label:
            self.add_label(pr, label)
        return f"{GITHUB_URL}pull/{pr}#issuecomment-1"

    def add_label(self, pr, label):
        self.calls.append(("add_label", pr, label))
        self.labels.append(label)

    def disable_auto_merge(self, pr):
        self.calls.append(("disable_auto_merge", pr))
        return True

    def create_issue(self, title, body, labels):
        self.calls.append(("create_issue", title))
        self.issues.append((title, body))
        return f"{GITHUB_URL}issues/1"

    def wait_ci(self, branch, sha, timeout, detail=None):
        self.calls.append(("wait_ci", branch))
        if detail is not None:
            detail["run_ids"] = list(self.run_ids)
        return True, ""

    def existing_pr(self, branch):
        return None


class ReviewGh:
    """评审用 gh 桩：记录 gh 调用形状；评论捕获正文并返回固定 URL（与 T105 夹具同形）。"""

    def __init__(self, pr_json: dict, checks: list):
        self.pr_json = pr_json
        self.checks = checks
        self.calls: list[str] = []
        self.comments: list[str] = []
        self.removed_labels = 0

    def _run(self, argv, cwd=None, agent=False, stdin=None):
        joined = " ".join(argv)
        self.calls.append(joined)
        if joined.startswith("gh pr view"):
            fields = argv[argv.index("--json") + 1].split(",")
            data = dict(self.pr_json)
            if "comments" in fields:
                data["comments"] = data.get("comments", [])
            return json.dumps(data)
        if joined.startswith("gh pr checks") and "name,state,link" in joined:
            return json.dumps(self.checks)
        if joined.startswith("gh pr edit") and "--remove-label" in joined:
            self.removed_labels += 1
            return ""
        raise AssertionError(f"未预期的 gh 调用：{joined}")

    def comment(self, pr, body, label=None):
        self.calls.append(f"gh pr comment {pr}")
        self.comments.append(body)
        return f"{GITHUB_URL}pull/{pr}#issuecomment-9"

    def disable_auto_merge(self, pr):
        return True


class FakeReviewer:
    """假评审方：argv 启动固定脚本；read 返回展示模型与 model_basis（C6）。"""

    name = "claude-code"
    env: ClassVar[dict] = {}

    def __init__(self, script: str):
        self.script = script

    def argv(self, prompt, workspace, output):
        return [sys.executable, "-c", self.script]

    def read(self, stdout, output):
        return stdout, "reported/model-1", "reported"


class PolicyGh:
    """policy.main 生成路由事件时的 gh 桩：标签、escape 议题与该分支已完成的 CI 运行按夹具应答。"""

    def __init__(self, head: str, *, labels=(), escapes=()):
        self.head = head
        self.labels = list(labels)
        self.escapes = list(escapes)
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        if joined.startswith("pr view"):
            return json.dumps({"labels": [{"name": name} for name in self.labels], "comments": []})
        if joined.startswith("pr list"):
            return json.dumps([])
        if joined.startswith("issue list"):
            return json.dumps(self.escapes)
        if joined.startswith("run list"):
            runs = [{"headSha": self.head, "status": "completed", "event": "pull_request"}]
            return json.dumps(runs)
        raise AssertionError(f"未预期的 gh 调用：{joined}")


class FakeWeeklyGh:
    """weekly.collect/compute 用 gh 桩：只返回构造时给定的合并 PR 与已完成的分支运行。"""

    def __init__(self, prs, head: str):
        self.prs = list(prs)
        self.head = head
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        if joined.startswith("pr list"):
            return json.dumps([] if "--head" in args else self.prs)
        if joined.startswith("issue list"):
            return json.dumps([])
        if joined.startswith("run list"):
            if "--branch" in args:
                runs = [{"headSha": self.head, "status": "completed", "event": "pull_request"}]
                return json.dumps(runs)
            return json.dumps([])
        if joined.startswith("label create"):
            return ""
        raise AssertionError(f"未预期的 gh 调用：{joined}")


class AlertPublisher:
    """C4 发布桩：publish 所需方法全部实现并记录（创建/更新/标签次数供幂等断言）。"""

    def __init__(self):
        self.comments: list[str] = []
        self.edits: list[tuple] = []
        self.labels: list[str] = []
        self.issues: list[str] = []

    def list_comments(self, pr):
        return [{"id": index + 1, "body": body} for index, body in enumerate(self.comments)]

    def comment(self, pr, body, label=None):
        self.comments.append(body)
        if label:
            self.add_label(pr, label)
        return f"{GITHUB_URL}pull/{pr}#issuecomment-{len(self.comments)}"

    def add_label(self, pr, label):
        self.labels.append(label)

    def edit_comment(self, comment_id, body):
        self.edits.append((comment_id, body))
        self.comments[comment_id - 1] = body

    def list_issues(self, label):
        return []

    def edit_issue(self, number, body):
        pass

    def create_issue(self, title, body, labels):
        self.issues.append(title)
        return f"{GITHUB_URL}issues/9"


class FakePlatform(FakeGhBase):
    """load_ci/audit/ledger/sync 共用平台桩：在 FakeGhBase 上补 pr() 的 createdAt 与 B117 判定运行
    所需的 actions/workflows、actions/workflows/<id>/runs 路由；未配置路由继续失败关闭。"""

    def __init__(self, *, created_at=CREATED_AT, workflows=(), judge_runs=(), **kwargs):
        super().__init__(**kwargs)
        self.created_at = created_at
        self.workflows = list(workflows)
        self.judge_runs = list(judge_runs)

    def pr(self, pr: int) -> dict:
        info = super().pr(pr)
        info["createdAt"] = self.created_at
        info["url"] = f"{GITHUB_URL}/pull/{pr}"
        return info

    def api(self, route: str, *, method: str = "GET", payload=None):
        path = route.partition("?")[0]
        if method == "GET" and re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows", path):
            self.calls.append(("GET", route))
            return {"total_count": len(self.workflows), "workflows": self.workflows}
        if method == "GET" and (match := re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows/(\d+)/runs", path)):
            self.calls.append(("GET", route))
            items = [run for run in self.judge_runs if run.get("workflow_id") == int(match[1])]
            return {"total_count": len(items), "workflow_runs": items}
        return super().api(route, method=method, payload=payload)


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-observability-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = _Clock()
        clock = mock.patch.object(events_db, "_now", new=self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [name for name in os.environ if name.startswith(CLEAN_PREFIXES)]:
            os.environ.pop(key, None)
        os.environ.update(GIT_ENV)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)
        self._seq = itertools.count()
        self._root_patcher = None
        self._package_dirs: dict[str, Path] = {}

    def set_root(self, project: Path) -> None:
        """切换事件库隔离到指定夹具项目（同一用例内多个世界各自生效；用例结束统一撤销）。"""
        if self._root_patcher is None:
            self.addCleanup(self._clear_root)
        else:
            self._root_patcher.stop()
        self._root_patcher = mock.patch.object(events_db, "ROOT", project)
        self._root_patcher.start()

    def _clear_root(self) -> None:
        if self._root_patcher is not None:
            self._root_patcher.stop()
            self._root_patcher = None

    # ---------- 基础夹具（匿名临时 git 仓库 + 待测引擎安装） ----------

    def git(self, *args: str, cwd: Path, check: bool = True) -> str:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=False)
        if check and done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout.strip()

    def subprocess_run(self, argv: list[str], cwd: Path, env_extra: dict | None = None,
                       check: bool = True, *, ambient: bool = False) -> subprocess.CompletedProcess:
        if ambient:
            env = dict(os.environ)
        else:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GIT_", "GITHUB_", "HARNESS_")) and key != "CI"}
            env.update(GIT_ENV)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(env_extra or {})
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env, check=False, timeout=600)
        if check and done.returncode != 0:
            raise AssertionError(f"{' '.join(map(str, argv))} 失败：{done.stdout}\n{done.stderr}")
        return done

    def fresh_project(self, name: str) -> Path:
        path = self.tmp / f"{name}-{next(self._seq)}"
        path.mkdir()
        self.git("init", "-q", "-b", "main", cwd=path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        tests = path / "tests"
        tests.mkdir()
        (tests / "test_placeholder.py").write_text(
            "import unittest\n\n\nclass PlaceholderTest(unittest.TestCase):\n"
            "    def test_placeholder(self):\n        self.assertTrue(True)\n", encoding="utf-8")
        (path / ".gitignore").write_text("build/\n__pycache__/\n*.pyc\n", encoding="utf-8")
        (path / TASKBOOK_REL).parent.mkdir(parents=True, exist_ok=True)
        (path / TASKBOOK_REL).write_text(TASKBOOK_TEXT, encoding="utf-8")
        config = path / ".harness" / "config"
        config.mkdir(parents=True)
        (config / "checks.toml").write_text(FIXTURE_CHECKS, encoding="utf-8")
        state = path / ".harness" / "state"
        state.mkdir(parents=True)
        (state / "quality-baseline.json").write_text('{"complex_functions": 0, "files_over_800": 0}',
                                                     encoding="utf-8")
        (state / "mutation-baseline.json").write_text("{}", encoding="utf-8")
        self.git("add", "-A", cwd=path)
        self.git("commit", "-q", "-m", "init", cwd=path)
        return path

    def install_engine(self, project: Path) -> None:
        """真实产品入口：install 把待测引擎与模板装进夹具项目（--allow-dirty 仅开发调试口径）。"""
        self.subprocess_run([sys.executable, str(ENGINE_REPO / "engine" / "cli.py"),
                             "install", "--target", str(project), "--allow-dirty"], cwd=project)
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "chore：安装 harness 引擎与模板", cwd=project)
        self.subprocess_run([sys.executable, str(project / ".harness" / "engine" / "cli.py"),
                             "guard-git", "install"], cwd=project)

    def bare_origin(self, project: Path) -> Path:
        origin = self.tmp / f"origin-{next(self._seq)}.git"
        self.git("init", "--bare", "-q", "--initial-branch=main", str(origin), cwd=project)
        self.git("remote", "add", "origin", str(origin), cwd=project)
        self.git("push", "-q", "origin", "main", cwd=project)
        return origin

    def publish_main(self, project: Path) -> None:
        """把本地 main 更新到 bare 远端（守卫装好后 push main 被夹具自己的钩子拒绝，改用取回），
        并刷新本机的 origin/main 远程追踪引用。"""
        origin = self.git("remote", "get-url", "origin", cwd=project)
        self.git("fetch", "--quiet", str(project), "main:main", cwd=Path(origin))
        self.git("fetch", "--quiet", "origin", cwd=project)

    def db_query(self, sql: str, params=()) -> list[tuple]:
        path = events_db.db_path()
        assert path is not None and path.exists()
        with contextlib.closing(sqlite3.connect(path)) as conn:
            return conn.execute(sql, params).fetchall()

    def db_exec(self, sql: str, params=()) -> None:
        path = events_db.db_path()
        assert path is not None
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute(sql, params)
            conn.commit()

    def rows(self, trace_id: str, *, source: str | None = None, stage: str | None = None) -> list[dict]:
        rows = events_io.query(trace_id=trace_id, stage=stage)
        if source is not None:
            rows = [row for row in rows if row["source"] == source]
        return sorted(rows, key=lambda row: row["seq"])

    # ---------- 任务 PR 全链：真实 dispatch → 合并 → 评审 → CI 包 → 事实/账本/锚点 ----------

    def executor_script(self) -> str:
        """假执行方：第 1 轮写失败测试，第 2 轮修复；提交带 Task trailer；流是真实 JSONL 形状。"""
        commit = f"test：夹具测试\\n\\nTask: {TASK_ID}"
        return (
            "import pathlib, subprocess, sys, time\n"
            "sys.stdout.write(" + repr(EXECUTOR_STREAM) + ")\n"
            "def git(argv):\n"
            "    err = ''\n"
            "    for delay in (0.0, 0.1, 0.3, 0.9):\n"
            "        if delay:\n"
            "            time.sleep(delay)\n"
            "        done = subprocess.run(['git', *argv], capture_output=True, text=True)\n"
            "        if done.returncode == 0:\n"
            "            return\n"
            "        err = done.stderr\n"
            "    sys.stdout.write('git 重试失败：' + err)\n"
            "    sys.exit(1)\n"
            "target = pathlib.Path('tests/test_greeting.py')\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            "target.write_text(" + repr(FIXED_TEST) + " if target.exists() else " + repr(BROKEN_TEST)
            + ", encoding='utf-8')\n"
            "git(['add', '-A'])\n"
            "git(['commit', '-q', '-m', '" + commit + "', '--allow-empty'])\n"
        )

    def dispatch_task(self, project: Path) -> tuple[DispatchGh, ExecutorHost]:
        """真实产品入口：Dispatcher.run 派发夹具任务书（本地判定用项目自己的 bin/verify）。"""
        gh = DispatchGh(PR, run_ids=[9001, 9002])
        host = ExecutorHost(self.executor_script())
        config = dispatch.Config(slots=2, stall_seconds=120, poll_seconds=0.05, ci_timeout_seconds=5,
                                 verify=["bin/verify"], slot_root=self.tmp)
        runner = dispatch.Dispatcher(project, config, gh, host, identity=dict(GIT_ENV))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = runner.run(TASKBOOK_REL)
        self.assertEqual(code, 0, f"dispatch 失败：{out.getvalue()}\n{err.getvalue()}")
        return gh, host

    def merge_pr(self, project: Path, branch: str, number: int, body: str) -> tuple[str, str]:
        """推 pull/<head> 引用并在本地 bare 远端上合并（--no-ff，合并信息带 PR 号）。"""
        head = self.git("rev-parse", branch, cwd=project)
        self.git("push", "-q", "origin", f"{head}:refs/pull/{number}/head", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m",
                 f"Merge pull request #{number} from {branch}\n\n{body}", branch, cwd=project)
        merge_sha = self.git("rev-parse", "HEAD", cwd=project)
        self.publish_main(project)
        return head, merge_sha

    def review_pr_real(self, project: Path, number: int, branch: str, head: str,
                       *, title: str, body: str) -> str:
        """真实产品入口：review.review_pr（假评审方脚本，gh 桩记录评论正文）。"""
        pr_json = {"title": title, "body": body, "headRefName": branch, "headRefOid": head,
                   "baseRefName": "main", "state": "OPEN", "mergeCommit": None}
        checks = [{"name": "harness", "state": "SUCCESS", "link": "https://ci.example.invalid/run/1"}]
        gh = ReviewGh(pr_json, checks)
        with mock.patch.object(review, "make_reviewer", lambda name: FakeReviewer(verdict_script("通过"))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = review.review_pr(number, "claude-code", root=project, github=gh)
        self.assertEqual(code, 0, gh.calls)
        self.assertTrue(gh.comments)
        return gh.comments[0]

    @contextlib.contextmanager
    def ci_env(self, run_id: int, attempt: int, job: str, *, head_ref: str | None):
        keys = ("CI", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB",
                "GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_WORKFLOW_REF")
        saved = {key: os.environ.pop(key, None) for key in keys}
        os.environ.update({"CI": "true", "GITHUB_RUN_ID": str(run_id), "GITHUB_RUN_ATTEMPT": str(attempt),
                           "GITHUB_JOB": job})
        if head_ref is not None:
            os.environ["GITHUB_HEAD_REF"] = head_ref
        else:
            os.environ["GITHUB_REF_NAME"] = "main"
        try:
            yield
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value

    def export_package(self, run_id: int, attempt: int, job: str) -> tuple[bytes, Path]:
        """真实产品入口：ci_events.export 导出当前 CI 链的事件包（须在对应 CI 环境内调用）。"""
        name = f"harness-events-{run_id}-{attempt}-{job}"
        target = self.tmp / f"export-{name}-{next(self._seq)}"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(ci_events.export_main(["--target", str(target)]), 0, err.getvalue())
        content = (target / "harness-events.json").read_text(encoding="utf-8")
        return zip_bytes({f"{name}/harness-events.json": content}), target

    def run_policy_ci(self, project: Path, branch: str, base: str, head: str, *, run_id: int,
                      attempt: int, job: str, head_ref: str | None, classes: dict, pr: int) -> tuple[bytes, Path]:
        """真实产品入口：CI 环境下的 policy.main（判级与路由埋点），随后在同一环境内导出事件包。"""
        stub = PolicyGh(head)
        autonomy = {"size": {"max_lines": 400, "exclude": []}, "classes": classes}
        original = policy.gather

        def gather(*args, **kwargs):
            kwargs.setdefault("cwd", project)
            kwargs.setdefault("gh", stub)
            return original(*args, **kwargs)

        with self.ci_env(run_id, attempt, job, head_ref=head_ref), \
                mock.patch.object(r1_checks, "load_autonomy", lambda *args, **kwargs: autonomy), \
                mock.patch.object(policy, "gather", gather), \
                mock.patch.object(policy, "_gh", stub), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(policy.main(["--base", base, "--head", head, "--pr", str(pr),
                                          "--branch", branch]), 0)
            return self.export_package(run_id, attempt, job)

    def build_packages(self, project: Path, branch: str, base: str, branch_head: str, merge_sha: str,
                       pr: int, *, route: bool = True) -> dict[str, bytes]:
        """三种 CI 来源的事件包：harness 的 verify job、route job（重跑 attempt=2）与判定运行。

        当前 head 的包全部由真实产品入口在对应 head 上生成（verify 子进程 / policy.main），包 origin 由
        export 按当时环境写入；判定运行的包以默认分支分检出、以合并提交为 head（与 workflow_run 一致）。
        """
        real_url = self.git("remote", "get-url", "origin", cwd=project)
        packages: dict[str, bytes] = {}
        dirs: dict[str, Path] = {}
        try:
            self.git("remote", "set-url", "origin", GITHUB_URL, cwd=project)
            self.git("checkout", "-q", "--detach", branch_head, cwd=project)
            with self.ci_env(9001, 1, "job_x", head_ref=branch):
                self.subprocess_run([sys.executable, str(project / ".harness" / "engine" / "cli.py"),
                                     "verify", "--quick"], cwd=project, ambient=True)
                packages["harness-events-9001-1-job_x"], dirs["job_x"] = self.export_package(9001, 1, "job_x")
            if route:
                classes = {"K2": {"name": "测试", "level": "L4", "window": 20, "max_escapes": 0,
                                  "audit_every": 0}}
                packages["harness-events-9002-2-job_y"], dirs["job_y"] = self.run_policy_ci(
                    project, branch, base, branch_head, run_id=9002, attempt=2, job="job_y",
                    head_ref=branch, classes=classes, pr=pr)
                self.git("checkout", "-q", "--detach", merge_sha, cwd=project)
                packages["harness-events-9003-1-judge"], dirs["judge"] = self.run_policy_ci(
                    project, branch, base, branch_head, run_id=9003, attempt=1, job="judge",
                    head_ref=None, classes=classes, pr=pr)
        finally:
            self.git("checkout", "-q", "main", cwd=project)
            self.git("remote", "set-url", "origin", real_url, cwd=project)
        self._package_dirs = dirs
        return packages

    def platform(self, fx: SimpleNamespace, *, comments=(), runs=(), artifacts=None, downloads=None,
                 reviews=None, pulls=None, judge: bool = True) -> FakePlatform:
        """PR 的整套假平台：合并/抽审事实路由 + CI 运行与事件包 + 评论（锚点评论由真实 writer 追加）。"""
        default_pulls = {fx.pr: {
            "number": fx.pr, "state": "closed", "merged": True, "merged_at": fx.merged_at,
            "merge_commit_sha": fx.merge_sha, "title": PR_TITLE, "body": "夹具实现。",
            "labels": [{"name": "class:K2"}],
            "head": {"ref": fx.branch, "sha": fx.branch_head}, "merged_by": dict(fx.merged_by)}}
        workflows = judge_runs = ()
        if judge:
            workflows = ({"id": JUDGE_WORKFLOW_ID, "name": "auto-merge", "path": AUTOMERGE_PATH,
                          "state": "active"},)
            judge_runs = ({"id": 9003, "run_attempt": 1, "event": "workflow_run",
                           "workflow_id": JUDGE_WORKFLOW_ID,
                           "display_title": events_judge.judge_run_name(fx.pr, fx.branch_head),
                           "head_branch": "main", "head_sha": fx.merge_sha, "path": AUTOMERGE_PATH,
                           "created_at": self.clock.iso()},)
        return FakePlatform(
            pulls=pulls or default_pulls,
            reviews=reviews if reviews is not None else {fx.pr: [dict(BOT_APPROVAL, commit_id=fx.branch_head)]},
            pr_commits={fx.pr: [{"sha": "c1"}]},
            commits={fx.merge_sha: {"parents": [{"sha": "p1"}, {"sha": "p2"}],
                                    "commit": {"message": f"Merge pull request #{fx.pr} from {fx.branch}"}}},
            comments=list(comments), runs=list(runs), artifacts=dict(artifacts or {}),
            downloads=dict(downloads or {}), created_at=CREATED_AT,
            workflows=workflows, judge_runs=judge_runs)

    @staticmethod
    def platform_network(packages: dict[str, bytes], runs: list[dict]) -> tuple[dict, dict]:
        """artifact 路由与下载：物理名与运行 id/attempt 相符的包按 archive_download_url 提供。"""
        artifacts: dict[int, list[dict]] = {}
        downloads: dict[str, bytes] = {}
        for name, data in packages.items():
            match = re.fullmatch(r"harness-events-(\d+)-(\d+)-(.+)", name)
            run_id = int(match[1])
            artifacts.setdefault(run_id, []).append(
                {"id": 100 + len(downloads), "name": name, "expired": False,
                 "archive_download_url": f"https://dl/{name}"})
            downloads[f"https://dl/{name}"] = data
        return {run.get("id"): artifacts.get(run.get("id"), []) for run in runs}, downloads

    def build_task_world(self, *, route: bool = True, review_comment: bool = True,
                         merged_by: dict | None = None, malformed_head: bool = False,
                         mutate_ledger=None, with_ledger: bool = True,
                         pr: int = PR, branch: str = BRANCH) -> SimpleNamespace:
        """一个世界的完整任务链：安装 → 派发 → 合并 → 评审 → CI 包 → 事实同步 → 账本 → 锚点评论。"""
        self.set_root(self.fresh_project("app"))
        project = events_db.ROOT
        origin = self.bare_origin(project)
        self.install_engine(project)
        self.publish_main(project)
        base = self.git("rev-parse", "origin/main", cwd=project)
        self.dispatch_task(project)
        branch_head = self.git("rev-parse", branch, cwd=project)
        head, merge_sha = self.merge_pr(project, branch, pr, "夹具实现。")
        body = self.review_pr_real(project, pr, branch, head, title=PR_TITLE, body="夹具实现。") \
            if review_comment else None
        packages = self.build_packages(project, branch, base, branch_head, merge_sha, pr, route=route)
        fx = SimpleNamespace(project=project, origin=origin, base=base, branch=branch, pr=pr,
                             branch_head=branch_head, head=head, merge_sha=merge_sha,
                             merged_at=self.clock.iso(), merged_by=dict(merged_by or BOT_MERGER),
                             review_body=body, packages=packages, task_id=TASK_ID)
        runs = [{"id": 9001, "run_attempt": 1, "head_sha": branch_head, "head_branch": branch,
                 "path": HARNESS_PATH},
                {"id": 9005, "run_attempt": 1, "head_sha": base, "head_branch": branch,
                 "path": HARNESS_PATH}]
        if route:
            runs[1:1] = [{"id": 9002, "run_attempt": 2, "head_sha": branch_head, "head_branch": branch,
                          "path": HARNESS_PATH},
                         {"id": 9003, "run_attempt": 1, "head_sha": merge_sha, "head_branch": "main",
                          "path": AUTOMERGE_PATH}]
        if malformed_head:
            runs.append({"id": 9006, "run_attempt": 1, "head_sha": "short-sha", "head_branch": branch,
                         "path": HARNESS_PATH})
        artifacts, downloads = self.platform_network(packages, runs)
        comments = [self.as_comment(500, body)] if body else []
        gh = self.platform(fx, comments=comments, runs=runs, artifacts=artifacts, downloads=downloads,
                           judge=route)
        first = github_events.sync(fx.pr, gh=gh)
        self.assertGreaterEqual(first["events"], 2, first["findings"])
        self.assertTrue(first["snapshots"])
        built = anchor = None
        if with_ledger:
            built = ledger.build_ledger(fx.pr, gh=gh, cwd=project)
            tolerated = {"head_mismatch"} | ({"api"} if malformed_head else set())
            extra = [item for item in built["missing"]
                     if item["reason"] not in tolerated and item["item"] != "route"]
            self.assertEqual(extra, [], built["missing"])
            if mutate_ledger is not None:
                mutate_ledger(built)
            result = ledger.publish_ledger(built, gh=gh)
            self.assertTrue(result["ok"], result["findings"])
            anchor = gh.comments[-1]
        fx.platform = gh
        fx.ledger = built
        fx.anchor = anchor
        return fx

    @staticmethod
    def as_comment(comment_id: int, body: str) -> dict:
        return {"id": comment_id, "body": body, "created_at": CREATED_AT,
                "html_url": f"{GITHUB_URL}/issues/comments/{comment_id}"}

    # ---------- 无派发的轻量世界（设计方侧/缺陷注入用） ----------

    def write_lean_record(self, project: Path, branch: str, base: str) -> None:
        """带全部运行记录字段的夹具记录（真实 run_timeline 锚点 + prompt 文件与 sha256 一致）。"""
        task_raw = (project / TASKBOOK_REL).read_bytes()
        self.assertIsNotNone(events.emit(
            "dispatch", "admit", "ok", trace_id=branch, duration_ms=5,
            inputs=[{"kind": "taskbook", "ref": f"{TASKBOOK_REL}@{base}", "sha256": digest(task_raw),
                     "size": len(task_raw)}],
            decision={"by": "taskbook", "rule": "admit"}))
        stored = events.store_artifact(b"verify log\n")
        self.assertIsNotNone(events.emit(
            "verify", "verify.tests", "ok", trace_id=branch, duration_ms=10,
            outputs={"log.sha256": stored["sha256"], "log.size": stored["size"], "log.ref": stored["ref"]}))
        timeline, head = run_timeline.record_fields(branch)
        run_timeline.fix_anchor(branch, head)
        prompt_rel = "docs/runs/task-901-observability/1.prompt.md"
        prompt = project / prompt_rel
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("夹具提示词\n", encoding="utf-8")
        raw = prompt.read_bytes()
        record = {"task": TASK_ID, "class": "K2", "attempt": 1, "branch": branch,
                  "gen_ai.agent.name": "fake-pi", "host_version": "9.9.9-fake",
                  "gen_ai.request.model": "provider/model-fixture",
                  "prompt_sha256": digest(raw), "prompt_path": prompt_rel, "guard_ref": base,
                  "started_at": self.clock.iso(), "ended_at": self.clock.iso(),
                  "executor_seconds": 1.0, "exit": "ok", "retries": 0, "failure_signatures": [],
                  "ci_rounds_before": 0, "guard_denials": {}, "missing_context": [],
                  "missing_context_status": "reported", "gen_ai.usage.input_tokens": 10,
                  "gen_ai.usage.output_tokens": 5, "cost": 0.5, "escalation": None, **timeline}
        target = project / RECORD_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", f"run：{TASK_ID} 派发记录\n\nTask: {TASK_ID}", cwd=project)

    def build_lean_world(self, *, branch: str, pr: int, files: dict[str, str], message: str,
                         merged_by: dict, klass: str, with_records: bool = False,
                         review_body: str | None = None) -> SimpleNamespace:
        """无派发世界：分支直接提交（trailer 由 message 携带）、合并、一次 route 事件包与平台桩。"""
        self.set_root(self.fresh_project("lean"))
        project = events_db.ROOT
        origin = self.bare_origin(project)
        self.install_engine(project)
        self.publish_main(project)
        base = self.git("rev-parse", "origin/main", cwd=project)
        self.git("checkout", "-q", "-b", branch, cwd=project)
        for rel, content in files.items():
            target = project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", message, cwd=project)
        if with_records:
            self.write_lean_record(project, branch, base)
        branch_head = self.git("rev-parse", "HEAD", cwd=project)
        head, merge_sha = self.merge_pr(project, branch, pr, message)
        real_url = self.git("remote", "get-url", "origin", cwd=project)
        packages = {}
        try:
            self.git("remote", "set-url", "origin", GITHUB_URL, cwd=project)
            self.git("checkout", "-q", "--detach", branch_head, cwd=project)
            classes = {klass: {"name": klass, "level": "L4", "window": 20, "max_escapes": 0,
                               "audit_every": 0}}
            packages["harness-events-9101-1-job_r"], _dir = self.run_policy_ci(
                project, branch, base, branch_head, run_id=9101, attempt=1, job="job_r",
                head_ref=branch, classes=classes, pr=pr)
        finally:
            self.git("checkout", "-q", "main", cwd=project)
            self.git("remote", "set-url", "origin", real_url, cwd=project)
        fx = SimpleNamespace(project=project, origin=origin, base=base, branch=branch, pr=pr,
                             branch_head=branch_head, head=head, merge_sha=merge_sha,
                             merged_at=self.clock.iso(), merged_by=dict(merged_by),
                             review_body=review_body, packages=packages, task_id=TASK_ID)
        runs = [{"id": 9101, "run_attempt": 1, "head_sha": branch_head, "head_branch": branch,
                 "path": HARNESS_PATH}]
        artifacts, downloads = self.platform_network(packages, runs)
        comments = [self.as_comment(500, review_body)] if review_body else []
        gh = self.platform(fx, comments=comments, runs=runs, artifacts=artifacts, downloads=downloads,
                           reviews={pr: []}, judge=False)
        github_events.sync(fx.pr, gh=gh)
        fx.platform = gh
        fx.ledger = None
        fx.anchor = None
        return fx

    # ---------- 产品入口的小包装 ----------

    def inspect(self, fx: SimpleNamespace, gh=None, pr: int | None = None) -> dict:
        return audit.inspect_pr(pr or fx.pr, gh=gh or fx.platform, cwd=fx.project)

    def run_trace(self, gh, *args: str) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(events_io, "GhClient", lambda: gh), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = trace.trace_main(list(args))
        view = json.loads(out.getvalue()) if out.getvalue().strip() else {}
        return code, view, err.getvalue()

    def alert_with_bundle(self, fx: SimpleNamespace, reason: str, *, pr: int | None = None) -> AlertPublisher:
        """真实产品入口：alert --bundle（证据核对 + 发布），返回发布桩供命中/跳过断言。"""
        bundle = self.tmp / f"bundle-{reason}-{next(self._seq)}.json"
        events_io.write_bundle(events_io.export_bundle(), bundle)
        publisher = AlertPublisher()
        with contextlib.redirect_stdout(io.StringIO()):
            code = alerts.main([reason, "--trace", fx.branch, "--bundle", str(bundle),
                                "--pr", str(pr or fx.pr), "--task", fx.task_id, "--json"], gh=publisher)
        self.assertEqual(code, 0)
        return publisher

    def backup_db(self, project: Path) -> Path:
        backup = self.tmp / f"db-backup-{next(self._seq)}"
        shutil.copytree(project / ".git" / "harness", backup)
        return backup

    def restore_db(self, project: Path, backup: Path) -> None:
        shutil.rmtree(project / ".git" / "harness")
        shutil.copytree(backup, project / ".git" / "harness")

    # ---------- 验收 1：实际产品入口形成从 admit 到合并账本的任务链 ----------

    def test_real_entrypoints_deliver_complete_task_chain(self):
        fx = self.build_task_world()
        branch = fx.branch

        # dispatch 链：真实 Dispatcher 产出的派发阶段事件（第 1 轮本地判定失败、第 2 轮修复后通过）
        dispatch_rows = self.rows(branch, source="local", stage="dispatch")
        self.assertEqual([(row["step"], row["status"]) for row in dispatch_rows],
                         [("admit", "ok"), ("guard_preflight", "ok"), ("slot", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "fail"), ("executor_round", "ok"),
                          ("local_verify", "ok"), ("push_pr", "ok"), ("ci_wait", "ok")])
        # verify 埋点：dispatch 的 bin/verify（默认档）每项检查留痕，失败→修复可见，integrity 在内
        verify_rows = self.rows(branch, source="local", stage="verify")
        tests = [(row["step"], row["status"]) for row in verify_rows if row["step"] == "verify.tests"]
        self.assertIn(("verify.tests", "fail"), tests)
        self.assertEqual(tests[-1], ("verify.tests", "ok"))
        self.assertTrue(any(row["step"] == "verify.summary" and row["status"] == "ok" for row in verify_rows))
        self.assertTrue(any(row["step"] == "verify.integrity" for row in verify_rows))

        # 合并 head 上的运行记录：锚点固定在已发生前缀，stages 携带 attempt/round
        record = json.loads(self.git("show", f"{fx.merge_sha}:{RECORD_REL}", cwd=fx.project))
        self.assertEqual(record["exit"], "ok")
        anchor_hash = record["anchors"][0]["head_hash"]
        self.assertIn(anchor_hash, [row["hash"] for row in self.rows(branch, source="local")])
        rounds = sorted(item["round"] for item in record["stages"] if item["step"] == "local_verify")
        self.assertEqual(rounds, [1, 2])

        # CI 两种来源 + 重跑 + 判定运行都经真实 load_ci 导入；旧 head 只留信息性发现
        result = events_io.load_ci(fx.pr, head=fx.branch_head, gh=fx.platform)
        self.assertEqual(sorted(item["code"] for item in result["findings"]), ["head_mismatch"])
        for source in ("ci:9001:1:job_x", "ci:9002:2:job_y", "ci:9003:1:judge"):
            self.assertTrue(self.rows(branch, source=source), source)
        judge = self.rows(branch, source="ci:9003:1:judge")
        facts = next(row for row in judge if row["step"] == "facts")
        self.assertEqual((facts["outputs"]["risk"], facts["outputs"]["machine_class"]), ("R0", "K2"))
        result_row = next(row for row in judge if row["step"] == "result")
        self.assertIs(result_row["outputs"]["auto_merge"], True)

        # trace：--ci 后时间线覆盖三种来源，最长阶段与首失败来自实际事件
        code, view, err = self.run_trace(fx.platform, str(fx.pr), "--ci", "--json")
        self.assertEqual(code, 0, err)
        self.assertIn("1 个其他 head", err)
        self.assertEqual(view["trace_id"], branch)
        # 首失败是冻结时钟下最早的失败事件（dispatch 第 1 轮的 local_verify；子进程事件用真实时钟更晚）
        self.assertEqual(view["first_failure"]["step"], "local_verify")
        self.assertEqual(view["first_failure"]["status"], "fail")
        totals: dict[str, int] = {}
        for row in view["events"]:
            if isinstance(row["duration_ms"], int):
                totals[row["stage"]] = totals.get(row["stage"], 0) + row["duration_ms"]
        longest = max(sorted(totals), key=lambda name: totals[name])
        self.assertEqual(view["longest_stage"], {"stage": longest, "duration_ms": totals[longest]})
        self.assertEqual(view["pr"], fx.pr)

        # audit 无适用 error（旧 head 不算失败）；重复审计幂等、重复同步不新增事件
        report = self.inspect(fx)
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertTrue(report["ok"])
        sources = {row["source"] for row in report["stages"] if row["evidence"] == "event"}
        self.assertTrue({"local", "ci:9001:1:job_x", "ci:9002:2:job_y", "ci:9003:1:judge"} <= sources)
        self.assertTrue(any(source.startswith("github:901:") for source in sources))
        self.assertEqual(report["coverage"]["chains"], 6)
        self.assertEqual(report["coverage"]["ledger"], "verified")
        self.assertTrue(any(item["fixed_in"] == "run_record" for item in report["anchors"]))
        self.assertTrue(any(item["fixed_in"] == "ci_artifact" for item in report["anchors"]))
        self.assertTrue({"material_task", "material_pack", "material_pr"}
                        <= {item["kind"] for item in report["references"]})
        self.assertEqual(self.inspect(fx)["findings"], [])
        self.assertEqual(len(self.rows(branch, stage="merge", source=None)), 2)  # merge+label 同步一次

        # 账本文件与发布锚点：git show 从 bare 远端取回并逐字段核对平台事实
        self.git("fetch", "-q", "origin", cwd=fx.project)
        doc = json.loads(self.git("show", f"origin/harness-audit:{fx.merged_at[:4]}/{fx.pr}.json",
                                  cwd=fx.project))
        self.assertEqual((doc["risk"], doc["class"], doc["pr"], doc["trace_id"]), ("R0", "K2", fx.pr, branch))
        self.assertEqual(doc["head_sha"], fx.branch_head)
        self.assertEqual(doc["merge_sha"], fx.merge_sha)
        self.assertEqual(doc["approval"]["approver_type"], "Bot")
        self.assertIs(doc["approval"]["approval_bound"], True)
        kinds = {item["kind"] for item in doc["references"]}
        self.assertEqual(kinds, {"run_record", "review_comment"})
        review_stage = next(item for item in doc["stages"]
                            if item.get("evidence_kind") == "review_comment_summary")
        self.assertEqual({item["kind"] for item in review_stage["materials"]},
                         {"task", "diff", "ci", "pr", "pack"})
        self.assertEqual(f"<!-- harness-audit:{fx.pr} -->", fx.anchor["body"].splitlines()[0])

        # weekly：事件小节追加在旧文本之后，数字与持久来源（账本/运行记录）一致
        week_gh = FakeWeeklyGh([{"number": fx.pr, "title": PR_TITLE, "labels": [{"name": "class:K2"}],
                                 "mergedAt": fx.merged_at, "createdAt": fx.merged_at,
                                 "headRefName": branch, "mergedBy": {"login": "app/github-actions"},
                                 "author": {"login": "fixture-agent"}, "reviews": [],
                                 "mergeCommit": {"oid": fx.merge_sha}}], fx.branch_head)
        text = weekly.build(datetime.now(UTC) + timedelta(minutes=5), gh=week_gh, cwd=fx.project,
                            comments=[])
        marker = text.index("### 事件汇总（B46）")
        self.assertGreater(text.index("<!-- weekly-data"), 0)
        self.assertLess(text.index("<!-- weekly-data"), marker)
        self.assertIn("本周合并 PR 1 个，有账本 1 个，无账本 0 个", text[marker:])
        expected = weekly_events._durations(weekly_events.ledger_stage_durations([doc]))
        self.assertIn("账本阶段耗时（A）：" + expected, text[marker:])
        self.assertIn("尝试 1 次；exit 分布：ok", text[marker:])
        self.assertIn("被拒工具调用数：0", text[marker:])
        self.assertIn(f"PR {fx.pr}（head", text[marker:])

        # alert：事件包里没有该 reason 的触发证据时诚实跳过，不发布评论
        publisher = self.alert_with_bundle(fx, "audit_missing_stage")
        self.assertEqual(publisher.comments, [])
        self.assertEqual(publisher.labels, [])
        skips = events_io.query(stage="alert", status="skip")
        self.assertTrue(any(row["step"] == "audit_missing_stage" for row in skips))

    # ---------- 验收 2：设计方 PR 可审、冷机复原、幂等导入、旧 head 信息性、畸形 head 报 api ----------

    def test_designer_pr_and_cold_machine_reconstruction(self):
        fx = self.build_task_world(malformed_head=True)
        project = fx.project

        # 畸形 head 是非信息性 api，旧 head 仍是信息性：load_ci 与 trace 都如实报告
        result = events_io.load_ci(fx.pr, head=fx.branch_head, gh=fx.platform)
        self.assertEqual(sorted(item["code"] for item in result["findings"]), ["api", "head_mismatch"])
        code, _view, err = self.run_trace(fx.platform, str(fx.pr), "--ci", "--json")
        self.assertEqual(code, 1)
        self.assertIn("1 个其他 head", err)
        self.assertIn("缺失或形状不符", err)
        report = self.inspect(fx)
        self.assertEqual([item["rule"] for item in report["findings"]], ["reference_unavailable"])
        self.assertEqual(report["findings"][0]["source"], "ci")
        self.assertIn("api", report["findings"][0]["reason"])

        # 无派发的设计方 PR：可审、不要求运行记录（本地库健全时无任何发现）
        designer_branch = "codex/designer-fixture"
        self.git("checkout", "-q", "-b", designer_branch, cwd=project)
        (project / "README.md").write_text("# fixture（设计方说明）\n", encoding="utf-8")
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "docs：设计方说明（无派发）", cwd=project)
        designer_head, designer_merge = self.merge_pr(project, designer_branch, 902, "docs：设计方说明")
        designer_review = self.review_pr_real(project, 902, designer_branch, designer_head,
                                              title="docs：设计方说明", body="docs")
        designer_pull = {"number": 902, "state": "closed", "merged": True, "merged_at": self.clock.iso(),
                         "merge_commit_sha": designer_merge, "title": "docs：设计方说明", "body": "docs",
                         "labels": [], "head": {"ref": designer_branch, "sha": designer_head},
                         "merged_by": {"login": "alice", "type": "User"}}
        designer_gh = self.platform(fx, pulls={902: designer_pull}, reviews={902: []},
                                    comments=[self.as_comment(600, designer_review)], runs=[], judge=False)
        designer_gh.commits = {designer_merge: {"parents": [{"sha": "d1"}, {"sha": "d2"}],
                                                "commit": {"message": "Merge pull request #902"}}}
        built = ledger.build_ledger(902, gh=designer_gh, cwd=project)
        self.assertTrue(any(item["item"] == "run_record" for item in built["missing"]))
        published = ledger.publish_ledger(built, gh=designer_gh)
        self.assertTrue(published["ok"], published["findings"])
        designer_gh = self.platform(fx, pulls={902: designer_pull}, reviews={902: []},
                                    comments=[self.as_comment(600, designer_review),
                                              designer_gh.comments[-1]], runs=[], judge=False)
        designer_gh.commits = {designer_merge: {"parents": [{"sha": "d1"}, {"sha": "d2"}],
                                                "commit": {"message": "Merge pull request #902"}}}
        report = self.inspect(fx, designer_gh, pr=902)
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertFalse(any(item["rule"].startswith("missing_") for item in report["findings"]))

        # 删除本机事件库：从记录/账本/CI 包复原——设计方 PR 审计仍无发现，任务 PR 的记录锚点如实报告链缺失
        db = events_db.db_path()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        restored = events_io.load_ci(fx.pr, head=fx.branch_head, gh=fx.platform)
        self.assertGreater(restored["imported"], 0)
        again = events_io.load_ci(fx.pr, head=fx.branch_head, gh=fx.platform)
        self.assertEqual((again["imported"], sorted(item["code"] for item in again["findings"])),
                         (0, ["api", "head_mismatch"]))
        cold_designer = self.inspect(fx, designer_gh, pr=902)
        self.assertEqual(cold_designer["findings"], [], cold_designer["findings"])
        cold_task = self.inspect(fx)
        # 畸形 head 的 api 发现冷机后仍如实报告（不因历史/换机被吞），记录锚点报告本机链缺失
        self.assertEqual(sorted(item["rule"] for item in cold_task["findings"]),
                         ["anchor_mismatch", "reference_unavailable"])
        anchor_finding = next(item for item in cold_task["findings"] if item["rule"] == "anchor_mismatch")
        self.assertIn("不在对应链上", anchor_finding["reason"])
        code, _view, _err = self.run_trace(designer_gh, "902", "--ci", "--json")
        self.assertEqual(code, 0)

    # ---------- 验收 3：缺环节与篡改逐项注入，稳定 finding，恢复后通过 ----------

    def test_injected_missing_stages_and_tamper_are_found(self):
        # (a) R2 路径缺独立评审：机器判定 R2、无评审评论 → missing_review；补评审后恢复
        world = self.build_lean_world(
            branch="feature/r2-change", pr=911, files={"src/app.py": "VALUE = 1\n"},
            message="feat：产品改动（无 trailer）", merged_by={"login": "alice", "type": "User"},
            klass="K5")
        report = self.inspect(world)
        self.assertEqual([item["rule"] for item in report["findings"]], ["missing_review"])
        self.assertIn("没有评审摘要", report["findings"][0]["reason"])
        publisher = self.alert_with_bundle(world, "audit_missing_stage")
        self.assertEqual(len(publisher.comments), 1)
        recovery = dispatch_observation.review_audit(
            trace_id=world.branch, head=world.branch_head, base=world.base, reviewer="opencode",
            model="review-model/1", model_basis="reported", designer="codex", implementers=[],
            independent=True, same_host=False, parsed=True, verdict="通过", duration_ms=1000,
            findings=[], materials=[])
        body = ("### 独立评审（试行）：通过\n\n<!-- independent-review {} -->\n"
                "<!-- harness-review-audit " + json.dumps(recovery, ensure_ascii=False) + " -->\n")
        gh = self.platform(world, comments=[self.as_comment(501, body)], runs=world.platform.runs,
                           artifacts=world.platform.artifacts, downloads=world.platform.downloads,
                           reviews={world.pr: []}, judge=False)
        self.assertEqual(self.inspect(world, gh)["findings"], [])

        # (b) 自动合并缺路由：世界生成时路由包未导入、本库先移除 route/判定链（导出时已写入本库），
        # 平台只提供 verify job 的运行 → missing_route；放回平台后真实导入恢复
        fx = self.build_task_world(with_ledger=False)
        self.db_exec("DELETE FROM events WHERE source LIKE 'ci:9002:%' OR source LIKE 'ci:9003:%'")
        self.db_exec("DELETE FROM anchors WHERE source LIKE 'ci:9002:%' OR source LIKE 'ci:9003:%'")
        runs = [run for run in fx.platform.runs if run["id"] in (9001, 9005)]
        artifacts, downloads = self.platform_network(
            {name: data for name, data in fx.packages.items() if name.startswith("harness-events-9001")}, runs)
        gh = self.platform(fx, comments=[self.as_comment(500, fx.review_body)], runs=runs,
                           artifacts=artifacts, downloads=downloads, judge=False)
        report = self.inspect(fx, gh)
        self.assertEqual([item["rule"] for item in report["findings"]], ["missing_route"])
        publisher = self.alert_with_bundle(fx, "audit_missing_stage")
        self.assertEqual(len(publisher.comments), 1)
        self.assertEqual(self.inspect(fx)["findings"], [])

        # (c) 任务 PR 缺运行记录：Task trailer 与任务书都在、记录缺失 → missing_run_record；补记录后恢复
        world = self.build_lean_world(
            branch=BRANCH, pr=912, files={"tests/test_extra.py": FIXED_TEST},
            message=f"test：夹具测试\n\nTask: {TASK_ID}", merged_by={"login": "alice", "type": "User"},
            klass="K2")
        report = self.inspect(world)
        self.assertEqual([item["rule"] for item in report["findings"]], ["missing_run_record"])
        publisher = self.alert_with_bundle(world, "audit_missing_stage")
        self.assertEqual(len(publisher.comments), 1)
        recovery_world = self.build_lean_world(
            branch=BRANCH, pr=913, files={"tests/test_extra.py": FIXED_TEST},
            message=f"test：夹具测试\n\nTask: {TASK_ID}", merged_by={"login": "alice", "type": "User"},
            klass="K2", with_records=True)
        self.assertEqual(self.inspect(recovery_world)["findings"], [])

        # (d) 改内容：平台提供的评审评论正文被改 → 账本引用哈希不符（github/review）；恢复后通过
        fx = self.build_task_world()
        tampered = self.as_comment(500, fx.review_body + "\n（评论已被改动）")
        gh = self.platform(fx, comments=[tampered, fx.anchor], runs=fx.platform.runs,
                           artifacts=fx.platform.artifacts, downloads=fx.platform.downloads)
        report = self.inspect(fx, gh)
        self.assertEqual([item["rule"] for item in report["findings"]], ["hash_mismatch"])
        self.assertEqual((report["findings"][0]["source"], report["findings"][0]["stage"]),
                         ("github", "review"))
        self.assertEqual(self.inspect(fx)["findings"], [])

        # (e)/(f)/(g) 本机链删中间/删尾/锚点不符：各自独立命中，恢复后通过；删尾内部链仍合法
        fx = self.build_task_world()
        self.assertEqual(self.inspect(fx)["findings"], [])
        backup = self.backup_db(fx.project)
        local_seqs = [row["seq"] for row in self.rows(fx.branch, source="local")]
        self.db_exec("DELETE FROM events WHERE source='local' AND seq=?", (local_seqs[len(local_seqs) // 2],))
        report = self.inspect(fx)
        self.assertEqual([item["rule"] for item in report["findings"]], ["chain_invalid"])
        publisher = self.alert_with_bundle(fx, "audit_anchor_mismatch")
        self.assertEqual(len(publisher.comments), 1)
        self.restore_db(fx.project, backup)
        self.assertEqual(self.inspect(fx)["findings"], [])

        record = json.loads(self.git("show", f"{fx.merge_sha}:{RECORD_REL}", cwd=fx.project))
        anchored = next(row["seq"] for row in self.rows(fx.branch, source="local")
                        if row["hash"] == record["anchors"][0]["head_hash"])
        self.db_exec("DELETE FROM events WHERE source='local' AND seq>=?", (anchored,))
        self.assertEqual(events_db.verify(trace_id=fx.branch, source="local"), [])  # 内部链仍合法
        report = self.inspect(fx)
        self.assertEqual([item["rule"] for item in report["findings"]], ["anchor_mismatch"])
        publisher = self.alert_with_bundle(fx, "audit_anchor_mismatch")
        self.assertEqual(len(publisher.comments), 1)
        self.restore_db(fx.project, backup)
        self.assertEqual(self.inspect(fx)["findings"], [])

        events.set_anchor(fx.branch, "dispatch", "e" * 64, "run_record")
        report = self.inspect(fx)
        self.assertEqual([item["rule"] for item in report["findings"]], ["anchor_mismatch"])
        publisher = self.alert_with_bundle(fx, "audit_anchor_mismatch")
        self.assertEqual(len(publisher.comments), 1)
        self.restore_db(fx.project, backup)
        self.assertEqual(self.inspect(fx)["findings"], [])

        # (i) github: 快照九字段语义核对：同义快照（跨环境时间戳不同）不误报，真实字段变化会报
        self.db_exec("DELETE FROM refs")
        self.db_exec("DELETE FROM events")
        self.db_exec("DELETE FROM anchors")
        report = self.inspect(fx)
        self.assertNotIn("ledger_mismatch", [item["rule"] for item in report["findings"]])
        self.db_exec("UPDATE events SET outputs=json_set(outputs,'$.merger','mallory') WHERE step='github.merge'")
        self.assertIn("ledger_mismatch", [item["rule"] for item in self.inspect(fx)["findings"]])

        # (h) 引用哈希不符：账本里的运行记录引用指向别的内容 → hash_mismatch(local/dispatch)；修正后通过
        def mutate(doc: dict) -> None:
            ref = next(item for item in doc["references"] if item["kind"] == "run_record")
            ref["sha256"] = digest(b"mutated")
            ref["size"] = 7

        fx = self.build_task_world(mutate_ledger=mutate)
        report = self.inspect(fx)
        self.assertEqual([item["rule"] for item in report["findings"]], ["hash_mismatch"])
        self.assertEqual((report["findings"][0]["source"], report["findings"][0]["stage"]),
                         ("local", "dispatch"))
        self.git("update-ref", "-d", "refs/heads/harness-audit", cwd=fx.origin)
        rebuilt = ledger.build_ledger(fx.pr, gh=fx.platform, cwd=fx.project)
        republish = self.platform(fx, comments=[self.as_comment(500, fx.review_body)],
                                  runs=fx.platform.runs, artifacts=fx.platform.artifacts,
                                  downloads=fx.platform.downloads)
        result = ledger.publish_ledger(rebuilt, gh=republish)
        self.assertTrue(result["ok"], result["findings"])
        gh = self.platform(fx, comments=[self.as_comment(500, fx.review_body), republish.comments[-1]],
                           runs=fx.platform.runs, artifacts=fx.platform.artifacts,
                           downloads=fx.platform.downloads)
        self.assertEqual(self.inspect(fx, gh)["findings"], [])

    # ---------- 验收 4：告警幂等与不外发原始数据 ----------

    def test_alert_idempotence_and_no_raw_data_publication(self):
        fx = self.build_task_world()
        events.set_anchor(fx.branch, "dispatch", "d" * 64, "run_record")
        report = self.inspect(fx)
        self.assertEqual([item["rule"] for item in report["findings"]], ["anchor_mismatch"])

        # 同 trace 同 reason 反复发布：远端标记去重，只一条评论、一次标签，第二次为更新
        bundle = self.tmp / "bundle-alert.json"
        events_io.write_bundle(events_io.export_bundle(), bundle)
        publisher = AlertPublisher()
        argv = ["audit_anchor_mismatch", "--trace", fx.branch, "--bundle", str(bundle),
                "--pr", str(fx.pr), "--task", fx.task_id, "--json"]
        results = []
        for _round in range(2):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(alerts.main(list(argv), gh=publisher), 0)
            results.append(json.loads(out.getvalue()))
        self.assertEqual(results[0], {"ok": True, "target": "pr", "updated": False, "error_kind": None})
        self.assertEqual((results[1]["ok"], results[1]["updated"]), (True, True))
        self.assertEqual(len(publisher.comments), 1)
        self.assertEqual(publisher.labels, ["escalation"])
        self.assertEqual(len(publisher.edits), 1)
        self.assertIn(f"**trace**：`{fx.branch}`", publisher.comments[0])

        # 外发面扫描：事件包目录、账本与 summary 不含 db/WAL/SHM、原始日志、会话正文与本机路径
        judge_dir = self._package_dirs["judge"]
        exported = sorted(path.name for path in judge_dir.iterdir())
        self.assertTrue(all(name.endswith(".json") for name in exported), exported)
        bundle_text = (judge_dir / "harness-events.json").read_text(encoding="utf-8")
        ledger_text = self.git("show", f"origin/harness-audit:{fx.merged_at[:4]}/{fx.pr}.json",
                               cwd=fx.project)
        summary = ci_events.render_summary(json.loads(bundle_text))
        for text in (bundle_text, ledger_text, summary, publisher.comments[0]):
            for banned in ("harness.db", ".db\"", "-wal", "-shm", "round-1.jsonl", "verify log",
                           "user:", "assistant:", str(self.tmp), "/Users/"):
                self.assertNotIn(banned, text)
        parsed = json.loads(bundle_text)
        for artifact in parsed["artifacts"]:
            content = (judge_dir / artifact["file"]).read_bytes()
            self.assertEqual(digest(content), artifact["sha256"])
            self.assertIsInstance(json.loads(content), dict)
        # 原始执行方流只有哈希与大小：executor_round 的流产物不进 manifest（内容不是安全 JSON）
        stream_rows = [row for row in self.rows(fx.branch, source="local") if row["step"] == "executor_round"]
        self.assertTrue(stream_rows)
        streamed = {row["outputs"]["stream.sha256"] for row in stream_rows}
        self.assertTrue(streamed)
        self.assertFalse(streamed & {item["sha256"] for item in parsed["artifacts"]})

        # 平台隔离：全部 gh 交互只落在假平台的 owner/repo 路由上，无真实平台访问
        for call in fx.platform.calls:
            if call[0] == "GET":
                self.assertTrue(str(call[1]).startswith(f"repos/{REPO}"), call)
            self.assertNotIn("https://api.github.com", str(call))

        # 恢复（移除注入的假锚点）后 audit 通过；重复导入当前 head 幂等
        self.db_exec("DELETE FROM anchors WHERE head_hash=?", ("d" * 64,))
        self.assertEqual(self.inspect(fx)["findings"], [])
        again = events_io.load_ci(fx.pr, head=fx.branch_head, gh=fx.platform)
        self.assertEqual((again["imported"], sorted(item["code"] for item in again["findings"])),
                         (0, ["head_mismatch"]))


if __name__ == "__main__":
    unittest.main()
