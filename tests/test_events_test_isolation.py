"""T710 测试事件隔离（B92 第 2 部分）：HARNESS_EVENTS_REDIRECT 只对指定的那个仓库生效。

验收调用真实产品入口（engine/core/events_db 的路径解析与 engine.core.events.emit），夹具全部为
匿名临时 git 仓库，不写真实事件库：
- 匹配当前公共目录时，db_path/artifacts_dir 与 emit 写入的事件都落在临时目录，真实库无此事件；
- for_common_dir 与当前公共目录不同（patch ROOT 指向夹具、夹具仓库里起的子进程）时路径照旧；
- 变量值不是合法 JSON、缺字段、类型不对时按原路径处理且不抛异常（观察旁路不影响主流程）；
- 安装布局夹具里跑 verify：项目检查子进程收到匹配当前仓库的变量且事件不进夹具真实库，
  引擎检查照常写夹具真实库，运行结束后临时目录被删除。
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

from engine import __version__ as ENGINE_VERSION
from engine.checks import integrity
from engine.core import events, events_db

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
# 夹具仓库里再起的子进程：真实引擎入口 + emit，事件必须写进夹具自己的库（不被导走）。
CHILD_EMIT = """\
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / ".harness"))
from engine.core import events
ok = isinstance(events.emit(stage="ci", step="iso.child", status="ok", trace_id="t710-child"), int)
raise SystemExit(0 if ok else 1)
"""


# 安装布局夹具的项目检查：把收到的重定向变量、自己解析出的事件库路径与 emit 结果写进报告文件。
DEMO_CHECK = """\
import json, os, pathlib, sys
sys.path.insert(0, ".harness")
from engine.core import events, events_db
report = {
    "redirect": os.environ.get("HARNESS_EVENTS_REDIRECT"),
    "db": str(events_db.db_path()),
    "emit": events.emit(stage="ci", step="demo.project_check", status="ok", trace_id="t710-verify"),
}
pathlib.Path("demo-report.json").write_text(json.dumps(report), encoding="utf-8")
"""
CHECKS_TOML = """\
[verify]

