"""T501 周报事件汇总测试：账本阶段耗时、运行记录指标、本机补充与审计发现数、旧小节逐字前缀。

夹具全部为匿名临时 git 仓库 + 隔离 events_db.ROOT + 可推进的冻结时钟 + 假 gh：账本世界里至少一份
账本由真实 ledger.build_ledger + publish_ledger 写出（证明读取方与写入方形状一致），其余手写 JSON 以便
精确算术；本机补充经真实 events.emit 写入临时库，重复导入经真实 events_io.export_bundle/import_bundle；
不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import run_timeline  # noqa: F401  （账本夹具的真实生产者之一，保留可用性核对）
from engine.checks import r1_checks
from engine.core import events, events_db, events_io
from engine.reports import audit, ledger, weekly, weekly_events
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

    # ---- 本机事件库世界 ----

    TRACE = "task/303-a"

    def local_world(self, name: str = "app") -> Path:
        """项目仓库 + 本机库：经真实 emit 写入事件（缺省 source=local、trace 固定）。"""
        project = self.fresh_repo(name)
        self.use_root(project)
        self.state_files(project)
        return project

    def deny(self, step: str, rule: str, role: str | None) -> None:
        actor = {"role": role} if role else None  # git 守卫不传 actor（缺省角色 engine）
        self.assertIsNotNone(events.emit("guard", step, "deny", trace_id=self.TRACE,
                                         decision={"by": "guard", "rule": rule}, actor=actor))

    def empty_week(self):
        week = weekly.Week(END)
        return week

    # ---- 验收 3：本机补充分列、口径不混淆 ----

    def test_ledger_and_local_events_coexist_without_changing_main_totals(self):
        """同一任务同时有真实账本与本机耗时，A 不能因 B 存在而覆盖或重复计数。"""
        project = self.ledger_world()
        week, _gh = self.week_for_ledgers(project)
        with mock.patch.dict(os.environ, {"CI": "true"}):
            before = weekly_events.render_events(week, cwd=project)
        self.assertIsNotNone(events.emit("verify", "tests", "ok", trace_id="task/301-real-ledger",
                                         source="local", duration_ms=99999))
        after = weekly_events.render_events(week, cwd=project)
        main_part = lambda text: text.split("- 审计发现数（A）")[0]
        self.assertEqual(main_part(after), main_part(before))
        self.assertNotIn("99999", main_part(after))
        self.assertIn("verify：事件 1、合计 99999 ms、最大 99999 ms",
                      after.split("#### 本机补充（B）")[1])

    def test_local_supplement_is_separate_and_units_are_not_conflated(self):
        project = self.local_world()
        # 调用 1、命中 2：一次被拒调用（guard_denied=1）命中两条理由（两条 deny 事件）
        events.emit("dispatch", "executor_round", "ok", trace_id=self.TRACE, duration_ms=1000,
                    outputs={"exit": "ok", "guard_denied": 1})
        self.deny("command", "prohibit-a", "designer")
        self.deny("command", "prohibit-share", "designer")
        self.deny("command", "protect-engine", "implementer")
        self.deny("git", "no-main", None)
        self.deny("git", "no-main", None)
        # 调用 2、命中 0：两次被拒调用（guard_denied=2）零条理由事件
        events.emit("dispatch", "executor_round", "fail", trace_id=self.TRACE, duration_ms=1000,
                    outputs={"exit": "error", "guard_denied": 2})
        events.emit("verify", "tests", "ok", trace_id=self.TRACE, duration_ms=200)
        events.emit("review", "review", "ok", trace_id=self.TRACE, duration_ms=300)
        events.emit("dispatch", "escalate", "ok", trace_id=self.TRACE, outputs={"reason": "clarify"})
        events.emit("dispatch", "escalate", "ok", trace_id=self.TRACE, outputs={"reason": "budget"})
        # 跨周事件（ts 在窗口终点之后）：不计入任何统计
        self.clock.current = dt.datetime(2026, 10, 20, 0, 0, tzinfo=dt.UTC)
        events.emit("dispatch", "executor_round", "ok", trace_id=self.TRACE, duration_ms=500,
                    outputs={"exit": "ok", "guard_denied": 4})
        text = weekly_events.render_events(self.empty_week(), cwd=project)
        section_b = text.split("#### 本机补充（B）")[1]
        self.assertIn("窗口内 source=local 事件 11 个（跨周事件不计入）", section_b)
        self.assertIn("dispatch：事件 2、合计 2000 ms、最大 1000 ms", section_b)
        self.assertIn("review：事件 1、合计 300 ms、最大 300 ms", section_b)
        self.assertIn("verify：事件 1、合计 200 ms、最大 200 ms", section_b)
        self.assertIn("升级原因（本机 escalate 事件）：budget 1、clarify 1", section_b)
        self.assertIn("守卫拒绝（按种类×角色分列，各组分别列出、不相加）："
                      "command×designer：prohibit-a 1、prohibit-share 1；"
                      "command×implementer：protect-engine 1；"
                      "git×engine（不可归属）：no-main 2", section_b)
        self.assertIn("被拒工具调用数：3（executor_round 事件计数，一次被拒调用计一次；"
                      "与上方「规则命中数」口径不同，二者分别统计）", section_b)
        for word in ("不一致", "违反", "相符", "相等", "一致", "匹配"):
            self.assertNotIn(word, text, f"不得对两个守卫计数做比较判定：{word}")
        # 同一事件重复导入不双算：真实 export_bundle → import_bundle 幂等
        bundle = events_io.export_bundle(source="local", trace_id=self.TRACE)
        result = events_io.import_bundle(bundle)
        self.assertEqual(result, {"imported": 0, "skipped": len(bundle["events"]), "findings": []})
        again = weekly_events.render_events(self.empty_week(), cwd=project)
        self.assertIn("窗口内 source=local 事件 11 个", again)
        self.assertIn("被拒工具调用数：3", again)
        # A 的数字不因本机库存在而变化：删库前后 A 部分逐字相等，B 各写不可用/统计
        part_a_with_db = text.split("#### 本机补充（B）")[0].split("- 审计发现数（A）")[0]
        shutil.rmtree(project / ".git" / "harness", ignore_errors=True)
        without_db = weekly_events.render_events(self.empty_week(), cwd=project)
        self.assertEqual(without_db.split("#### 本机补充（B）")[0].split("- 审计发现数（A）")[0],
                         part_a_with_db, "A 的指标不因本机库存在而变化")
        self.assertIn("不可用（没有本机事件库）。", without_db)

    # ---- 验收 4：B 按运行环境（是否 CI）启停，不按库是否存在 ----

    def test_local_supplement_is_disabled_by_environment_not_by_database_presence(self):
        project = self.local_world()
        # 模拟 quality.yml 在周报前写进 runner 临时库的事件：本机与 ci 来源、含审计事件
        events.emit("dispatch", "executor_round", "ok", trace_id=self.TRACE, duration_ms=100,
                    outputs={"exit": "ok", "guard_denied": 1})
        events.emit("dispatch", "escalate", "ok", trace_id=self.TRACE, outputs={"reason": "clarify"})
        head = "1" * 40
        events.emit("ci", "audit.summary", "fail", trace_id=self.TRACE, source="ci:1:1:quality",
                    outputs={"pr": 305, "head_sha": head, "ok": False})
        events.emit("ci", "audit.finding", "fail", trace_id=self.TRACE, source="ci:1:1:quality",
                    outputs={"pr": 305, "head_sha": head, "rule": "missing_anchor"})
        events.emit("verify", "tests", "ok", trace_id=self.TRACE, source="ci:9:1:job", duration_ms=777777)
        with mock.patch.dict(os.environ, {"CI": "true"}):
            in_ci = weekly_events.render_events(self.empty_week(), cwd=project)
        section_b = in_ci.split("#### 本机补充（B）")[1]
        self.assertIn("不可用（CI 环境不统计本机库）。", section_b)
        for absent in ("被拒工具调用数", "阶段耗时", "升级原因", "审计发现数（同 PR/head", "source=local 事件"):
            self.assertNotIn(absent, section_b)
        self.assertIn("- 审计发现数（A）：不可用（CI 不实算审计；见本机补充）", in_ci)
        local = weekly_events.render_events(self.empty_week(), cwd=project)
        head_a = lambda s: s.split("#### 本机补充（B）")[0].split("- 审计发现数（A）")[0]
        self.assertEqual(head_a(local), head_a(in_ci), "CI 只关 B，A 的指标不变")
        section_b = local.split("#### 本机补充（B）")[1]
        self.assertIn("来源 3 个（ci、local）", section_b)
        self.assertIn("窗口内 source=local 事件 2 个", section_b)
        self.assertNotIn("777777", section_b, "导入的 ci 来源事件不计入本机统计")
        # 非 CI：无库、较新版本库写「不可用」并带原因；合法空库写「0 个事件」（表述不同）
        no_db = self.fresh_repo("no-db")
        self.use_root(no_db)
        self.assertIn("不可用（没有本机事件库）。",
                      weekly_events.render_events(self.empty_week(), cwd=no_db)
                      .split("#### 本机补充（B）")[1])
        newer = self.local_world("newer-db")
        events.emit("verify", "tests", "ok", trace_id=self.TRACE)
        with closing(sqlite3.connect(events_db.db_path())) as conn:
            conn.execute("PRAGMA user_version=2")
        self.assertIn("不可用（本机事件库 schema 较新（user_version=2 > 1））。",
                      weekly_events.render_events(self.empty_week(), cwd=newer).split("#### 本机补充（B）")[1])
        empty = self.local_world("empty-db")
        events_db.import_rows([], [])  # 真实路径建出合法空库（schema v1、零事件）
        empty_section = weekly_events.render_events(self.empty_week(), cwd=empty) \
            .split("#### 本机补充（B）")[1]
        self.assertIn("0 个事件", empty_section)
        self.assertNotIn("不可用", empty_section)
        corrupted = self.local_world("corrupted-db")
        db = events_db.db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"this is not a sqlite database")
        self.assertIn("不可用（本机事件库无法读取",
                      weekly_events.render_events(self.empty_week(), cwd=corrupted)
                      .split("#### 本机补充（B）")[1])

    # ---- 验收 5：审计发现数（CI 不可用；本机同 PR/head 取最新、不残留） ----

    def test_audit_findings_unavailable_in_ci_and_deduped_locally(self):
        project = self.local_world()
        h1, h2, h3, h4 = "1" * 40, "3" * 40, "5" * 40, "7" * 40

        def summary(pr, head):
            events.emit("ci", "audit.summary", "fail", trace_id=self.TRACE,
                        outputs={"pr": pr, "head_sha": head, "ok": False})

        def finding(pr, head, rule):
            events.emit("ci", "audit.finding", "fail", trace_id=self.TRACE,
                        outputs={"pr": pr, "head_sha": head, "rule": rule, "severity": "error"})

        summary(305, h1)  # 旧结果有发现……
        finding(305, h1, "missing_anchor")
        finding(305, h1, "ledger_mismatch")
        summary(305, h1)  # ……同 head 最新结果无发现：按无发现算，不残留旧 finding
        summary(401, h2)  # 同 head 重复审计不累加：最新一次只有 hash_mismatch
        finding(401, h2, "missing_review")
        finding(401, h2, "approval_actor")
        summary(401, h2)
        finding(401, h2, "hash_mismatch")
        summary(305, h3)  # 更新 head 的分别标明
        finding(305, h3, "anchor_mismatch")
        summary(402, h4)  # 同一规则键去重（同次两条同规则只计一条）
        finding(402, h4, "missing_route")
        finding(402, h4, "missing_route")
        text = weekly_events.render_events(self.empty_week(), cwd=project)
        section_b = text.split("#### 本机补充（B）")[1]
        self.assertIn("审计发现数（同 PR/head 只取最新一次）："
                      "PR 305（head 1111111…）：0 条；PR 305（head 5555555…）：1 条（anchor_mismatch）；"
                      "PR 401（head 3333333…）：1 条（hash_mismatch）；"
                      "PR 402（head 7777777…）：1 条（missing_route）", section_b)
        for stale in ("missing_anchor", "ledger_mismatch", "missing_review", "approval_actor"):
            self.assertNotIn(stale, text, f"旧结果残留：{stale}")
        # 同一批事件重复导入不累加
        bundle = events_io.export_bundle(source="local", trace_id=self.TRACE)
        self.assertEqual(events_io.import_bundle(bundle)["imported"], 0)
        again = weekly_events.render_events(self.empty_week(), cwd=project)
        self.assertEqual(again.split("#### 本机补充（B）")[1], section_b)
        # CI 写「不可用」，不写 0、不出任何计数
        with mock.patch.dict(os.environ, {"CI": "true"}):
            in_ci = weekly_events.render_events(self.empty_week(), cwd=project)
        self.assertIn("- 审计发现数（A）：不可用（CI 不实算审计；见本机补充）", in_ci)
        self.assertNotIn("0 条", in_ci)
        self.assertNotIn("missing_route", in_ci)
        # 非 CI 无库同样写「不可用」
        no_db = self.fresh_repo("no-db")
        self.use_root(no_db)
        self.assertIn("- 审计发现数（A）：不可用（CI 不实算审计；见本机补充）",
                      weekly_events.render_events(self.empty_week(), cwd=no_db))
        # 本机库可用时 A 不实算，指向本机补充
        self.assertIn("- 审计发现数（A）：不在 A 实算，见「本机补充」", text)

    # ---- 验收 6：旧小节逐字前缀、不可用绝不写成 0、发布与历史解析不受影响 ----

    def test_existing_sections_are_a_verbatim_prefix_and_unavailable_is_never_zero(self):
        scenarios = []
        # 场景 1：无 harness-audit 分支、无运行记录、本机库缺失
        bare = self.fresh_repo("bare-world")
        self.state_files(bare)
        gh = FakeWeeklyGh(prs=[merged_pr(601, merged="2026-10-08T10:00:00Z", branch="task/601-x")])
        scenarios.append((bare, gh, "无分支无库"))
        # 场景 2：坏 JSON 账本 + 一份好账本（真实分支与默认 git show 读取）
        badjson = self.fresh_repo("badjson")
        self.bare_origin(badjson)
        self.state_files(badjson)
        self.git("checkout", "-q", "-b", "harness-audit", cwd=badjson)
        (badjson / "2026").mkdir()
        (badjson / "2026" / "601.json").write_text("{not json at all", encoding="utf-8")
        good = {"schema_version": 1, "pr": 602, "trace_id": "task/602-x",
                "merged_at": "2026-10-07T10:00:00Z",
                "stages": [{"stage": "verify", "evidence_kind": "event", "duration_ms": 88}]}
        (badjson / "2026" / "602.json").write_text(json.dumps(good), encoding="utf-8")
        self.git("add", "2026", cwd=badjson)
        self.git("commit", "-q", "-m", "ledgers", cwd=badjson)
        self.git("push", "-q", "origin", "harness-audit", cwd=badjson)
        self.git("checkout", "-q", "main", cwd=badjson)
        self.git("fetch", "-q", "origin", cwd=badjson)
        gh2 = FakeWeeklyGh(prs=[merged_pr(601, merged="2026-10-08T10:00:00Z", branch="task/601-x"),
                                merged_pr(602, merged="2026-10-07T10:00:00Z", branch="task/602-x")])
        scenarios.append((badjson, gh2, "坏 JSON"))
        # 场景 3：本机库损坏
        corrupted = self.fresh_repo("corrupt-db")
        self.use_root(corrupted)
        self.state_files(corrupted)
        db = events_db.db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"garbage bytes")
        scenarios.append((corrupted, FakeWeeklyGh(), "坏库"))
        # 场景 4：有账本（真实 build_ledger + publish_ledger）
        rich = self.ledger_world()
        week_gh = self.week_for_ledgers(rich)
        scenarios.append((rich, week_gh[1], "有账本"))
        texts = {}
        for project, gh, label in scenarios:
            with self.subTest(scenario=label):
                self.use_root(project)  # 各场景的 B 都看自己的本机库（mock 叠加，后启者生效）
                text = weekly.build(END, gh, project)
                texts[label] = text
                week = weekly.collect(END, gh, project)
                old = self.old_section(week, gh, project)
                self.assertTrue(text.startswith(old), "旧输出必须是逐字前缀")
                merged_total = re.search(r"（(\d+) 个合并）", old)
                if merged_total:
                    self.assertIn(f"本周合并 PR {merged_total[1]} 个", text)
        # 不可用各带原因，不补造 0
        text1 = texts["无分支无库"]
        self.assertIn("- 账本阶段耗时（A）：不可用（本周没有可用账本）", text1)
        self.assertIn("exit 分布：不可用（窗口内没有运行记录）", text1)
        self.assertIn("- 审计发现数（A）：不可用（CI 不实算审计；见本机补充）", text1)
        self.assertIn("不可用（没有本机事件库）。", text1)
        text2 = texts["坏 JSON"]
        self.assertIn("无账本 1 个（JSON 损坏 1）", text2)
        self.assertIn("verify：事件 1、合计 88 ms、最大 88 ms", text2)
        self.assertIn("不可用（本机事件库无法读取", texts["坏库"])
        # history_from_comments 仍按旧 weekly-data 注释解析（新小节不影响）
        key = json.loads(weekly.DATA_MARK.search(text1)[1])["week"]
        comments = [{"body": text1},
                    {"body": '<!-- weekly-data {"week": "2026-W40", "escapes": 1} -->'}]
        history = weekly.history_from_comments(comments, key)
        self.assertEqual(history, [{"week": "2026-W40", "escapes": 1}])
        # publish（假 gh）仍创建固定议题并评论完整文本
        publisher = FakeWeeklyGh(prs=[merged_pr(601, merged="2026-10-08T10:00:00Z", branch="task/601-x")])
        number = weekly.publish(text1, gh=publisher)
        self.assertEqual(number, 70)
        created = [args for action, args in publisher.writes if action == "create"]
        commented = [args for action, args in publisher.writes if action == "comment"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][created[0].index("--title") + 1], weekly.REPORT_TITLE)
        self.assertEqual(created[0][created[0].index("--body") + 1], text1, "议题正文是完整新文本")
        self.assertEqual(commented[0][commented[0].index("--body") + 1], text1, "评论是完整新文本")

    # ---- 验收 7：模块边界与隐私 ----

    def test_ledger_reader_oserror_keeps_old_report_and_hides_private_path(self):
        """真实 git show 执行失败不能阻断旧周报，也不能把异常里的本机路径放进新小节。"""
        project = self.ledger_world()
        week, gh = self.week_for_ledgers(project)
        old = self.old_section(week, gh, project)
        original_run = subprocess.run
        private_path = str(self.tmp / "private-config")

        def fail_ledger_read(args, *positional, **keywords):
            if args[:2] == ["git", "show"] and str(args[2]).startswith("origin/harness-audit:"):
                raise OSError(private_path)
            return original_run(args, *positional, **keywords)

        with mock.patch.object(subprocess, "run", new=fail_ledger_read):
            text = weekly.build(END, gh, cwd=project, comments=[])
        self.assertTrue(text.startswith(old))
        self.assertIn("有账本 0 个，无账本 5 个（读取失败 5）", text)
        self.assertIn("账本阶段耗时（A）：不可用（本周没有可用账本）", text)
        self.assertNotIn(private_path, text)

    def test_audit_latest_summary_wins_when_timestamp_ties_across_chains(self):
        """真实 audit 连续产出同毫秒事件；查询按链排序，不能把排序位置当审计先后。"""
        project = self.local_world()
        head = "a" * 40
        report = {"pr": 999, "head_sha": head, "ok": False, "coverage": {},
                  "findings": [{"rule": "hash_mismatch", "severity": "error", "source": "ci",
                                "stage": "ci", "ref": "sha256:" + "b" * 64}]}
        with mock.patch.object(events_db, "_now", return_value="2026-10-08T10:00:00.000Z"):
            with mock.patch.object(events, "current_trace", return_value="task/z-old"):
                audit._emit_observations(report)
            with mock.patch.object(events, "current_trace", return_value="task/a-new"):
                audit._emit_observations({**report, "ok": True, "findings": []})
        rows = events_io.query(source="local")
        self.assertEqual([row["id"] for row in rows], [3, 1, 2],
                         "真实查询按 trace/seq 排序，新 summary 不一定是列表最后一项")
        self.assertEqual(weekly_events.local_supplement(START, END)["audit"], {(999, head): set()})
        text = weekly_events.render_events(self.empty_week(), cwd=project)
        self.assertIn("PR 999（head aaaaaaa…）：0 条", text)
        self.assertNotIn("hash_mismatch", text, "同毫秒的旧 finding 不得残留")

    def test_module_boundaries_and_privacy(self):
        source = Path(weekly_events.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name == "weekly" or alias.name.endswith(".weekly")
                                     or alias.name == "engine.reports.weekly",
                                     f"weekly_events 不得反向导入 weekly：import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertFalse((node.level and module.split(".")[-1] == "weekly")
                                 or module in ("weekly", "engine.reports.weekly")
                                 or module.endswith(".weekly"),
                                 f"weekly_events 不得反向导入 weekly：from {module} import")
                for alias in node.names:
                    self.assertNotEqual(alias.name, "weekly",
                                        "weekly_events 不得反向导入 weekly（from 包导入成员）")
        # 全局体量边界；本任务净增 ≤ 25 行由设计方对批准基点做一次性验收，不能比较 HEAD 自己。
        current = len(Path(weekly.__file__).read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(current, 800)
        # 输出不含临时目录与本机用户目录片段
        project = self.local_world()
        events.emit("dispatch", "executor_round", "ok", trace_id=self.TRACE, duration_ms=10,
                    outputs={"exit": "ok", "guard_denied": 0})
        events.emit("guard", "command", "deny", trace_id=self.TRACE,
                    decision={"by": "guard", "rule": "deny-rule"}, actor={"role": "designer"})
        text = weekly_events.render_events(self.empty_week(), cwd=project)
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn(os.path.expanduser("~"), text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("/var/folders/", text)


if __name__ == "__main__":
    unittest.main()
