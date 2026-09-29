"""守卫拒绝埋点测试（T104）：命令守卫与 git 守卫在拒绝报告边界写 guard 事件。

全部用匿名临时 git 仓库与隔离的事件库 ROOT（子进程内 mock），守卫规则取自临时仓库自己的
origin/main，不碰真实仓库的库、远端与工作流。守卫经真实入口（main）以子进程运行，核对
stderr 文本、退出码与库中的事件；放行与写入失败场景核对判定逐字不变（设计 3.6、合同 C0）。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.core import events_db

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
EVENT_COLUMNS = ["id", "ts", "source", "trace_id", "seq", "prev_hash", "hash", "stage", "step", "status",
                 "duration_ms", "actor_role", "actor_host", "model", "decision_by", "decision_rule",
                 "decision_reason", "error_kind", "error_signature", "outputs", "engine_version", "redacted"]
COMMAND_GUARD = "engine.guards.command_guard"
GIT_GUARD = "engine.guards.git_guard"
ZERO_SHA = "0" * 40
WRITE_FAILURE_NOTICE = "harness：事件写入失败，已跳过（不影响本次运行）\n"
# 夹具规则：守卫按临时仓库 origin/main 上的这份执行（评审 PR7-R3），全部为匿名值。raw 字符串保持正则里的反斜杠。
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


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-guard-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        config = self.repo / ".harness" / "config"
        config.mkdir(parents=True)
        (config / "rules.toml").write_text(RULES_TOML, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        # 匿名远端：trusted_rules 以 origin/main 上的规则为准；本地与远端一致，不产生不一致提示。
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
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def guard(self, module: str, argv: list[str], stdin: str = "",
              env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """在隔离 ROOT 的子进程里运行守卫真实入口（main），返回 CompletedProcess。"""
        script = "\n".join([
            "import runpy, sys",
            "from pathlib import Path",
            "from unittest import mock",
            "from engine.core import common, events_db",
            f"repo = Path({str(self.repo)!r})",
            "with mock.patch.object(events_db, 'ROOT', repo), \\",
            "        mock.patch.object(common, 'RULES_PATH', repo / '.harness' / 'config' / 'rules.toml'):",
            f"    sys.argv = [{(module.rpartition('.')[2] + '.py')!r}, *{argv!r}]",
            f"    runpy.run_module({module!r}, run_name='__main__')",
        ])
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_", "GITHUB_")) and key != "CI"}
        if env_extra:
            env.update(env_extra)
        return subprocess.run([sys.executable, "-c", script], input=stdin, cwd=self.repo, timeout=120,
                              capture_output=True, text=True, check=False,
                              env={**env, **GIT_ENV, "PYTHONPATH": str(ENGINE_REPO)})

    def events_rows(self) -> list[dict]:
        path = events_db.db_path()
        if path is None or not path.exists():
            return []
        with contextlib.closing(sqlite3.connect(path)) as conn:
            rows = conn.execute(f"SELECT {','.join(EVENT_COLUMNS)} FROM events ORDER BY id").fetchall()
        return [dict(zip(EVENT_COLUMNS, row)) for row in rows]

    def stored_text(self) -> str:
        """库里 events 与 refs 两表的全部文本，用于扫描禁项（命令、理由正文、绝对路径）。"""
        path = events_db.db_path()
        if path is None or not path.exists():
            return ""
        with contextlib.closing(sqlite3.connect(path)) as conn:
            cells = conn.execute("SELECT * FROM events").fetchall() + conn.execute("SELECT * FROM refs").fetchall()
        return "\n".join(str(value) for row in cells for value in row)

    def test_command_denial_metadata_without_payload(self):
        """命令与编辑拒绝各含规则键、角色、类别和安全相对目标；库里无命令全文、理由正文与绝对路径。"""
        result = self.guard(COMMAND_GUARD, ["--format", "claude", "--role", "implementer"],
                            stdin='{"tool_name": "Bash", "tool_input": {"command": "git push --force origin feature-x"}}')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("harness 守卫拒绝了这次操作：", result.stderr)
        self.assertIn("强制推送：已推送的历史不改写", result.stderr)  # 理由照旧写 stderr
        rows = self.events_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["stage"], row["step"], row["status"]), ("guard", "command", "deny"))
        self.assertEqual((row["decision_by"], row["decision_rule"]), ("guard", "force_push"))
        self.assertEqual(row["decision_reason"], None)  # 理由文本不进库
        self.assertEqual(row["actor_role"], "implementer")
        self.assertEqual(row["source"], "local")
        self.assertEqual(row["trace_id"], "main@" + self.git("rev-parse", "--short=7", "HEAD").stdout.strip())
        self.assertEqual(json.loads(row["outputs"]), {"category": "command"})  # 命令拒绝没有目标，也不复制命令

        edit_payload = {"tool_name": "Write", "tool_input": {"file_path": str(self.repo / ".github" / "workflows" / "ci.yml")}}
        result = self.guard(COMMAND_GUARD, ["--format", "claude", "--role", "implementer"], stdin=json.dumps(edit_payload))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("执行者不能编辑判定器与护栏", result.stderr)
        rows = self.events_rows()
        self.assertEqual(len(rows), 2)
        row = rows[1]
        self.assertEqual((row["stage"], row["step"], row["status"]), ("guard", "command", "deny"))
        self.assertEqual((row["decision_by"], row["decision_rule"]), ("guard", "edit_protected"))
        self.assertEqual(row["actor_role"], "implementer")
        self.assertEqual(json.loads(row["outputs"]), {"category": "edit", "target": ".github/workflows/ci.yml"})

        text = self.stored_text()
        self.assertNotIn("git push", text)
        self.assertNotIn("强制推送", text)
        self.assertNotIn("执行者不能编辑", text)
        self.assertNotIn(str(self.tmp), text)  # 载荷里的绝对路径不进库
        self.assertEqual(events_db.verify(), [])

    def test_all_git_hooks_record_denial(self):
        """三类 git 钩子的拒绝夹具逐个核对 hook、分支、规则与 deny 状态；stderr 文本与退出码照旧。"""
        result = self.guard(GIT_GUARD, ["pre-commit"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("harness：提交被拒绝：", result.stderr)
        self.assertIn("在保护分支 main 上提交：请在功能分支上工作", result.stderr)
        rows = self.events_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["stage"], rows[0]["step"], rows[0]["status"]), ("guard", "git", "deny"))
        self.assertEqual((rows[0]["decision_by"], rows[0]["decision_rule"]), ("guard", "commit_protected_branch"))
        self.assertEqual(json.loads(rows[0]["outputs"]), {"hook": "pre-commit", "branch": "main"})

        self.git("checkout", "-q", "-b", "task/x")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "note")
        local = self.git("rev-parse", "HEAD").stdout.strip()
        remote_main = self.git("rev-parse", "main").stdout.strip()
        result = self.guard(GIT_GUARD, ["pre-push"], stdin=f"refs/heads/task/x {local} refs/heads/main {remote_main}\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("harness：推送被拒绝：", result.stderr)
        self.assertIn("直接推送保护分支 refs/heads/main", result.stderr)
        rows = self.events_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]["stage"], rows[1]["step"], rows[1]["status"]), ("guard", "git", "deny"))
        self.assertEqual((rows[1]["decision_by"], rows[1]["decision_rule"]), ("guard", "push_protected_branch"))
        self.assertEqual(json.loads(rows[1]["outputs"]), {"hook": "pre-push", "branch": "refs/heads/main"})

        self.git("checkout", "-q", "main")
        self.git("tag", "v1")
        tag_sha = self.git("rev-parse", "v1").stdout.strip()
        result = self.guard(GIT_GUARD, ["reference-transaction", "prepared"],
                            stdin=f"{tag_sha} {ZERO_SHA} refs/tags/v1\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("harness：引用更新被拒绝：", result.stderr)
        self.assertIn("删除已有 tag refs/tags/v1", result.stderr)
        rows = self.events_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[2]["stage"], rows[2]["step"], rows[2]["status"]), ("guard", "git", "deny"))
        self.assertEqual((rows[2]["decision_by"], rows[2]["decision_rule"]), ("guard", "tag_rewrite"))
        self.assertEqual(json.loads(rows[2]["outputs"]), {"hook": "reference-transaction", "branch": "refs/tags/v1"})
        self.assertEqual(events_db.verify(), [])

    def test_allow_does_not_emit(self):
        """设计方与执行方的放行都不逐条记事件、不写计数（设计 3.6），放行路径完全安静。"""
        cases = [
            (COMMAND_GUARD, ["--format", "json", "--role", "designer"], '{"command": "git status --short"}', 0),
            (COMMAND_GUARD, ["--format", "claude", "--role", "implementer"],
             json.dumps({"file_path": str(self.repo / "docs" / "note.md")}), 0),
            (COMMAND_GUARD, ["--format", "claude", "--role", "implementer"], '{"tool_name": "web_search"}', 0),
        ]
        for module, argv, stdin, expected in cases:
            result = self.guard(module, argv, stdin=stdin)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(result.stderr, "")
        # git 守卫放行：功能分支上的提交、新建远端分支的推送与引用创建。
        self.git("checkout", "-q", "-b", "task/x")
        self.assertEqual(self.guard(GIT_GUARD, ["pre-commit"]).returncode, 0)
        local = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.guard(GIT_GUARD, ["pre-push"], stdin=f"refs/heads/task/x {local} refs/heads/task/x {ZERO_SHA}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        result = self.guard(GIT_GUARD, ["reference-transaction", "prepared"],
                            stdin=f"{ZERO_SHA} {local} refs/heads/task/x\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.events_rows(), [])

    def test_sink_failure_preserves_denial(self):
        """事件关闭与写入失败时，同一拒绝的 JSON/文本输出与退出码不变；失败只追加 T101 的固定提示一次。"""
        payload = '{"tool_name": "Bash", "tool_input": {"command": "git push --force origin feature-x"}}'
        argv = ["--format", "claude", "--role", "implementer"]
        command_baseline = self.guard(COMMAND_GUARD, argv, stdin=payload)
        self.assertEqual(command_baseline.returncode, 2, command_baseline.stderr)
        git_baseline = self.guard(GIT_GUARD, ["pre-commit"])
        self.assertEqual(git_baseline.returncode, 1, git_baseline.stderr)
        self.assertEqual(len(self.events_rows()), 2)

        command_off = self.guard(COMMAND_GUARD, argv, stdin=payload, env_extra={"HARNESS_EVENTS": "off"})
        self.assertEqual(command_off.returncode, 2, command_off.stderr)
        self.assertEqual(command_off.stderr, command_baseline.stderr)  # 关闭时无额外提示
        git_off = self.guard(GIT_GUARD, ["pre-commit"], env_extra={"HARNESS_EVENTS": "off"})
        self.assertEqual(git_off.returncode, 1, git_off.stderr)
        self.assertEqual(git_off.stderr, git_baseline.stderr)
        self.assertEqual(len(self.events_rows()), 2)  # 关闭期间不新增事件

        saved = self.db_path.read_bytes()  # 备份原库，事后核验失败写入没有碰它
        self.db_path.write_bytes(b"this is not a sqlite database at all")  # 库文件损坏：写入失败
        command_broken = self.guard(COMMAND_GUARD, argv, stdin=payload)
        self.assertEqual(command_broken.returncode, 2, command_broken.stderr)
        self.assertEqual(command_broken.stderr, command_baseline.stderr + WRITE_FAILURE_NOTICE)
        git_broken = self.guard(GIT_GUARD, ["pre-commit"])
        self.assertEqual(git_broken.returncode, 1, git_broken.stderr)
        self.assertEqual(git_broken.stderr, git_baseline.stderr + WRITE_FAILURE_NOTICE)
        self.db_path.write_bytes(saved)
        self.assertEqual(len(self.events_rows()), 2)  # 失败的写入不留半条事件，原事件原样保留
        self.assertEqual(events_db.verify(), [])


if __name__ == "__main__":
    unittest.main()
