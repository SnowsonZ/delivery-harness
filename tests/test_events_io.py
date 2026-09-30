"""T301 事件导出与幂等导入测试：bundle v1 形状与哈希往返、安全 manifest、多来源隔离幂等导入、
坏包原子拒绝、展示过滤不冒充可导入包。

规范化哈希由本文件独立计算（不调用 events_db 的哈希函数）：把 bundle 事件独立拼回存储行形状再算
摘要，证明 canonical 字段/refs 能按 T101 规则逐字恢复。夹具全部用匿名临时 git 仓库、隔离 ROOT 与
递增冻结时钟，不碰真实库、PR 或工作流；不调用 gh。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from engine.core import events, events_db, events_io

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
EVENT_COLUMNS = ["id", "ts", "source", "trace_id", "seq", "prev_hash", "hash", "stage", "step", "status",
                 "duration_ms", "actor_role", "actor_host", "model", "decision_by", "decision_rule",
                 "decision_reason", "error_kind", "error_signature", "outputs", "engine_version", "redacted"]
GITHUB_KEYS = ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB")
BUNDLE_KEYS = {"schema_version", "origin", "events", "anchors", "artifacts", "chains", "findings"}
EVENT_KEYS = {"source", "trace_id", "seq", "prev_hash", "hash", "ts", "stage", "step", "status",
              "engine_version", "redacted", "id", "duration_ms", "inputs", "outputs", "decision",
              "error", "actor"}
SAFE_JSON = b'{"check":"lint","ok":true}'
LOG_TEXT = "2026-01-02 03:04:05 lint | tee /tmp/x.log\nerror: second line\n"


class _Clock:
    """递增冻结时钟：从 2026-01-02T03:01 起每次调用前进一分钟，事件 ts 严格递增且可比。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        hour, minute = divmod(self.step, 60)
        return f"2026-01-02T{3 + hour // 24:02d}:{(hour % 24 + 1):02d}:{minute:02d}.000Z"


def canonical_digest(row: dict, inputs: list[dict]) -> str:
    """独立实现的规范化哈希规则：除 id、hash 外全部列加 inputs，键排序、紧凑分隔符、UTF-8 后 sha256。"""
    payload = {key: value for key, value in row.items() if key not in ("id", "hash")}
    payload["inputs"] = inputs
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bundle_row(event: dict) -> dict:
    """从 bundle 事件独立拼出存储行形状（不调用产品代码），供独立摘要计算。"""
    decision = event.get("decision") or {}
    error = event.get("error") or {}
    actor = event.get("actor") or {}
    return {
        "ts": event["ts"], "source": event["source"], "trace_id": event["trace_id"],
        "seq": event["seq"], "prev_hash": event["prev_hash"], "stage": event["stage"],
        "step": event["step"], "status": event["status"], "duration_ms": event.get("duration_ms"),
        "actor_role": actor.get("role"), "actor_host": actor.get("host"), "model": actor.get("model"),
        "decision_by": decision.get("by"), "decision_rule": decision.get("rule"),
        "decision_reason": decision.get("reason"),
        "error_kind": error.get("kind"), "error_signature": error.get("signature"),
        "outputs": json.dumps(event.get("outputs") or {}, sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False),
        "engine_version": event["engine_version"], "redacted": event["redacted"],
    }


def forged_chain_event(*, source: str, trace: str, seq: int, prev_hash: str, ts: str,
                       step: str, status: str) -> dict:
    """自洽的伪造事件：内容不同但按规范化规则重算 hash，用于冲突与掩盖篡改夹具。"""
    event = {"source": source, "trace_id": trace, "seq": seq, "prev_hash": prev_hash, "ts": ts,
             "stage": "verify", "step": step, "status": status, "duration_ms": 1, "inputs": [],
             "outputs": {"forged": seq}, "decision": {}, "error": {}, "actor": {},
             "engine_version": "0.0.0-forged", "redacted": 0}
    event["hash"] = canonical_digest(bundle_row(event), event["inputs"])
    return event


