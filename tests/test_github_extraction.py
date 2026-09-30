"""T123 结构断言：GitHub 封装类抽离为 engine/agents/github.py，dispatch.py 仅保留再导出。

纯搬移任务：类行为由既有测试覆盖（如 tests.test_ci_workflows、tests.test_events_agents），
这里只断言抽离本身成立——再导出别名、无循环导入、dispatch.py 较 main 收缩且新模块逐字含完整类。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ENGINE_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_REPO))


def class_block(source: str) -> list[str]:
    """顶层 class GitHub 定义块：从 class 行到下一个顶层语句（不含），去块尾空行。"""
    lines = source.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("class GitHub:"))
    end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i][0].isspace()), len(lines))
    while lines[end - 1] == "":
        end -= 1
    return lines[start:end]


def git_show(rev_path: str) -> str:
    return subprocess.run(["git", "show", rev_path], cwd=ENGINE_REPO, capture_output=True, text=True,
                          check=True).stdout


class GithubExtractionTest(unittest.TestCase):
    def test_alias_and_no_cycle(self):
        """dispatch.GitHub 就是新模块里的同一个类；两模块按任一顺序导入都不循环。"""
        for order in ("engine.agents.github, engine.agents.dispatch",
                      "engine.agents.dispatch, engine.agents.github"):
            result = subprocess.run([sys.executable, "-c", f"import {order}"], cwd=ENGINE_REPO,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        from engine.agents import dispatch, github
        self.assertIs(dispatch.GitHub, github.GitHub)
        self.assertEqual(dispatch.GitHub.__module__, "engine.agents.github")

    def test_dispatch_shrinks_github_moved(self):
        """dispatch.py 较 main 收缩 ≥100 行且不再定义类本身；github.py 逐字含 main 上的完整类与常量。"""
        current = (ENGINE_REPO / "engine/agents/dispatch.py").read_text(encoding="utf-8")
        github_source = (ENGINE_REPO / "engine/agents/github.py").read_text(encoding="utf-8")
        self.assertNotIn("class GitHub:", current)  # 搬移而非复制
        self.assertIn("CI_QUERY_ATTEMPTS = 3", github_source)
        self.assertIn("CI_QUERY_RETRY_SECONDS = 5", github_source)
        main_source = git_show("origin/main:engine/agents/dispatch.py")
        if "class GitHub:" in main_source:  # main 吸收本次抽离之前（含本 PR 的 CI）
            self.assertLessEqual(len(current.splitlines()), len(main_source.splitlines()) - 100)
            self.assertEqual(class_block(main_source), class_block(github_source))
        else:  # main 已吸收抽离之后：行数对照失去基准，退化为断言新模块仍含完整类
            self.assertIn("class GitHub:", github_source)
            self.assertIn("def wait_ci", github_source)


if __name__ == "__main__":
    unittest.main()
