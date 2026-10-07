"""T302 events 与 trace 命令测试：过滤/计数/JSON 与 C5 导出导入、任务/PR/分支解析与歧义候选、
时间线决定/最长阶段/首个失败、--ci 下载分页与幂等导入。

夹具沿用 tests/test_events_io.py 的模式：匿名临时 git 仓库、隔离 events_db.ROOT、递增冻结时钟、
假 gh（记录调用序列，提供分页 API 与 artifact zip 下载），全部经真实产品入口 cli.main 驱动；
不碰真实库/PR/工作流，不调用真 gh。CI 包由构建仓库里真实的 emit/export 生成，再打成 artifact zip。
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
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli
from engine.core import events, events_db, events_io

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
GITHUB_KEYS = ("CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
               "GITHUB_JOB")
BUILDER_BRANCH = "task/402-ci"
BUILDER_REPO = "owner/repo"
REPO_URL = f"https://github.com/{BUILDER_REPO}.git"
HEAD_E, HEAD_F = "e" * 64, "f" * 64


def canonical(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def zip_bytes(members: dict[str, str]) -> bytes:
    """把包成员打成 artifact 下载 zip（GitHub 下载物为「artifact 名/文件」一层目录）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def short_digest(digest) -> str:
    return digest[:7] if isinstance(digest, str) and digest else "—"