def good_bundle() -> dict:
    """单事件自洽包（本地链 seq=1），供导入范围与隔离夹具使用。"""
    event = forged_chain_event(source="local", trace="task/T301-good", seq=1, prev_hash="",
                               ts="2026-01-02T09:00:00.000Z", step="good", status="ok")
    return {"schema_version": 1, "origin": {}, "events": [event], "anchors": [], "artifacts": [],
            "chains": [{"source": "local", "trace_id": "task/T301-good",
                        "head_hash": event["hash"]}], "findings": []}


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-events-io-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.fresh_repo("app")
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", new=_Clock())  # 递增冻结时钟，ts 可比
        clock.start()
        self.addCleanup(clock.stop)
        self.db_path = events_db.db_path()
        self.assertIsNotNone(self.db_path)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("HARNESS_EVENTS", "CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME", *GITHUB_KEYS):
            os.environ.pop(key, None)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    def fresh_repo(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir()
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **GIT_ENV}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True,
                       capture_output=True, env=env)
        (path / "README.md").write_text("# fixture\n")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True,
                       capture_output=True, env=env)
        return path

    def in_repo(self, repo: Path):
        return mock.patch.object(events_db, "ROOT", repo)

    def rows_in(self, repo: Path, sql: str, params=()):
        with self.in_repo(repo):
            path = events_db.db_path()
            if path is None or not path.exists():
                return []
            with contextlib.closing(sqlite3.connect(path)) as conn:
                return conn.execute(sql, params).fetchall()

    def snapshot_in(self, repo: Path):
        """先 WAL 截断检查点再取四表逻辑行与主库文件字节，供坏包导入前后的原子性比对。"""
        with self.in_repo(repo):
            path = events_db.db_path()
            if path is None or not path.exists():
                return None, b""
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                state = tuple(sorted(map(repr, conn.execute(f"SELECT * FROM {table}")))
                              for table in ("events", "refs", "artifacts", "anchors"))
            return state, path.read_bytes()

    def logical_in(self, repo: Path):
        """四表逻辑行快照（不比字节）：供本身要改库头（如 user_version）的隔离检查用。"""
        with self.in_repo(repo):
            path = events_db.db_path()
            if path is None or not path.exists():
                return None
            with contextlib.closing(sqlite3.connect(path)) as conn:
                return tuple(sorted(map(repr, conn.execute(f"SELECT * FROM {table}")))
                             for table in ("events", "refs", "artifacts", "anchors"))

    def seed_rich(self, trace: str) -> tuple[str, str]:
        """两条内容丰富的事件（含安全 JSON 与日志两个本地产物）加一条链头锚点。"""
        safe = events.store_artifact(SAFE_JSON)
        log = events.store_artifact(LOG_TEXT.encode("utf-8"))
        self.assertIsNotNone(safe)
        self.assertIsNotNone(log)
        first = events.emit("verify", "verify.lint", "ok", trace_id=trace, duration_ms=12,
                            inputs=[events.ref("head", "main@abc1234"),
                                    {"kind": "artifact", "ref": safe["ref"], "sha256": safe["sha256"],
                                     "size": safe["size"]}],
                            outputs={"exit": 0, "log.sha256": log["sha256"], "log.size": log["size"],
                                     "log.ref": log["ref"]},
                            decision={"by": "verify", "rule": "lint", "reason": "检查通过"},
                            actor={"role": "engine", "host": "local", "model": "fixture-model"})
        second = events.emit("guard", "guard.command", "deny", trace_id=trace,
                             inputs=[events.ref("target", "engine/core/x.py"),
                                     events.ref("dropped", "/home/x/y")],
                             outputs={"note": "/Users/x/secret.txt"},
                             decision={"by": "command_guard", "rule": "deny_force_push", "reason": "拒绝强推"},
                             error={"kind": "denied"})
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        events.set_anchor(trace, "verify", events_db.chain_head("local", trace), "run_record",
                          source="local")
        return safe["sha256"], log["sha256"]

    def seed_two_sources(self, trace: str) -> tuple[str, str]:
        """同 trace 的两条 Actions 子链（不同 run/attempt/job），各自成链、seq 都从 1 开始。"""
        first, second = "ci:9001:1:job_a", "ci:9002:2:job_b"
        emitted = [
            events.emit("verify", "ci.lint", "ok", trace_id=trace, source=first, duration_ms=5,
                        inputs=[events.ref("head", "main@abc1234")]),
            events.emit("route", "route.result", "ok", trace_id=trace, source=first,
                        decision={"by": "policy", "rule": "auto_merge", "reason": "满足自动合并"}),
            events.emit("ci", "harness.start", "ok", trace_id=trace, source=second),
            events.emit("ci", "harness.check", "fail", trace_id=trace, source=second),
            events.emit("route", "route.facts", "ok", trace_id=trace, source=second,
                        inputs=[events.ref("base", "main@abc1234"), events.ref("head", "main@def5678")]),
        ]
        self.assertEqual([bool(item) for item in emitted], [True] * 5)
        for source in (first, second):
            events.set_anchor(trace, "ci", events_db.chain_head(source, trace), "ci_artifact",
                              source=source)
        return first, second

    def assert_rejected(self, repo: Path, bundle: dict, code: str, before) -> dict:
        """坏包导入：imported/skipped 均为 0、出现指定发现、库的逻辑行与主库字节原样。"""
        with self.in_repo(repo):
            result = events_io.import_bundle(bundle)
        self.assertEqual(result["imported"], 0, result)
        self.assertEqual(result["skipped"], 0, result)
        self.assertIn(code, [finding["code"] for finding in result["findings"]], result)
        self.assertEqual(self.snapshot_in(repo), before)
        return result

    # ---- 验收 1：往返后 canonical 字段/refs/hash 一致；manifest 只含过滤的 JSON ----

    def test_export_shape_hashes_and_safe_manifest(self):
        trace = "task/T301-x"
        safe_sha, _ = self.seed_rich(trace)
        bundle = events_io.export_bundle(trace_id=trace)
        self.assertEqual(set(bundle), BUNDLE_KEYS)
        self.assertEqual(bundle["schema_version"], 1)
        origin = bundle["origin"]
        for key in ("run_id", "run_attempt", "job", "workflow_ref"):
            self.assertNotIn(key, origin)  # local 导出不伪造 Actions 键
        self.assertRegex(origin["head_sha"], r"\A[0-9a-f]{40}\Z")
        self.assertEqual(origin["head_branch"], "main")
        events_out = bundle["events"]
        self.assertEqual(len(events_out), 2)
        for event in events_out:
            self.assertEqual(set(event), EVENT_KEYS)
            # 独立重算：bundle 事件按 T101 规范化规则恢复的行与 hash 逐字一致
            self.assertEqual(canonical_digest(bundle_row(event), event["inputs"]), event["hash"])
        db_rows = self.rows_in(self.repo, "SELECT * FROM events WHERE trace_id=? ORDER BY seq", (trace,))
        self.assertEqual(len(db_rows), 2)
        rows_dicts = [dict(zip(EVENT_COLUMNS, values)) for values in db_rows]
        first = rows_dicts[0]
        self.assertEqual((events_out[0]["id"], events_out[0]["ts"], events_out[0]["seq"],
                          events_out[0]["prev_hash"], events_out[0]["hash"]),
                         (first["id"], first["ts"], first["seq"], first["prev_hash"], first["hash"]))
        self.assertEqual(events_out[0]["decision"],
                         {"by": "verify", "rule": "lint", "reason": "检查通过"})
        self.assertEqual(events_out[0]["actor"],
                         {"role": "engine", "host": "local", "model": "fixture-model"})
        self.assertEqual(events_out[0]["duration_ms"], 12)
        self.assertEqual(events_out[0]["outputs"], json.loads(first["outputs"]))
        self.assertEqual(events_out[1]["decision"]["rule"], "deny_force_push")
        self.assertEqual(events_out[1]["error"], {"kind": "denied"})
        self.assertEqual(events_out[1]["outputs"], {})
        self.assertEqual(events_out[1]["redacted"], 2)  # 路径 outputs 与路径引用各计 1
        self.assertEqual(events_out[1]["inputs"], [{"kind": "target", "ref": "engine/core/x.py"},
                                                   {"kind": "dropped"}])  # 违规值被丢，引用本体保留
        self.assertEqual(bundle["chains"],
                         [{"source": "local", "trace_id": trace, "head_hash": events_out[-1]["hash"]}])
        self.assertEqual(len(bundle["anchors"]), 1)
        self.assertEqual(bundle["anchors"][0]["head_hash"], events_out[-1]["hash"])
        self.assertEqual(bundle["anchors"][0]["fixed_in"], "run_record")
        # manifest：只收安全 JSON 产物；日志（非规范化 JSON、含命令与多行）被扫描排除并写 findings
        self.assertEqual(bundle["artifacts"],
                         [{"sha256": safe_sha, "size": len(SAFE_JSON), "file": safe_sha}])
        self.assertEqual([finding["code"] for finding in bundle["findings"]], ["artifact_unsafe"])
        # 幂等导入到干净仓库：全部字段/refs/hash 一致，id 重映射，链校验通过
        other = self.fresh_repo("roundtrip")
        with self.in_repo(other):
            self.assertIsNotNone(events.emit("verify", "pre", "ok", trace_id="other/seed"))
            self.assertEqual(events_io.import_bundle(bundle),
                             {"imported": 2, "skipped": 0, "findings": []})
            again = events_io.export_bundle(trace_id=trace)
            self.assertEqual(events_db.verify(), [])
            self.assertEqual(events_db.chain_head("local", trace), bundle["chains"][0]["head_hash"])
        self.assertNotEqual(again["events"][0]["id"], events_out[0]["id"])
        self.assertEqual([dict(event, id=None) for event in again["events"]],
                         [dict(event, id=None) for event in events_out])
        self.assertEqual(again["anchors"], bundle["anchors"])
        self.assertEqual(again["chains"], bundle["chains"])
        # 导入不带产物内容：再次导出时 manifest 如实报告缺失，不虚构
        self.assertEqual(again["artifacts"], [])
        self.assertIn("artifact_missing", [finding["code"] for finding in again["findings"]])
        # write_bundle：规范化 UTF-8 JSON 文本，扫描无库/日志/会话痕迹
        target = self.tmp / "out" / "bundle.json"
        events_io.write_bundle(bundle, target)
        raw = target.read_bytes()
        self.assertEqual(raw, json.dumps(bundle, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8"))
        text = raw.decode("utf-8")
        for banned in ("SQLite format", "harness.db", ".db", "tee /tmp/x.log", "second line",
                       "secret.txt", "User:", "Assistant:"):
            self.assertNotIn(banned, text)

    # ---- 验收 2：同包导入两次行数不变；两 run/attempt/job 同 trace 的 seq=1 各自存在、链各自完整 ----

    def test_reimport_is_idempotent_and_sources_isolated(self):
        trace = "task/T301-y"
        first, second = self.seed_two_sources(trace)
        bundle = events_io.export_bundle(trace_id=trace)
        self.assertEqual(len(bundle["events"]), 5)
        self.assertEqual([chain["source"] for chain in bundle["chains"]], [first, second])
        for chain in bundle["chains"]:
            self.assertEqual(chain["head_hash"], events_db.chain_head(chain["source"], trace))
        self.assertEqual(len(bundle["anchors"]), 2)
        other = self.fresh_repo("isolated")
        with self.in_repo(other):
            self.assertEqual(events_io.import_bundle(bundle),
                             {"imported": 5, "skipped": 0, "findings": []})
            counts = self._table_counts()
            self.assertEqual(counts["events"], 5)
            self.assertEqual(counts["refs"], 3)
            self.assertEqual(counts["anchors"], 2)
            # 同包再导入：全部按哈希去重，行数不变
            self.assertEqual(events_io.import_bundle(bundle),
                             {"imported": 0, "skipped": 5, "findings": []})
            self.assertEqual(self._table_counts(), counts)
            # 两条链的 seq=1 各自存在且内容就是包里的事件，未统一 source、未串链
            for chain in bundle["chains"]:
                rows = self.rows_in(other, "SELECT hash FROM events WHERE source=? AND seq=1",
                                    (chain["source"],))
                self.assertEqual(len(rows), 1)
                seq1 = next(event for event in bundle["events"]
                            if event["source"] == chain["source"] and event["seq"] == 1)
                self.assertEqual(rows[0][0], seq1["hash"])
                self.assertEqual(events_db.verify(source=chain["source"]), [])
            self.assertEqual(events_db.verify(), [])
        # 来源过滤导出：前缀与精确链匹配
        self.assertEqual([event["source"] for event in events_io.export_bundle(source="ci")["events"]],
                         [first, first, second, second, second])
        self.assertEqual(len(events_io.export_bundle(source=second)["events"]), 3)

    def _table_counts(self) -> dict:
        """当前 ROOT 下库的四表行数（必须在 in_repo 上下文内调用）。"""
        with contextlib.closing(sqlite3.connect(events_db.db_path())) as conn:
            return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("events", "refs", "artifacts", "anchors")}

    # ---- 验收 3：坏哈希、缺中间项、冲突 seq、未来 schema、隐私禁项各自失败且原库不动 ----

    def test_bad_bundle_is_atomic_and_not_repaired(self):
        trace = "task/T301-z"
        self.seed_rich(trace)
        # 先补一条，让链有中间项可删
        self.assertIsNotNone(events.emit("verify", "verify.extra", "ok", trace_id=trace))
        good = events_io.export_bundle(trace_id=trace)
        self.assertEqual(len(good["events"]), 3)
        empty = self.fresh_repo("empty")
        full = self.fresh_repo("full")
        with self.in_repo(full):
            self.assertEqual(events_io.import_bundle(good),
                             {"imported": 3, "skipped": 0, "findings": []})
        full_before = self.snapshot_in(full)
        empty_before = self.snapshot_in(empty)

        # 坏哈希：内容没动，hash 改一个字符
        bad_hash = copy.deepcopy(good)
        bad_hash["events"][1]["hash"] = "0" * 64
        self.assert_rejected(empty, bad_hash, "hash_mismatch", empty_before)
        # 篡改内容不重算 hash
        tampered = copy.deepcopy(good)
        tampered["events"][1]["status"] = "fail"
        self.assert_rejected(empty, tampered, "hash_mismatch", empty_before)
        # 重算 hash 掩盖篡改：内容与 hash 自洽，但链头不再等于 bundle.chains 声明
        masked = copy.deepcopy(good)
        event = masked["events"][-1]
        event["status"] = "fail"
        event["hash"] = canonical_digest(bundle_row(event), event["inputs"])
        self.assert_rejected(empty, masked, "chain_head_mismatch", empty_before)
        # 缺中间项：导出完整前缀被删掉 seq=2 后不是可导入包
        gapped = copy.deepcopy(good)
        del gapped["events"][1]
        self.assert_rejected(empty, gapped, "chain_gap", empty_before)
        # 未来 schema：只读不写
        future = copy.deepcopy(good)
        future["schema_version"] = 2
        self.assert_rejected(empty, future, "future_schema", empty_before)
        # 隐私禁项：emit 会过滤的路径与换行出现在包里即拒绝
        privacy_path = copy.deepcopy(good)
        privacy_path["events"][0]["decision"]["reason"] = "/Users/x/secret"
        self.assert_rejected(empty, privacy_path, "privacy", empty_before)
        privacy_newline = copy.deepcopy(good)
        privacy_newline["events"][0]["step"] = "a\nb"
        self.assert_rejected(empty, privacy_newline, "privacy", empty_before)
        # 伪造锚点：head_hash 不在链前缀内
        bad_anchor = copy.deepcopy(good)
        bad_anchor["anchors"][0]["head_hash"] = "f" * 64
        self.assert_rejected(empty, bad_anchor, "anchor_mismatch", empty_before)
        # 来源 schema：ci: 前缀必须带全 run/attempt/job 三键（C2）
        bad_source_event = forged_chain_event(source="ci:123:1", trace="task/T301-bad-src", seq=1,
                                              prev_hash="", ts="2026-01-02T09:00:00.000Z",
                                              step="forged", status="ok")
        bad_source = {"schema_version": 1, "origin": {}, "events": [bad_source_event],
                      "anchors": [], "artifacts": [],
                      "chains": [{"source": "ci:123:1", "trace_id": "task/T301-bad-src",
                                  "head_hash": bad_source_event["hash"]}], "findings": []}
        self.assert_rejected(empty, bad_source, "schema", empty_before)
        # 冲突 seq：同 source/trace/seq 不同 hash，原库不覆盖
        clash_source, clash_trace = good["events"][0]["source"], good["events"][0]["trace_id"]
        clash = {"schema_version": 1, "origin": {},
                 "events": [forged_chain_event(source=clash_source, trace=clash_trace, seq=1,
                                               prev_hash="", ts="2026-01-02T09:00:00.000Z",
                                               step="forged", status="ok")],
                 "anchors": [], "artifacts": [],
                 "chains": [{"source": clash_source, "trace_id": clash_trace,
                             "head_hash": "TBD"}], "findings": []}
        clash["chains"][0]["head_hash"] = clash["events"][0]["hash"]
        self.assert_rejected(full, clash, "seq_conflict", full_before)
        # 正面对照：同样的坏包流程后，原包仍可完整导入干净库，原库重导仍全部去重
        self.assertEqual(self.snapshot_in(full), full_before)
        with self.in_repo(empty):
            self.assertEqual(events_io.import_bundle(good),
                             {"imported": 3, "skipped": 0, "findings": []})
        with self.in_repo(full):
            self.assertEqual(events_io.import_bundle(good),
                             {"imported": 0, "skipped": 3, "findings": []})

    # ---- 验收 4：since/stage 只缩小展示；导出仍是完整前缀并核对链头 ----

    def test_filtered_display_does_not_export_partial_chain(self):
        trace = "task/T301-w"
        for step, status in (("verify.a", "ok"), ("guard.b", "deny"), ("verify.c", "ok")):
            self.assertIsNotNone(events.emit(step.split(".")[0], step, status, trace_id=trace))
        timeline = self.rows_in(self.repo, "SELECT seq, ts FROM events WHERE trace_id=? ORDER BY seq",
                                (trace,))
        self.assertEqual([seq for seq, _ in timeline], [1, 2, 3])
        _, ts2 = timeline[1]
        since_dt = datetime.fromisoformat(ts2)

        def seqs(items):
            return [event["seq"] for event in items]

        self.assertEqual(seqs(events_io.query(trace_id=trace, stage="verify")), [1, 3])
        self.assertEqual(seqs(events_io.query(trace_id=trace, status="deny")), [2])
        self.assertEqual(seqs(events_io.query(trace_id=trace, source="local")), [1, 2, 3])
        self.assertEqual(events_io.query(trace_id=trace, source="ci"), [])
        self.assertEqual(seqs(events_io.query(trace_id=trace, since=ts2)), [2, 3])  # 含边界
        self.assertEqual(seqs(events_io.query(trace_id=trace, since=ts2, stage="verify")), [3])
        expected = [seq for seq, ts in timeline
                    if datetime.fromisoformat(ts) >= since_dt]
        self.assertEqual(expected, [2, 3])
        self.assertEqual(seqs(events_io.query(trace_id=trace, since=since_dt)), expected)
        self.assertEqual(events_io.query(trace_id="task/none"), [])
        with self.assertRaises(ValueError):
            events_io.query(since="not-a-time")
        # 展示结果是事件列表，不是 bundle
        self.assertNotIn("schema_version", events_io.query(trace_id=trace)[0])
        # 导出不受展示过滤影响：完整前缀 + 链头，导入后链校验通过
        bundle = events_io.export_bundle(trace_id=trace)
        self.assertEqual(len(bundle["events"]), 3)
        head = bundle["chains"][0]["head_hash"]
        self.assertEqual(head, dict(zip(EVENT_COLUMNS, self.rows_in(
            self.repo, "SELECT * FROM events WHERE trace_id=? ORDER BY seq DESC LIMIT 1",
            (trace,))[0]))["hash"])
        other = self.fresh_repo("filtered-import")
        with self.in_repo(other):
            self.assertEqual(events_io.import_bundle(bundle),
                             {"imported": 3, "skipped": 0, "findings": []})
            self.assertEqual(events_db.verify(), [])
            self.assertEqual(events_db.chain_head("local", trace), head)
        # 筛选后的断链子集当包导入：拒绝
        partial = copy.deepcopy(bundle)
        del partial["events"][0]
        empty = self.fresh_repo("filtered-empty")
        self.assert_rejected(empty, partial, "chain_gap", self.snapshot_in(empty))
        # 无库环境：查询明确为空，导出为空包
        bare = self.fresh_repo("bare")
        with self.in_repo(bare):
            self.assertEqual(events_io.query(), [])
            nothing = events_io.export_bundle()
        self.assertEqual((nothing["events"], nothing["chains"], nothing["anchors"],
                          nothing["artifacts"]), ([], [], [], []))

    # ---- C5 合同：load_ci 由 T302 实现，T301 不留成功的假实现 ----

    def test_load_ci_is_contract_only(self):
        with self.assertRaises(NotImplementedError):
            events_io.load_ci(1)
        with self.assertRaises(NotImplementedError):
            events_io.load_ci(1, head="a" * 40, gh="gh")

    # ---- C0/C5 失败隔离与范围：读不建库、写原子、不碰 artifacts 范围、本地新 schema 不写 ----

    def test_import_scope_and_readonly_isolation(self):
        # 无库环境：读与导出都不得创建 harness.db
        bare = self.fresh_repo("scope-bare")
        with self.in_repo(bare):
            self.assertEqual(events_io.query(), [])
            self.assertEqual(events_io.export_bundle()["events"], [])
            self.assertFalse(events_db.db_path().exists())
        # 存储层直接写入撞唯一约束：整体回滚，半包不落盘
        row = {"ts": "2026-01-02T03:00:00.000Z", "source": "local", "trace_id": "task/T301-scope",
               "seq": 1, "prev_hash": "", "hash": "a" * 64, "stage": "verify", "step": "x",
               "status": "ok", "duration_ms": None, "actor_role": None, "actor_host": None,
               "model": None, "decision_by": None, "decision_rule": None, "decision_reason": None,
               "error_kind": None, "error_signature": None, "outputs": "{}",
               "engine_version": "0.0.0", "redacted": 0}
        with self.assertRaises(sqlite3.IntegrityError):
            events_db.import_rows([(dict(row), []), (dict(row), [])], [])
        self.assertEqual(self.rows_in(self.repo, "SELECT count(*) FROM events")[0][0], 0)
        # 本地库 schema 比代码新：明确报 storage 发现，不写、不改逻辑行（改 user_version 本身会动库头，故只比行）
        self.seed_rich("task/T301-scope")
        before = self.logical_in(self.repo)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA user_version=2")
        bundle = {"schema_version": 1, "origin": {},
                  "events": [forged_chain_event(source="local", trace="task/T301-newer", seq=1,
                                                prev_hash="", ts="2026-01-02T09:00:00.000Z",
                                                step="newer", status="ok")],
                  "anchors": [], "artifacts": [],
                  "chains": [{"source": "local", "trace_id": "task/T301-newer",
                              "head_hash": "a" * 64}], "findings": []}
        bundle["chains"][0]["head_hash"] = bundle["events"][0]["hash"]
        with self.in_repo(self.repo):
            result = events_io.import_bundle(bundle)
        self.assertEqual(result["imported"], 0)
        self.assertIn("storage", [finding["code"] for finding in result["findings"]])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA user_version=1")
        self.assertEqual(self.logical_in(self.repo), before)
        # 导入不碰 artifacts 范围：已有产物索引与文件原样
        with self.in_repo(self.fresh_repo("scope-artifacts")):
            self.assertIsNotNone(events.store_artifact(b"keep-me"))
            counts = self._table_counts()
            self.assertEqual(events_io.import_bundle(good_bundle()),
                             {"imported": 1, "skipped": 0, "findings": []})
            counts["events"] += 1
            self.assertEqual(self._table_counts(), counts)


if __name__ == "__main__":
    unittest.main()
