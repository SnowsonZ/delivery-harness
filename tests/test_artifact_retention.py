"""T204 本机产物保留期与每日清理测试（B40）：checks.toml 可选 [events] artifact_days，缺省 30。

每天首次 emit 顺带清理（无守护进程，跨进程一天一轮）：过期的内容寻址产物文件与索引、
终止运行的派发原始流（<git 公共目录>/dispatch/runs/<任务>/<attempt>/round-N.jsonl）；
events/refs/anchors 行永不因保留期删除。以 UTC created/mtime 判定，超过保留期才删；
活动槽位锁保护该任务全部 attempt（F4），锁读取或探活不确定当轮整体跳过；符号链接与
路径穿越不追随、不删除；清理失败只提示一次、不影响 emit 与业务返回。

验收调用真实产品入口（events.emit / events.store_artifact）；夹具与 T101/T203 同型：
匿名临时 git 仓库、隔离 events_db.ROOT 与配置路径、冻结时钟、按 pid 打桩探活，
不碰真实库、PR 或工作流。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from engine.core import common, events, events_db

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
T0 = datetime(2026, 1, 5, 3, 4, 5, 678000, tzinfo=UTC)
# 死进程与权限不明进程的打桩 pid：不指向真实进程，由 fake_kill 决定探活结果。
DEAD_PID = 290000001
PERM_PID = 290000002
# 并发子进程脚本：冻结时钟、给清理入口计数后调用真实 emit（产品入口），结果写进 marker。
CHILD = r"""
import sys
from pathlib import Path
repo, marker, now_iso = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
from engine.core import events, events_db
events_db.ROOT = repo
events_db._now = lambda: now_iso
real = events_db.run_retention_cleanup
state = {"rounds": 0}
def spy(cutoff):
    state["rounds"] += 1
    marker.write_text(str(state["rounds"]))
    return real(cutoff)
events_db.run_retention_cleanup = spy
event_id = events.emit(stage="dispatch", step="claim", status="ok",
                       trace_id="task/concurrent-" + marker.name, outputs={"claimed": True})
