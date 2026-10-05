"""B92 第 1 部分（T709）：运行记录的 stages 只保留派发里程碑与失败事件。

夹具与 test_events.py 同型：匿名临时 git 仓库、隔离 events_db.ROOT、冻结时钟，经真实 events.emit 构造
混有 ci/route/verify 噪声的本机链，验收 run_timeline.record_fields 的过滤口径、整链链头与锚点、
attempt/round 编号不受噪声影响、记录不随测试噪声膨胀，以及 alerts.stage_lines（同走 record_fields）
保留失败行且不再被 ok 噪声刷屏。不碰真实库、PR 或工作流。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agents import run_timeline
from engine.core import alerts, events, events_db

FROZEN_TS = "2026-03-04T05:06:07.890Z"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


class RunRecordMilestonesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-run-record-milestones-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
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

    # ---------- 夹具 ----------

    def git(self, *args, check=True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def emit(self, stage: str, step: str, status: str = "ok", trace: str = "", **kwargs):
        """写一条事件并要求成功：夹具链必须完整，否则被测行为无从谈起。"""
        self.assertIsNotNone(events.emit(stage, step, status, trace_id=trace, **kwargs))

    def trace_rows(self, trace: str) -> list[dict]:
        """该 trace 的本机链（seq 序），取 hash/stage/step/status 供断言。"""
        with contextlib.closing(sqlite3.connect(events_db.db_path())) as conn:
            return [dict(zip(("hash", "stage", "step", "status"), values)) for values in conn.execute(
                "SELECT hash,stage,step,status FROM events WHERE source=? AND trace_id=? ORDER BY seq",
                ("local", trace))]

    # ---------- 验收 1：stages 只含 dispatch 里程碑，条数等于派发事件数 ----------

    def test_only_dispatch_stage_kept(self):
        trace = "task/901-a"
        self.emit("dispatch", "admit", trace=trace, duration_ms=5)
        self.emit("verify", "verify.tests", trace=trace)  # 槽位里单测调用引擎写下的噪声
        self.emit("dispatch", "guard_preflight", trace=trace)
        self.emit("route", "facts", trace=trace)
        self.emit("ci", "cli.evidence", trace=trace)
        self.emit("dispatch", "claim", trace=trace)
        self.emit("verify", "verify.summary", trace=trace)
        self.emit("dispatch", "executor_round", trace=trace, duration_ms=1000)
        self.emit("dispatch", "local_verify", trace=trace, duration_ms=50)
        timeline, head = run_timeline.record_fields(trace)
        dispatch_count = len([row for row in self.trace_rows(trace) if row["stage"] == "dispatch"])
        self.assertEqual([item["stage"] for item in timeline["stages"]], ["dispatch"] * dispatch_count)
        self.assertEqual([(item["step"], item["status"]) for item in timeline["stages"]],
                         [("admit", "ok"), ("guard_preflight", "ok"), ("claim", "ok"),
                          ("executor_round", "ok"), ("local_verify", "ok")])
        self.assertEqual(head, self.trace_rows(trace)[-1]["hash"])
        self.assertEqual(events_db.verify(), [])  # 被过滤的事件仍在链上，链校验完好

    # ---------- 验收 2：链头是整条链的最后一个事件，即使它是非 dispatch 事件 ----------

    def test_head_is_whole_chain_tail(self):
        trace = "task/901-b"
        self.emit("dispatch", "admit", trace=trace)
        self.emit("dispatch", "local_verify", trace=trace, duration_ms=50)
        self.emit("verify", "verify.tests", trace=trace)  # 链尾是非 dispatch 的 ok 事件
        self.emit("route", "facts", trace=trace)
        tail = self.trace_rows(trace)[-1]["hash"]
        timeline, head = run_timeline.record_fields(trace)
        self.assertEqual(head, tail)
        self.assertEqual(timeline["anchors"],
                         [{"source": "local", "stage": "dispatch", "head_hash": tail,
                           "fixed_in": "run_record"}])
        # 过滤把最后的噪声挡在 stages 外，但链头与锚点仍指向它：防篡改覆盖整条链
        self.assertEqual([(item["step"], item["status"]) for item in timeline["stages"]],
                         [("admit", "ok"), ("local_verify", "ok")])

    # ---------- 验收 3：噪声事件不影响 attempt/round 编号与更早 attempt 的指针行 ----------

    def test_attempt_numbering_unchanged_by_noise(self):
        def dispatch_chain(trace: str):
            self.emit("dispatch", "admit", trace=trace, duration_ms=5)
            self.emit("dispatch", "executor_round", trace=trace, duration_ms=1000)
            self.emit("dispatch", "local_verify", trace=trace, duration_ms=50)
            self.emit("dispatch", "push_pr", trace=trace)
            self.emit("dispatch", "ci_wait", "fail", trace=trace, duration_ms=900)  # 尝试 1 的边界
            self.emit("dispatch", "executor_round", trace=trace, duration_ms=800)
            self.emit("dispatch", "local_verify", trace=trace, duration_ms=40)

        dispatch_chain("task/901-c-clean")
        noisy = "task/901-c-noisy"
        self.emit("verify", "verify.tests", trace=noisy)
        self.emit("dispatch", "admit", trace=noisy, duration_ms=5)
        self.emit("route", "facts", trace=noisy)
        self.emit("dispatch", "executor_round", trace=noisy, duration_ms=1000)
        self.emit("ci", "cli.evidence", trace=noisy)
        self.emit("dispatch", "local_verify", trace=noisy, duration_ms=50)
        self.emit("verify", "verify.summary", trace=noisy)
        self.emit("dispatch", "push_pr", trace=noisy)
        self.emit("dispatch", "ci_wait", "fail", trace=noisy, duration_ms=900)  # 尝试 1 的边界
        self.emit("route", "result", trace=noisy)  # 边界之后的噪声：不开启新的编号窗口
        self.emit("dispatch", "executor_round", trace=noisy, duration_ms=800)
        self.emit("verify", "verify.tests", trace=noisy)
        self.emit("dispatch", "local_verify", trace=noisy, duration_ms=40)
        self.emit("route", "risk.file", trace=noisy)

        clean_timeline, _ = run_timeline.record_fields("task/901-c-clean")
        noisy_timeline, _ = run_timeline.record_fields(noisy)
        # 编号、指针行与整个时间线和「链上只有派发事件」时完全相同
        self.assertEqual(noisy_timeline["stages"], clean_timeline["stages"])
        self.assertEqual([(item["step"], item["status"], item["attempt"], item["round"])
                          for item in noisy_timeline["stages"]],
                         [("push_pr", "prior", 1, 0), ("ci_wait", "prior", 1, 0),
                          ("executor_round", "ok", 2, 1), ("local_verify", "ok", 2, 1)])

    # ---------- 验收 4：千条噪声不撑大记录，也不触发保底截断 ----------

    def test_noise_does_not_grow_record(self):
        trace = "task/901-d"
        self.emit("dispatch", "admit", trace=trace, duration_ms=5)
        self.emit("dispatch", "claim", trace=trace)
        for index in range(1000):  # 槽位里跑全量单测的 ok 事件量级
            self.emit(("verify", "route", "ci")[index % 3], f"noise.{index}", trace=trace)
        self.emit("dispatch", "executor_round", trace=trace, duration_ms=1000)
        self.emit("dispatch", "local_verify", trace=trace, duration_ms=50)
        timeline, _head = run_timeline.record_fields(trace)
        record = {"task": "T901-D", "class": "K7", "attempt": 1, "branch": trace,
                  "started_at": FROZEN_TS, "ended_at": FROZEN_TS, "exit": "ok", **timeline}
        self.assertEqual(len(record["stages"]), 4)  # 只剩四个派发里程碑
        self.assertLess(len(json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")),
                        16 * 1024)
        self.assertNotIn("stages_truncated", record)  # 距 256KB 保底很远，不该触发

    # ---------- 验收 5：失败事件保留，告警有关键失败行且不被 ok 噪声刷屏 ----------

    def test_failures_kept_for_record_and_alert(self):
        trace = "task/901-e"
        self.emit("dispatch", "admit", trace=trace, duration_ms=5)
        self.emit("dispatch", "executor_round", trace=trace, duration_ms=1000)
        self.emit("dispatch", "local_verify", trace=trace, duration_ms=50)
        self.emit("verify", "verify.tests", trace=trace)  # ok 噪声
        self.emit("route", "facts", trace=trace)
        self.emit("review", "review", "fail", trace=trace, duration_ms=800)  # 独立评审不通过
        self.emit("verify", "verify.tests", "fail", trace=trace, duration_ms=60)  # 本地检查失败
        timeline, _head = run_timeline.record_fields(trace)
        kept = [(item["stage"], item["step"], item["status"]) for item in timeline["stages"]]
        self.assertIn(("review", "review", "fail"), kept)
        self.assertIn(("verify", "verify.tests", "fail"), kept)
        self.assertNotIn(("verify", "verify.tests", "ok"), kept)
        self.assertNotIn(("route", "facts", "ok"), kept)
        # 告警的「已发生的阶段」同走 record_fields：关键失败行在，ok 噪声不在
        lines = alerts.stage_lines(trace)
        self.assertIn("- `review/review` fail（attempt 1）", lines)
        self.assertIn("- `verify/verify.tests` fail（attempt 1）", lines)
        joined = "\n".join(lines)
        self.assertNotIn("`verify/verify.tests` ok", joined)
        self.assertNotIn("`route/facts` ok", joined)


if __name__ == "__main__":
    unittest.main()
