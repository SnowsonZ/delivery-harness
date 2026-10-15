"""R1开档配置回归：用Git路径匹配核对CODEOWNERS有限排除，不访问GitHub。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PilotCodeownersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='pilot-codeowners-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.repo = Path(cls.temporary.name)
        cls.env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        cls.git = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=',
                   '-c', 'core.ignorecase=false']
        subprocess.run([*cls.git, 'init', '-q', str(cls.repo)], env=cls.env, check=True,
                       capture_output=True)
        attributes = []
        # 当前CODEOWNERS使用根路径、目录和*；目录在gitattributes中须显式展开。
        # 路径匹配交给Git，预期审批范围由下方测试直接给出，不重写通配符匹配器。
        for line in (ROOT / '.github/CODEOWNERS').read_text().splitlines():
            fields = line.strip().split()
            if not fields or fields[0].startswith('#'):
                continue
            pattern, owners = fields[0], fields[1:]
            if pattern.endswith('/'):
                pattern += '**'
            value = 'pilot_owner=required' if owners else '-pilot_owner'
            attributes.append(f'{pattern} {value}')
        (cls.repo / '.gitattributes').write_text('\n'.join(attributes) + '\n')

    def approvals(self, paths):
        result = subprocess.run([*self.git, 'check-attr', '-z', '--stdin', 'pilot_owner'],
                                cwd=self.repo, env=self.env, input=('\0'.join(paths) + '\0').encode(),
                                capture_output=True, check=True)
        fields = result.stdout.decode().split('\0')[:-1]
        self.assertEqual(len(fields), 3 * len(paths))
        return {fields[i]: fields[i + 2] for i in range(0, len(fields), 3)}

    def test_backlog_and_pilot_repro_need_no_owner_review(self):
        paths = ['docs/backlog.md', 'tests/test_pilot_task_20990101_01.py']
        self.assertEqual(self.approvals(paths), {p: 'unset' for p in paths})

    def test_existing_tests_and_control_files_keep_owner_review(self):
        paths = ['tests/test_events_agents.py', '.github/CODEOWNERS',
                 '.github/workflows/pilot-acceptance.yml', 'AGENTS.md', 'requirements-dev.txt',
                 'docs/plans/a.md', 'docs/CHANGELOG.md', 'docs/backlog.md.bak']
        self.assertEqual(self.approvals(paths), {p: 'required' for p in paths})

    def test_reserved_pattern_stays_in_tests_root_and_python_extension(self):
        paths = ['tests/sub/test_pilot_task_x.py', 'tests/test_pilot_task_x.txt',
                 'tests/test_pilot_task_x.py/data.py', 'test_pilot_task_x.py',
                 'tests/test_Pilot_task_x.py']
        self.assertEqual(self.approvals(paths), {p: 'required' for p in paths})

    def test_existing_runtime_exclusions_are_preserved(self):
        paths = ['engine/agents/github.py', 'templates/a/b.py', 'CHANGELOG.md']
        self.assertEqual(self.approvals(paths), {p: 'unset' for p in paths})


if __name__ == '__main__':
    unittest.main()
