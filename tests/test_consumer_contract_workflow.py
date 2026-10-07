"""B44 消费方契约测试入 CI：ci.yml 的 consumer-contract job 结构断言。

四要素缺一不可：消费方（Agent-Notification）main 检出到独立目录、用本检出引擎
`engine/cli.py upgrade --target` 升级、`test_harness*` 发现式契约测试、`bin/verify --full`（消费方等价验证 G2，T716 起由快速档升为完整档）。
按整行精确断言，改错命令、删步骤、漏要素都失败；结构断言只看工作流文本、不执行它，
job 真实可跑以本 PR 的 CI 运行为准。
"""

from __future__ import annotations

import re
import unittest
from itertools import pairwise
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
JOB = "consumer-contract"
STEP_MARK = re.compile(r"^      - (?:name|uses):", re.MULTILINE)


def job_block(text: str) -> str:
    """顶层 job 的整块文本：从 `  <job>:` 行到下一个同层键或文件结束。"""
    match = re.search(rf"^  {re.escape(JOB)}:", text, re.MULTILINE)
    if not match:
        raise AssertionError(f"ci.yml 缺少 {JOB} job")
    nxt = re.search(r"^  \S", text[match.end():], re.MULTILINE)
    end = match.end() + nxt.start() if nxt else len(text)
    return text[match.end():end]


def named_step(blocks: list[str], name: str) -> str:
    for block in blocks:
        if re.search(rf"^      - name: {re.escape(name)}$", block, re.MULTILINE):
            return block
    raise AssertionError(f"{JOB} job 缺少步骤「{name}」")


class ConsumerContractWorkflowTest(unittest.TestCase):
    def setUp(self):
        job = job_block(WORKFLOW.read_text(encoding="utf-8"))
        marks = [m.start() for m in STEP_MARK.finditer(job)] + [len(job)]
        self.steps = [job[a:b] for a, b in pairwise(marks)]

    def assert_has_line(self, block: str, line: str):
        """整行精确断言：改命令的任何字符（含尾部追加）都算失败。"""
        self.assertIn("\n" + line + "\n", block + "\n")

    def test_job_structure(self):
        # 要素一：消费方 main 浅克隆到独立目录 consumer。
        checkout = named_step(self.steps, "Checkout consumer main")
        self.assertIn("actions/checkout@", checkout)
        for line in (
            "          repository: SnowsonZ/Agent-Notification",
            "          ref: main",
            "          path: consumer",
            "          fetch-depth: 1",
        ):
            self.assert_has_line(checkout, line)
        # 要素二：用本仓库当前检出的引擎升级消费方 .harness（CI 检出不在引擎 origin/main 上，需 --allow-dirty）。
        upgrade = named_step(self.steps, "Upgrade consumer harness with this checkout")
        self.assert_has_line(upgrade, "        run: python3 engine/cli.py upgrade --target consumer --allow-dirty")
        # 要素三：契约测试按模式发现式运行，ResourceWarning 升级为错误。
        tests = named_step(self.steps, "Consumer harness contract tests")
        self.assert_has_line(tests, "        working-directory: consumer")
        self.assert_has_line(
            tests,
            '        run: python3 -W error::ResourceWarning -m unittest discover -s tests -p "test_harness*.py" -v',
        )
        # 要素四：消费方完整档验证（G2 成为 CI 事实；完整档已包含快速档的全部检查）。
        verify = named_step(self.steps, "Consumer full verify (G2)")
        self.assert_has_line(verify, "        working-directory: consumer")
        self.assert_has_line(verify, "        run: bin/verify --full")


if __name__ == "__main__":
    unittest.main()
