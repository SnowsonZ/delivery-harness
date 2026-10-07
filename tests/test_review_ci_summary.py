"""评审材料的 CI 结论（ci_summary）与评审口径（提示词、清单、CI 里的消费方 G2）：

- 成功路径输出逐字不变；读不到时回退 statusCheckRollup，两条都失败时写明各自原因；
- 异常形状（null、缺键、非列表、非对象、缺名称）不抛异常，也不被静默写成「没有 CI 检查」；
- 提示词新增的「不属于你评审的」一节只放宽「缺少设计方本机输出」一类，verdict 规则原文仍在；
- 评审清单与提示词、ci.yml 三处对 G2 的口径一致。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from engine.agents import review
from tests.test_ci_events_workflows import parse_workflow

ROOT = Path(__file__).resolve().parents[1]


class FakeGh:
    """按命令前缀应答的 gh 桩：值为字符串则返回，为异常则抛出；未登记的命令直接失败。"""

    def __init__(self, checks=None, view=None):
        self.checks, self.view = checks, view
        self.calls: list[str] = []

    def _run(self, argv, **_):
        joined = " ".join(argv)
        self.calls.append(joined)
        for prefix, answer in (("gh pr checks", self.checks), ("gh pr view", self.view)):
            if joined.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                if answer is None:
                    raise AssertionError(f"未预期的调用：{joined}")
                return answer
        raise AssertionError(f"未登记的 gh 调用：{joined}")


def rollup(*items):
    return json.dumps({"statusCheckRollup": list(items)})


class CiSummaryTest(unittest.TestCase):
    def test_success_output_is_unchanged(self):
        gh = FakeGh(checks=json.dumps([{"name": "harness", "state": "SUCCESS", "link": "https://x/1"},
                                       {"name": "alert", "state": "SKIPPED", "link": "https://x/2"}]))
        self.assertEqual(review.ci_summary(7, gh), "- harness：SUCCESS（https://x/1）\n- alert：SKIPPED（https://x/2）")
        self.assertEqual(len(gh.calls), 1)  # 成功时不碰回退路径
        self.assertEqual(review.ci_summary(7, FakeGh(checks="[]")), "没有 CI 检查。")

    def test_failed_or_pending_checks_are_still_listed_when_checks_command_fails(self):
        gh = FakeGh(checks=RuntimeError("gh pr checks 失败：exit status 1"), view=rollup(
            {"__typename": "CheckRun", "name": "test (macos-latest)", "status": "COMPLETED", "conclusion": "FAILURE",
             "detailsUrl": "https://x/10"},
            {"__typename": "CheckRun", "name": "harness", "status": "IN_PROGRESS", "conclusion": "", "detailsUrl": "https://x/11"},
            {"__typename": "StatusContext", "context": "legacy/ci", "state": "SUCCESS", "targetUrl": "https://x/12"}))
        text = review.ci_summary(7, gh)
        self.assertIn("- test (macos-latest)：FAILURE（https://x/10）", text)
        self.assertIn("- harness：IN_PROGRESS（https://x/11）", text)
        self.assertIn("- legacy/ci：SUCCESS（https://x/12）", text)
        self.assertNotIn("读不到", text)

    def test_invalid_output_falls_back_too(self):
        # 非 JSON、非列表的合法 JSON（对象、空对象、字符串、空字符串、数字、null）、缺键的列表都要回退，
        # 其中 {} 与 "" 迭代为空，若不检查顶层类型会被误写成「没有 CI 检查」。
        for bad in ("不是 JSON", json.dumps({"x": 1}), json.dumps({}), json.dumps(""), json.dumps(5),
                    json.dumps([{"name": "a"}]), json.dumps(None)):
            with self.subTest(output=bad[:12]):
                gh = FakeGh(checks=bad, view=rollup({"name": "harness", "status": "COMPLETED", "conclusion": "SUCCESS",
                                                    "detailsUrl": "u"}))
                self.assertEqual(review.ci_summary(7, gh), "- harness：SUCCESS（u）")

    def test_odd_rollups_are_never_silently_empty(self):
        broken = RuntimeError("boom")
        # 空列表才是「没有 CI 检查」
        self.assertEqual(review.ci_summary(7, FakeGh(checks=broken, view=rollup())), "没有 CI 检查。")
        # 缺名称、非对象的项被跳过但要说明，不能因此变成「没有 CI 检查」
        text = review.ci_summary(7, FakeGh(checks=broken, view=rollup({"status": "COMPLETED"}, "x", None)))
        self.assertIn("另有 3 项", text)
        self.assertIn("不完整", text)
        self.assertNotEqual(text, "没有 CI 检查。")
        text = review.ci_summary(7, FakeGh(checks=broken, view=rollup({"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u"}, "x")))
        self.assertIn("- a：SUCCESS（u）", text)
        self.assertIn("另有 1 项", text)
        # null、缺键、非列表、非对象：读不到并写明原因
        for view in (json.dumps({"statusCheckRollup": None}), json.dumps({}), json.dumps({"statusCheckRollup": {"a": 1}}),
                     json.dumps([1, 2]), "不是 JSON"):
            with self.subTest(view=view[:20]):
                text = review.ci_summary(7, FakeGh(checks=broken, view=view))
                self.assertTrue(text.startswith("读不到 CI 检查结果："), text)
                self.assertIn("boom", text)  # 第一条路径的原因

    def test_unreadable_message_carries_both_reasons_and_is_bounded(self):
        text = review.ci_summary(7, FakeGh(checks=RuntimeError("gh pr checks 失败：网络断开"),
                                           view=RuntimeError("gh pr view 失败：TLS 握手失败")))
        self.assertTrue(text.startswith("读不到 CI 检查结果："))
        self.assertIn("网络断开", text)
        self.assertIn("TLS 握手失败", text)
        # 两条原因都很长时仍能分辨双方：各自保留来源名与错误尾部，总长受限
        long = review.ci_summary(7, FakeGh(checks=RuntimeError("a" * 5000 + "TAIL-ONE"),
                                           view=RuntimeError("b" * 5000 + "TAIL-TWO")))
        self.assertLessEqual(len(long), len("读不到 CI 检查结果：") + review.CI_REASON_LIMIT + 1)
        for token in ("gh pr checks：", "TAIL-ONE", "gh pr view：", "TAIL-TWO"):
            self.assertIn(token, long)


class CiReasonPrivacyTest(unittest.TestCase):
    def test_local_paths_are_not_echoed_into_the_materials(self):
        # 占位用户名取仓库卫生规则允许的 someone；路径后面的部分用独特词，便于断言没有原样出现
        paths = ("/Users/someone/secretdir/repo", "/home/someone/secretdir/repo", "C:\\Users\\someone\\secretdir\\repo")
        for path in paths:
            with self.subTest(path=path):
                text = review.ci_summary(7, FakeGh(checks=RuntimeError(f"gh 失败：cwd={path}/x 不存在"),
                                                   view=RuntimeError(f"gh 失败：{path}: 拒绝访问")))
                self.assertTrue(text.startswith("读不到 CI 检查结果："), text)
                self.assertNotIn("secretdir", text)
                self.assertNotIn("someone", text)
                self.assertIn("<本机路径>", text)
                self.assertIn("拒绝访问", text)  # 脱敏不丢诊断信息


class ReviewScopeTest(unittest.TestCase):
    def setUp(self):
        self.prompt = (ROOT / "engine/prompts/review_prompt.md").read_text(encoding="utf-8")

    def test_designer_mutation_output_is_out_of_scope_but_real_defects_are_not(self):
        self.assertIn("## 不属于你评审的", self.prompt)
        section = self.prompt.split("## 不属于你评审的", 1)[1].split("## 怎么评", 1)[0]
        for phrase in ("定向变异复核", "不是发现", "「需用户验收」", "`consumer-contract`", "`bin/verify --full`",
                       "测试设计无效", "CI 失败", "不要猜测通过或失败", "仅评审 PR 时",
                       "只适用于配置了它的项目", "不要求补它", "不要把它的缺失写成发现"):
            self.assertIn(phrase, self.prompt if phrase == "仅评审 PR 时" else section, phrase)
        self.assertLess(self.prompt.index("## 不属于你评审的"), self.prompt.index("## 怎么评"))

    def test_verdict_rules_are_unchanged(self):
        for rule in ("有阻断或严重发现时 verdict 为「不通过」。",
                     "只有机器判定不了、需要人在真机或真实数据上确认的项时，verdict 为「需用户验收」",
                     "没有发现时 findings 为空数组。不要为了显得认真而编造发现。",
                     "不采信自述：「已修复」「测试通过」以证据包为准。"):
            self.assertIn(rule, self.prompt)

    def test_checklist_prompt_and_ci_agree_on_g2(self):
        checklist = (ROOT / "docs/templates/review-checklist.md").read_text(encoding="utf-8")
        self.assertIn("`consumer-contract`", checklist)
        self.assertNotIn("附消费方等价验证（G2）证据", checklist)  # 旧口径：要求设计方附证据
        self.assertIn("定向变异复核的原始输出不属于评审要求的证据", checklist)
        jobs = parse_workflow((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
        runs = {step.get("name"): step for step in jobs["consumer-contract"]["steps"]}
        verify = runs["Consumer full verify (G2)"]
        self.assertEqual(verify["run"], "bin/verify --full")
        self.assertEqual(verify["working-directory"], "consumer")
        self.assertNotIn("Consumer quick verify", runs)  # 完整档已包含快速档


if __name__ == "__main__":
    unittest.main()