[[verify.checks]]
name = "demo"
tiers = ["default", "full"]
command = ["{python}", "demo_check.py"]
"""
REPLAY_EMPTY = "BASELINE = []\nCASES = []\nGUARDED = {}\nDEFERRED = {}\n"


def rows_in(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with contextlib.closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql, params).fetchall()


class TestEventsIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-iso-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.fresh_repo("app")
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "HARNESS_EVENTS_REDIRECT", "CI",
                    "GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_STEP_SUMMARY"):
            os.environ.pop(key, None)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)
        self.fixture_db = events_db.db_path()  # 未设变量的基线路径（夹具库）
        self.assertIsNotNone(self.fixture_db)

    # ---------- 夹具与查询 ----------

    def fresh_repo(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir()
        self.git("init", "-q", "-b", "main", cwd=path)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "-A", cwd=path)
        self.git("commit", "-q", "-m", "init", cwd=path)
        return path

    def git(self, *args: str, cwd: Path | None = None, check: bool = True):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "HARNESS_")) and k != "CI"}
        return subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def child_env(self, **extra: str) -> dict[str, str]:
        """夹具子进程环境：不继承钩子注入与外层 HARNESS_/CI 变量，只带显式给出的键。"""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "GITHUB_", "HARNESS_")) and k != "CI"}
        env.update(GIT_ENV)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(extra)
        return env

    def other_common_dir(self, repo: Path) -> str:
        with mock.patch.object(events_db, "ROOT", repo):
            return str(events_db.common_dir())

    # ---------- 验收 1：匹配当前公共目录时事件导到临时库 ----------

    def test_redirect_applies_to_matching_repo(self):
        holder = self.tmp / "holder"
        holder.mkdir()
        redirect = json.dumps({"dir": str(holder), "for_common_dir": str(events_db.common_dir())})
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS_REDIRECT": redirect}):
            self.assertEqual(events_db.db_path(), holder / "harness" / "harness.db")
            self.assertEqual(events_db.artifacts_dir(), holder / "harness" / "artifacts")
            event_id = events.emit(stage="ci", step="iso.redirected", status="ok", trace_id="t710-iso")
            self.assertIsInstance(event_id, int)
        self.assertEqual(rows_in(holder / "harness" / "harness.db",
                                 "SELECT step, trace_id FROM events"),
                         [("iso.redirected", "t710-iso")])
        self.assertFalse(self.fixture_db.exists())  # 真实（夹具）库没有这条事件
        self.assertEqual(events_db.db_path(), self.fixture_db)  # 变量撤销后回到原路径

    # ---------- 验收 2：公共目录不同（patch ROOT / 夹具子进程）时照旧 ----------

    def test_redirect_ignored_for_other_repo(self):
        holder = self.tmp / "holder"
        holder.mkdir()
        other = self.fresh_repo("other")
        redirect = json.dumps({"dir": str(holder), "for_common_dir": self.other_common_dir(other)})

        # patch ROOT 指向的夹具与 for_common_dir 不同：路径照旧，事件写夹具库。
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS_REDIRECT": redirect}):
            self.assertEqual(events_db.db_path(), self.fixture_db)
            self.assertEqual(events_db.artifacts_dir(), self.fixture_db.parent / "artifacts")
            self.assertIsInstance(events.emit(stage="ci", step="iso.patched", status="ok",
                                             trace_id="t710-patched"), int)
        self.assertEqual(rows_in(self.fixture_db, "SELECT step FROM events"), [("iso.patched",)])
        self.assertFalse((holder / "harness").exists())

        # 夹具仓库里再起的子进程：变量在环境里但公共目录不同，照旧写夹具库。
        child = self.fresh_repo("child")
        shutil.copytree(ENGINE_REPO / "engine", child / ".harness" / "engine",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        completed = subprocess.run([sys.executable, "-c", CHILD_EMIT], cwd=child,
                                   capture_output=True, text=True, timeout=120, check=False,
                                   env=self.child_env(HARNESS_EVENTS_REDIRECT=redirect))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(rows_in(child / ".git" / "harness" / "harness.db",
                                 "SELECT step, trace_id FROM events"), [("iso.child", "t710-child")])
        self.assertEqual(rows_in(self.fixture_db, "SELECT COUNT(*) FROM events"), [(1,)])
        self.assertFalse((holder / "harness").exists())

    # ---------- 验收 3：坏值按原路径处理且不抛异常 ----------

    def test_bad_redirect_falls_back(self):
        common = str(events_db.common_dir())
        bad_values = [
            "不是 JSON",
            "",
            "[]",                                              # 合法 JSON 但不是对象
            "{}",                                              # 缺字段
            '{"dir": "/tmp/dh-iso"}',                          # 缺 for_common_dir
            json.dumps({"dir": 1, "for_common_dir": common}),  # dir 类型不对
            json.dumps({"dir": "/tmp/dh-iso", "for_common_dir": 5}),  # for_common_dir 类型不对
            json.dumps({"dir": "/tmp/dh-iso", "for_common_dir": str(self.tmp)}),  # 公共目录不同
        ]
        for value in bad_values:
            with self.subTest(value=value), mock.patch.dict(os.environ, {"HARNESS_EVENTS_REDIRECT": value}):
                self.assertEqual(events_db.db_path(), self.fixture_db)
                self.assertEqual(events_db.artifacts_dir(), self.fixture_db.parent / "artifacts")
                self.assertIsInstance(events.emit(stage="ci", step="iso.bad", status="ok",
                                                 trace_id="t710-bad"), int)
        self.assertEqual(rows_in(self.fixture_db, "SELECT COUNT(*) FROM events WHERE step=?", ("iso.bad",)),
                         [(len(bad_values),)])  # 每次都照常写进原路径，主流程不受影响

    # ---------- 验收 4：verify 只给项目检查设置变量并清理临时目录 ----------

    def test_verify_sets_redirect_only_for_project_checks(self):
        repo = self.fresh_repo("verify")
        engine_dir = repo / ".harness" / "engine"
        shutil.copytree(ENGINE_REPO / "engine", engine_dir,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        lock = {"engine": "delivery-harness", "version": ENGINE_VERSION, "commit": "fixture",
                "tree": integrity.tree_hash(engine_dir)}
        (repo / ".harness" / "engine.lock").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        (repo / ".harness" / "config").mkdir(parents=True, exist_ok=True)
        (repo / ".harness" / "config" / "checks.toml").write_text(CHECKS_TOML, encoding="utf-8")
        (repo / ".harness" / "project").mkdir(parents=True, exist_ok=True)
        (repo / ".harness" / "project" / "replay_cases.py").write_text(REPLAY_EMPTY, encoding="utf-8")
        (repo / "demo_check.py").write_text(DEMO_CHECK, encoding="utf-8")
        (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "fixture", cwd=repo)

        proc = subprocess.run(
            [sys.executable, str(engine_dir / "cli.py"), "verify", "--only", "integrity,demo"],
            cwd=repo, capture_output=True, text=True, timeout=300, check=False, env=self.child_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verify 通过", proc.stdout)

        # 项目检查子进程收到匹配当前仓库的变量，事件解析并写进临时库。
        report = json.loads((repo / "demo-report.json").read_text(encoding="utf-8"))
        self.assertIsInstance(report["redirect"], str)
        data = json.loads(report["redirect"])
        self.assertEqual(set(data), {"dir", "for_common_dir"})
        self.assertEqual(Path(data["for_common_dir"]).resolve(), (repo / ".git").resolve())
        self.assertEqual(Path(report["db"]), Path(data["dir"]) / "harness" / "harness.db")
        self.assertIsInstance(report["emit"], int)
        self.assertFalse(Path(data["dir"]).exists())  # 运行结束后临时目录已删除

        # 引擎检查照常写夹具真实库；项目检查子进程的事件（demo.project_check）不在其中。
        # （cli.<命令> 入口事件在命令返回后才记录，所以排在各自内容事件之后。）
        self.assertEqual([row[0] for row in rows_in(repo / ".git" / "harness" / "harness.db",
                                                   "SELECT step FROM events ORDER BY id")],
                         ["integrity", "cli.integrity", "verify.integrity", "verify.demo",
                          "verify.summary", "cli.verify"])

    # ---------- 修订记录 1：test_install 的 env() 清洗时保留重定向变量 ----------

    def test_install_env_keeps_redirect(self):
        """安装/升级子进程跑本仓库引擎（公共目录相同），env() 去掉重定向变量会把测试事件写回真实库。"""
        from tests import test_install

        redirect = json.dumps({"dir": str(self.tmp / "holder"), "for_common_dir": str(events_db.common_dir())})
        with mock.patch.dict(os.environ, {"HARNESS_EVENTS_REDIRECT": redirect,
                                          "HARNESS_EVENTS": "0", "HARNESS_ALLOW_REWRITE": "1"}):
            cleaned = test_install.env()
        self.assertEqual(cleaned.get("HARNESS_EVENTS_REDIRECT"), redirect)
        self.assertNotIn("HARNESS_EVENTS", cleaned)  # 其余 HARNESS_ 覆盖开关照旧去掉
        self.assertNotIn("HARNESS_ALLOW_REWRITE", cleaned)


if __name__ == "__main__":
    unittest.main()