marker.write_text(f"{state['rounds']}:{'ok' if isinstance(event_id, int) else 'none'}")
"""


def iso(moment: datetime) -> str:
    """引擎 _now 的形状：UTC 毫秒 ISO8601 带 Z 后缀。"""
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-artifact-retention-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        config_dir = self.repo / ".harness" / "config"
        config_dir.mkdir(parents=True)
        for target, name, value in (
            (events_db, "ROOT", self.repo),
            (common, "CONFIG_DIR", config_dir),
        ):
            self.patch(target, name, value)
        self.now_dt = T0
        clock = mock.patch.object(events_db, "_now", lambda: iso(self.now_dt))
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
            os.environ.pop(key, None)
        for name in ("_warned", "_cleanup_warned", "_days_warned"):
            setattr(events, name, False)
            self.addCleanup(setattr, events, name, getattr(events, name))
        self.stderr_chunks: list[str] = []
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)

    # ---------- 夹具 ----------

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, check=True):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def set_clock(self, moment: datetime) -> None:
        self.now_dt = moment

    def age_iso(self, days) -> str:
        """比冻结时钟早 days 天的 created 时间戳。"""
        return iso(self.now_dt - timedelta(days=days))

    def age_epoch(self, days) -> float:
        return (self.now_dt - timedelta(days=days)).timestamp()

    def db_exec(self, sql, params=()) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(sql, params)
            conn.commit()

    def db_query(self, sql, params=()) -> list[tuple]:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def table_counts(self) -> dict[str, int]:
        tables = {"events", "refs", "artifacts", "anchors"}
        return {name: self.db_query(f"SELECT COUNT(*) FROM {name}")[0][0] for name in tables}

    def write_events_config(self, artifact_days) -> None:
        """写 checks.toml 的 [events] 节并清掉配置缓存；None 表示节里不写 artifact_days。"""
        lines = ["[events]\n"]
        if artifact_days is not None:
            lines.append(f"artifact_days = {json.dumps(artifact_days)}\n")
        (common.CONFIG_DIR / "checks.toml").write_text("".join(lines), encoding="utf-8")
        common._load_toml.cache_clear()

    def make_artifact(self, *, created: str | None = None) -> str:
        """真实 store_artifact 转存一份产物，再把索引 created 改成指定时刻；返回 sha256。"""
        entry = events.store_artifact(f"artifact for {self.id()} at {created}".encode())
        self.assertIsNotNone(entry)
        digest = entry["sha256"]
        if created is not None:
            self.db_exec("UPDATE artifacts SET created=? WHERE sha256=?", (created, digest))
        return digest

    def artifact_path(self, digest: str) -> Path:
        return events_db.artifacts_dir() / digest

    def insert_artifact_row(self, name: str, created: str) -> None:
        """伪造一条索引行（名字可以不是哈希），不创建对应文件。"""
        directory = events_db.artifacts_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.db_exec("INSERT INTO artifacts (sha256,size,path,created) VALUES (?,?,?,?)",
                     (name, 3, (directory / name).as_posix(), created))

    def runs_dir(self) -> Path:
        return events_db.common_dir() / "dispatch" / "runs"

    def make_stream(self, task: str, attempt: str, name: str, *, age_days: float | None = None,
                    content: bytes = b"round stream\n", symlink: Path | None = None) -> Path:
        """建一份派发原始流（或指向 symlink 的符号链接）；age_days 设文件 mtime（UTC）。"""
        path = self.runs_dir() / task / attempt / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if symlink is not None:
            path.symlink_to(symlink)
        else:
            path.write_bytes(content)
        if age_days is not None:
            moment = self.age_epoch(age_days)
            os.utime(path, (moment, moment))
        return path

    def write_lock(self, index: int, payload) -> Path:
        slots = events_db.common_dir() / "dispatch" / "slots"
        slots.mkdir(parents=True, exist_ok=True)
        lock = slots / f"{index}.json"
        text = payload if isinstance(payload, str) else json.dumps(payload)
        lock.write_text(text, encoding="utf-8")
        return lock

    def emit(self, trace_id="task/t204", **kwargs) -> int | None:
        kwargs.setdefault("outputs", {"claimed": True})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = events.emit(stage="dispatch", step="claim", status="ok", trace_id=trace_id, **kwargs)
        self.stderr_chunks.append(err.getvalue())
        return result

    def hint_count(self, marker: str) -> int:
        return "".join(self.stderr_chunks).count(marker)

    # ---------- 验收 1：缺省/用户/非法天数与 29/30/31 边界，事件引用锚点完整 ----------

    def test_default_custom_and_boundary_days(self):
        # 缺省 30：29、30 天整保留（超过才清），31 天删除；emit 返回事件 id（不影响原判定）
        keep29 = self.make_artifact(created=self.age_iso(29))
        keep30 = self.make_artifact(created=self.age_iso(30))
        gone31 = self.make_artifact(created=self.age_iso(31))
        fresh = self.make_artifact(created=self.age_iso(1))
        before = self.table_counts()
        result = self.emit()
        self.assertIsInstance(result, int)
        after = self.table_counts()
        self.assertTrue(self.artifact_path(keep29).is_file())
        self.assertTrue(self.artifact_path(keep30).is_file())
        self.assertTrue(self.artifact_path(fresh).is_file())
        self.assertFalse(self.artifact_path(gone31).exists())
        self.assertEqual({row[0] for row in self.db_query("SELECT sha256 FROM artifacts")},
                         {keep29, keep30, fresh})
        # 事件/引用/锚点行完整：只有本次 emit 新增一条事件，其余三表不因保留期变化
        self.assertEqual(after["events"], before["events"] + 1)
        self.assertEqual(after["refs"], before["refs"])
        self.assertEqual(after["anchors"], before["anchors"])
        self.assertEqual(self.hint_count("清理未完成"), 0)
        self.assertEqual(self.hint_count("配置非法"), 0)

        # 用户天数 2：上一轮的 29 天文件如今 30 天仍保留，30 天文件如今 31 天被清；3 天删除、2 天整保留
        self.set_clock(T0 + timedelta(days=1))
        self.write_events_config(2)
        keep1 = self.make_artifact(created=self.age_iso(1))
        keep2 = self.make_artifact(created=self.age_iso(2))
        gone3 = self.make_artifact(created=self.age_iso(3))
        result = self.emit()
        self.assertIsInstance(result, int)
        # keep29 如今 30 天，超过新保留期 2 天，一并被清
        self.assertFalse(self.artifact_path(keep29).exists())
        self.assertFalse(self.artifact_path(keep30).exists())
        self.assertTrue(self.artifact_path(keep1).is_file())
        self.assertTrue(self.artifact_path(keep2).is_file())
        self.assertFalse(self.artifact_path(gone3).exists())

        # 非法配置逐个按 30 处理：提示一次、事件照常写入
        for value in (0, -3, "30", 2.5, True, [30]):
            with self.subTest(value=value):
                self.set_clock(self.now_dt + timedelta(days=1))
                self.write_events_config(value)
                keep = self.make_artifact(created=self.age_iso(29))
                gone = self.make_artifact(created=self.age_iso(31))
                result = self.emit()
                self.assertIsInstance(result, int)
                self.assertTrue(self.artifact_path(keep).is_file())
                self.assertFalse(self.artifact_path(gone).exists())
        self.assertEqual(self.hint_count("配置非法"), 1)

        # 缺省（不写 artifact_days）同样按 30，且不再提示
        self.set_clock(self.now_dt + timedelta(days=1))
        self.write_events_config(None)
        keep = self.make_artifact(created=self.age_iso(29))
        gone = self.make_artifact(created=self.age_iso(31))
        self.assertIsInstance(self.emit(), int)
        self.assertTrue(self.artifact_path(keep).is_file())
        self.assertFalse(self.artifact_path(gone).exists())
        self.assertEqual(self.hint_count("配置非法"), 1)
        self.assertEqual(self.hint_count("清理未完成"), 0)

    # ---------- 验收 2：当天一轮、跨日再清、时钟回退不提前删、跨进程一天一轮 ----------

    def test_once_per_day_and_concurrent_emit(self):
        # 当天首次 emit 清掉过期产物；同日再 emit 不再清（新一轮过期文件留到次日）
        first = self.make_artifact(created=self.age_iso(31))
        self.assertIsInstance(self.emit(), int)
        self.assertFalse(self.artifact_path(first).exists())
        same_day = self.make_artifact(created=self.age_iso(31))
        self.assertIsInstance(self.emit(), int)
        self.assertTrue(self.artifact_path(same_day).exists())

        # 跨日再清理：昨日漏下的过期文件今天被清
        self.set_clock(T0 + timedelta(days=1))
        self.assertIsInstance(self.emit(), int)
        self.assertFalse(self.artifact_path(same_day).exists())

        # 时钟回退：回退日即使跑一轮，删除阈值随当前时钟前移——
        # 对回退后的时钟未过保留期的文件（对被回退前时钟已过期）不得删除
        rolled_back = T0 - timedelta(days=2)
        self.set_clock(rolled_back)
        tricky = self.make_artifact(
            created=iso(T0 + timedelta(days=1) - timedelta(days=30, seconds=1)))
        self.assertIsInstance(self.emit(), int)
        self.assertTrue(self.artifact_path(tricky).exists())
        self.assertIsInstance(self.emit(), int)  # 回退日同样一天一轮
        self.assertTrue(self.artifact_path(tricky).exists())
        # 时钟回到已被清理过的日期：当天不再清；到新的一天才按当时阈值清掉
        self.set_clock(T0 + timedelta(days=1))
        self.assertIsInstance(self.emit(), int)
        self.assertTrue(self.artifact_path(tricky).exists())
        self.set_clock(T0 + timedelta(days=2))
        self.assertIsInstance(self.emit(), int)
        self.assertFalse(self.artifact_path(tricky).exists())
        self.assertEqual(self.hint_count("清理未完成"), 0)

        # 跨进程并发：两个进程同日 emit 只有一轮清理成功，两边的 emit 都照常写入事件
        self.set_clock(T0 + timedelta(days=3))
        now_iso = iso(self.now_dt)
        shared = self.make_artifact(created=self.age_iso(31))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_", "GITHUB_"))}
        env["PYTHONPATH"] = str(ENGINE_REPO)
        markers = [self.tmp / f"marker-{name}" for name in ("a", "b")]
        procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(self.repo), str(marker), now_iso],
                                  cwd=self.tmp, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for marker in markers]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, f"{out}\n{err}")
        rounds = 0
        for marker in markers:
            text = marker.read_text()
            count, _, status = text.partition(":")
            self.assertEqual(status, "ok")
            rounds += int(count)
        self.assertEqual(rounds, 1)  # 两个进程合计只有一轮清理
        self.assertFalse(self.artifact_path(shared).exists())
        flags = list((events_db.common_dir() / "harness").glob(".cleanup-*"))
        self.assertIn(f".cleanup-{self.now_dt:%Y-%m-%d}", [flag.name for flag in flags])
        rows = self.db_query("SELECT COUNT(*) FROM events WHERE trace_id LIKE 'task/concurrent-%'")
        self.assertEqual(rows[0][0], 2)
        self.assertEqual(self.hint_count("清理未完成"), 0)

    # ---------- 验收 3：只清范围内文件；活动流保护矩阵；引用过期可在查询层判定 ----------

    def test_cleanup_stays_inside_artifacts_and_finished_runs(self):
        # 产物侧：普通过期文件删除；符号链接、目录、无索引的散文件、伪造索引一律保留
        victim = self.tmp / "outside-victim.txt"
        victim.write_text("keep me", encoding="utf-8")
        a_normal = self.make_artifact(created=self.age_iso(31))
        a_fresh = self.make_artifact(created=self.age_iso(5))
        link_name = "b" * 64
        os.symlink(victim, self.artifact_path(link_name))
        self.insert_artifact_row(link_name, self.age_iso(31))
        self.insert_artifact_row("../../escape", self.age_iso(31))
        dir_name = "c" * 64
        (self.artifact_path(dir_name) / "inner.bin").parent.mkdir(parents=True)
        (self.artifact_path(dir_name) / "inner.bin").write_bytes(b"x")
        self.insert_artifact_row(dir_name, self.age_iso(31))
        stray = events_db.artifacts_dir() / "stray-not-indexed.txt"
        stray.write_text("no index row", encoding="utf-8")
        old_moment = self.age_epoch(40)
        os.utime(stray, (old_moment, old_moment))

        # 原始流侧：先转存一份（T105 形态）并写一条引用它的真实事件，再把流与产物一起过期待清
        stream_path = self.make_stream("T300", "1", "round-1.jsonl", age_days=31)
        transferred = events.store_artifact(stream_path.read_bytes())
        self.assertIsNotNone(transferred)
        self.db_exec("UPDATE artifacts SET created=? WHERE sha256=?",
                     (self.age_iso(31), transferred["sha256"]))
        result = self.emit(trace_id="task/t300",
                           inputs=[{"kind": "artifact", "ref": transferred["sha256"],
                                    "sha256": transferred["sha256"], "size": transferred["size"]}],
                           outputs={"stream.ref": transferred["sha256"],
                                    "stream.sha256": transferred["sha256"],
                                    "stream.size": transferred["size"]})
        self.assertIsInstance(result, int)
        # 无锁任务：过期 round 流删除；符号链接流、非 round 名、续跑更新过 mtime 的流保留
        link_target = self.tmp / "stream-victim.txt"
        link_target.write_text("stream victim", encoding="utf-8")
        self.make_stream("T300", "1", "round-2.jsonl", age_days=31, symlink=link_target)
        self.make_stream("T300", "1", "notes.txt", age_days=40)
        self.make_stream("T300", "2", "round-1.jsonl", age_days=0)
        attempt_link = self.runs_dir() / "T300" / "3"
        attempt_link.symlink_to(self.tmp / "attempt-target", target_is_directory=True)
        (self.tmp / "attempt-target").mkdir()
        self.make_stream("T300", "3", "round-1.jsonl", age_days=40)
        # 活动锁（pid 存活，含权限不明）：保护该任务全部 attempt；死 PID 锁不保护
        self.write_lock(1, {"pid": os.getpid(), "task": "T301", "branch": "task/t301",
                            "started_at": self.age_iso(1), "attempt": 3})
        self.write_lock(2, {"pid": DEAD_PID, "task": "T302", "branch": "task/t302",
                            "started_at": self.age_iso(2)})
        self.write_lock(3, {"pid": PERM_PID, "task": "T303", "branch": "task/t303",
                            "started_at": self.age_iso(3)})
        self.make_stream("T301", "1", "round-1.jsonl", age_days=40)
        self.make_stream("T301", "2", "round-1.jsonl", age_days=40)
        t302 = self.make_stream("T302", "1", "round-1.jsonl", age_days=40)
        self.make_stream("T303", "1", "round-1.jsonl", age_days=40)

        real_kill = os.kill

        def fake_kill(pid, signal):
            if pid == DEAD_PID:
                raise ProcessLookupError
            if pid == PERM_PID:
                raise PermissionError
            return real_kill(pid, signal)

        # 引用事件已领当日清理旗，探活打桩后的清理在次日首次 emit 顺带执行
        self.set_clock(T0 + timedelta(days=1))
        with mock.patch.object(events_db.os, "kill", fake_kill):
            result = self.emit()
        self.assertIsInstance(result, int)
        # 产物侧断言
        self.assertFalse(self.artifact_path(a_normal).exists())
        self.assertTrue(self.artifact_path(a_fresh).is_file())
        self.assertTrue(self.artifact_path(link_name).is_symlink())
        self.assertEqual(victim.read_text(), "keep me")
        self.assertTrue(stray.is_file())
        self.assertEqual({row[0] for row in self.db_query("SELECT sha256 FROM artifacts")},
                         {a_fresh, link_name, dir_name, "../../escape"})
        self.assertTrue((self.artifact_path(dir_name) / "inner.bin").is_file())
        # 原始流侧断言
        self.assertFalse(stream_path.exists())
        self.assertTrue(self.runs_dir().joinpath("T300", "1", "round-2.jsonl").is_symlink())
        self.assertEqual(link_target.read_text(), "stream victim")
        self.assertTrue(self.runs_dir().joinpath("T300", "1", "notes.txt").is_file())
        self.assertTrue(self.runs_dir().joinpath("T300", "2", "round-1.jsonl").is_file())
        self.assertTrue(attempt_link.is_symlink())
        self.assertTrue((self.tmp / "attempt-target" / "round-1.jsonl").is_file())
        self.assertTrue(self.runs_dir().joinpath("T301", "1", "round-1.jsonl").is_file())
        self.assertTrue(self.runs_dir().joinpath("T301", "2", "round-1.jsonl").is_file())
        self.assertFalse(t302.exists())
        self.assertTrue(self.runs_dir().joinpath("T303", "1", "round-1.jsonl").is_file())
        # 转存产物也已过期删除，但事件与引用行完整、refs 无状态列：报告/查询层可标 reference_expired
        self.assertFalse(self.artifact_path(transferred["sha256"]).exists())
        refs = self.db_query("SELECT kind,ref,sha256,size FROM refs WHERE sha256=?",
                             (transferred["sha256"],))
        self.assertEqual(refs, [("artifact", transferred["sha256"], transferred["sha256"],
                                 transferred["size"])])
        self.assertEqual([row[1] for row in self.db_query("PRAGMA table_info(refs)")],
                         ["event_id", "direction", "kind", "ref", "sha256", "size"])
        event_rows = self.db_query("SELECT COUNT(*) FROM events WHERE trace_id='task/t300'")
        self.assertEqual(event_rows[0][0], 1)
        self.assertEqual(self.table_counts()["anchors"], 0)
        self.assertEqual(self.hint_count("清理未完成"), 1)  # 符号链接/目录/伪造索引用一条提示报告

        # 坏锁：当轮整体跳过原始流清理并报告，产物清理不受影响；锁修好后跨日再清
        self.set_clock(T0 + timedelta(days=2))
        self.write_lock(9, "{not json")
        blocked = self.make_stream("T305", "1", "round-1.jsonl", age_days=31)
        a_next = self.make_artifact(created=self.age_iso(31))
        self.assertIsInstance(self.emit(), int)
        self.assertTrue(blocked.is_file())
        self.assertFalse(self.artifact_path(a_next).exists())
        (events_db.common_dir() / "dispatch" / "slots" / "9.json").unlink()
        self.set_clock(T0 + timedelta(days=3))
        self.assertIsInstance(self.emit(), int)
        self.assertFalse(blocked.exists())
        self.assertEqual(self.hint_count("清理未完成"), 1)  # 每进程至多一条清理提示

    # ---------- 验收 4：权限、锁、删除失败时业务照常，至多一条清理提示 ----------

    def test_cleanup_error_does_not_break_emit(self):
        expired = self.make_artifact(created=self.age_iso(31))
        stream = self.make_stream("T310", "1", "round-1.jsonl", age_days=31)
        counts_before = self.table_counts()

        # 删除失败（权限）：emit 正常返回并写入事件，文件保留，只提示一次
        with mock.patch.object(Path, "unlink", side_effect=PermissionError(13, "Permission denied")):
            result = self.emit()
        self.assertIsInstance(result, int)
        self.assertTrue(self.artifact_path(expired).is_file())
        self.assertTrue(stream.is_file())
        self.assertEqual(self.db_query("SELECT COUNT(*) FROM artifacts WHERE sha256=?",
                                       (expired,))[0][0], 1)
        self.assertEqual(self.table_counts()["events"], counts_before["events"] + 1)
        self.assertEqual(self.hint_count("清理未完成"), 1)

        # 恢复后跨日重试成功；坏锁与缺 pid 锁都只跳过原始流并保持一条提示
        self.set_clock(T0 + timedelta(days=1))
        self.assertIsInstance(self.emit(), int)
        self.assertFalse(self.artifact_path(expired).exists())
        self.assertFalse(stream.exists())
        self.write_lock(1, "{broken")
        bad_lock_stream = self.make_stream("T311", "1", "round-1.jsonl", age_days=31)
        self.set_clock(T0 + timedelta(days=2))
        self.assertIsInstance(self.emit(), int)
        self.assertTrue(bad_lock_stream.is_file())
        self.write_lock(1, json.dumps({"task": "T312", "branch": "task/t312",
                                       "started_at": self.age_iso(1)}))
        missing_pid_stream = self.make_stream("T312", "1", "round-1.jsonl", age_days=31)
        self.set_clock(T0 + timedelta(days=3))
        self.assertIsInstance(self.emit(), int)
        self.assertTrue(missing_pid_stream.is_file())
        self.assertEqual(self.hint_count("清理未完成"), 1)

        # 锁移除后跨日清理成功；期间事件链完好（业务判定不受影响）
        (events_db.common_dir() / "dispatch" / "slots" / "1.json").unlink()
        self.set_clock(T0 + timedelta(days=4))
        self.assertIsInstance(self.emit(), int)
        self.assertFalse(bad_lock_stream.exists())
        self.assertFalse(missing_pid_stream.exists())
        self.assertEqual(self.hint_count("清理未完成"), 1)
        self.assertEqual(events.verify_chain("task/t204"), [])


if __name__ == "__main__":
    unittest.main()
