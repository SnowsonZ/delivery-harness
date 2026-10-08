"""T501 周报事件汇总测试：账本阶段耗时、运行记录指标、本机补充与审计发现数、旧小节逐字前缀。

夹具全部为匿名临时 git 仓库 + 隔离 events_db.ROOT + 可推进的冻结时钟 + 假 gh：账本世界里至少一份
账本由真实 ledger.build_ledger + publish_ledger 写出（证明读取方与写入方形状一致），其余手写 JSON 以便
精确算术；本机补充经真实 events.emit 写入临时库，重复导入经真实 events_io.export_bundle/import_bundle；
不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import run_timeline  # noqa: F401  （账本夹具的真实生产者之一，保留可用性核对）
from engine.checks import r1_checks
from engine.core import events, events_db
from engine.reports import ledger, weekly, weekly_events
from tests.gh_fakes import FakeGhBase

END = dt.datetime(2026, 10, 13, 3, 0, tzinfo=dt.UTC)
START = END - dt.timedelta(days=7)
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
REPO = "owner/repo"
GITHUB_ORIGIN = "https://github.com/owner/repo.git"
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")


class Clock:
    """可推进的冻结时钟：缺省从窗口内 2026-10-06T08:00 起，每次 emit 前进一分钟。"""

    def __init__(self, start: str = "2026-10-06T08:00:00"):
        self.current = dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC)

    def __call__(self) -> str:
        self.current += dt.timedelta(minutes=1)
        return self.current.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def merged_pr(number: int, *, merged: str, branch: str, auto: bool = True) -> dict:
    """周报 collect 看到的已合并 PR（mergeCommit 留空：不触发 _modifies_tests 的 git 调用）。"""
    return {"number": number, "title": f"PR {number}", "labels": [{"name": "class:K7"}],
            "mergedAt": merged, "createdAt": merged, "headRefName": branch,
            "author": {"login": "pi"},
            "mergedBy": {"login": "app/github-actions" if auto else "alice"},
            "reviews": [], "commits": [], "mergeCommit": None}


class FakeWeeklyGh:
    """weekly.collect/compute/publish 用 gh 桩：只返回构造时给定的数据，记录全部调用。"""

    def __init__(self, prs=(), escapes=(), escalations=(), audits=(), quality=(), builds=()):
        self.prs, self.builds, self.quality = list(prs), list(builds), list(quality)
        self.issues = {"escape": list(escapes), "escalation": list(escalations), "audit": list(audits)}
        self.calls, self.writes = [], []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "list"):
            if "--head" in args:
                branch = args[args.index("--head") + 1]
                return json.dumps([{"number": pr["number"]} for pr in self.prs if pr["headRefName"] == branch])
            return json.dumps(self.prs)
        if args[:2] == ("pr", "view"):
            found = next(pr for pr in self.prs if str(pr["number"]) == args[2])
            if "commits" in args[args.index("--json") + 1].split(","):
                return json.dumps({"commits": found.get("commits", [])})
            return json.dumps({**found, "state": "MERGED"})
        if args[:2] == ("issue", "list"):
            label = args[args.index("--label") + 1]
            return json.dumps(self.issues.get(label, []))
        if args[:2] == ("run", "list"):
            return json.dumps(self.builds if "--branch" in args else self.quality)
        if args[:2] == ("label", "create"):
            return ""
        if args[:2] == ("issue", "create"):
            self.writes.append(("create", args))
            return f"{GITHUB_ORIGIN}/issues/70\n"
        if args[:2] in (("issue", "edit"), ("issue", "comment")):
            self.writes.append((args[1], args))
            return ""
        return "[]"


class FakeLedgerGh(FakeGhBase):
    """build_ledger/publish_ledger 用 gh 桩：合并事实齐全，其余事实源为空（missing 如实入账本）。"""

    def __init__(self, pulls: dict, commits: dict):
        super().__init__(pulls=pulls, commits=commits,
                         pr_commits={number: [] for number in pulls},
                         reviews={number: [] for number in pulls},
                         issues={"audit": [], "escape": []})


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-weekly-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        patcher = mock.patch.object(events_db, "_now", new=self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [name for name in os.environ if name.startswith(CLEAN_PREFIXES)]:
            os.environ.pop(key, None)
        os.environ.update(GIT_ENV)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    # ---- 通用夹具 ----

    def git(self, *args: str, cwd: Path, check: bool = True) -> str:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              env={**os.environ, **GIT_ENV}, check=False)
        if check and done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout.strip()

    def fresh_repo(self, name: str, branch: str = "main") -> Path:
        path = self.tmp / name
        path.mkdir()
        self.git("init", "-q", "-b", branch, cwd=path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "-A", cwd=path)
        self.git("commit", "-q", "-m", "init", cwd=path)
        return path

    def bare_origin(self, project: Path) -> Path:
        origin = self.tmp / f"{project.name}-origin.git"
        self.git("init", "--bare", "-q", "--initial-branch=main", str(origin), cwd=project)
        self.git("remote", "add", "origin", str(origin), cwd=project)
        return origin

    def use_root(self, project: Path) -> None:
        patch = mock.patch.object(events_db, "ROOT", project)
        patch.start()
        self.addCleanup(patch.stop)

    def state_files(self, project: Path) -> None:
        """weekly.build 读取的质量/变异基线（cwd 相对路径，不入库也不影响 git 读取）。"""
        state = project / ".harness" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "quality-baseline.json").write_text('{"complex_functions": 1, "files_over_800": 0}',
                                                     encoding="utf-8")
        (state / "mutation-baseline.json").write_text("{}", encoding="utf-8")

    def write_record(self, project: Path, folder: str, **fields) -> Path:
        number = fields.setdefault("attempt", 1)
        path = project / "docs" / "runs" / folder / f"{number}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fields, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def old_section(self, week, gh, project: Path, comments=None) -> str:
        """与 weekly.build 相同口径的旧周报文本（验收：新输出以它为逐字前缀、N 与旧合并数相等）。"""
        quality = json.loads((project / ".harness/state/quality-baseline.json").read_text(encoding="utf-8"))
        mutation = json.loads((project / ".harness/state/mutation-baseline.json").read_text(encoding="utf-8"))
        data = weekly.compute(week, quality, mutation)
        history = weekly.history_from_comments(comments or [], week.key)
        history_path = project / "docs" / "runs" / "history.json"
        history_map = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
        return weekly.render(week, data, weekly.spikes(data["_signals"], history),
                             weekly.budget_rows(r1_checks.load_autonomy(), gh),
                             weekly.trial_rows(history_map, week.records, gh))

    # ---- 账本世界：真实 build_ledger + publish_ledger 写出的账本 + 手写账本 ----

    def ledger_world(self) -> Path:
        """项目仓库（main + 任务分支合并提交 + bare origin）：harness-audit 分支上有 5 份账本。

        PR 301 的账本由真实 build_ledger + publish_ledger 写出（ci 来源事件经真实 emit 进入本机临时库，
        由 build_ledger 导入账本，evidence_kind=event）；302–305 为手写 JSON、同样经真实 publish_ledger
        追加到分支；306 故意不留账本（无账本原因分类）、305 的账本只用于窗口排除（mergedAt 在终点上）。
        """
        project = self.fresh_repo("app")
        self.bare_origin(project)
        self.use_root(project)
        self.state_files(project)
        base = self.git("rev-parse", "HEAD", cwd=project)
        branch = "task/301-real-ledger"
        self.git("checkout", "-q", "-b", branch, cwd=project)
        # ci 来源链（真实 emit）：build_ledger 会把它们作为账本事件（evidence_kind=event）
        self.assertIsNotNone(events.emit("verify", "tests", "ok", trace_id=branch,
                                         source="ci:9001:1:job_x", duration_ms=100))
        self.assertIsNotNone(events.emit("verify", "tests", "fail", trace_id=branch,
                                         source="ci:9001:1:job_x", duration_ms=50))
        # 合并 head 上的运行记录（ended_at 在窗口外：不进本周运行记录指标）
        record = {"task": "T301", "class": "K7", "attempt": 1, "branch": branch, "trace_id": branch,
                  "started_at": "2026-10-04T00:00:00+00:00", "ended_at": "2026-10-04T00:10:00+00:00",
                  "exit": "ok", "executor_seconds": 123.0}
        self.write_record(project, "task-301-real-ledger", **record)
        self.git("add", "-A", cwd=project)
        self.git("commit", "-q", "-m", "run：task-301 第 1 次派发记录（ok）", cwd=project)
        branch_head = self.git("rev-parse", "HEAD", cwd=project)
        self.git("checkout", "-q", "main", cwd=project)
        self.git("merge", "--no-ff", "-q", "-m", f"Merge pull request #301 from {branch}\n\nbody",
                 branch, cwd=project)
        merge_sha = self.git("rev-parse", "HEAD", cwd=project)
        self.git("push", "-q", "origin", "main", cwd=project)
        pulls = {301: {"number": 301, "state": "closed", "merged": True,
                       "merged_at": "2026-10-08T10:00:00Z", "merge_commit_sha": merge_sha,
                       "head": {"ref": branch, "sha": branch_head},
                       "merged_by": {"login": "alice", "type": "User"}, "labels": [{"name": "class:K7"}]}}
        gh = FakeLedgerGh(pulls, {merge_sha: {"parents": [{"sha": base}, {"sha": branch_head}]}})
        built = ledger.build_ledger(301, gh=gh, cwd=project)
        self.assertTrue(ledger.publish_ledger(built, gh=gh)["ok"])
        # 手写账本（精确算术）：同样经真实 publish_ledger 追加到 harness-audit 分支
        handwritten = {
            302: [{"stage": "verify", "step": "tests", "evidence_kind": "event", "duration_ms": 40},
                  {"stage": "verify", "step": "lint", "evidence_kind": "event"},
                  {"stage": "dispatch", "step": "executor_round", "evidence_kind": "run_record_summary",
                   "duration_ms": 999},
                  {"stage": "route", "step": "facts", "evidence_kind": "event", "duration_ms": 10}],
            303: [{"stage": "verify", "step": "tests", "evidence_kind": "event", "duration_ms": 60},
                  {"stage": "verify", "step": "lint", "evidence_kind": "event", "duration_ms": 20}],
            304: [{"stage": "guard", "step": "command", "evidence_kind": "event", "duration_ms": 33}],
            305: [{"stage": "review", "step": "review", "evidence_kind": "event", "duration_ms": 777}],
        }
        merged_at = {302: "2026-10-07T10:00:00Z", 303: "2026-10-09T10:00:00Z",
                     304: "2026-10-06T03:00:00Z", 305: "2026-10-13T03:00:00Z"}
        for number, stages in handwritten.items():
            entry = {"schema_version": 1, "repository": REPO, "pr": number,
                     "trace_id": f"task/{number}-x", "merged_at": merged_at[number], "stages": stages}
            self.assertTrue(ledger.publish_ledger(entry, gh=FakeLedgerGh({}, {}))["ok"])
        self.git("fetch", "-q", "origin", cwd=project)
        return project

    def week_for_ledgers(self, project: Path):
        """collect 出本周世界：301–304、306 在窗口内（304 恰在起点上），305 恰在终点上被排除。"""
        gh = FakeWeeklyGh(prs=[
            merged_pr(301, merged="2026-10-08T10:00:00Z", branch="task/301-real-ledger"),
            merged_pr(302, merged="2026-10-07T10:00:00Z", branch="task/302-x"),
            merged_pr(303, merged="2026-10-09T10:00:00Z", branch="task/303-x"),
            merged_pr(304, merged="2026-10-06T03:00:00Z", branch="task/304-x"),
            merged_pr(305, merged="2026-10-13T03:00:00Z", branch="task/305-x"),
            merged_pr(306, merged="2026-10-10T10:00:00Z", branch="task/306-x"),
        ])
        week = weekly.collect(END, gh, project)
        return week, gh

    # ---- 验收 1：账本阶段耗时与窗口边界 ----

    def test_ledger_stage_durations_and_window(self):
        project = self.ledger_world()
        week, gh = self.week_for_ledgers(project)
        self.assertEqual(len(week.prs), 5, "终点上的 PR 排除、起点上的 PR 计入（起点含、终点不含）")
        text = weekly_events.render_events(week, cwd=project)
        # 覆盖说明：N 与旧小节「人工干预率」的合并数相等；无账本给原因分类
        old = self.old_section(week, gh, project)
        merged_total = re.search(r"（(\d+) 个合并）", old)
        self.assertIsNotNone(merged_total)
        self.assertEqual(int(merged_total[1]), len(week.prs))
        self.assertIn(f"覆盖说明：本周合并 PR {len(week.prs)} 个，有账本 4 个，无账本 1 个"
                      "（分支上无该文件 1）", text)
        # 阶段耗时：真实账本（verify 100+50）+ 手写账本逐项算术
        self.assertIn("账本阶段耗时（A）：guard：事件 1、合计 33 ms、最大 33 ms；"
                      "route：事件 1、合计 10 ms、最大 10 ms；"
                      "verify：事件 5、合计 270 ms、最大 100 ms", text)
        # run_record_summary 不重复计入（999）、窗口外的账本不计入（777）、无 duration 的事件不计入
        self.assertNotIn("999", text)
        self.assertNotIn("777", text)
        self.assertNotIn("事件 6、合计", text)
        # 无账本的 PR 计入「无账本」且不影响其余统计；读取走真实 git show origin/harness-audit
        ledgers, missing = weekly_events.load_ledgers(week.prs, weekly_events.git_show_reader(project))
        self.assertEqual([item["pr"] for item in sorted(ledgers, key=lambda x: x["pr"])], [301, 302, 303, 304])
        self.assertEqual(dict(missing), {"分支上无该文件": 1})

    # ---- 验收 2：运行记录指标（逐项算术） ----

    def test_run_record_denials_escalation_and_context_totals(self):
        project = self.fresh_repo("app")
        self.state_files(project)
        base = {"class": "K7", "ci_rounds_before": 1, "retries": 0, "cost": 0.01}
        self.write_record(project, "task-201-a", **base, attempt=1, task="T201", branch="task/201-a",
                          started_at="2026-10-07T00:00:00+00:00", ended_at="2026-10-07T00:10:00+00:00",
                          executor_seconds=100.5, exit="ok", escalation=None,
                          guard_denials={"rule-a": 2, "rule-b": 1},
                          missing_context=[{"category": "spec", "summary": "spec_ambiguous", "ref": "docs/a.md"},
                                           {"category": "tool", "summary": "required_tool_unavailable",
                                            "ref": "gh"}],
                          missing_context_status="reported")
        self.write_record(project, "task-201-a", **base, attempt=2, task="T201", branch="task/201-a",
                          started_at="2026-10-08T00:00:00+00:00", ended_at="2026-10-08T00:10:00+00:00",
                          executor_seconds=59.5, exit="clarify", escalation="clarify",
                          guard_denials={"rule-a": 1}, missing_context=[],
                          missing_context_status="bogus-status")
        self.write_record(project, "task-202-b", **base, attempt=1, task="T202", branch="task/202-b",
                          started_at="2026-10-09T00:00:00+00:00", ended_at="2026-10-09T00:10:00+00:00",
                          executor_seconds=40, exit="timeout", escalation="budget",
                          guard_denials={}, )  # 历史记录：缺 missing_context/missing_context_status 字段
        # ended_at 恰在窗口终点上：不进本周指标
        self.write_record(project, "task-203-c", **base, attempt=1, task="T203", branch="task/203-c",
                          started_at="2026-10-13T02:00:00+00:00", ended_at="2026-10-13T03:00:00+00:00",
                          executor_seconds=999, exit="error", escalation="loop")
        gh = FakeWeeklyGh()
        week = weekly.collect(END, gh, project)
        self.assertEqual(len(week.records), 3, "窗口按 ended_at（起点含、终点不含）")
        text = weekly_events.render_events(week, cwd=project)
        self.assertIn("运行记录读取 3 份尝试记录，涉及 2 个任务（一个任务可有多次尝试，份数与任务数分开计）",
                      text)
        self.assertIn("executor_seconds 合计 200 秒；尝试 3 次", text)
        self.assertIn("exit 分布：clarify 1、ok 1、timeout 1", text)
        self.assertIn("守卫拒绝（A）：规则命中数合计 4（按拒绝理由计数，一次被拒的工具调用可命中多条理由）："
                      "rule-a 3、rule-b 1", text)
        self.assertIn("设计方一侧：不可得（只记在本机）", text)
        self.assertNotRegex(text, r"设计方[^；\n]*0")
        self.assertIn("escalation 分布：budget 1、clarify 1、无 1", text)
        self.assertIn("缺上下文分类（A）：spec 1、tool 1", text)
        self.assertIn("状态分布：reported 1、覆盖不足 2", text)
        self.assertNotIn("999", text)
        self.assertIn("账本阶段耗时（A）：不可用（本周没有可用账本）", text,
                      "无 harness-audit 分支时写不可用，不补造 0")


if __name__ == "__main__":
    unittest.main()