class _Clock:
    """递增冻结时钟：从 2026-01-02T03:01 起每次调用前进一分钟，事件 ts 严格递增且可比。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        hour, minute = divmod(self.step, 60)
        return f"2026-01-02T{3 + hour // 24:02d}:{(hour % 24 + 1):02d}:{minute:02d}.000Z"


class FakeGh:
    """trace/load_ci 用 gh 桩：pr 视图、分页 API 与 artifact zip 下载，全部记录调用序列。"""

    def __init__(self, *, head: str = "c" * 40, run_pages=None, artifact_pages=None, downloads=None,
                 pr_info=None):
        self.calls: list[tuple] = []
        self.pr_info = pr_info or {"headRefName": BUILDER_BRANCH, "headRefOid": head,
                                   "repository": BUILDER_REPO}
        self.run_pages = run_pages or {}
        self.artifact_pages = artifact_pages or {}
        self.downloads = downloads or {}

    def pr(self, pr: int) -> dict:
        self.calls.append(("pr", pr))
        return dict(self.pr_info)

    def api(self, route: str):
        self.calls.append(("api", route))
        page = int(urllib.parse.parse_qs(route.split("?", 1)[1])["page"][0])
        if "/artifacts" in route:
            run_id = int(route.split("/actions/runs/")[1].split("/")[0])
            return self.artifact_pages[run_id][page - 1]
        return self.run_pages[page - 1]

    def download(self, url: str) -> bytes:
        self.calls.append(("download", url))
        return self.downloads[url]


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-trace-events-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.fresh_repo("app")
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", new=_Clock())
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in GITHUB_KEYS:
            os.environ.pop(key, None)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    def fresh_repo(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir()
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **GIT_ENV}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True,
                       capture_output=True, env=env)
        (path / "README.md").write_text("# fixture\n")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True,
                       capture_output=True, env=env)
        return path

    def in_repo(self, repo: Path):
        return mock.patch.object(events_db, "ROOT", repo)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        """真实产品入口：捕获 stdout/stderr，返回（退出码, stdout, stderr）；args 为完整命令。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def json_cli(self, *args: str) -> dict:
        code, out, err = self.run_cli(*args, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def write_record(self, folder: str, number: int, task: str, branch: str, attempt: int,
                     started: str, ended: str, head: str | None = None) -> None:
        record = {"task": task, "class": "K7", "attempt": attempt, "branch": branch,
                  "started_at": started, "ended_at": ended, "exit": "ok",
                  "anchors": ([{"source": "local", "stage": "dispatch", "head_hash": head,
                                "fixed_in": "run_record", "ts": ended}] if head else [])}
        path = self.repo / "docs" / "runs" / folder / f"{number}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

    # ---- 验收 1：多来源/状态/阶段/日期事件核对筛选集合、计数、JSON 与 import/export，非法参数为 2 ----

    def test_events_filters_counts_json_and_io(self):
        trace = "task/T302-a"
        emitted = [
            events.emit("verify", "verify.lint", "ok", trace_id=trace, duration_ms=10),
            events.emit("guard", "guard.command", "deny", trace_id=trace),
            events.emit("verify", "verify.tests", "fail", trace_id=trace, duration_ms=30),
            events.emit("ci", "harness.check", "ok", trace_id=trace, source="ci:900:1:job_x",
                        duration_ms=5),
            events.emit("route", "route.result", "ok", trace_id=trace, source="ci:900:1:job_x"),
        ]
        self.assertEqual([bool(item) for item in emitted], [True] * 5)
        rows = sorted(events_io.query(trace_id=trace), key=lambda row: row["ts"])
        steps = [row["step"] for row in rows]
        self.assertEqual(steps, ["verify.lint", "guard.command", "verify.tests", "harness.check",
                                 "route.result"])
        since = rows[2]["ts"]  # 第 3 条事件起（含边界）

        # 全量 JSON：count/total/计数与事件集一致
        view = self.json_cli("events")
        self.assertEqual((view["count"], view["total"]), (5, 5))
        self.assertEqual(view["counts"], {"stage": {"ci": 1, "guard": 1, "route": 1, "verify": 2},
                                          "status": {"deny": 1, "fail": 1, "ok": 3}})
        self.assertEqual([row["step"] for row in view["events"]], steps)

        # stage/status/since 过滤：筛选集、计数跟随过滤结果（不是只改标题或总计）
        view = self.json_cli("events", "--stage", "verify")
        self.assertEqual((view["count"], view["total"]), (2, 5))
        self.assertEqual(view["counts"]["stage"], {"verify": 2})
        self.assertEqual(view["counts"]["status"], {"fail": 1, "ok": 1})
        self.assertEqual([row["step"] for row in view["events"]], ["verify.lint", "verify.tests"])
        view = self.json_cli("events", "--status", "deny")
        self.assertEqual(view["count"], 1)
        self.assertEqual((view["events"][0]["stage"], view["events"][0]["step"]),
                         ("guard", "guard.command"))
        view = self.json_cli("events", "--since", since)
        self.assertEqual((view["count"], view["total"]), (3, 5))
        self.assertEqual([row["step"] for row in view["events"]], ["verify.tests", "harness.check",
                                                                  "route.result"])
        view = self.json_cli("events", "--since", since, "--stage", "verify")
        self.assertEqual([row["step"] for row in view["events"]], ["verify.tests"])

        # 文本模式：总数与计数可见
        code, out, _ = self.run_cli("events", "--stage", "verify")
        self.assertEqual(code, 0)
        self.assertIn("事件 2 条 / 总计 5 条", out)
        self.assertIn("verify 2", out)

        # 非法枚举/时间输入返回 2；非法组合返回 2
        for args in (("--stage", "bogus"), ("--status", "bogus"), ("--since", "nonsense"),
                     ("--since", "1x"), ("--export", "x.json", "--import", "y.json"),
                     ("--export", "x.json", "--stage", "verify")):
            with self.subTest(args=args):
                if args[0] in ("--stage", "--status"):
                    with self.assertRaises(SystemExit) as caught:  # argparse choices → 2
                        self.run_cli("events", *args)
                    self.assertEqual(caught.exception.code, 2)
                else:
                    code, _, _ = self.run_cli("events", *args)
                    self.assertEqual(code, 2)

        # --export：整库导出（不含展示过滤），包可导入
        target = self.tmp / "out" / "bundle.json"
        code, out, err = self.run_cli("events", "--export", str(target))
        self.assertEqual((code, err), (0, ""))
        bundle = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(bundle["events"]), 5)
        other = self.fresh_repo("roundtrip")
        with self.in_repo(other):
            code, out, err = self.run_cli("events", "--import", str(target))
            self.assertEqual(code, 0, err)
            self.assertIn("新增 5，跳过 0", out)
            self.assertEqual(len(events_io.query(trace_id=trace)), 5)
            self.assertEqual(events_db.verify(), [])
            # 幂等：再导入零新增；坏文件与坏包有区分（2 与 1）
            code, out, _ = self.run_cli("events", "--import", str(target))
            self.assertEqual(code, 0)
            self.assertIn("新增 0，跳过 5", out)
            bad = self.tmp / "bad.json"
            bad.write_text("不是 JSON", encoding="utf-8")
            self.assertEqual(self.run_cli("events", "--import", str(bad))[0], 2)
            bad.write_text("{}", encoding="utf-8")
            code, _, err = self.run_cli("events", "--import", str(bad))
            self.assertEqual(code, 1)
            self.assertIn("发现 schema", err)

    # ---- 验收 2：三类标识归到同 trace，重用分支/多个尝试按 head/时间分段，歧义明确列候选 ----

    def test_trace_resolves_task_pr_branch_and_ambiguity(self):
        trace = "task/402-demo"
        events.emit("dispatch", "admit", "ok", trace_id=trace)
        events.emit("verify", "verify.lint", "ok", trace_id=trace, duration_ms=10)
        events.emit("dispatch", "push_pr", "ok", trace_id=trace, outputs={"pr": 77, "head": HEAD_E})
        events.emit("verify", "verify.tests", "ok", trace_id=trace, duration_ms=5)
        events.emit("guard", "guard.command", "deny", trace_id=trace)  # 两次尝试窗口之外
        rows = events_io.query(trace_id=trace)
        ts = [row["ts"] for row in rows]
        (self.repo / "docs" / "plans").mkdir(parents=True)
        (self.repo / "docs" / "plans" / "task-402-demo.md").write_text("---\ntask: T402\n---\n")
        self.write_record("task-402-demo", 1, "T402", trace, 1, ts[0], ts[1], head=HEAD_E)
        self.write_record("task-402-demo", 2, "T402", trace, 2, ts[2], ts[3], head=HEAD_F)
        # 干扰项：目录同名但 task 字段不相符的记录不参与映射
        self.write_record("task-402-demo", 9, "T999", "task/999-evil", 1, ts[0], ts[4])

        # 任务号：映射到分支；两次尝试按窗口/链头分段，窗口外事件不丢
        view = self.json_cli("trace", "T402")
        self.assertEqual((view["kind"], view["trace_id"], view["ambiguous"]), ("task", trace, False))
        self.assertEqual([item["attempt"] for item in view["records"]], [1, 2])
        segments = {item["segment"]: item for item in view["segments"]}
        self.assertEqual(segments["attempt 1"]["count"], 2)
        self.assertEqual(short_digest(segments["attempt 1"]["head"]), HEAD_E[:7])
        self.assertEqual(segments["attempt 2"]["count"], 2)
        self.assertEqual(short_digest(segments["attempt 2"]["head"]), HEAD_F[:7])
        self.assertEqual(segments["本机（窗口外）"]["count"], 1)
        self.assertEqual([row["attempt"] for row in view["events"]], [1, 1, 2, 2, None])

        # 分支名：同一 trace；文本模式展示分段与首个失败
        self.assertEqual(self.json_cli("trace", trace)["trace_id"], trace)
        code, out, _ = self.run_cli("trace", "T402")
        self.assertEqual(code, 0)
        self.assertIn("attempt 1", out)
        self.assertIn("attempt 2", out)
        self.assertIn("首个失败", out)

        # PR 号：trace 取 API headRefName，不是把 PR 号当 trace；也不读检出分支
        fake = FakeGh(pr_info={"headRefName": trace, "headRefOid": HEAD_E, "repository": BUILDER_REPO})
        with mock.patch.object(events_io, "GhClient", lambda: fake):
            view = self.json_cli("trace", "77")
        self.assertEqual(view["trace_id"], trace)
        self.assertEqual(view["kind"], "pr")
        self.assertIn(("pr", 77), fake.calls)

        # 分支重用关联多个 PR：列候选（pr/链头/时间），不随意选一条下载
        events.emit("dispatch", "push_pr", "ok", trace_id=trace, outputs={"pr": 78})
        fake = FakeGh()
        with mock.patch.object(events_io, "GhClient", lambda: fake):
            code, out, err = self.run_cli("trace", trace, "--ci", "--json")
        self.assertEqual(code, 1)
        self.assertIn("多个 PR", err)
        view = json.loads(out)
        self.assertEqual(sorted(item["pr"] for item in view["pr_candidates"]), [77, 78])
        self.assertFalse([call for call in fake.calls if call[0] == "api"])  # 未下载任何东西

        # 多分支同任务：明确列候选（attempt/链头/时间），不选第一条；文本与 JSON 都可见
        self.write_record("task-403-a", 1, "T403", "task/403-a", 1, ts[0], ts[1], head=HEAD_E)
        self.write_record("task-403-b", 1, "T403", "task/403-b", 1, ts[0], ts[1], head=HEAD_F)
        view = self.json_cli("trace", "T403")
        self.assertEqual((view["ambiguous"], view["trace_id"]), (True, None))
        self.assertEqual({item["branch"] for item in view["candidates"]}, {"task/403-a", "task/403-b"})
        for item in view["candidates"]:
            self.assertEqual(item["attempt"], 1)
            self.assertIsNotNone(item["ts"])
        code, out, _ = self.run_cli("trace", "T403")
        self.assertEqual(code, 0)
        self.assertIn("task/403-a", out)
        self.assertIn("task/403-b", out)

        # 只有任务书没有运行记录：映射到任务书分支，无事件如实展示；无法识别的标识返回 2
        (self.repo / "docs" / "plans" / "task-404-solo.md").write_text("---\ntask: T404\n---\n")
        view = self.json_cli("trace", "T404")
        self.assertEqual((view["trace_id"], view["events"]), ("task/404-solo", []))
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("trace", "--what")
        self.assertEqual(caught.exception.code, 2)

    # ---- 验收 3：确定性事件夹具核对输入输出、来源、最长耗时和首个失败 ----

    def test_timeline_decisions_longest_and_first_failure(self):
        trace = "task/405-x"
        self.assertIsNotNone(events.emit(
            "verify", "verify.lint", "ok", trace_id=trace, duration_ms=12,
            inputs=[events.ref("head", "main@abc1234"), events.ref("policy", "main@a1b2c3d")],
            outputs={"exit": 0, "log.sha256": "a" * 64},
            decision={"by": "verify", "rule": "lint", "reason": "lint 通过"},
            actor={"role": "engine", "host": "local", "model": "fixture-model"}))
        self.assertIsNotNone(events.emit(
            "verify", "verify.tests", "fail", trace_id=trace, duration_ms=40,
            decision={"by": "verify", "rule": "tests", "reason": "用例失败"},
            error={"kind": "AssertionError"}))
        self.assertIsNotNone(events.emit(
            "guard", "guard.command", "deny", trace_id=trace, duration_ms=3,
            decision={"by": "command_guard", "rule": "deny_force_push", "reason": "拒绝强推"}))
        self.assertIsNotNone(events.emit(
            "route", "route.result", "ok", trace_id=trace, duration_ms=5, source="ci:70:1:job_r",
            decision={"by": "policy", "rule": "auto_merge", "reason": "满足自动合并"}))
        self.assertIsNotNone(events.emit(
            "ci", "harness.check", "error", trace_id=trace, duration_ms=9, source="ci:70:1:job_h",
            error={"kind": "ToolError"}))

        view = self.json_cli("trace", trace)
        self.assertEqual(view["trace_id"], trace)
        self.assertEqual(view["chains"], ["ci:70:1:job_h", "ci:70:1:job_r", "local"])
        # 每个事件带输入/输出引用、决定、角色、时长、来源与段标注
        first = view["events"][0]
        self.assertEqual([item["ref"] for item in first["inputs"]], ["main@abc1234", "main@a1b2c3d"])
        self.assertEqual(first["outputs"], {"exit": 0, "log.sha256": "a" * 64})
        self.assertEqual(first["decision"], {"by": "verify", "rule": "lint", "reason": "lint 通过"})
        self.assertEqual(first["actor"], {"role": "engine", "host": "local", "model": "fixture-model"})
        self.assertEqual((first["duration_ms"], first["source"], first["segment"]),
                         (12, "local", "本机（窗口外）"))
        self.assertEqual(view["events"][3]["source"], "ci:70:1:job_r")
        self.assertEqual(view["events"][3]["segment"], "ci:70:1:job_r")
        self.assertEqual(view["events"][4]["attempt"], 1)  # CI 链 attempt 取自来源
        # 最长阶段按累计时长（verify 12+40=52），不是第一条也不是最后一条
        self.assertEqual(view["longest_stage"], {"stage": "verify", "duration_ms": 52})
        # 首个失败是时间最早的一个，状态按原值区分（fail 不与 deny/error 混同）
        self.assertEqual(view["first_failure"],
                         {"ts": view["events"][1]["ts"], "source": "local", "seq": 2,
                          "stage": "verify", "step": "verify.tests", "status": "fail"})
        self.assertEqual({row["status"] for row in view["events"]}, {"ok", "fail", "deny", "error"})
        code, out, _ = self.run_cli("trace", trace)
        self.assertEqual(code, 0)
        self.assertIn("最长阶段 verify（52ms）", out)
        self.assertIn("首个失败 #2", out)
        self.assertIn("verify.tests fail", out)
        self.assertIn("guard.command deny", out)
        self.assertIn("harness.check error", out)

    # ---- 验收 4：假 gh 两页 artifact、重跑/过期/坏包；只导入匹配 head，重跑无重复且失败有诊断 ----

    def test_ci_download_pagination_and_idempotence(self):
        head, zips = self.build_ci_packages()
        runs = [
            {"id": 9000, "head_sha": "b" * 40, "path": ".github/workflows/harness.yml", "run_attempt": 1},
            {"id": 9001, "head_sha": head, "path": ".github/workflows/harness.yml", "run_attempt": 2},
            {"id": 9002, "head_sha": head, "path": ".github/workflows/auto-merge.yml", "run_attempt": 1},
        ]
        artifacts_9001 = [
            {"id": 1, "name": "harness-events-9001-1-job_b", "expired": True,
             "archive_download_url": "https://dl/expired"},
            {"id": 2, "name": "harness-events-9001-2-job_b", "expired": False,
             "archive_download_url": "https://dl/jb"},
            {"id": 3, "name": "harness-events-9001-2-job_c", "expired": False,
             "archive_download_url": "https://dl/jc"},
        ]
        artifacts_9002 = [
            {"id": 4, "name": "harness-events-9002-1-report", "expired": False,
             "archive_download_url": "https://dl/report"},
            {"id": 5, "name": "harness-events-9002-1-broken", "expired": False,
             "archive_download_url": "https://dl/broken"},
        ]
        # 9001 第一页 2/3 条 → 必须翻到第二页才取全（分页被真实读取）
        fake = FakeGh(head=head,
                      run_pages=[{"total_count": 3, "workflow_runs": runs}],
                      artifact_pages={9001: [{"total_count": 3, "artifacts": artifacts_9001[:2]}],
                                      9002: [{"total_count": 2, "artifacts": artifacts_9002}]},
                      downloads={"https://dl/jb": zips["jb"], "https://dl/jc": zips["jc"],
                                 "https://dl/report": zips["report"], "https://dl/broken": b"not a zip"})
        fake.artifact_pages[9001].append({"total_count": 3, "artifacts": artifacts_9001[2:]})
        with mock.patch.object(events_io, "GhClient", lambda: fake):
            code, out, err = self.run_cli("trace", "77", "--ci", "--json")
        view = json.loads(out)
        self.assertEqual(code, 1, err)  # 有发现：退出码 1，但时间线仍展示
        self.assertEqual(view["trace_id"], BUILDER_BRANCH)
        self.assertEqual((view["ci"]["imported"], view["ci"]["skipped"]), (4, 0))
        self.assertEqual(sorted(f["code"] for f in view["ci"]["findings"]),
                         ["artifact_expired", "head_mismatch", "package_corrupt"])
        self.assertIn("1 个其他 head", err)  # 只导入匹配 head，其余运行有诊断
        self.assertIn("已过期", err)
        self.assertEqual(len(view["events"]), 4)
        self.assertTrue(all(row["source"].startswith("ci:") for row in view["events"]))
        self.assertIn("ci:9002:1:report", view["chains"])  # auto-merge 的路由 job 没被遗漏

        # 入库核对：三个来源链各自从 seq=1 开始，链完整；重跑零新增
        rows = events_io.query(source="ci")
        self.assertEqual(sorted({row["source"] for row in rows}),
                         ["ci:9001:2:job_b", "ci:9001:2:job_c", "ci:9002:1:report"])
        self.assertEqual({row["source"] for row in rows if row["seq"] == 1},
                         {"ci:9001:2:job_b", "ci:9001:2:job_c", "ci:9002:1:report"})
        self.assertEqual(len([row for row in rows if row["source"] == "ci:9001:2:job_b"]), 2)
        self.assertEqual(events_db.verify(), [])
        clean = FakeGh(head=head,
                       run_pages=[{"total_count": 2, "workflow_runs": runs[1:]}],
                       artifact_pages={9001: [{"total_count": 2, "artifacts": artifacts_9001[1:]}],
                                       9002: [{"total_count": 1, "artifacts": artifacts_9002[:1]}]},
                       downloads={"https://dl/jb": zips["jb"], "https://dl/jc": zips["jc"],
                                  "https://dl/report": zips["report"]})
        with mock.patch.object(events_io, "GhClient", lambda: clean):
            code, out, err = self.run_cli("trace", "77", "--ci", "--json")
        view = json.loads(out)
        self.assertEqual((code, view["ci"]), (0, {"imported": 0, "skipped": 4, "findings": []}))
        self.assertEqual(len(events_io.query(source="ci")), 4)  # 重跑无重复
        # 直接入口同样幂等；坏 PR 信息与缺本机库：明确发现，不报伪成功
        self.assertEqual(events_io.load_ci(77, gh=clean), {"imported": 0, "skipped": 4, "findings": []})
        broken = FakeGh(pr_info={"headRefName": BUILDER_BRANCH})
        result = events_io.load_ci(77, gh=broken)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["findings"][0]["code"], "api")
        bare = self.tmp / "bare"
        bare.mkdir()
        with self.in_repo(bare):
            result = events_io.load_ci(77, gh=clean)
        self.assertEqual(result["imported"], 0)
        self.assertIn("storage", [finding["code"] for finding in result["findings"]])

    # ---- 夹具：在构建仓库里经真实 emit/export 生成三个 CI 包（harness 两 job + auto-merge）----
    def build_ci_packages(self) -> tuple[str, dict[str, bytes]]:
        """构建仓库里经真实 emit/export 生成三个 CI 包（harness 两 job + auto-merge），打成 zip。

        构建期间 events_db.ROOT 指向构建仓库，with 块结束立即摘除，之后导入读目标仓库的库。
        """
        builder = self.fresh_repo("builder")
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **GIT_ENV}
        subprocess.run(["git", "checkout", "-q", "-b", BUILDER_BRANCH], cwd=builder, check=True,
                       capture_output=True, env=env)
        subprocess.run(["git", "remote", "add", "origin", REPO_URL], cwd=builder, check=True,
                       capture_output=True, env=env)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=builder, check=True,
                              capture_output=True, text=True, env=env).stdout.strip()
        members: dict[str, str] = {}
        with mock.patch.object(events_db, "ROOT", builder):
            for job, run_id, attempt, steps in (
                    ("job_b", "9001", "2",
                     (("ci", "harness.check", "ok"), ("verify", "verify.summary", "fail"))),
                    ("job_c", "9001", "2", (("route", "route.result", "ok"),)),
                    ("report", "9002", "1", (("route", "route.facts", "ok"),))):
                for key, value in (("CI", "true"), ("GITHUB_HEAD_REF", BUILDER_BRANCH),
                                   ("GITHUB_RUN_ID", run_id), ("GITHUB_RUN_ATTEMPT", attempt),
                                   ("GITHUB_JOB", job)):
                    os.environ[key] = value
                for stage, step, status in steps:
                    self.assertIsNotNone(events.emit(stage, step, status, duration_ms=7,
                                                     inputs=[events.ref("head", f"main@{'a' * 12}")]))
                source = f"ci:{run_id}:{attempt}:{job}"
                bundle = events_io.export_bundle(source=source)
                self.assertEqual([event["source"] for event in bundle["events"]],
                                 [source] * len(bundle["events"]))
                self.assertEqual(bundle["origin"]["head_branch"], BUILDER_BRANCH)
                self.assertEqual((bundle["origin"]["run_id"], bundle["origin"]["run_attempt"],
                                  bundle["origin"]["job"]), (run_id, attempt, job))
                self.assertEqual(bundle["origin"]["head_sha"], head)
                members[f"harness-events-{run_id}-{attempt}-{job}/harness-events.json"] = canonical(bundle)
        for key in GITHUB_KEYS:
            os.environ.pop(key, None)
        zips = {name.split("/")[0]: zip_bytes({name: content}) for name, content in members.items()}
        return head, {"jb": zips["harness-events-9001-2-job_b"],
                      "jc": zips["harness-events-9001-2-job_c"],
                      "report": zips["harness-events-9002-1-report"]}


if __name__ == "__main__":
    unittest.main()