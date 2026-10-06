"""T713 pre-push 复用测试：verify 通过记录的写入条件、复用条件与拒绝不受影响。

夹具全部为匿名临时 git 仓库（带 origin，守卫规则取自夹具自己的 origin/main）与隔离的事件库
ROOT，不碰真实仓库的库与远端。verify 走真实入口 verify.main（只隔离检查清单、日志目录与 git
位置）；pre-push 走真实入口 git_guard.cmd_pre_push，_run_verify 换成记录调用的桩。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.checks import verify
from engine.checks.verify import ALL_TIERS, Check
from engine.core import events_db
from engine.guards import git_guard

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
ZERO_SHA = "0" * 40
LOCK_TREE = "sha256:" + "a" * 64
# 夹具规则（与守卫事件测试同款匿名值）：build/** 禁止入库，main 为保护分支。
RULES_TOML = r"""
[hygiene]
forbidden = ["build/**", "**/.DS_Store"]
allowed = []
max_file_kb = 1024
secret_patterns = ['ghp_[A-Za-z0-9]{30,}']
home_path_pattern = '(/Users/|/home/|C:\\Users\\)(?!(x|you|me|user|name|example|test|someone)[/\\])[A-Za-z0-9._-]+[/\\]'

[guard]
protected_branches = ["main"]
implementer_protected = []
"""


def write_lock(repo: Path, tree: str) -> None:
    path = repo / ".harness" / "engine.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"engine": "delivery-harness", "version": "0.1.0",
                                "commit": "f" * 40, "tree": tree}) + "\n", encoding="utf-8")


class PrepushReuseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-prepush-reuse-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        config = self.repo / ".harness" / "config"
        config.mkdir(parents=True)
        (config / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        write_lock(self.repo, LOCK_TREE)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        origin = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", ".", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("fetch", "-q", "origin")
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in list(os.environ):
            if key.startswith(("HARNESS_", "GIT_", "GITHUB_")) or key == "CI":
                os.environ.pop(key, None)

    # ---------- 夹具与查询 ----------

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def sha(self, *args: str) -> str:
        return self.git("rev-parse", *args).stdout.strip()

    def records_dir(self) -> Path:
        return (self.repo / self.git("rev-parse", "--git-common-dir").stdout.strip()).resolve() \
            / "harness" / "verify-pass"

    def records(self) -> list[str]:
        return sorted(path.name for path in self.records_dir().glob("*.json")) \
            if self.records_dir().is_dir() else []

    def write_record(self, tree: str, *, engine_tree: str = LOCK_TREE, name: str | None = None,
                     content: str | None = None) -> None:
        directory = self.records_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (name or f"{tree}.json")
        if content is None:
            content = json.dumps({"tree": tree, "engine_tree": engine_tree, "tier": "default",
                                  "checks": {}, "at": "2026-10-05T08:00:00Z"}) + "\n"
        path.write_text(content, encoding="utf-8")

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_verify(self, checks: list[Check]) -> None:
        """隔离 verify 的项目面：检查清单、日志目录与 git 位置都指到临时仓库。"""
        real_git = verify.git
        self.patch(verify, "LOG_DIR", self.repo / "build" / "verify")
        self.patch(verify, "ROOT", self.repo)
        self.patch(verify, "build_checks", lambda strict=False: list(checks))
        self.patch(verify, "git", lambda *args, **kwargs: real_git(*args, **{**kwargs, "cwd": self.repo}))

    def run_verify(self, argv: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return verify.main(argv)

    def stub_run_verify(self) -> list:
        calls: list = []

        def fake_run_verify(repo, *args):
            calls.append((repo, args))
            return 0

        self.patch(git_guard, "_run_verify", fake_run_verify)
        return calls

    def pre_push(self, line: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = git_guard.cmd_pre_push(self.repo, line + "\n")
        return code, err.getvalue()

    # ---------- 验收：通过记录只在干净且全部通过时写入 ----------

    def test_pass_record_written_only_when_clean_pass(self):
        self.install_verify([
            Check("alpha", ALL_TIERS, func=lambda: (True, "正常")),
            Check("beta", ALL_TIERS, func=lambda: (True, "正常")),
        ])
        tree = self.sha("HEAD^{tree}")

        # 全部通过、工作区干净：写记录（tree、engine_tree、tier）。
        self.assertEqual(self.run_verify([]), 0)
        self.assertEqual(self.records(), [f"{tree}.json"])
        record = json.loads((self.records_dir() / f"{tree}.json").read_text(encoding="utf-8"))
        self.assertEqual(record["tree"], tree)
        self.assertEqual(record["engine_tree"], LOCK_TREE)
        self.assertEqual(record["tier"], "default")
        self.assertEqual(record["checks"], {"alpha": "pass", "beta": "pass"})
        self.assertTrue(record["at"])

        # 有失败：不写（先清掉上一条的记录）。
        shutil.rmtree(self.records_dir())
        self.install_verify([Check("alpha", ALL_TIERS, func=lambda: (True, "正常")),
                             Check("beta", ALL_TIERS, func=lambda: (False, "坏了"))])
        self.assertEqual(self.run_verify([]), 1)
        self.assertEqual(self.records(), [])

        # 全部通过但工作区有已跟踪文件的改动：不写。
        self.install_verify([Check("alpha", ALL_TIERS, func=lambda: (True, "正常"))])
        (self.repo / "README.md").write_text("# app 改动\n", encoding="utf-8")
        self.assertEqual(self.run_verify([]), 0)
        self.assertEqual(self.records(), [])
        self.git("checkout", "-q", "--", "README.md")

        # quick 档：不写。
        self.assertEqual(self.run_verify(["--quick"]), 0)
        self.assertEqual(self.records(), [])

        # 锁文件读不到（提交了损坏的锁，保持工作区干净以隔离原因）：不写。
        (self.repo / ".harness" / "engine.lock").write_text("{not json", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "broken lock")
        self.assertEqual(self.run_verify([]), 0)
        self.assertEqual(self.records(), [])

    # ---------- 验收（修订 2）：验证期间代码树/引擎标识/工作区发生变化就不写记录 ----------

    def test_no_record_when_tree_changes_during_verify(self):
        """检查运行中分别发生已跟踪文件改动、提交新 HEAD、引擎锁 tree 变化，都不写通过
        记录；什么都不变时照常写。引擎锁先移出版本控制，使「引擎标识变化」可以单独发生。"""
        self.git("rm", "-q", "--cached", ".harness/engine.lock")
        self.git("commit", "-q", "-m", "untrack lock")  # 锁留在磁盘上但不再入库：改锁不弄脏工作区

        # 什么都不变：照常写。
        self.install_verify([Check("alpha", ALL_TIERS, func=lambda: (True, "正常"))])
        self.assertEqual(self.run_verify([]), 0)
        self.assertEqual(self.records(), [f"{self.sha('HEAD^{tree}')}.json"])
        shutil.rmtree(self.records_dir())

        # 检查运行中改动已跟踪文件：不写。
        def touch_readme():
            (self.repo / "README.md").write_text("# 改动\n", encoding="utf-8")
            return True, "正常"

        self.install_verify([Check("alpha", ALL_TIERS, func=touch_readme),
                             Check("beta", ALL_TIERS, func=lambda: (True, "正常"))])
        self.assertEqual(self.run_verify([]), 0)
        self.assertEqual(self.records(), [])
        self.git("checkout", "-q", "--", "README.md")

        # 检查运行中提交新 HEAD（tree 变化）：旧代码的验证结果不得绑到新 tree。
        def commit_during_check():
            (self.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
            self.git("add", "extra.txt")
            self.git("commit", "-q", "-m", "during verify")
            return True, "正常"

        self.install_verify([Check("alpha", ALL_TIERS, func=commit_during_check),
                             Check("beta", ALL_TIERS, func=lambda: (True, "正常"))])
        self.assertEqual(self.run_verify([]), 0)
        self.assertEqual(self.records(), [])

        # 检查运行中引擎锁 tree 变化（锁未入库：工作区仍干净、HEAD 不变）：不写。
        def change_lock_during_check():
            write_lock(self.repo, "sha256:" + "c" * 64)
            return True, "正常"

        self.install_verify([Check("alpha", ALL_TIERS, func=change_lock_during_check),
                             Check("beta", ALL_TIERS, func=lambda: (True, "正常"))])
        self.assertEqual(self.run_verify([]), 0)
        self.assertEqual(self.records(), [])

    # ---------- 验收：pre-push 只在记录完全匹配时跳过 ----------

    def test_prepush_skips_only_on_matching_record(self):
        self.git("checkout", "-q", "-b", "task/x")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "task")
        calls = self.stub_run_verify()
        local = self.sha("HEAD")
        tree = self.sha("HEAD^{tree}")
        push = f"refs/heads/task/x {local} refs/heads/task/x {ZERO_SHA}\n"

        # tree 有记录、工作区干净、engine_tree 一致：跳过 verify（桩未被调用）。
        self.write_record(tree)
        code, err = self.pre_push(push)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [])
        self.assertIn("同一代码树已通过 verify（default，2026-10-05T08:00:00Z），跳过重跑", err)

        # tree 不同（记录属于另一个 tree）：照旧调用 verify。
        shutil.rmtree(self.records_dir())
        self.write_record(self.sha("main^{tree}"))
        code, err = self.pre_push(push)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)

        # 工作区有已跟踪文件的改动：照旧调用 verify。
        calls.clear()
        self.write_record(tree)
        (self.repo / "README.md").write_text("# app 改动\n", encoding="utf-8")
        code, err = self.pre_push(push)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)
        self.git("checkout", "-q", "--", "README.md")

        # engine_tree 不同：照旧调用 verify。
        calls.clear()
        self.write_record(tree, engine_tree="sha256:" + "b" * 64)
        code, err = self.pre_push(push)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)

        # 记录损坏（同名文件不是合法 JSON）：照旧调用 verify。
        calls.clear()
        self.write_record(tree, content="{not json")
        code, err = self.pre_push(push)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)

        # 没有任何记录：照旧调用 verify。
        calls.clear()
        shutil.rmtree(self.records_dir())
        code, err = self.pre_push(push)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)

    # ---------- 验收：拒绝路径不受复用影响 ----------

    def test_denials_unaffected(self):
        calls = self.stub_run_verify()
        main_sha = self.sha("main")
        # 分叉出两条历史，模拟远端有本地没有的推进：推送不是快进。
        self.git("checkout", "-q", "-b", "task/x")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "task")
        local = self.sha("HEAD")
        self.write_record(self.sha("HEAD^{tree}"))  # 被推的提交有通过记录，拒绝仍必须生效

        code, err = self.pre_push(f"refs/heads/main {main_sha} refs/heads/main {ZERO_SHA}\n")
        self.assertEqual(code, 1)
        self.assertIn("直接推送保护分支 refs/heads/main", err)
        self.assertEqual(calls, [])

        self.git("checkout", "-q", "-b", "other", "main")
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "other")
        remote = self.sha("HEAD")
        code, err = self.pre_push(f"refs/heads/task/x {local} refs/heads/task/x {remote}\n")
        self.assertEqual(code, 1)
        self.assertIn("不是快进", err)
        self.assertEqual(calls, [])

        self.git("checkout", "-q", "task/x")
        self.git("rm", "-q", "docs/note.md")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "junk").write_text("junk\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "junk")
        self.write_record(self.sha("HEAD^{tree}"))
        code, err = self.pre_push(f"refs/heads/task/x {self.sha('HEAD')} refs/heads/task/x {ZERO_SHA}\n")
        self.assertEqual(code, 1)
        self.assertIn("build/junk", err)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
