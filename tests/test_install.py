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

    def test_ci_templates_and_rulesets_agree(self):
        """工作流、ruleset 是一套：ruleset 要求的状态检查就是 harness 工作流的 job，auto-merge 监听同名工作流。"""
        github = self.repo / ".github"
        harness = (github / "workflows/harness.yml").read_text()
        self.assertTrue(harness.startswith("name: harness\n"))
        self.assertRegex(harness, r"\n  harness:\n")
        self.assertIn("workflows: [harness]", (github / "workflows/auto-merge.yml").read_text())
        for name, approvals in (("main.json", 1), ("main-single-account.json", 0)):
            ruleset = json.loads((github / "rulesets" / name).read_text())
            rules = {rule["type"]: rule.get("parameters", {}) for rule in ruleset["rules"]}
            contexts = [c["context"] for c in rules["required_status_checks"]["required_status_checks"]]
            self.assertEqual(contexts, ["harness"], name)
            self.assertEqual(rules["pull_request"]["required_approving_review_count"], approvals, name)
            self.assertIn("non_fast_forward", rules)
            self.assertEqual(ruleset["bypass_actors"], [])

    def policy_outputs(self):
        out = self.tmp / "github-output"
        out.unlink(missing_ok=True)
        result = subprocess.run([str(self.repo / "bin" / "harness"), "policy", "--base", "HEAD", "--head", "HEAD", "--github"],
                                cwd=self.repo, capture_output=True, text=True, check=False,
                                env={**env(), "GITHUB_OUTPUT": str(out)})
        return result, dict(line.split("=", 1) for line in out.read_text().splitlines()) if out.exists() else {}

    def set_platform(self, text):
        checks = self.repo / ".harness/config/checks.toml"
        checks.write_text(checks.read_text().replace("[platform]\n", "[platform]\n" + text, 1))

    def test_platform_defaults_and_overrides_reach_the_workflow_outputs(self):
        # 默认：两个账号 + App，变量名是通用默认值，不含任何使用者自己的名字。
        _, out = self.policy_outputs()
        self.assertEqual((out["approval"], out["app_client_id_var"], out["environment"]),
                         ("app", "HARNESS_APP_CLIENT_ID", "harness-auto-merge"))
        self.assertEqual(out["app_private_key_secret"], "HARNESS_APP_PRIVATE_KEY")
        self.set_platform('approval = "none"\napp_client_id_var = "MY_APP_ID"\nenvironment = "merge-env"\n')
        _, out = self.policy_outputs()
        self.assertEqual((out["approval"], out["app_client_id_var"], out["environment"]), ("none", "MY_APP_ID", "merge-env"))

    def test_unknown_approval_mode_is_an_explicit_error(self):
        self.set_platform('approval = "maybe"\n')
        result, out = self.policy_outputs()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("approval", result.stderr + result.stdout)
        self.assertNotIn("approval", out)

    def test_project_without_tests_dir_is_not_a_base_tests_failure(self):
        self.git("checkout", "-q", "-b", "feat")
        (self.repo / "app.py").write_text("x = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "app")
        result = self.harness("base-tests", "--base", "main")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("无已有测试可回放", result.stdout)

    def test_metrics_baseline_is_only_shown_when_the_project_registers_one(self):
        result = self.harness("metrics", "--base", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("基线", result.stdout)
        checks = self.repo / ".harness/config/checks.toml"
        checks.write_text(checks.read_text() + '\n[metrics.baseline]\nlabel = "v1"\nvalues = { "评审轮次" = 2 }\n')
        self.assertIn("v1 基线：评审轮次 2。", self.harness("metrics", "--base", "HEAD").stdout)


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
