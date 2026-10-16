"""B131：events/trace按时间点排序；保留时间戳原文、链与只读查询语义。"""
from __future__ import annotations

from unittest import TestCase, mock

from engine.core import events, events_db, events_io
from tests import test_trace_events_cli as fixture


class TraceTimeOrderTest(TestCase):
    fresh_repo = fixture.ObservabilityTaskTest.fresh_repo
    run_cli = fixture.ObservabilityTaskTest.run_cli
    json_cli = fixture.ObservabilityTaskTest.json_cli
    write_record = fixture.ObservabilityTaskTest.write_record

    def setUp(self):
        fixture.ObservabilityTaskTest.setUp(self)
        self.trace_id = "task/777-time-order"
        patch = mock.patch.dict("os.environ", {"HARNESS_EVENTS": "on"})
        patch.start()
        self.addCleanup(patch.stop)

    def emit_at(self, stamp, step, *, source="local", status="fail", duration=10):
        with mock.patch.object(events_db, "_now", return_value=stamp):
            event_id = events.emit("verify", step, status, trace_id=self.trace_id,
                                   source=source, duration_ms=duration)
        self.assertIsNotNone(event_id)
        return event_id

    def seed_offsets(self):
        self.emit_at("2026-10-16T10:00:00+08:00", "early", duration=10)
        self.emit_at("2026-10-16T02:30:00Z", "late", duration=30)

    def test_trace_offset_order_and_first_failure(self):
        self.seed_offsets()
        view = self.json_cli("trace", self.trace_id)
        self.assertEqual([row["step"] for row in view["events"]], ["early", "late"])
        self.assertEqual(view["first_failure"]["step"], "early")
        self.assertEqual(view["longest_stage"], {"stage": "verify", "duration_ms": 40})
        self.assertEqual(view["events"][0]["ts"], "2026-10-16T10:00:00+08:00")

    def test_events_offset_order_json_and_text(self):
        self.seed_offsets()
        view = self.json_cli("events")
        self.assertEqual([row["step"] for row in view["events"]], ["early", "late"])
        code, output, _ = self.run_cli("events")
        self.assertEqual(code, 0)
        self.assertLess(output.index("verify/early"), output.index("verify/late"))

    def test_fractional_and_second_zulu_order(self):
        self.emit_at("2026-10-16T02:00:00Z", "whole")
        self.emit_at("2026-10-16T02:00:00.100Z", "fraction")
        for command in [("events",), ("trace", self.trace_id)]:
            with self.subTest(command=command):
                view = self.json_cli(*command)
                self.assertEqual([row["step"] for row in view["events"]], ["whole", "fraction"])

    def test_segment_order_and_bounds_use_actual_time(self):
        self.emit_at("2026-10-16T10:00:00+08:00", "early", source="ci:100:1:one")
        self.emit_at("2026-10-16T02:30:00Z", "late", source="ci:200:1:two")
        self.emit_at("2026-10-16T03:00:00Z", "last", source="ci:100:1:one")
        view = self.json_cli("trace", self.trace_id)
        self.assertEqual([s["segment"] for s in view["segments"]], ["ci:100:1:one", "ci:200:1:two"])
        self.assertEqual((view["segments"][0]["start"], view["segments"][0]["end"]),
                         ("2026-10-16T10:00:00+08:00", "2026-10-16T03:00:00Z"))
        self.assertEqual([row["step"] for row in view["events"]], ["early", "late", "last"])

    def test_same_instant_ties_keep_source_then_sequence(self):
        self.emit_at("2026-10-16T02:00:00Z", "source_a", source="ci:100:1:one")
        self.emit_at("2026-10-16T02:00:00.000+00:00", "source_b", source="ci:200:1:two")
        self.emit_at("2026-10-16T10:00:00+08:00", "source_a_second", source="ci:100:1:one")
        for command in [("events",), ("trace", self.trace_id)]:
            with self.subTest(command=command):
                view = self.json_cli(*command)
                self.assertEqual([row["step"] for row in view["events"]],
                                 ["source_a", "source_a_second", "source_b"])

    def test_import_accepts_forms_and_display_preserves_chains(self):
        self.seed_offsets()
        package = events_io.export_bundle()
        imported = events_io.import_bundle(package)
        self.assertEqual(imported["findings"], [])
        self.assertEqual(imported["imported"], 0)
        self.assertEqual(imported["skipped"], 2)
        before = events_io.export_bundle()
        view = self.json_cli("trace", self.trace_id)
        self.assertEqual(view["first_failure"]["step"], "early")
        self.assertEqual(events_io.export_bundle(), before)
        self.assertEqual(events.verify_chain(self.trace_id, "local"), [])

    def test_canonical_times_filter_counts_and_read_only(self):
        self.emit_at("2026-10-16T02:00:00.000Z", "ok", status="ok")
        self.emit_at("2026-10-16T02:01:00.000Z", "deny", status="deny")
        before = events_io.export_bundle()
        view = self.json_cli("events", "--since", "2026-10-16T10:00:30+08:00", "--status", "deny")
        self.assertEqual((view["count"], view["total"]), (1, 2))
        self.assertEqual(view["counts"], {"stage": {"verify": 1}, "status": {"deny": 1}})
        self.assertEqual(view["events"][0]["step"], "deny")
        trace = self.json_cli("trace", self.trace_id)
        self.assertEqual(trace["first_failure"]["status"], "deny")
        self.assertEqual(events_io.export_bundle(), before)

    def test_naive_utc_and_negative_offset_order(self):
        self.emit_at("2026-10-16T02:00:00", "naive")
        self.emit_at("2026-10-15T22:30:00-04:00", "later")
        view = self.json_cli("trace", self.trace_id)
        self.assertEqual([row["step"] for row in view["events"]], ["naive", "later"])
        self.assertEqual(view["first_failure"]["step"], "naive")

    def test_unparseable_historical_string_does_not_crash(self):
        for step in ["valid", "unknown_first", "unknown_second"]:
            self.emit_at("2026-10-16T02:00:00Z", step, source="ci:777:1:fixture")
        rows = events_io.query(trace_id=self.trace_id)
        rows[1]["ts"], rows[2]["ts"] = "historical-z", "historical-a"
        with mock.patch.object(events_io, "query", return_value=list(reversed(rows))):
            for command in [("events",), ("trace", self.trace_id)]:
                with self.subTest(command=command):
                    view = self.json_cli(*command)
                    self.assertEqual([row["step"] for row in view["events"]],
                                     ["valid", "unknown_first", "unknown_second"])
                    self.assertEqual([row["ts"] for row in view["events"]][1:],
                                     ["historical-z", "historical-a"])

    def check_year_boundary(self, stamps, expected):
        for source in ["ci:777:1:fixture", "local"]:
            self.trace_id = "task/777-boundary-" + source
            for step, stamp in stamps:
                self.emit_at(stamp, step, source=source)
            before = events_io.export_bundle()
            for command in [("events",), ("trace", self.trace_id)]:
                with self.subTest(source=source, command=command):
                    try:
                        view = self.json_cli(*command)
                    except OverflowError as error:
                        self.fail(f"query overflowed at valid ISO year boundary: {error}")
                    selected = [row for row in view["events"] if row["trace_id"] == self.trace_id]
                    self.assertEqual([row["step"] for row in selected], expected)
                    self.assertEqual({row["step"]: row["ts"] for row in selected}, dict(stamps))
                    if command[0] == "trace":
                        self.assertEqual(view["first_failure"]["step"], expected[0])
            self.assertEqual(events_io.export_bundle(), before)
            self.assertEqual(events.verify_chain(self.trace_id, source), [])

    def test_minimum_year_positive_offset_display(self):
        self.check_year_boundary([("floor", "0001-01-01T00:00:00Z"),
                                  ("before_floor", "0001-01-01T00:00:00+01:00")],
                                 ["before_floor", "floor"])

    def test_maximum_year_negative_offset_display(self):
        self.check_year_boundary([("after_ceiling", "9999-12-31T23:59:59-01:00"),
                                  ("ceiling", "9999-12-31T23:59:59Z")],
                                 ["ceiling", "after_ceiling"])

    def test_empty_trace_and_existing_argument_errors(self):
        view = self.json_cli("trace", self.trace_id)
        self.assertEqual((view["events"], view["segments"], view["first_failure"]), ([], [], None))
        code, _, _ = self.run_cli("events", "--since", "not-a-time")
        self.assertEqual(code, 2)
