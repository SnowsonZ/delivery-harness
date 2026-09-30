"""T123 结构断言：GitHub 封装类抽离为 engine/agents/github.py，dispatch.py 仅保留再导出。

纯搬移任务：类行为由既有测试覆盖（如 tests.test_ci_workflows、tests.test_events_agents），
这里只断言抽离本身成立。按任务书修订（PR #74）用源码结构断言，不依赖 git 引用——
CI 浅克隆里没有 origin/main。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ENGINE_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_REPO))


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
        """dispatch.py 不再定义 GitHub 类、保留再导出行；github.py 含完整类与专属常量。"""
        dispatch_source = (ENGINE_REPO / "engine/agents/dispatch.py").read_text(encoding="utf-8")
        github_source = (ENGINE_REPO / "engine/agents/github.py").read_text(encoding="utf-8")
        self.assertNotIn("class GitHub:", dispatch_source)  # 搬移而非复制
        self.assertIn("from engine.agents.github import GitHub", dispatch_source)  # dispatch.GitHub 逐字可用
        self.assertIn("class GitHub:", github_source)  # 完整类在新模块
        self.assertIn("CI_QUERY_ATTEMPTS = 3", github_source)
        self.assertIn("CI_QUERY_RETRY_SECONDS = 5", github_source)
        self.assertIn("# ---- GitHub（推送与写操作以 Agent 身份经 bin/as-agent） ----", github_source)
        self.assertIn("def wait_ci", github_source)


if __name__ == "__main__":
    unittest.main()
