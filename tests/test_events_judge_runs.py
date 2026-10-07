"""T720 判定运行关联（B117）：模板 run-name 与 events_judge 的耦合、判定运行的挑选、经 load_ci
的端到端导入、消费者（audit 完整性与合并账本）效果、分页提前停止与请求形状、核对严格性、隔离与
降级、体量与结构。判定运行由 workflow_run 触发、在 API 里归在默认分支，按 PR 分支查询永远查不
到：信任来自默认分支上的工作流定义渲染的运行名（REST display_title），不信任事件包自述；判定包
origin 按该运行自己的 API 记录逐项核对。夹具的判定包经真实 emit + ci_events.export 生成（含
cli.policy 的 main@<短 SHA> 与 route.facts/route.result 的 PR 分支两种 trace），假客户端按 REST
形状提供 PR/运行/工作流/artifact；不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.core import events_judge
from tests.test_ci_events_workflows import parse_workflow

ENGINE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = ENGINE_DIR / "templates" / ".github" / "workflows" / "auto-merge.yml"
TRUSTED = frozenset({".github/workflows/auto-merge.yml", ".github/workflows/auto-merge.yaml",
                     ".github/workflows/harness.yml"})
PR = 187
HEAD = "8f42c2b" + "0" * 33
MAIN_HEAD = "9" * 40
JUDGE_TITLE = events_judge.judge_run_name(PR, HEAD)


def _literal(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else value


class RunNameTest(unittest.TestCase):
    """验收第 1 行：模板顶层 run-name 与 events_judge 生成/解析的名字耦合；条件与 judge.if 逐字相同。"""

    @classmethod
    def setUpClass(cls):
        cls.workflow = parse_workflow(TEMPLATE.read_text(encoding="utf-8"))

    def expression(self) -> str:
        body = str(self.workflow["run-name"]).strip()
        self.assertTrue(body.startswith("${{") and body.endswith("}}"), body)
        return body[3:-2].strip()

    def evaluate(self, context: dict[str, str]) -> str:
        """按 GitHub 表达式的与或短路语义求值 run-name（本模板只含 ==、&&、||、format 与字符串）。"""
        left, separator, fallback = self.expression().partition(" || ")
        self.assertTrue(separator, "run-name 缺少「其余情形保持普通名」的兜底分支")
        terms = left.split(" && ")
        for term in terms[:-1]:
            if not self._comparison(term, context):
                return _literal(fallback.strip())
        return self._rendered(terms[-1], context)

    def _comparison(self, term: str, context: dict[str, str]) -> bool:
        field, marker, expected = term.partition("==")
        self.assertTrue(marker, f"run-name 条件项不是比较式：{term}")
        expected = expected.strip()
        value = _literal(expected) if expected[:1] in ("'", '"') else context.get(expected)
        return context.get(field.strip()) == value

    def _rendered(self, term: str, context: dict[str, str]) -> str:
        match = re.fullmatch(r"format\('([^']+)',\s*([^,]+),\s*(.+?)\)", term.strip())
        self.assertIsNotNone(match, f"run-name 的取值不是 format 串：{term}")
        fmt, pr_field, head_field = match[1], match[2].strip(), match[3].strip()
        # 取值字段恰为 workflow_run 载荷里的 PR 号与被评估的 head（与 judge 等 job 在用的变量一致）
        self.assertEqual(pr_field, "github.event.workflow_run.pull_requests[0].number")
        self.assertEqual(head_field, "github.event.workflow_run.head_sha")
        return fmt.format(context[pr_field], context[head_field])

    def test_format_string_matches_events_judge(self):
        # 格式串经示例值（PR 号、40 位小写 SHA）代入后等于 events_judge 生成的名字
        context = {"github.event.workflow_run.event": "pull_request",
                   "github.event.workflow_run.conclusion": "success",
                   "github.event.workflow_run.head_repository.full_name": "owner/repo",
                   "github.repository": "owner/repo",
                   "github.event.workflow_run.pull_requests[0].number": str(PR),
                   "github.event.workflow_run.head_sha": HEAD}
        self.assertEqual(self.evaluate(context), JUDGE_TITLE)
        self.assertEqual(JUDGE_TITLE, f"auto-merge PR #{PR} @ {HEAD}")
        self.assertEqual(events_judge.parse_judge_run_name(JUDGE_TITLE), (PR, HEAD))

    def test_condition_is_judge_if_verbatim(self):
        # run-name 里的条件与 judge.if 逐字相同（从模板文本里取出两处比较，忽略空白）
        condition = self.expression().split(" && format(")[0]
        judge_if = self.workflow["jobs"]["judge"]["if"]
        self.assertEqual(" ".join(condition.split()), " ".join(str(judge_if).split()))

    def test_other_cases_keep_the_plain_name(self):
        # push 触发、上游 CI 失败（judge 被跳过）、来自 fork 的运行：run-name 求值为普通名 auto-merge
        base = {"github.event.workflow_run.pull_requests[0].number": str(PR),
                "github.event.workflow_run.head_sha": HEAD,
                "github.repository": "owner/repo"}
        cases = {
            "push 触发": {**base, "github.event.workflow_run.event": "push",
                        "github.event.workflow_run.conclusion": "success",
                        "github.event.workflow_run.head_repository.full_name": "owner/repo"},
            "上游 CI 失败因而 judge 被跳过": {**base, "github.event.workflow_run.event": "pull_request",
                                          "github.event.workflow_run.conclusion": "failure",
                                          "github.event.workflow_run.head_repository.full_name": "owner/repo"},
            "来自 fork 的运行": {**base, "github.event.workflow_run.event": "pull_request",
                             "github.event.workflow_run.conclusion": "success",
                             "github.event.workflow_run.head_repository.full_name": "forker/repo"},
        }
        for label, context in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.evaluate(context), "auto-merge")


def api_run(run_id: int, *, title=None, event="workflow_run", path=".github/workflows/auto-merge.yml",
            head_branch="main", head_sha=MAIN_HEAD, attempt=1) -> dict:
    """REST 形状的运行记录（display_title 缺省为该 PR 关联名之外的普通名）。"""
    run = {"id": run_id, "run_attempt": attempt, "event": event, "path": path,
           "head_branch": head_branch, "head_sha": head_sha, "name": "auto-merge"}
    if title is not None:
        run["display_title"] = title
    return run


class SelectTest(unittest.TestCase):
    """验收第 2 行：只有 path 可信、event 为 workflow_run、display_title 整串等于目标名的被挑出。"""

    def collect(self, runs: list[dict]) -> tuple[list[dict], set[str]]:
        return events_judge.select_judge_runs(runs, PR, HEAD, TRUSTED)

    def test_only_exact_matches_are_selected(self):
        matched, stale = self.collect([api_run(1, title=JUDGE_TITLE)])
        self.assertEqual(([run["id"] for run in matched], stale), ([1], set()))

    def test_loose_or_malformed_titles_are_rejected(self):
        other_pr = events_judge.judge_run_name(18, HEAD)
        runs = [api_run(2, title=f"auto-merge PR #18 @ {HEAD}"),           # PR 号前缀相同（#18 对 #187）
                api_run(3, title=other_pr),                                # 另一个 PR 号
                api_run(4, title=events_judge.judge_run_name(1870, HEAD)),  # PR 号前缀相同（#1870 对 #187）
                api_run(4, title=f"auto-merge PR #{PR} @ {HEAD.upper()}"),  # 大写十六进制
                api_run(5, title=f"auto-merge PR #{PR} @ {HEAD[:7]}"),      # 短 SHA
                api_run(6, title=f"  {JUDGE_TITLE}  "),                     # 前后多空白
                api_run(7, title=f"{JUDGE_TITLE}\n"),                       # 尾随换行
                api_run(8),                                                 # display_title 缺失
                api_run(9, title=187),                                      # display_title 非字符串
                api_run(10, title=JUDGE_TITLE, event="push"),               # event 不是 workflow_run
                api_run(12, title=JUDGE_TITLE, path=".github/workflows/evil.yml"),  # path 不可信
                api_run(13, title=JUDGE_TITLE, head_sha=HEAD, event=None)]
        matched, stale = self.collect(runs)
        self.assertEqual((matched, stale), ([], set()))

    def test_same_pr_other_head_counts_as_stale(self):
        other = "d" * 40
        matched, stale = self.collect([api_run(21, title=events_judge.judge_run_name(PR, other), head_sha=other),
                                       api_run(22, title=events_judge.judge_run_name(999, other), head_sha=other)])
        self.assertEqual(([run["id"] for run in matched], stale), ([], {other}))


if __name__ == "__main__":
    unittest.main()
