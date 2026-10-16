"""B132候选：--since超界输入返回2；有效筛选与只读语义保持。"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest import TestCase, mock

from engine.core import events, events_db, events_io
from engine.reports import trace
from tests import test_trace_events_cli as fixture

MIN_OFFSET = "0001-01-01T00:00:00+01:00"
MAX_OFFSET = "9999-12-31T23:59:59-01:00"
BAD_DURATIONS = ("1000000000d", "999999999d")


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 16, 12, tzinfo=UTC)
        return value if tz is None else value.astimezone(tz)


class SinceInputBoundaryTest(TestCase):
    fresh_repo = fixture.ObservabilityTaskTest.fresh_repo
    run_cli = fixture.ObservabilityTaskTest.run_cli
    json_cli = fixture.ObservabilityTaskTest.json_cli

    def setUp(self):
        fixture.ObservabilityTaskTest.setUp(self)
        for patch in [mock.patch.dict("os.environ", {"HARNESS_EVENTS": "on"}),
                      mock.patch.object(events, "_maybe_cleanup", return_value=None),
                      mock.patch.object(trace, "datetime", FrozenDatetime)]:
            patch.start()
            self.addCleanup(patch.stop)

    def emit_at(self, stamp, step, status="ok"):
        with mock.patch.object(events_db, "_now", return_value=stamp):
            self.assertIsNotNone(events.emit("verify", step, status, trace_id="task/777-since"))

    def reject_input(self, value):
        for suffix in [(), ("--json",)]:
            with (self.subTest(value=value, suffix=suffix),
                  mock.patch.object(events_io, "query", wraps=events_io.query) as query):
                try:
                    code, out, err = self.run_cli("events", "--since", value, *suffix)
                except OverflowError as error:
                    self.fail(f"--since输入超界异常逃出CLI：{error}")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--since", err)
                self.assertNotIn("Traceback", err)
                query.assert_not_called()
        try:
            trace._parse_since(value)
        except OverflowError as error:
            self.fail(f"解析器未将超界转换为ValueError：{error}")
        except ValueError:
            pass
        else:
            self.fail("不可表示的输入必须报ValueError")

    def test_minimum_year_offset_is_cli_error(self):
        self.reject_input(MIN_OFFSET)

    def test_maximum_year_offset_is_cli_error(self):
        self.reject_input(MAX_OFFSET)

    def test_duration_construction_overflow_is_cli_error(self):
        self.reject_input(BAD_DURATIONS[0])

    def test_duration_subtraction_underflow_is_cli_error(self):
        self.reject_input(BAD_DURATIONS[1])

    def test_valid_relative_units_keep_inclusive_boundary(self):
        cases = [("1d", "2026-10-15T12:00:00Z", "2026-10-15T11:59:59Z"),
                 ("12h", "2026-10-16T00:00:00Z", "2026-10-15T23:59:59Z"),
                 ("60m", "2026-10-16T11:00:00Z", "2026-10-16T10:59:59Z"),
                 ("30s", "2026-10-16T11:59:30Z", "2026-10-16T11:59:29Z")]
        for index, (value, boundary, earlier) in enumerate(cases):
            self.emit_at(earlier, f"old-{index}")
            self.emit_at(boundary, f"boundary-{index}")
        for index, (value, boundary, _) in enumerate(cases):
            with self.subTest(value=value):
                view = self.json_cli("events", "--since", value)
                self.assertIn(f"boundary-{index}", [row["step"] for row in view["events"]])
                self.assertNotIn(f"old-{index}", [row["step"] for row in view["events"]])
                self.assertEqual(view["total"], 8)
                self.assertEqual(trace._parse_since(value).isoformat(),
                                 datetime.fromisoformat(boundary).isoformat())

    def test_valid_iso_representations_keep_filter_counts(self):
        self.emit_at("2026-10-16T01:59:59Z", "old")
        self.emit_at("2026-10-16T02:00:00Z", "boundary", "deny")
        self.emit_at("2026-10-16T02:30:00Z", "later", "deny")
        for value in ["2026-10-16T02:00:00Z", "2026-10-16T10:00:00+08:00",
                      "2026-10-15T22:00:00-04:00", "2026-10-16T02:00:00",
                      " 2026-10-16T02:00:00.000+00:00 "]:
            with self.subTest(value=value):
                view = self.json_cli("events", "--since", value, "--status", "deny")
                self.assertEqual([row["step"] for row in view["events"]], ["boundary", "later"])
                self.assertEqual((view["count"], view["total"]), (2, 3))
                self.assertEqual(view["counts"], {"stage": {"verify": 2}, "status": {"deny": 2}})

    def test_representable_native_year_boundaries_remain_valid(self):
        self.emit_at("2026-10-16T02:00:00Z", "middle")
        for value, count in [("0001-01-01T00:00:00Z", 1),
                             ("0001-01-01T01:00:00+01:00", 1),
                             ("9999-12-31T23:59:59Z", 0),
                             ("9999-12-31T22:59:59-01:00", 0)]:
            with self.subTest(value=value):
                self.assertEqual(self.json_cli("events", "--since", value)["count"], count)

    def test_malformed_inputs_keep_cli_error(self):
        for value in ["not-a-time", "1.5d", "1w", "2026-02-30T00:00:00Z"]:
            self.reject_input(value)

    def test_valid_and_invalid_queries_preserve_bundle_and_chain(self):
        self.emit_at("2026-10-16T02:00:00Z", "keep", "fail")
        before = events_io.export_bundle()
        self.reject_input(MIN_OFFSET)
        self.json_cli("events", "--since", "2026-10-16T02:00:00Z")
        self.assertEqual(events_io.export_bundle(), before)
        self.assertEqual(events.verify_chain("task/777-since", "local"), [])

    def test_query_failures_still_propagate(self):
        for error in [OSError("query read failed"), OverflowError("downstream overflow")]:
            with (self.subTest(error=type(error).__name__),
                  mock.patch.object(events_io, "query", side_effect=error)):
                with self.assertRaises(type(error)) as caught:
                    self.run_cli("events", "--since", "2026-10-16T02:00:00Z")
                self.assertIs(caught.exception, error)
