"""消费方 harness 契约测试（B42，T118）：独立评审契约——只读性、结论解析、失败判定、校准停止。

蓝本：Agent-Notification main 的 tests/test_harness_review_independent.py（ParseTest、ReviewerTest、
FailureTest 与 CommentAndScoreTest 中对应四个契约场景的断言），按本仓库当前引擎逐项核对接口后移植；
评审方一律用假脚本（不调用真实 codex/claude/pi/opencode 二进制）。无法移植或不在本任务范围内的蓝本
场景在文末列明。

与蓝本的形状差异（逐条列明；照抄蓝本形状在本仓库引擎上会失败或行为不同）：
1. 导入位置：蓝本从消费方仓库的 `.harness/engine` 导入引擎（安装形态）；本仓库是引擎仓库，从仓库根
   `engine/` 导入。
2. `Reviewer.read` 返回三元组：本仓库按共用合同 C6 返回 (最终文本, 模型, model_basis)，蓝本返回二元组
   (stdout, 模型)；全部假评审方与逐事件解析断言均已改为三元组解包。
3. `make_reviewer` 的模型断言：蓝本依赖消费方仓库 rules.toml [review] 里的具体模型值；移植后 mock
   `review.load_rules` 注入夹具配置，断言「配置 → 评审方」的接线本身，不依赖本仓库真实配置的模型值。
4. `run_reviewer` 的失败文案取自 `_failure_line`（只留以 ERROR 开头的行）；蓝本的额度用尽样例文本在
   此口径下仍能命中，断言保持原文。校准停止的补丁面（calibration_samples / review_workspace / git /
   prepare_sample / write_materials / EVALS）逐一核对于本仓库 calibrate（含 jobs 参数，缺省 1）仍全部生效。

跳过的蓝本场景及原因：
- 分离规则（designer_of）、评论渲染、比较基点（review_base）、后台评审（pending/watch）、Pi 宿主
  argv 细节：不属任务书点名的四个契约场景；其中评审元数据与 C6 审计摘要已由本仓库
  tests/test_events_agents.py 按当前接口覆盖。
- 校准样本覆盖度（calibration_samples 对照 evals/review/samples.json 与回放用例）与坏样本注入
  （prepare_sample）：本仓库没有评审校准集（evals/review/），样本清单断言无法夹具化；
  prepare_sample 注入不属点名场景。
- WorkflowTest（auto-merge 工作流给 R2 打标签）：断言的是消费方仓库自己的 CI 文件布局，本仓库工作流
  不同，无法移植。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import review


class ReviewContractTest(unittest.TestCase):
    def test_reviewers_are_read_only(self):
        # Codex：read-only 沙箱；推理档位经 -c 传入；Claude：只读工具白名单。
        codex = review.CodexReviewer("host/model-a").argv("p", Path("/w"), Path("/o"))
        self.assertEqual(codex[:4], ["codex", "exec", "-s", "read-only"])
        tuned = review.CodexReviewer("host/model-a", "max").argv("p", Path("/w"), Path("/o"))
        self.assertIn('model_reasoning_effort="max"', tuned)
        claude = review.ClaudeReviewer().argv("p", Path("/w"), Path("/o"))
        self.assertEqual(claude[claude.index("--allowedTools") + 1], "Read,Grep,Glob")
        # Pi：只读工具白名单 + 显式模型。
        pi = review.PiReviewer("host/model-p").argv("p", Path("/w"), Path("/o"))
        self.assertEqual(pi[:4], ["pi", "-na", "--tools", "read,grep,find,ls"])
        self.assertEqual(pi[pi.index("--model") + 1], "host/model-p")
        # OpenCode：pure + plan 代理；配置层面锁死命令、子代理、网页与写入类工具（plan 本身不禁 bash）。
        opencode = review.OpenCodeReviewer("host/model-o").argv("p", Path("/w"), Path("/o"))
        self.assertEqual(opencode[:6], ["opencode", "run", "--pure", "--agent", "plan", "--format"])
        config = json.loads(review.OpenCodeReviewer.env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"]["bash"], "deny")
        self.assertFalse(any(config["tools"][name] for name in ("bash", "task", "webfetch", "write", "edit", "patch")))
        # 差异 3：配置 → 评审方的接线用夹具配置断言，不依赖本仓库真实 rules.toml 的模型值。
        rules = {"review": {"codex_model": "host/model-c", "codex_reasoning_effort": "max",
                            "pi_model": "host/model-p", "opencode_model": "host/model-o",
                            "claude_model": "host/model-l"}}
        with mock.patch.object(review, "load_rules", return_value=rules):
            self.assertEqual(review.make_reviewer("codex").argv("p", Path("/w"), Path("/o"))[5], "host/model-c")
            self.assertEqual(review.make_reviewer("pi").argv("p", Path("/w"), Path("/o"))[-2], "host/model-p")
            self.assertIsInstance(review.make_reviewer("opencode"), review.OpenCodeReviewer)

        # 评审方的运行环境没有 GitHub 凭据：凭据就在派发进程的环境里也拿不到（差异 2：read 三元组）。
        class TokenProbe(review.Reviewer):
            name = "probe"

            def argv(self, prompt, workspace, output):
                code = ("import os; print('意见');"
                        "print('{\"verdict\": \"' + ('不通过' if os.environ.get('GH_TOKEN') else '通过') + '\", \"findings\": []}')")
                return [sys.executable, "-c", code]

            def read(self, stdout, output):
                return stdout, "host/model-fake", "reported"

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"GH_TOKEN": "secret-token", "GITHUB_TOKEN": "x"}):
            verdict, model, _ = review.run_reviewer(TokenProbe(), Path(tmp), 60)
        self.assertEqual((verdict.verdict, model), ("通过", "host/model-fake"))

        # read() 的逐事件解析契约（差异 2：一律三元组）。
        pi_events = [
            {"type": "message_end", "message": {"role": "assistant", "model": "host/model-x",
                                                "content": [{"type": "text", "text": "先看看"}]}},
            {"type": "message_end", "message": {"role": "toolResult", "content": [{"type": "text", "text": "x"}]}},
            {"type": "message_end", "message": {"role": "assistant", "model": "host/model-x", "content": [
                {"type": "thinking", "thinking": "…"}, {"type": "text", "text": '意见\n{"verdict": "通过", "findings": []}'}]}},
        ]
        text, model, basis = review.PiReviewer().read(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in pi_events), Path("/o"))
        self.assertEqual((review.parse_output(text).verdict, model, basis), ("通过", "host/model-x", "reported"))
        oc_events = [
            {"type": "step_start", "part": {"type": "step-start"}},
            {"type": "tool_use", "part": {"type": "tool", "tool": "read"}},
            {"type": "text", "part": {"type": "text", "text": "意见"}},
            {"type": "text", "part": {"type": "text",
                                      "text": '```json\n{"verdict": "不通过", "findings": [{"severity": "严重"}]}\n```'}},
        ]
        text, model, basis = review.OpenCodeReviewer("host/model-o").read(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in oc_events), Path("/o"))
        verdict = review.parse_output(text)
        self.assertEqual((verdict.verdict, verdict.flagged, model, basis),
                         ("不通过", True, "host/model-o", "explicit_request"))
        claude_stdout = json.dumps({"result": '好\n{"verdict": "通过", "findings": []}',
                                    "modelUsage": {"host/model-l": {}}})
        text, model, basis = review.ClaudeReviewer().read(claude_stdout, Path("/none"))
        self.assertEqual((review.parse_output(text).verdict, model, basis), ("通过", "host/model-l", "reported"))

    def test_verdict_json_parsing(self):
        # 取最后一个带 verdict 的 JSON 行：旧结论被新结论覆盖；阻断级发现使 flagged 为真。
        text = '评审意见……\n{"verdict": "通过", "findings": [], "summary": "旧"}\n更多\n' \
               '{"verdict": "不通过", "findings": [{"severity": "阻断", "location": "a.py:3", "problem": "p", "fix": "f"}], "summary": "s"}'
        verdict = review.parse_output(text)
        self.assertEqual((verdict.verdict, verdict.summary, len(verdict.findings)), ("不通过", "s", 1))
        self.assertTrue(verdict.flagged)
        # 代码围栏里的结论可解析；未知 verdict 按「需用户验收」且标未解析。
        self.assertEqual(review.parse_output('```json {"verdict": "通过", "findings": []}').verdict, "通过")
        missing = review.parse_output('{"verdict": "好"}\n没有结论')
        self.assertEqual((missing.verdict, missing.parsed), ("需用户验收", False))
        # 严重级发现即使结论是通过也视为抓住问题；一般级不算。
        self.assertTrue(review.Verdict("通过", [{"severity": "严重"}]).flagged)
        self.assertFalse(review.Verdict("需用户验收", [{"severity": "一般"}]).flagged)

    def test_reviewer_failure_is_not_a_verdict(self):
        # 评审方报错退出（如额度用尽）不是结论：记为评审失败，不评论为结论，不计入校准。
        class Scripted(review.Reviewer):
            name = "scripted"

            def __init__(self, outputs):
                self.outputs = list(outputs)

            def argv(self, prompt, workspace, output):
                code, text = self.outputs.pop(0)
                return [sys.executable, "-c", f"import sys; print({text!r}); sys.exit({code})"]

            def read(self, stdout, output):
                return stdout, "host/model-scripted", "reported"

        quota = "ERROR: You've hit your usage limit. Try again at 12:51 AM."
        with tempfile.TemporaryDirectory() as tmp:
            verdict, _, _ = review.run_reviewer(Scripted([(1, quota)]), Path(tmp), 60)
        self.assertEqual(verdict.verdict, "评审失败")
        self.assertIn("usage limit", verdict.failure)
        self.assertFalse(verdict.flagged)
        # 打分口径：失败（parsed=False）与样本注入出错不进 TPR/TNR，不冒充「放过」。
        results = [
            {"kind": "bad", "flagged": True}, {"kind": "bad", "flagged": False},
            {"kind": "good", "flagged": False}, {"kind": "good", "flagged": False, "parsed": False},
            {"kind": "bad", "error": "注入点过期"},
        ]
        self.assertEqual(review.score(results),
                         {"bad": 2, "good": 1, "errors": 1, "tpr": 0.5, "tnr": 1.0, "unparsed": 1, "total": 5})

    def test_calibration_stops_after_three_failures_and_resumes(self):
        ok = (0, '{"verdict": "不通过", "findings": []}')
        fail = (1, "ERROR: usage limit")
        samples = [{"kind": "bad", "id": f"S{n}", "title": "t", "file": "a", "find": "x", "replace": "y"}
                   for n in range(6)]

        class Scripted(review.Reviewer):
            name = "scripted"

            def __init__(self, outputs):
                self.outputs = list(outputs)

            def argv(self, prompt, workspace, output):
                code, text = self.outputs.pop(0)
                return [sys.executable, "-c", f"import sys; print({text!r}); sys.exit({code})"]

            def read(self, stdout, output):
                return stdout, "host/model-scripted", "reported"

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result.json"
            with mock.patch.object(review, "calibration_samples", return_value=samples), \
                    mock.patch.object(review, "review_workspace", return_value=Path(tmp)), \
                    mock.patch.object(review, "git", return_value="abc"), \
                    mock.patch.object(review, "prepare_sample", return_value=("abc", "pr")), \
                    mock.patch.object(review, "write_materials"), \
                    mock.patch.object(review, "EVALS", Path(tmp)):
                out.write_text(json.dumps({"main": "abc", "date": "d", "samples": []}))
                first = review.calibrate("codex", root=Path(tmp), resume=out,
                                         reviewer=Scripted([ok, fail, fail, fail, ok, ok]))
                self.assertEqual([item["id"] for item in first["samples"]], ["S0", "S1", "S2", "S3"])
                self.assertEqual((first["bad"], first["tpr"], first["unparsed"]), (1, 1.0, 3))
                saved = json.loads(out.read_text())
                self.assertEqual(len(saved["samples"]), 4)  # 每个已评审样本都已落盘
                # 恢复：未解析的失败样本重评，其余跳过。
                second = review.calibrate("codex", root=Path(tmp), resume=out,
                                          reviewer=Scripted([ok, ok, ok, ok, ok]))
        self.assertEqual(sorted(item["id"] for item in second["samples"]), [f"S{n}" for n in range(6)])
        self.assertEqual((second["bad"], second["unparsed"]), (6, 0))


if __name__ == "__main__":
    unittest.main()
