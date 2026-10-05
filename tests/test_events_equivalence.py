"""T106 事件三态判定等价测试（B46）：事件开启、关闭与写入失败三种状态下判定逐字相同。

设计 2.5「事件不进任何判定」与共用合同 C0 的验收：CLI 全部判定入口（verify、integrity、hygiene、
acceptance、taskbook、docs、base-tests、evidence、r1、replay、mutate、quality、release-check、
risk、policy、run-check、guard-command 与 git 守卫三钩子）在完全相同的输入上比较 stdout、
业务 stderr、退出码与结构化结果。三态分别证实：开启态写入事件（含 cli.<命令> 入口事件与各
埋点 step）、`HARNESS_EVENTS=off` 关闭态零事件、失败态真实进入写入点（.git/harness 被换成
普通文件，SQLite 写入前的建目录抛 OSError，T101 的固定提示恰好出现一次且库目录不被重建）。

夹具是安装布局的匿名临时仓库：.harness/engine/ 为引擎工作区副本并配好 engine.lock，全部命令经
该副本的 cli.py 运行（与真实安装一致，不注入路径、不 mock 判定与 emit）；守卫规则取夹具自己的
origin/main；外部工具与平台读取用确定性夹具（假 gh、GITHUB_OUTPUT/GITHUB_STEP_SUMMARY）。
环境变量只通过测试 Python 传给子进程的 env 设置。integrity 的 skip 状态只在「引擎仓库自身」
形态出现（安装布局下核对对象恒存在），不属于本夹具的适用输入，其事件元数据由 T102 覆盖。

逐字比较之外仅按白名单剔除三处（其余任何差异都算失败，不允许任意正则吃差异）：
1. T101 固定的一行写入失败提示——先断言每进程至多一次，再整行剔除；
2. verify 每项检查结论行里的真实耗时字段（verify.py 的 `{seconds:5.1f}s`）；
3. build/verify/summary.json 里 results[].seconds——两处均另断言是非负数值。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import __version__ as ENGINE_VERSION
from engine.checks import integrity

ENGINE_REPO = Path(__file__).resolve().parents[1]
WARN = "harness：事件写入失败，已跳过（不影响本次运行）"
ZERO_SHA = "0" * 40
SUMMARY_REL = "build/verify/summary.json"
OFF_ENV = {"HARNESS_EVENTS": "off"}
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}
# verify 检查结论行的耗时字段（`✓ tools  0.1s` 的 `\d+\.\d+s`），白名单剔除只针对这一处。
DURATION_LINE = re.compile(r"^([✓✗-] \S+ +)(\d+\.\d+)s", re.MULTILINE)

# 夹具配置：全部匿名值，且覆盖各判定入口读到的节。
RULES_TOML = """\
[hygiene]
forbidden = ["build/**", "**/.DS_Store"]
allowed = []
max_file_kb = 1024
secret_patterns = ['XSECRETKEY-[A-Za-z0-9]{20,}']
home_path_pattern = '/Users/(?!placeholder/)'

[risk]
r3 = ["critical/**"]
r2 = ["src/**", "app.py"]
r0 = ["docs/**", "README*.md"]
tests = ["tests/**"]
contracts = []
taskbooks = ["docs/plans/task-*.md"]
shrink_only = []
golden = []

[taskbook]
guard_paths = ["engine/**"]
architecture_paths = []

[taskbook.modules]

[guard]
protected_branches = ["main"]
implementer_protected = []
"""
CHECKS_TOML = """\
[sources]
python_dirs = ["src"]
python_glob = "*.py"
code = ["src/**", "app.py"]
ui = []

[[mutation.targets]]
name = "sign"
file = "src/calc.py"
functions = ["sign"]
tests = ["test_calc.CalcTest"]

[[mutation.targets]]
name = "broken"
file = "src/calc.py"
functions = ["sign"]
tests = ["test_calc.MissingTest"]

[release]
version_file = "version.py"
"""
AUTONOMY_TOML = """\
[size]
max_lines = 400
exclude = []

[classes.K1]
name = "说明与记录"
level = "L4"
window = 20
max_escapes = 0
audit_every = 1
"""
TASKBOOK_TEMPLATE = """\
---
task: {task}
class: K1
risk: R0
designer: codex
size: small
architecture: false
spec_refs: []
no_spec_reason: 测试夹具
budget:
  wall_clock_min: 10
  ci_rounds: {ci_rounds}
  retries: 1
  tokens: null
rollback: git revert
---

## 目标终态

夹具任务书。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| {acceptance} | 夹具 | 人工 | 人工核对 | 断言失败 |
"""
GITIGNORE = "build/\n"
VERSION_PY = 'VERSION = "1.2.3"\nBUILD = "5"\n'
README = "# app\n"
APP_PY = "def flag():\n    return False\n"
CALC_PY = "def sign(v):\n    return 1 if v > 0 else -1\n"
TEST_CALC_PY = """\
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from calc import sign


