"""安装到全新仓库的集成测试：锁文件与完整性、模板不覆盖、缺配置明确报错、守卫生效、verify 快速档通过。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE_REPO = Path(__file__).resolve().parents[1]
CLI = ENGINE_REPO / "engine" / "cli.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
# 本方身份词：引擎与模板里出现任何一个都说明又把使用者自己的值写死了。
OWN_VALUES = ("Snowson", "snowsonz", "glm-", "zai-coding", "gpt-6", "iterm-probe", "Agent-Notification",
              "build_inbox_app", "session-manager")


def env() -> dict[str, str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and not k.startswith("HARNESS_")}
    return {**clean, **GIT_ENV, "PYTHONDONTWRITEBYTECODE": "1"}


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.assertEqual(self.cli_install().returncode, 0)

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, env=env(), check=check)

    def cli_install(self, command="install"):
        return subprocess.run([sys.executable, str(CLI), command, "--target", str(self.repo), "--allow-dirty"],
                              capture_output=True, text=True, env=env(), check=False)

    def harness(self, *args, stdin=None):
        return subprocess.run([str(self.repo / "bin" / "harness"), *args], cwd=self.repo, input=stdin,
                              capture_output=True, text=True, env=env(), check=False)

    def set_identity(self):
        checks = self.repo / ".harness/config/checks.toml"
        checks.write_text(checks.read_text().replace('# agent_login = ""', 'agent_login = "example-bot"'))

    def test_lock_matches_and_tampering_is_caught(self):
        lock = json.loads((self.repo / ".harness/engine.lock").read_text())
        self.assertEqual(lock["engine"], "delivery-harness")
        self.assertTrue(lock["tree"].startswith("sha256:"))
        self.assertEqual(self.harness("integrity").returncode, 0)
        target = self.repo / ".harness/engine/guards/command_guard.py"
        target.write_text(target.read_text() + "\n# 本地改动\n")
        result = self.harness("integrity")
        self.assertEqual(result.returncode, 1)
        self.assertIn("不一致", result.stdout)
        self.assertEqual(self.cli_install("upgrade").returncode, 0)  # 升级整体替换，恢复一致
        self.assertEqual(self.harness("integrity").returncode, 0)

    def test_templates_never_overwrite_existing_files(self):
        readme_hooks = self.repo / ".claude/settings.json"
        readme_hooks.write_text('{"mine": true}\n')
        result = self.cli_install()
        self.assertIn("= .claude/settings.json", result.stdout)
        self.assertEqual(readme_hooks.read_text(), '{"mine": true}\n')

    def test_missing_identity_is_an_explicit_error(self):
        result = self.harness("identity")
        self.assertEqual(result.returncode, 1)
        self.assertIn("[identity] agent_login", result.stderr)
        self.set_identity()
        result = self.harness("identity")
        self.assertEqual(result.stdout.strip(), "example-bot example-bot@users.noreply.github.com")

    def test_guards_are_live_in_the_new_repo(self):
        denied = self.harness("guard-command", "--format", "json", "--role", "implementer",
                              stdin=json.dumps({"file_path": ".harness/config/rules.toml"}))
        self.assertEqual(denied.returncode, 2)
        allowed = self.harness("guard-command", "--format", "json", "--role", "implementer",
                               stdin=json.dumps({"file_path": "src/app.py"}))
        self.assertEqual(allowed.returncode, 0)
        self.assertEqual(self.harness("guard-git", "install").returncode, 0)
        (self.repo / "x.txt").write_text("x\n")
        self.git("add", "x.txt")
        commit = self.git("commit", "-q", "-m", "on main", check=False)
        self.assertNotEqual(commit.returncode, 0)
        self.assertIn("保护分支", commit.stderr)

    def test_quick_verify_passes_on_a_fresh_repo(self):
        self.set_identity()
        self.assertEqual(self.harness("guard-git", "install").returncode, 0)
        result = self.harness("verify", "--quick")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("integrity", result.stdout)


class OwnValuesTest(unittest.TestCase):
    def test_engine_and_templates_carry_no_user_specific_values(self):
        hits = []
        for folder in ("engine", "templates"):
            for path in (ENGINE_REPO / folder).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    hits += [f"{path.relative_to(ENGINE_REPO)}: {word}" for word in OWN_VALUES if word in text]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
