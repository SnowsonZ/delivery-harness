"""T715 B104：CI 轮次不计同步合并——同步 App update-branch 产生的双亲提交（其余父提交都在
主线上）不算执行方的轮次；普通合并与「只有部分父提交在主线上」的多亲合并照常计入；取不到
提交、git 出错照常计入；mainline=None 行为与改动前完全一致；同一提交的重跑仍只算一轮。

验收经真实产品入口：is_sync_merge 直接断言，gather 传入 cwd 与主线（默认分支不叫 main，
防止查错仓库）。夹具与 T108/T290 同型：匿名临时 git 仓库（含匿名 bare 远端）、隔离配置与事件。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.core import common, events_db
from engine.routing import policy, run_check

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
RULES_TOML = """\
[risk]
r3 = []
r2 = []
r0 = ["docs/**", "README*.md"]
tests = ["tests/**"]
contracts = []
taskbooks = ["docs/plans/task-*.md"]
shrink_only = []
golden = []

[dispatch]
ci_workflows = ["harness"]
"""
TASK = "T916"
BRANCH = "task/916-rounds"
TASKBOOK = f"""---
task: {TASK}
class: K7
risk: R3
designer: codex
size: small
architecture: false
spec_refs: []
no_spec_reason: 测试夹具
budget:
  wall_clock_min: 10
  ci_rounds: 5
  retries: 1
  tokens: null
rollback: git revert
---

