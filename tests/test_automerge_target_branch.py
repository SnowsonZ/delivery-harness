"""auto-merge 只判定指向默认分支的 PR：Target branch 步骤在 policy 之前，非默认分支跳过判定与告警。

执行步骤里的 shell 脚本本身（假 gh 放在 PATH 最前），覆盖事件带 base、事件不带 base 时按头分支查询、
查询为空时按原行为继续三类情形；另核对实例与模板逐字相同。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_ci_events_workflows import parse_workflow

ENGINE_REPO = Path(__file__).resolve().parents[1]
TEMPLATE = ENGINE_REPO / "templates" / ".github" / "workflows" / "auto-merge.yml"
INSTANCE = ENGINE_REPO / ".github" / "workflows" / "auto-merge.yml"


class TargetBranchStepTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = parse_workflow(TEMPLATE.read_text(encoding="utf-8"))
        cls.judge = cls.workflow["jobs"]["judge"]
        cls.names = [step.get("name") for step in cls.judge["steps"]]

    def test_target_step_gates_policy(self):
        target = self.names.index("Target branch")
        policy = self.names.index("Merge policy of the evaluated head (default branch's rules)")
        self.assertLess(target, policy)
        self.assertEqual(self.judge["steps"][policy]["if"], "steps.target.outputs.eligible == 'true'")
        self.assertEqual(self.judge["outputs"]["eligible"], "${{ steps.target.outputs.eligible }}")

    def test_alert_skips_non_default_target(self):
        alert_step = self.workflow["jobs"]["alert"]["steps"][-1]
        self.assertEqual(alert_step["env"]["ELIGIBLE"], "${{ needs.judge.outputs.eligible }}")
        self.assertIn('if [ "$ELIGIBLE" = "false" ]', alert_step["run"])

    def test_instance_matches_template(self):
        if INSTANCE.exists():
            self.assertEqual(INSTANCE.read_text(encoding="utf-8"), TEMPLATE.read_text(encoding="utf-8"))

    def _run(self, pr_base: str, gh_answer: str | None) -> tuple[str, str]:
        script = self.judge["steps"][self.names.index("Target branch")]["run"]
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            calls = Path(tmp) / "calls"
            gh = bin_dir / "gh"
            body = f"printf '%s\\n' \"$*\" >> {calls}\n"
            body += f"printf '%s' '{gh_answer}'\n" if gh_answer is not None else "exit 1\n"
            gh.write_text("#!/bin/sh\n" + body, encoding="utf-8")
            gh.chmod(0o755)
            output = Path(tmp) / "output"
            output.touch()
            env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output),
                   "GITHUB_REPOSITORY": "owner/repo", "GITHUB_REPOSITORY_OWNER": "owner",
                   "PR_BASE": pr_base, "HEAD_BRANCH": "pilot/task-1", "DEFAULT_BRANCH": "main", "GH_TOKEN": "x"}
            done = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=True)
            called = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return output.read_text(encoding="utf-8").strip(), done.stdout + called

    def test_event_base_is_default(self):
        self.assertEqual(self._run("main", None)[0], "eligible=true")

    def test_event_base_is_other_branch(self):
        out, log = self._run("pilot", None)
        self.assertEqual(out, "eligible=false")
        self.assertIn("pilot", log)

    def test_missing_event_base_looks_up_open_prs(self):
        out, log = self._run("", "pilot")
        self.assertEqual(out, "eligible=false")
        self.assertIn("pulls?state=open&head=owner:pilot/task-1", log)
        self.assertEqual(self._run("", "main")[0], "eligible=true")

    def test_lookup_failure_keeps_previous_behaviour(self):
        self.assertEqual(self._run("", None)[0], "eligible=true")
        self.assertEqual(self._run("", "")[0], "eligible=true")


if __name__ == "__main__":
    unittest.main()