class CalcTest(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(sign(5), 1)

    def test_one(self):
        self.assertEqual(sign(1), 1)

    def test_zero(self):
        self.assertEqual(sign(0), -1)

    def test_negative(self):
        self.assertEqual(sign(-3), -1)
"""
TEST_APP_BASE = """\
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class AppTest(unittest.TestCase):
    def test_base(self):
        self.assertFalse(app.flag())
"""
TEST_DEMO_PY = """\
import unittest


class AppTest(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""
SPEC_MD = """\
# 夹具规格

| 编号 | 验收内容 | 证据类型 | 覆盖 |
|---|---|---|---|
| U4 | 夹具条目 | 单测 | `test_demo.AppTest.test_ok` |
"""
REPLAY_EMPTY = "BASELINE = []\nCASES = []\nGUARDED = {}\nDEFERRED = {}\n"
# 假 gh：labels/escapes/CI 运行按固定形状应答，其余调用显式失败（确定性夹具，不做真实网络读写）。
GH_ALLOW = """\
#!/bin/sh
case "$*" in
  *"pr view"*--json*labels*) echo '{"labels": []}';;
  *"pr list"*) echo '[]';;
  *"issue list"*) echo '[]';;
  *"run list"*) echo '[]';;
  *) echo "夹具 gh：意外的调用 $*" >&2; exit 1;;
esac
"""
GH_DOWN = "#!/bin/sh\necho '夹具 gh：不可用' >&2\nexit 1\n"
# verify 的恒败项目检查（不经引擎命令，避免子进程的提示进入结论尾部）。
DEMO_CHECK = """

[[verify.checks]]
name = "demo"
tiers = ["default", "full"]
command = ["{python}", "-c", "import sys; print('demo 失败'); sys.exit(3)"]
"""


def taskbook_text(task: str, *, ci_rounds: int = 1, acceptance: str = "不挂规格：B46 夹具") -> str:
    return TASKBOOK_TEMPLATE.format(task=task, ci_rounds=ci_rounds, acceptance=acceptance)


class ObservabilityTaskTest(unittest.TestCase):
    """验收见 docs/plans/task-106-events-equivalence.md；夹具与比较器约定见模块 docstring。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-equivalence-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        for rel in (
            ".harness/config",
            ".harness/state",
            ".harness/project",
            "docs/plans",
            "docs/specs",
            "src",
            "tests",
        ):
            (self.repo / rel).mkdir(parents=True)
        (self.repo / ".harness/config/rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (self.repo / ".harness/config/checks.toml").write_text(CHECKS_TOML, encoding="utf-8")
        (self.repo / ".harness/config/autonomy.toml").write_text(AUTONOMY_TOML, encoding="utf-8")
        (self.repo / ".harness/project/replay_cases.py").write_text(REPLAY_EMPTY, encoding="utf-8")
        (self.repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        (self.repo / "README.md").write_text(README, encoding="utf-8")
        (self.repo / "app.py").write_text(APP_PY, encoding="utf-8")
        (self.repo / "src/calc.py").write_text(CALC_PY, encoding="utf-8")
        (self.repo / "version.py").write_text(VERSION_PY, encoding="utf-8")
        (self.repo / "tests/test_calc.py").write_text(TEST_CALC_PY, encoding="utf-8")
        (self.repo / "tests/test_app.py").write_text(TEST_APP_BASE, encoding="utf-8")
        (self.repo / "tests/test_demo.py").write_text(TEST_DEMO_PY, encoding="utf-8")
        (self.repo / "docs/specs/spec.md").write_text(SPEC_MD, encoding="utf-8")
        (self.repo / "docs/plans/task-901-legal.md").write_text(taskbook_text("T901"), encoding="utf-8")
        # 安装布局：引擎副本 + 配套锁文件（integrity 的真实核对对象；其余命令自然以夹具为根）。
        engine_dir = self.repo / ".harness/engine"
        shutil.copytree(ENGINE_REPO / "engine", engine_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        lock = {
            "engine": "delivery-harness",
            "version": ENGINE_VERSION,
            "commit": "fixture",
            "tree": integrity.tree_hash(engine_dir),
        }
        (self.repo / ".harness/engine.lock").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        self.cli = engine_dir / "cli.py"
        self.git("init", "-q", "-b", "main")
        # verify 的 tools 检查要求 git 守卫已安装；夹具只写配置（.githooks 目录不存在时 git 静默跳过钩子）。
        config = self.repo / ".git/config"
        config.write_text(config.read_text(encoding="utf-8") + "[core]\n\thooksPath = .githooks\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.base = self.head_sha()
        # 守卫规则以 origin/main 为准（trusted_rules）：匿名远端与本地一致。
        origin = self.tmp / "origin.git"
        # --no-hardlinks：夹具 origin 不需要硬链接提速，绕开 macOS runner 上偶发的
        # "hardlink different from source"（B73 系，#103 实例）。
        self.git("clone", "-q", "--bare", "--no-hardlinks", ".", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("fetch", "-q", "origin")
        self.db_path = self.repo / ".git/harness/harness.db"

    # ---------- 夹具操作 ----------

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "HARNESS_"))}
        proc = subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, env={**env, **GIT_ENV}, check=False
        )
        if check and proc.returncode:
            self.fail(f"git {' '.join(args)} 失败：{proc.stderr}")
        return proc

    def commit(self, files: dict[str, str], message: str) -> str:
        for rel, content in files.items():
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A", "--", *files)
        self.git("commit", "-q", "-m", message)
        return self.head_sha()

    def head_sha(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def child_env(self, **extra: str) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "GITHUB_", "HARNESS_")) and key != "CI"
        }
        env.update(GIT_ENV)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(extra)
        return env

    def run_cli(
        self,
        argv: list[str],
        *,
        stdin: str = "",
        structured: tuple[str, ...] = (),
        env_extra: dict[str, str] | None = None,
        reset_verify_pass: bool = False,
    ) -> dict:
        if reset_verify_pass:
            # T713 修订 1：pre-push 会复用同一代码树的 verify 通过记录；三态比较要求每态都真正跑 verify，
            # 所以由需要该语义的调用点显式要求先清空夹具仓库的通过记录目录（git 公共目录下的
            # harness/verify-pass），而不是在这里按 argv 嗅探。
            shutil.rmtree(self.repo / ".git" / "harness" / "verify-pass", ignore_errors=True)
        for rel in structured:
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
        proc = subprocess.run(
            [sys.executable, str(self.cli), *argv],
            cwd=self.repo,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            env=self.child_env(**(env_extra or {})),
        )
        result = {"code": proc.returncode, "out": proc.stdout, "err": proc.stderr}
        if structured:
            result["structured"] = {rel: (self.repo / rel).read_bytes() for rel in structured}
        return result

    # ---------- 事件库与失败写入点 ----------

    def db_query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.db_path.exists():
            return []
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def max_event_id(self) -> int:
        rows = self.db_query("SELECT COALESCE(MAX(id), 0) FROM events")
        return rows[0][0] if rows else 0

    def events_after(self, min_id: int) -> list[dict]:
        rows = self.db_query("SELECT step, status, duration_ms FROM events WHERE id>? ORDER BY id", (min_id,))
        return [{"step": row[0], "status": row[1], "duration_ms": row[2]} for row in rows]

    def sink_break(self) -> None:
        """库目录换成普通文件：SQLite 写入前的建目录抛 FileExistsError/NotADirectoryError（OSError）。"""
        harness = self.repo / ".git/harness"
        if harness.is_dir():
            shutil.rmtree(harness)
        harness.write_text("harness sink broken", encoding="utf-8")

    def sink_restore(self) -> None:
        harness = self.repo / ".git/harness"
        if harness.is_file():
            harness.unlink()

    # ---------- 白名单比较器 ----------

    def normalize(self, result: dict, spots: tuple[str, ...] = ()) -> dict:
        """剔除位置：T101 固定提示（至多一次，整行剔除）；verify 结论行耗时与 summary.json 的 seconds
        （另断言非负数值）。stdout 其余部分、其余 stderr 行、退出码与结构化结果保持逐字比较。"""
        lines = result["err"].splitlines()
        warns = [line for line in lines if line == WARN]
        self.assertLessEqual(len(warns), 1, f"写入失败提示每进程最多一次：{result['err']!r}")
        durations: list[float] = []
        out = result["out"]
        if "verify" in spots:

            def replace(match: re.Match[str]) -> str:
                durations.append(float(match.group(2)))
                return f"{match.group(1)}<耗时>s"

            out = DURATION_LINE.sub(replace, out)
        structured = {}
        for rel, data in (result.get("structured") or {}).items():
            if "verify" in spots and rel == SUMMARY_REL:
                doc = json.loads(data)
                for entry in doc["results"]:
                    durations.append(entry.pop("seconds"))
                structured[rel] = doc
            else:
                structured[rel] = data
        for value in durations:
            self.assertIsInstance(value, (int, float), f"耗时字段必须是数值：{durations}")
            self.assertGreaterEqual(value, 0.0, f"耗时字段必须非负：{durations}")
        return {
            "code": result["code"],
            "out": out,
            "err": [line for line in lines if line != WARN],
            "structured": structured,
        }

    def assert_equivalent(self, base: dict, *others: dict, spots: tuple[str, ...] = (), context: str = "") -> None:
        self.assertNotIn(WARN, base["err"], f"{context}：事件开启/关闭态不应出现写入失败提示")
        left = self.normalize(base, spots)
        for other in others:
            self.assertEqual(left, self.normalize(other, spots), context)

    def three_states(
        self,
        name: str,
        argv: list[str],
        expect_steps: tuple[str, ...] = (),
        *,
        stdin: str = "",
        structured: tuple[str, ...] = (),
        spots: tuple[str, ...] = (),
        emit: bool = True,
        env_extra: dict[str, str] | None = None,
        reset_verify_pass: bool = False,
    ) -> dict:
        extra = env_extra or {}

        def invoke(env_over: dict[str, str]) -> dict:
            return self.run_cli(
                argv,
                stdin=stdin,
                structured=structured,
                env_extra={**extra, **env_over},
                reset_verify_pass=reset_verify_pass,
            )

        before = self.max_event_id()
        on = invoke({})
        rows = self.events_after(before)
        steps = {row["step"] for row in rows}
        if emit:
            self.assertTrue(rows, f"{name}：事件开启时没有写入任何事件")
            missing = set(expect_steps) - steps
            self.assertFalse(missing, f"{name}：缺事件 step {sorted(missing)}（实际 {sorted(steps)}）")
        else:
            self.assertFalse(rows, f"{name}：该输入不应产生事件（实际 {sorted(steps)}）")
        for row in rows:  # 白名单的另一半：事件时长字段必须是合法数值
            if row["duration_ms"] is not None:
                self.assertIsInstance(row["duration_ms"], int, name)
                self.assertGreaterEqual(row["duration_ms"], 0, name)
        off = invoke(OFF_ENV)
        self.assertEqual(self.max_event_id(), before + len(rows), f"{name}：事件关闭时不得写事件")
        # 失败隔离范围（合同 C0）：关闭态不仅零事件，也没有任何写入尝试（库健康时不会出现失败提示）。
        self.assertNotIn(WARN, off["err"], f"{name}：事件关闭态不应尝试写入")
        self.sink_break()
        try:
            broken = invoke({})
            warn_count = broken["err"].count(WARN)
            self.assertLessEqual(warn_count, 1, f"{name}：写入失败提示最多一次\n{broken['err']}")
            if emit:
                self.assertEqual(warn_count, 1, f"{name}：未真实进入失败写入点\n{broken['err']}")
                self.assertTrue((self.repo / ".git/harness").is_file(), f"{name}：失败写入点未触发（库目录被重建）")
            else:
                self.assertEqual(warn_count, 0, f"{name}：无写入就不该有失败提示\n{broken['err']}")
        finally:
            self.sink_restore()
        if emit:  # 失败的写入不留半条事件：库目录未被重建，原库保持关闭前的状态
            self.assertFalse(self.db_path.exists(), f"{name}：失败态不应产生库文件")
        self.assert_equivalent(on, off, broken, spots=spots, context=name)
        return on

    # ---------- 验收 1：各检查入口的成功/失败（及适用的 skip/异常）三态等价 ----------

    def test_verify_and_checks_equivalent_three_states(self):
        def verify_triple(name: str, argv: list[str], expected_code: int) -> dict:
            result = self.three_states(
                name, argv, ("cli.verify", "verify.summary"), structured=(SUMMARY_REL,), spots=("verify",)
            )
            self.assertEqual(result["code"], expected_code, name)
            return result

        verify_triple("verify 通过", ["verify"], 0)
        verify_triple("verify 手动跳过", ["verify", "--skip", "quality,docs,acceptance"], 0)
        checks_path = self.repo / ".harness/config/checks.toml"
        original = checks_path.read_text(encoding="utf-8")
        checks_path.write_text(original + DEMO_CHECK, encoding="utf-8")
        try:  # 失败结论来自非引擎命令的项目检查，子进程提示不会进入结论尾部
            verify_triple("verify 失败", ["verify"], 1)
        finally:
            checks_path.write_text(original, encoding="utf-8")

        self.three_states("hygiene 通过", ["hygiene", "--tracked"], ("cli.hygiene", "hygiene.summary"))
        self.commit({"notes.md": "记录\nlog /Users/test/data\n", ".DS_Store": "夹具\n"}, "违规夹具")
        result = self.three_states(
            "hygiene 失败", ["hygiene", "--range", self.base], ("cli.hygiene", "hygiene.summary", "hygiene.violation")
        )
        self.assertEqual(result["code"], 1)
        self.git("reset", "-q", "--hard", self.base)

        self.three_states("taskbook 通过", ["taskbook"], ("cli.taskbook", "taskbook.admit"))
        self.commit({"docs/plans/task-902-bad.md": taskbook_text("T902", ci_rounds=9, acceptance="ZZ99")}, "坏任务书")
        result = self.three_states("taskbook 失败", ["taskbook"], ("cli.taskbook", "taskbook.admit"))
        self.assertEqual(result["code"], 1)
        self.git("reset", "-q", "--hard", self.base)

        self.three_states("docs 通过", ["docs"], ("cli.docs",))
        self.commit({"docs/bad.md": "[断链](no-such-target.md)\n"}, "坏链接")
        result = self.three_states("docs 失败", ["docs"], ("cli.docs",))
        self.assertEqual(result["code"], 1)
        self.git("reset", "-q", "--hard", self.base)

        self.three_states("acceptance 通过", ["acceptance"], ("cli.acceptance",))
        gaps = self.repo / ".harness/state/acceptance-gaps.txt"
        gaps.write_text("ZZ99\n", encoding="utf-8")
        result = self.three_states("acceptance 失败", ["acceptance"], ("cli.acceptance",))
        self.assertEqual(result["code"], 1)
        gaps.unlink()

        result = self.three_states("integrity 通过", ["integrity"], ("cli.integrity", "integrity"))
        self.assertEqual(result["code"], 0)
        tampered = self.repo / ".harness/engine/core/common.py"
        original_bytes = tampered.read_bytes()
        tampered.write_bytes(original_bytes + "# 夹具篡改\n".encode())
        result = self.three_states("integrity 失败", ["integrity"], ("cli.integrity", "integrity"))
        self.assertEqual(result["code"], 1)
        tampered.write_bytes(original_bytes)

        head_doc = self.commit({"docs/note.md": "说明\n"}, "文档改动")
        self.three_states(
            "base-tests 通过", ["base-tests", "--base", self.base, "--head", head_doc], ("cli.base-tests", "base_tests")
        )
        head_fix = self.commit(
            {
                "app.py": "def flag():\n    return True\n",
                "tests/test_app.py": TEST_APP_BASE + "\n    def test_flag(self):\n"
                "        self.assertTrue(app.flag())  # T901-X1\n",
            },
            "修复\n\nDefect: T901-X1",
        )
        result = self.three_states(
            "base-tests 失败", ["base-tests", "--base", self.base, "--head", head_fix], ("cli.base-tests", "base_tests")
        )
        self.assertEqual(result["code"], 1)

        prompt = "为 T901 准备的提示词\n"
        record = {
            "task": "T901",
            "class": "K1",
            "attempt": 1,
            "branch": "task/901-x",
            "gen_ai.agent.name": "pi",
            "host_version": "0.85.1",
            "gen_ai.request.model": "test/model",
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_path": "docs/runs/task-901-legal/1.prompt.md",
            "guard_ref": "main@" + self.git("rev-parse", "--short", "main").stdout.strip(),
            "started_at": "2026-01-02T03:04:05Z",
            "ended_at": "2026-01-02T03:14:05Z",
            "exit": "ok",
            "retries": 0,
            "failure_signatures": [],
            "guard_denials": {},
        }
        self.commit(
            {
                "docs/runs/task-901-legal/1.prompt.md": prompt,
                "docs/runs/task-901-legal/1.json": json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            },
            "运行记录\n\nTask: T901",
        )
        replay_cases = self.repo / ".harness/project/replay_cases.py"
        replay_cases.write_text(
            "from engine.core.cases import Case\n"
            'BASELINE = ["T901-X1"]\nCASES: list[Case] = []\n'
            'GUARDED = {"T901-X1": ("test_app.AppTest.test_flag",)}\n'
            "DEFERRED = {}\n",
            encoding="utf-8",
        )
        result = self.three_states(
            "evidence 完整", ["evidence", "--base", self.base, "--head", head_fix], ("cli.evidence", "evidence")
        )
        self.assertEqual(result["code"], 0)
        result = self.three_states(
            "evidence 缺失",
            ["evidence", "--base", self.base, "--head", head_fix, "--ids", "T901-Z9", "--no-run"],
            ("cli.evidence", "evidence"),
        )
        self.assertEqual(result["code"], 1)

        replay_original = replay_cases.read_bytes()
        result = self.three_states("replay 通过", ["replay"], ("cli.replay",))
        self.assertEqual(result["code"], 0)
        replay_cases.write_text(
            "from engine.core.cases import Case\n"
            "BASELINE = []\n"
            'CASES = [Case("T901-L1", "无测试守护的注入", "README.md", '
            '"# app", "# app 变体", ("test_calc.CalcTest",))]\n'
            'GUARDED = {"T901-X1": ("test_app.AppTest.test_flag",)}\n'
            "DEFERRED = {}\n",
            encoding="utf-8",
        )
        result = self.three_states("replay 失败（漏过）", ["replay"], ("cli.replay",))
        self.assertEqual(result["code"], 1)
        replay_cases.write_bytes(replay_original)

        baseline = self.repo / ".harness/state/mutation-baseline.json"
        baseline.write_text(json.dumps({"sign": 1.0}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result = self.three_states("mutate 通过", ["mutate", "--check", "--only", "sign"], ("cli.mutate", "mutation"))
        self.assertEqual(result["code"], 0)
        result = self.three_states("mutate 失败", ["mutate", "--check", "--only", "broken"], ("cli.mutate", "mutation"))
        self.assertEqual(result["code"], 1)

        quality_baseline = self.repo / ".harness/state/quality-baseline.json"
        quality_baseline.write_text(
            json.dumps(
                {
                    "complex_functions": 0,
                    "files_over_800": 0,
                    "largest_file_lines": 2,
                    "swift_files_over_500": 0,
                    "swift_long_functions": 0,
                    "swift_deep_functions": 0,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.three_states("quality 通过", ["quality"], ("cli.quality", "quality"))
        quality_baseline.write_text(
            json.dumps(
                {
                    "complex_functions": 0,
                    "files_over_800": 0,
                    "largest_file_lines": 0,
                    "swift_files_over_500": 0,
                    "swift_long_functions": 0,
                    "swift_deep_functions": 0,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.commit({"src/big.py": "x = 1\n" * 801}, "体量夹具")
        result = self.three_states("quality 失败", ["quality"], ("cli.quality", "quality"))
        self.assertEqual(result["code"], 1)
        self.git("reset", "-q", "--hard", self.base)

        self.git("tag", "v1.2.3")
        self.three_states("release-check 通过", ["release-check", "--tag", "v1.2.3"], ("cli.release-check",))
        result = self.three_states("release-check 失败", ["release-check", "--tag", "v9.9.9"], ("cli.release-check",))
        self.assertEqual(result["code"], 1)

        result = self.three_states("r1 异常", ["r1", "--base", self.base, "--head", self.base], ("cli.r1",))
        self.assertEqual(result["code"], 1)
        self.assertIn("AttributeError", result["err"])

    # ---------- 验收 2：判级、误差预算路由与运行记录合格/缺失的三态比对 ----------

    def test_risk_policy_and_run_check_equivalent(self):
        head_r0 = self.commit({"docs/note.md": "说明\n"}, "文档改动")
        github_files = ("build/github/risk-output.txt", "build/github/risk-summary.md")
        github_env = {
            "GITHUB_OUTPUT": str(self.repo / github_files[0]),
            "GITHUB_STEP_SUMMARY": str(self.repo / github_files[1]),
        }
        result = self.three_states(
            "risk R0",
            ["risk", "--base", self.base, "--head", head_r0, "--github"],
            ("cli.risk", "risk.file", "risk.summary"),
            structured=github_files,
            env_extra=github_env,
        )
        self.assertEqual(result["code"], 0)
        self.assertIn(b"risk=R0", result["structured"][github_files[0]])

        head_r3 = self.commit({"critical/guard.py": "VALUE = 1\n"}, "碰护栏\n\nRisk: R1")
        result = self.three_states(
            "risk R3（声明 R1 被降级）",
            ["risk", "--base", head_r0, "--head", head_r3, "--github"],
            ("cli.risk", "risk.file", "risk.summary"),
            structured=github_files,
            env_extra=github_env,
        )
        self.assertIn(b"risk=R3", result["structured"][github_files[0]])
        self.assertIn("维持原等级", result["out"])

        gh_bin = self.tmp / "gh-allow"
        gh_bin.mkdir()
        (gh_bin / "gh").write_text(GH_ALLOW, encoding="utf-8")
        (gh_bin / "gh").chmod(0o755)
        gh_down = self.tmp / "gh-down"
        gh_down.mkdir()
        (gh_down / "gh").write_text(GH_DOWN, encoding="utf-8")
        (gh_down / "gh").chmod(0o755)
        policy_files = ("build/github/policy-output.txt", "build/github/policy-summary.md")
        policy_argv = [
            "policy",
            "--base",
            self.base,
            "--head",
            head_r0,
            "--pr",
            "9",
            "--branch",
            "task/routed",
            "--github",
        ]
        allow_env = {
            **github_env,
            "GITHUB_OUTPUT": str(self.repo / policy_files[0]),
            "GITHUB_STEP_SUMMARY": str(self.repo / policy_files[1]),
            "PATH": str(gh_bin) + os.pathsep + self.child_env()["PATH"],
        }
        deny_env = {**allow_env, "PATH": str(gh_down) + os.pathsep + self.child_env()["PATH"]}
        result = self.three_states(
            "policy 自动合并",
            policy_argv,
            ("cli.policy", "facts", "result"),
            structured=policy_files,
            env_extra=allow_env,
        )
        self.assertIn(b"auto_merge=true", result["structured"][policy_files[0]])
        result = self.three_states(
            "policy 转用户评审",
            policy_argv,
            ("cli.policy", "facts", "result"),
            structured=policy_files,
            env_extra=deny_env,
        )
        self.assertIn(b"auto_merge=false", result["structured"][policy_files[0]])

        taskbook_base = self.commit({"docs/plans/task-910-fixture.md": taskbook_text("T910")}, "任务书 910")
        self.git("checkout", "-q", "-b", "task/910-run")
        prompt = "为 T910 准备的提示词\n"
        record = {
            "task": "T910",
            "class": "K1",
            "attempt": 1,
            "branch": "task/910-run",
            "gen_ai.agent.name": "pi",
            "host_version": "0.85.1",
            "gen_ai.request.model": "test/model",
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_path": "docs/runs/task-910-fixture/1.prompt.md",
            "guard_ref": f"main@{self.git('rev-parse', '--short', 'main').stdout.strip()}",
            "started_at": "2026-01-02T03:04:05Z",
            "ended_at": "2026-01-02T03:14:05Z",
            "exit": "ok",
            "retries": 0,
            "failure_signatures": [],
            "guard_denials": {},
        }
        head_run = self.commit(
            {
                "docs/runs/task-910-fixture/1.prompt.md": prompt,
                "docs/runs/task-910-fixture/1.json": json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            },
            "实现\n\nTask: T910",
        )
        runcheck_files = ("build/github/runcheck-summary.md",)
        runcheck_env = {"GITHUB_STEP_SUMMARY": str(self.repo / runcheck_files[0])}
        result = self.three_states(
            "run-check 合格",
            ["run-check", "--base", taskbook_base, "--head", head_run, "--branch", "task/910-run", "--github"],
            ("cli.run-check", "run_check", "run_check.finding"),
            structured=runcheck_files,
            env_extra=runcheck_env,
        )
        self.assertEqual(result["code"], 0)
        self.assertIn("✅", result["out"])
        self.git("checkout", "-q", "main")
        head_miss = self.commit({"docs/plain.md": "普通\n"}, "无关提交")
        result = self.three_states(
            "run-check 缺记录",
            ["run-check", "--base", taskbook_base, "--head", head_miss, "--branch", "task/910-fixture"],
            ("cli.run-check", "run_check", "run_check.finding"),
            structured=runcheck_files,
            env_extra=runcheck_env,
        )
        self.assertEqual(result["code"], 0)
        self.assertIn("缺运行记录", result["out"])
        result = self.three_states(
            "run-check 不适用",
            ["run-check", "--base", taskbook_base, "--head", head_miss, "--branch", "feature/plain"],
            ("cli.run-check", "run_check"),
            structured=runcheck_files,
            env_extra=runcheck_env,
        )
        self.assertIn("不适用", result["out"])

    # ---------- 验收 3：JSON 与文本命令拒绝及三个 git 钩子三态比对 ----------

    def test_guard_command_and_git_hooks_equivalent(self):
        force = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push --force origin feature-x"}})
        result = self.three_states(
            "guard-command claude 拒绝",
            ["guard-command", "--format", "claude", "--role", "implementer"],
            ("command",),
            stdin=force,
        )
        self.assertEqual(result["code"], 2)
        result = self.three_states(
            "guard-command json 拒绝",
            ["guard-command", "--format", "json"],
            ("command",),
            stdin=json.dumps({"command": "gh pr merge 5 --squash"}),
        )
        self.assertEqual(result["code"], 2)
        result = self.three_states(
            "guard-command plain 拒绝",
            ["guard-command", "--format", "plain"],
            ("command",),
            stdin="git push origin main",
        )
        self.assertEqual(result["code"], 2)
        result = self.three_states(
            "guard-command 坏 JSON 拒绝", ["guard-command", "--format", "json"], (), stdin="{不是 JSON", emit=False
        )
        self.assertEqual(result["code"], 2)
        result = self.three_states(
            "guard-command 放行",
            ["guard-command", "--format", "json"],
            (),
            stdin=json.dumps({"command": "git status --short"}),
            emit=False,
        )
        self.assertEqual(result["code"], 0)

        result = self.three_states("pre-commit 拒绝（保护分支）", ["guard-git", "pre-commit"], ("git",))
        self.assertEqual(result["code"], 1)
        self.git("checkout", "-q", "-b", "task/x")
        self.commit({"docs/note.md": "note\n"}, "note")
        # 安装布局下放行的 pre-commit 会真实运行 verify --quick（含子进程与耗时字段，按 verify 白名单归一）
        result = self.three_states(
            "pre-commit 放行（含 verify --quick）",
            ["guard-git", "pre-commit"],
            ("cli.verify", "verify.summary"),
            spots=("verify",),
            structured=(SUMMARY_REL,),
        )
        self.assertEqual(result["code"], 0)
        local = self.head_sha()
        remote_main = self.git("rev-parse", "main").stdout.strip()
        result = self.three_states(
            "pre-push 拒绝（保护分支）",
            ["guard-git", "pre-push"],
            ("git",),
            stdin=f"refs/heads/task/x {local} refs/heads/main {remote_main}\n",
        )
        self.assertEqual(result["code"], 1)
        result = self.three_states(
            "pre-push 放行（新建分支，含 verify 默认档）",
            ["guard-git", "pre-push"],
            ("cli.verify", "verify.summary"),
            stdin=f"refs/heads/task/x {local} refs/heads/task/x {ZERO_SHA}\n",
            spots=("verify",),
            structured=(SUMMARY_REL,),
            # 每态调用前清空通过记录目录（T713 修订 1）：否则第一态跑完 verify 写下的记录会让后两态跳过 verify。
            reset_verify_pass=True,
        )
        self.assertEqual(result["code"], 0)
        self.git("checkout", "-q", "main")
        self.git("tag", "v1")
        tag_sha = self.git("rev-parse", "v1").stdout.strip()
        result = self.three_states(
            "reference-transaction 拒绝（删 tag）",
            ["guard-git", "reference-transaction", "prepared"],
            ("git",),
            stdin=f"{tag_sha} {ZERO_SHA} refs/tags/v1\n",
        )
        self.assertEqual(result["code"], 1)
        result = self.three_states(
            "reference-transaction 放行（建分支）",
            ["guard-git", "reference-transaction", "prepared"],
            (),
            stdin=f"{ZERO_SHA} {local} refs/heads/task/x\n",
            emit=False,
        )
        self.assertEqual(result["code"], 0)

    # ---------- 验收 4：比较器边界——注入差异必须被检出，白名单之外不吃任何差异 ----------

    def test_normalization_is_bounded(self):
        on = self.run_cli(["hygiene", "--tracked"])
        self.assertEqual(on["code"], 0)
        self.sink_break()
        try:
            broken = self.run_cli(["hygiene", "--tracked"])
        finally:
            self.sink_restore()
        self.assertEqual(broken["err"].count(WARN), 1)
        self.assert_equivalent(on, broken, context="hygiene 三态")

        def must_detect(name: str, base: dict, other: dict, spots: tuple[str, ...] = ()) -> None:
            with self.assertRaises(AssertionError, msg=name):
                self.assert_equivalent(base, other, spots=spots, context=name)

        # 开启态出现写入失败提示：不属于任何一态的合法形状
        must_detect("开启态提示", {**on, "err": on["err"] + WARN + "\n"}, on)
        # 提示出现两次：超过每进程一次的边界
        must_detect("提示两次", on, {**broken, "err": broken["err"] + WARN + "\n"})
        # 业务 stderr 多出一行、退出码不同、stdout 多出一行：全部必须检出
        must_detect("业务 stderr 注入", on, {**broken, "err": broken["err"] + "额外的业务错误\n"})
        must_detect("退出码不同", on, {**on, "code": 1})
        must_detect("stdout 注入", on, {**on, "out": on["out"] + "多出来的输出\n"})
        # 结构化结果差异必须检出
        must_detect("结构化结果不同", on, {**on, "structured": {SUMMARY_REL: b"{}"}})

        verify = self.run_cli(["verify"], structured=(SUMMARY_REL,))
        self.assertEqual(verify["code"], 0)
        self.assertTrue(verify["structured"][SUMMARY_REL])
        # 白名单内：真实耗时字段变化不算差异（stdout 结论行与 summary.json 的 seconds 各验一处）
        later = DURATION_LINE.sub(lambda match: f"{match.group(1)}9.9s", verify["out"], count=1)
        doc = json.loads(verify["structured"][SUMMARY_REL])
        doc["results"][0]["seconds"] = 9.9
        must_not_change = {
            "code": verify["code"],
            "out": later,
            "err": verify["err"],
            "structured": {SUMMARY_REL: json.dumps(doc, ensure_ascii=False).encode()},
        }
        self.assert_equivalent(verify, must_not_change, spots=("verify",), context="耗时白名单")
        # 白名单边界：耗时不合法（负数）或结论行状态被改，都必须检出
        bad = json.loads(verify["structured"][SUMMARY_REL])
        bad["results"][0]["seconds"] = -1
        must_detect(
            "负耗时", verify, {**verify, "structured": {SUMMARY_REL: json.dumps(bad).encode()}}, spots=("verify",)
        )
        must_detect(
            "检查结论被改", verify, {**verify, "out": verify["out"].replace("✓ tools", "✗ tools", 1)}, spots=("verify",)
        )
        must_detect(
            "耗时行以外的结论差异",
            verify,
            {**verify, "out": verify["out"].replace("verify 通过", "verify 失败", 1)},
            spots=("verify",),
        )


if __name__ == "__main__":
    unittest.main()