# 任务：夹具（T715 轮次测试）

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖 |
|---|---|---|---|
| 夹具 | 夹具 | 单测 | `tests.test_automerge_rounds` |
"""


class AutomergeRoundsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-automerge-rounds-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "trunk")  # 默认分支不叫 main：查错仓库会直接失败
        (self.repo / ".harness" / "config").mkdir(parents=True)
        (self.repo / ".harness" / "config" / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (self.repo / "docs" / "plans").mkdir(parents=True)
        (self.repo / "docs" / "plans" / "task-916-rounds.md").write_text(TASKBOOK, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "任务书")
        origin = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", ".", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-q", "origin", "trunk")
        self.git("fetch", "-q", "origin")
        for target, name, value in (
            (events_db, "ROOT", self.repo),
            (common, "RULES_PATH", self.repo / ".harness" / "config" / "rules.toml"),
            (common, "CONFIG_DIR", self.repo / ".harness" / "config"),
        ):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)

    # ---------- 夹具 ----------

    def git(self, *args, check=True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def sha(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def commit_file(self, name: str) -> str:
        (self.repo / name).write_text(f"{name}\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"{name}\n\nTask: {TASK}")
        return self.sha()

    def build_history(self) -> dict[str, str]:
        """T0 → A → M1(同步) → B → M2(同步) → N(普通合并) → O(三亲、只有部分父提交在主线)。"""
        t0 = self.sha()
        self.git("checkout", "-q", "-b", BRANCH)
        a = self.commit_file("a.txt")
        self.git("checkout", "-q", "trunk")
        t1 = self.commit_file("t1.txt")
        self.git("push", "-q", "origin", "trunk")
        self.git("checkout", "-q", BRANCH)
        self.git("merge", "-q", "--no-edit", "trunk")  # M1：父 (A, t1)，同步形状
        m1 = self.sha()
        b = self.commit_file("b.txt")
        self.git("checkout", "-q", "trunk")
        t2 = self.commit_file("t2.txt")
        self.git("push", "-q", "origin", "trunk")  # origin/trunk = t2（主线）
        self.git("checkout", "-q", BRANCH)
        self.git("merge", "-q", "--no-edit", "trunk")  # M2：父 (B, t2)，同步形状
        m2 = self.sha()
        self.git("checkout", "-q", "-b", "side", t1)
        s = self.commit_file("s.txt")  # s 不在主线上
        self.git("checkout", "-q", BRANCH)
        self.git("merge", "-q", "--no-edit", "-m", "feature", "side")  # N：父 (M2, s)，普通合并
        n = self.sha()
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        tree = self.git("write-tree").stdout.strip()
        o = subprocess.run(["git", "commit-tree", tree, "-p", n, "-p", t2, "-p", s,
                            "-m", "octo\n\nTask: T916"], cwd=self.repo, capture_output=True,
                           text=True, check=True, env={**env, **GIT_ENV}).stdout.strip()
        return {"t0": t0, "a": a, "t1": t1, "m1": m1, "b": b, "t2": t2, "m2": m2, "s": s, "n": n, "o": o}

    @staticmethod
    def gh_runs(head_shas: list[str]):
        table = {"harness": [{"headSha": sha, "status": "completed", "event": "pull_request"}
                             for sha in head_shas]}

        def gh(*args):
            return json.dumps(table[args[args.index("--workflow") + 1]])

        return gh

    @staticmethod
    def rounds_finding(facts) -> object:
        return next(item for item in facts.run_findings if item.name == "CI 轮次")

    # ---------- 验收第 8 行：经 gather 只计执行方的提交 ----------

    def test_rounds_exclude_sync_merges_via_gather(self):
        shas = self.build_history()
        gh = self.gh_runs([shas[key] for key in ("a", "m1", "b", "m2", "n", "o", "a")])  # 末位是 a 的重跑
        facts = policy.gather("origin/trunk", shas["o"], None, cwd=self.repo,
                              autonomy={"classes": {}, "size": {}}, gh=gh, branch=BRANCH)
        finding = self.rounds_finding(facts)
        self.assertTrue(finding.ok, finding.reason)
        # 同步合并 M1/M2 不计；执行方 A/B 与不在主线上的普通合并 N、三亲合并 O 计入；重跑只算一轮
        self.assertIn("已完成 4 轮，预算 5 轮", finding.reason)

    # ---------- 验收第 7 行：形状判定、取不到提交、默认行为 ----------

    def test_sync_merge_shape_and_unknown_commits(self):
        shas = self.build_history()
        self.assertTrue(run_check.is_sync_merge(shas["m1"], "origin/trunk", cwd=self.repo))
        self.assertTrue(run_check.is_sync_merge(shas["m2"], "origin/trunk", cwd=self.repo))
        self.assertFalse(run_check.is_sync_merge(shas["a"], "origin/trunk", cwd=self.repo))  # 单亲不是合并
        # 其余「任一」父提交不在主线上就不是同步合并（普通合并与三亲合并都照常计入）
        self.assertFalse(run_check.is_sync_merge(shas["n"], "origin/trunk", cwd=self.repo))
        self.assertFalse(run_check.is_sync_merge(shas["o"], "origin/trunk", cwd=self.repo))
        # 提交取不到、git 出错：返回假（照常计入）
        self.assertFalse(run_check.is_sync_merge("0" * 40, "origin/trunk", cwd=self.repo))
        self.assertFalse(run_check.is_sync_merge("", "origin/trunk", cwd=self.repo))

        def broken(*_args, **_kwargs):
            raise RuntimeError("git 失败")

        with mock.patch.object(run_check, "git", broken):
            self.assertFalse(run_check.is_sync_merge(shas["m1"], "origin/trunk", cwd=self.repo))

    def test_branch_rounds_default_and_mainline(self):
        shas = self.build_history()
        gh = self.gh_runs([shas[key] for key in ("a", "m1", "b", "m2", "n", "o", "a")])
        # mainline=None：行为与改动前完全一致（按不同 head 计，重跑不算新一轮）
        self.assertEqual(policy.branch_rounds(BRANCH, gh), 6)
        # 给出主线：同步合并被排除，且用的 cwd 是传入的仓库
        self.assertEqual(policy.branch_rounds(BRANCH, gh, cwd=self.repo, mainline="origin/trunk"), 4)
        # 取不到运行：None（按超预算处理，宁可转人审）
        def broken(*_args):
            raise RuntimeError("gh 失败")

        self.assertIsNone(policy.branch_rounds(BRANCH, broken, cwd=self.repo, mainline="origin/trunk"))


if __name__ == "__main__":
    unittest.main()
