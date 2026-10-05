"""T103 风险判级与路由埋点测试：risk 逐文件与汇总事件、route facts/规则/result 事件、
workflow_run 的 PR 分支 trace 贯穿 risk 与 route、事件与引用准备失败不影响原判定。

夹具全部使用匿名临时 git 仓库、隔离 events_db.ROOT、冻结时钟与假 gh，不碰真实库、PR 或工作流；
route 侧经 policy.main（与命令行同一入口）驱动，gather/decide 只注入 cwd 与 gh 桩，不改判定逻辑。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.checks import r1_checks
from engine.core import events, events_db
from engine.core.common import RULES_PATH
from engine.routing import policy, risk

FROZEN_TS = "2026-03-04T05:06:07.890Z"
WARN_LINE = "harness：事件写入失败，已跳过（不影响本次运行）"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
ROUTE_STEPS = {"risk.file", "risk.summary", "facts", "风险", "类别", "预算", "规模", "运行记录", "result"}


def fake_gh(head_ref="task/pr-77", issues=()):
    """gh 桩：labels/escapes/headRefName 按参数形状应答；其他调用显式失败。"""
    def gh(*args):
        joined = " ".join(args)
        if joined.startswith("pr view") and "headRefName" in joined:
            return json.dumps({"headRefName": head_ref})
        if joined.startswith("pr view") and "labels" in joined:
            return json.dumps({"labels": []})
        if joined.startswith("pr list"):
            return json.dumps([{"number": 101}])
        if joined.startswith("issue list"):
            return json.dumps(list(issues))
        raise AssertionError(f"未预期的 gh 调用：{joined}")

    return gh


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-route-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_sample.py").write_text("line1 = 1\nline2 = 2\nline3 = 3\nline4 = 4\nline5 = 5\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", return_value=FROZEN_TS)  # 冻结时钟，摘要可比
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME",
                    "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"):
            os.environ.pop(key, None)
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)
        self._seq = 0

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def commit(self, files: dict[str, str], message: str) -> str:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def query(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def recorded(self, step: str, trace: str) -> list[dict]:
        rows = self.query(
            "SELECT id, source, status, outputs, decision_by, decision_rule, decision_reason "
            "FROM events WHERE step=? AND trace_id=? ORDER BY id", (step, trace))
        return [{"id": row[0], "source": row[1], "status": row[2], "outputs": json.loads(row[3]),
                 "decision": {"by": row[4], "rule": row[5], "reason": row[6]}} for row in rows]

    def inputs_of(self, event_id: int) -> list[dict]:
        rows = self.query("SELECT kind, ref, sha256, size FROM refs WHERE event_id=? ORDER BY rowid", (event_id,))
        return [{"kind": row[0], "ref": row[1], "sha256": row[2], "size": row[3]} for row in rows]

    def traces(self) -> set[str]:
        return {row[0] for row in self.query("SELECT DISTINCT trace_id FROM events")}

    def steps_for(self, trace: str) -> set[str]:
        return {row[0] for row in self.query("SELECT DISTINCT step FROM events WHERE trace_id=?", (trace,))}

    def config_refs_expected(self) -> list[dict]:
        """main 配置引用的期望形状：仓库相对路径@HEAD 加文件内容 sha256 与字节数。"""
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r1_checks.AUTONOMY_PATH.parent,
                             capture_output=True, text=True, check=True, env={**env, **GIT_ENV}).stdout.strip()
        expected = []
        for kind, path in (("autonomy", r1_checks.AUTONOMY_PATH), ("rules", RULES_PATH)):
            content = path.read_bytes()
            expected.append({"kind": kind, "ref": f"{path.relative_to(path.parents[2]).as_posix()}@{rev}",
                             "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
        return expected

    def run_policy(self, argv: list[str], gh) -> dict:
        """驱动 policy.main（与命令行同一入口）：只注入 cwd 与 gh 桩，捕获规则快照与输出文件字节。"""
        self._seq += 1
        captured = []
        orig_gather, orig_decide = policy.gather, policy.decide

        def gather(*args, **kwargs):
            kwargs.setdefault("cwd", self.repo)
            kwargs.setdefault("gh", gh)
            return orig_gather(*args, **kwargs)

        def decide(facts, autonomy):
            rules = orig_decide(facts, autonomy)
            captured.append([(rule.name, rule.ok, rule.reason) for rule in rules])
            return rules

        output = self.tmp / f"github-output-{self._seq}.txt"
        summary = self.tmp / f"step-summary-{self._seq}.md"
        with mock.patch.object(policy, "gather", gather), \
                mock.patch.object(policy, "decide", decide), \
                mock.patch.object(policy, "_gh", gh), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}), \
                contextlib.redirect_stdout(io.StringIO()) as fake_out, \
                contextlib.redirect_stderr(io.StringIO()) as fake_err:
            code = policy.main(list(argv))
        self.assertEqual(code, 0)
        self.assertTrue(captured, "decide 未被调用")
        self.assertIn("合并路由", fake_out.getvalue())
        return {"stdout": fake_out.getvalue(), "stderr": fake_err.getvalue(), "rules": captured[0],
                "output": output.read_bytes() if output.exists() else b"",  # 无 --github 时不写输出文件
                "summary": summary.read_bytes() if summary.exists() else b""}

    def test_risk_file_rules_and_summary(self):
        head = self.commit({
            "README.md": "# app changed\n",
            "docs/specs/thing.md": "# spec\n",
            "bin/thing": "#!/bin/sh\n",
            "tests/test_sample.py": "line1 = 1\nline2 = 22\nline6 = 6\n",
        }, "change\n\nRisk: R1")
        report = risk.classify(self.base, head, cwd=self.repo, trace_id="task/r1")
        self.assertEqual(report.level, 3)
        self.assertTrue(report.claimed_r1)
        self.assertTrue(any(flag.startswith("改动已有测试") for flag in report.flags))
        self.assertEqual({item.path: item.level for item in report.files},
                         {"README.md": 0, "docs/specs/thing.md": 2, "bin/thing": 3,
                          "tests/test_sample.py": 2})

        # 逐文件事件：路径、状态、等级、命中规则与理由逐项对应，不漏文件
        rows = self.recorded("risk.file", "task/r1")
        self.assertEqual([row["outputs"]["path"] for row in rows], [item.path for item in report.files])
        expected_rule = {"README.md": "README*.md", "docs/specs/thing.md": "docs/specs/**",
                         "bin/thing": "bin/**", "tests/test_sample.py": "tests"}
        for row, item in zip(rows, report.files):
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["outputs"], {"path": item.path, "status": item.status, "level": item.level})
            self.assertEqual(row["decision"], {"by": "risk", "rule": expected_rule[item.path], "reason": item.reason})

        # 汇总事件：最终等级、R1 声明与降级、被改已有测试数、base/head 引用
        summaries = self.recorded("risk.summary", "task/r1")
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["outputs"], {"risk": "R3", "level": 3, "claimed_r1": True,
                                              "downgraded": True, "changed_tests": 1})
        self.assertEqual(summary["decision"], {"by": "risk", "rule": "risk", "reason": "R3：必须由用户批准后合并"})
        self.assertEqual(self.inputs_of(summary["id"]),
                         [{"kind": "rev", "ref": self.base, "sha256": None, "size": None},
                          {"kind": "rev", "ref": head, "sha256": None, "size": None}])

        # 旧调用（无 trace_id）仍有效：事件落在 current_trace() 的 main@<提交> 链上
        before = self.query("SELECT COUNT(*) FROM events")[0][0]
        report_default = risk.classify(self.base, head, cwd=self.repo)
        self.assertEqual(report_default.level, 3)
        fallback = events.current_trace()
        self.assertTrue(fallback.startswith("main@"), fallback)
        count = self.query("SELECT COUNT(*) FROM events WHERE trace_id=?", (fallback,))[0][0]
        self.assertEqual(count, len(report_default.files) + 1)
        total = self.query("SELECT COUNT(*) FROM events")[0][0]
        self.assertEqual(total, before + len(report_default.files) + 1)

    def test_route_records_all_rules_and_facts(self):
        head = self.commit({"README.md": "# app routed\n"}, "route fixture")
        autonomy = {
            "size": {"max_lines": 400, "exclude": []},
            "classes": {"K1": {"name": "说明与记录", "level": "L4", "window": 20, "max_escapes": 0,
                               "audit_every": 1}},
        }
        expected_refs = self.config_refs_expected()
        with mock.patch.object(r1_checks, "load_autonomy", return_value=autonomy):
            # 允许组：预算未耗尽 → 每条规则 ok，自动合并并抽审
            allowed = self.run_policy(["--base", self.base, "--head", head, "--pr", "9",
                                       "--branch", "task/route"], fake_gh())
            # 误差预算耗尽组：escape 议题指认了窗口内的合并 → 预算规则 fail，转用户评审
            self.run_policy(["--base", self.base, "--head", head, "--pr", "9",
                             "--branch", "task/route"],
                            fake_gh(issues=[{"number": 7, "title": "escape", "body": "#101"}]))

        self.assertEqual([name for name, _, _ in allowed["rules"]], ["风险", "类别", "预算", "规模", "运行记录"])
        self.assertTrue(all(ok for _, ok, _ in allowed["rules"]))

        # facts 事件：风险、机器类别、声明类别、增删行数、窗口逃逸数；配置引用带提交与内容哈希
        facts = self.recorded("facts", "task/route")
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0]["outputs"],
                         {"risk": "R0", "machine_class": "K1", "declared_class": None,
                          "changed_lines": r1_checks.changed_line_count(self.base, head, self.repo, []),
                          "escapes": 0, "window": 1})
        self.assertEqual(facts[1]["outputs"]["escapes"], 1)
        self.assertIsNone(facts[0]["decision"]["by"])
        self.assertEqual(self.inputs_of(facts[0]["id"]), expected_refs)

        # result 事件：允许组 auto_merge+audit 为真、拒绝组转用户评审，决定者都是 policy
        results = self.recorded("result", "task/route")
        self.assertEqual(len(results), 2)
        approval = policy.platform_outputs()["approval"]
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["outputs"], {"auto_merge": True, "audit": True, "approval": approval})
        self.assertEqual(results[0]["decision"], {"by": "policy", "rule": "result", "reason": "自动合并"})
        self.assertEqual(results[1]["status"], "deny")
        self.assertEqual(results[1]["outputs"], {"auto_merge": False, "audit": False, "approval": approval})
        self.assertEqual(results[1]["decision"], {"by": "policy", "rule": "result", "reason": "转用户评审"})
        for event in results:
            self.assertEqual(self.inputs_of(event["id"]), expected_refs)

        # 每条规则一条事件：ok、理由、决定者逐字对应，不把规则列表压成一个布尔
        for name, ok, reason in allowed["rules"]:
            rows = self.recorded(name, "task/route")
            self.assertEqual(len(rows), 2, name)
            self.assertEqual(rows[0]["status"], "ok" if ok else "fail")
            self.assertEqual(rows[0]["outputs"], {"ok": ok})
            self.assertEqual(rows[0]["decision"], {"by": "policy", "rule": name, "reason": reason})
            self.assertEqual(self.inputs_of(rows[0]["id"]), expected_refs)
        budget = self.recorded("预算", "task/route")[1]
        self.assertEqual(budget["status"], "fail")
        self.assertIn("逃逸 1", budget["decision"]["reason"])

    def test_workflow_run_uses_pr_branch(self):
        head = self.commit({"README.md": "# app ci\n"}, "ci fixture")
        gh = fake_gh(head_ref="task/pr-77")
        os.environ["CI"] = "true"
        os.environ["GITHUB_REF_NAME"] = "main"  # workflow_run 检出 main，不提供 GITHUB_HEAD_REF
        os.environ["GITHUB_RUN_ID"] = "555"
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        os.environ["GITHUB_JOB"] = "policy"

        # 情形一：--branch 提供 → risk 与 route 全部事件都落在 PR 分支，来源链为该次 run/attempt/job
        self.run_policy(["--base", self.base, "--head", head, "--pr", "9", "--branch", "task/pr-9"], gh)
        self.assertEqual(self.traces(), {"task/pr-9"})
        self.assertEqual(self.steps_for("task/pr-9"), ROUTE_STEPS)
        self.assertEqual(self.query("SELECT DISTINCT source FROM events")[0][0], "ci:555:2:policy")

        # 情形二：--branch 缺省、有 PR → 从 PR API 的 headRefName 取（不读 PR 正文）
        self.run_policy(["--base", self.base, "--head", head, "--pr", "9"], gh)
        self.assertEqual(self.traces(), {"task/pr-9", "task/pr-77"})
        self.assertEqual(self.steps_for("task/pr-77"), ROUTE_STEPS)

        # 情形三：无 PR、无 --branch → 回退 current_trace()（GITHUB_REF_NAME=main → main@<提交>）
        self.run_policy(["--base", self.base, "--head", head], gh)
        fallback = [trace for trace in self.traces() if trace.startswith("main@")]
        self.assertEqual(len(fallback), 1)
        self.assertEqual(self.steps_for(fallback[0]), ROUTE_STEPS)
        self.assertNotIn("main", self.traces())

    def test_event_failure_does_not_change_policy(self):
        head = self.commit({"README.md": "# app isolated\n"}, "isolation fixture")
        argv = ["--base", self.base, "--head", head, "--pr", "5", "--github"]

        def failing(*args):
            raise RuntimeError("gh down")

        os.environ["HARNESS_EVENTS"] = "off"  # 基线：事件关闭
        off = self.run_policy(argv, failing)
        del os.environ["HARNESS_EVENTS"]

        healthy = self.run_policy(argv, failing)  # 事件开启且库健康
        self.assertTrue(self.db_path.exists())

        self.db_path.unlink()  # 事件开启但库损坏，且 PR API 与配置引用准备全部失败
        self.db_path.mkdir()
        try:
            with mock.patch.object(policy, "_config_refs", side_effect=RuntimeError("refs down")):
                broken = self.run_policy(argv, failing)
        finally:
            self.db_path.rmdir()

        for key in ("stdout", "rules", "output", "summary"):
            self.assertEqual(off[key], healthy[key], key)
            self.assertEqual(off[key], broken[key], key)
        self.assertTrue(any(ok for _, ok, _ in off["rules"]))  # 等价不是空洞的：规则混合成败
        self.assertTrue(any(not ok for _, ok, _ in off["rules"]))
        self.assertEqual(off["stderr"], "")
        self.assertEqual(healthy["stderr"], "")
        self.assertEqual(broken["stderr"].splitlines(), [WARN_LINE])  # 仅 T101 固定的一次提示


if __name__ == "__main__":
    unittest.main()
