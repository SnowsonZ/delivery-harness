"""CI 增量化（纯文档跳过 + macOS 测试分片）的守护：

- bin/ci-shard 的各片互不相交、合起来恰好是 `unittest discover -s tests` 找到的全部模块；
- ci.yml 的跳过只在明确的纯文档改动时发生：上游失败或没起来时不得放行必需检查（跳过会被当成通过）；
- 汇总 job 用 always() 显式判定，分片被意外跳过必须失败；
- 变更分类脚本与汇总脚本按真实的 shell 逐场景回放。
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from tests.test_ci_events_workflows import parse_workflow

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
SKIP_IF = "${{ always() && needs.changes.outputs.docs_only != 'true' }}"


def load_shard():
    loader = SourceFileLoader("ci_shard", str(ROOT / "bin/ci-shard"))
    spec = importlib.util.spec_from_loader("ci_shard", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def discovered_modules() -> set[str]:
    """unittest discover -s tests 找到的模块（文件名主干），不含加载失败的占位项。"""
    names: set[str] = set()

    def walk(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                walk(item)
            else:
                names.add(type(item).__module__.rsplit(".", 1)[-1])

    walk(unittest.TestLoader().discover(str(ROOT / "tests")))
    return names


class ShardScriptTest(unittest.TestCase):
    def test_shards_partition_every_discovered_module_exactly_once(self):
        shard = load_shard()
        expected = {f"tests.{name}" for name in discovered_modules()}
        self.assertTrue(expected)
        for count in (1, 2, 3, 5):
            buckets = shard.shards(count)
            flat = [name for bucket in buckets for name in bucket]
            self.assertEqual(sorted(flat), sorted(set(flat)), f"count={count} 有模块被重复分配")
            self.assertEqual(set(flat), expected, f"count={count} 漏了或多了模块")
            self.assertTrue(all(buckets), f"count={count} 有空片")

    def test_assignment_is_deterministic_and_command_line_is_valid(self):
        shard = load_shard()
        self.assertEqual(shard.shards(3), shard.shards(3))
        first = subprocess.run(["python3", str(ROOT / "bin/ci-shard"), "1", "3"], capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout.split(), shard.shards(3)[0])
        for bad in (["0", "3"], ["4", "3"], ["x", "3"], ["1"]):
            result = subprocess.run(["python3", str(ROOT / "bin/ci-shard"), *bad], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, bad)


class ShardWeightsTest(unittest.TestCase):
    def test_weighted_shards_are_balanced(self):
        # 实测权重下三片的总权重接近（失衡超过 15% 说明权重过期或装箱退化）
        shard = load_shard()
        table = shard.weights()
        self.assertTrue(table)
        loads = [sum(table.get(name, shard.DEFAULT_WEIGHT) for name in bucket) for bucket in shard.shards(3)]
        self.assertLessEqual(max(loads) - min(loads), 0.15 * (sum(loads) / 3), loads)

    def test_unlisted_modules_get_default_weight_and_bad_lines_are_ignored(self):
        shard = load_shard()
        tmp = Path(tempfile.mkdtemp(prefix="dh-shard-weights-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        weights = tmp / "weights.txt"
        weights.write_text("# 注释\ntests.test_ci_sharding 99.5\n坏行\ntests.test_x notanumber\n\n", encoding="utf-8")
        original = shard.WEIGHTS_FILE
        shard.WEIGHTS_FILE = weights
        self.addCleanup(setattr, shard, "WEIGHTS_FILE", original)
        self.assertEqual(shard.weights(), {"tests.test_ci_sharding": 99.5})
        named = dict(shard.modules())
        self.assertEqual(named["tests.test_ci_sharding"], 99.5)
        self.assertEqual(named["tests.test_install"], shard.DEFAULT_WEIGHT)  # 没登记按默认，不会漏测
        flat = sorted(name for bucket in shard.shards(3) for name in bucket)
        self.assertEqual(flat, sorted(named))
        shard.WEIGHTS_FILE = tmp / "missing.txt"  # 权重文件缺失：全部按默认，仍完整分配
        self.assertEqual(shard.weights(), {})
        self.assertEqual(sorted(name for bucket in shard.shards(3) for name in bucket), sorted(named))


class CiWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = parse_workflow(CI.read_text(encoding="utf-8"))
        self.jobs = self.workflow["jobs"]

    def test_required_check_names_are_stable(self):
        # ruleset 登记的必需检查名：test (macos-latest)、harness；名字变了必需检查会永远等待
        self.assertEqual(self.jobs["test-macos-gate"]["name"], "test (macos-latest)")
        # ubuntu 测试已与 harness 的 verify --full 合并，不再有独立 job（必需检查已从 ruleset 移除）
        self.assertNotIn("test-ubuntu", self.jobs)
        self.assertEqual(sorted(self.jobs), ["changes", "consumer-contract", "test-macos", "test-macos-gate"])

    def test_skips_happen_only_on_explicit_docs_only(self):
        # always() 保证上游失败、没起来时 docs_only 为空，下游照常全跑；只有输出明确为 true 才跳过
        for name in ("test-macos", "consumer-contract"):
            self.assertEqual(self.jobs[name]["if"], SKIP_IF, name)
            self.assertEqual(self.jobs[name]["needs"], "changes", name)
        self.assertEqual(self.jobs["test-macos-gate"]["if"], "${{ always() }}")
        self.assertEqual(sorted(self.jobs["test-macos-gate"]["needs"]), ["changes", "test-macos"])
        # 分类 job 本身不检出 PR 代码（只调 API 读文件列表）
        self.assertFalse([s for s in self.jobs["changes"]["steps"] if "checkout" in str(s.get("uses", ""))])

    def test_shard_matrix_matches_the_shard_command(self):
        macos = self.jobs["test-macos"]
        shards = [int(x) for x in macos["strategy"]["matrix"]["shard"]]
        self.assertEqual(shards, list(range(1, len(shards) + 1)))
        run = next(step["run"] for step in macos["steps"] if "ci-shard" in str(step.get("run", "")))
        self.assertIn(f"bin/ci-shard ${{{{ matrix.shard }}}} {len(shards)}", run)  # 片数与矩阵一致，否则漏测
        self.assertIs(macos["strategy"]["fail-fast"], False)

    def test_docs_pattern_matches_only_documentation(self):
        env = self.jobs["changes"]["steps"][0]["env"]
        pattern = re.compile(env["DOCS_PATTERN"])
        for path in ("docs/backlog.md", "docs/plans/task-716-x.md", "README.md", "README.zh-CN.md",
                     "CHANGELOG.md", "SECURITY.md"):
            self.assertTrue(pattern.search(path), path)
        for path in (".github/workflows/ci.yml", "bin/ci-shard", "bin/verify", "engine/cli.py", "tests/test_x.py",
                     "templates/a.md", "docsx/a.md", "docs", "README.md.py", "xREADME.md", "AGENTS.md",
                     ".harness/config/rules.toml", "docs.py"):
            self.assertFalse(pattern.search(path), path)
        self.assertIn("previous_filename", self.jobs["changes"]["steps"][0]["run"])  # 改名前的路径也要检查


class ClassifyAndGateReplayTest(unittest.TestCase):
    """按真实 bash 回放 changes 的分类步骤与汇总 job 的判定步骤。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-ci-incr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        jobs = parse_workflow(CI.read_text(encoding="utf-8"))["jobs"]
        self.classify = jobs["changes"]["steps"][0]
        self.gate = jobs["test-macos-gate"]["steps"][0]["run"]
        shim = self.tmp / "bin"
        shim.mkdir()
        gh = shim / "gh"
        gh.write_text('#!/bin/sh\n[ -n "${GH_FAIL:-}" ] && exit 1\nprintf "%s" "${GH_FILES:-}"\n', encoding="utf-8")
        gh.chmod(0o755)
        self.path = f"{shim}:{os.environ['PATH']}"

    def run_classify(self, files: str, *, event="pull_request", fail=False, pr="7") -> str:
        output = self.tmp / "out"
        output.write_text("", encoding="utf-8")
        env = {"PATH": self.path, "GITHUB_OUTPUT": str(output), "GITHUB_REPOSITORY": "o/r", "EVENT_NAME": event,
               "PR": pr, "GH_TOKEN": "t", "DOCS_PATTERN": self.classify["env"]["DOCS_PATTERN"], "GH_FILES": files}
        if fail:
            env["GH_FAIL"] = "1"
        result = subprocess.run(["bash", "-e", "-c", self.classify["run"]], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_text(encoding="utf-8").strip()

    def test_classification_scenarios(self):
        self.assertEqual(self.run_classify("docs/a.md\nREADME.md\nCHANGELOG.md\n"), "docs_only=true")
        self.assertEqual(self.run_classify("docs/a.md\nengine/x.py\n"), "docs_only=false")  # 混合 → 要跑
        self.assertEqual(self.run_classify("docs/a.md\nengine/old.py\n"), "docs_only=false")  # 改名自代码
        self.assertEqual(self.run_classify(""), "docs_only=false")  # 空列表按要跑
        self.assertEqual(self.run_classify("docs/a.md\n", fail=True), "docs_only=false")  # API 失败按要跑
        self.assertEqual(self.run_classify("docs/a.md\n", event="push"), "docs_only=false")  # 非 PR 事件
        self.assertEqual(self.run_classify("docs/a.md\n", pr=""), "docs_only=false")

    def run_gate(self, docs_only: str, shards: str) -> int:
        env = {"PATH": os.environ["PATH"], "DOCS_ONLY": docs_only, "SHARDS": shards}
        return subprocess.run(["bash", "-e", "-c", self.gate], env=env, capture_output=True, text=True, check=False).returncode

    def test_gate_passes_only_when_expected(self):
        self.assertEqual(self.run_gate("false", "success"), 0)
        self.assertEqual(self.run_gate("", "success"), 0)  # 分类没起来时分片照跑，成功即通过
        self.assertEqual(self.run_gate("true", "skipped"), 0)  # 纯文档：允许全部跳过
        for docs_only, shards in (("false", "skipped"),   # 不是纯文档却被跳过：必须失败（不能被当成通过）
                                  ("", "skipped"), ("false", "failure"), ("false", "cancelled"),
                                  ("true", "failure"), ("true", "cancelled")):
            self.assertEqual(self.run_gate(docs_only, shards), 1, (docs_only, shards))


if __name__ == "__main__":
    unittest.main()
