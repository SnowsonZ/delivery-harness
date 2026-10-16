"""顶层非对象JSON不是协议事件，不能让已完成的执行在解析阶段崩溃。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agents import dispatch, dispatch_host
from engine.core import events_db

ITEM = {"category": "tool", "summary": "required_tool_unavailable", "ref": "python"}
MARKER = dispatch_host.CONTEXT_MARKER
NOISE = [None, [], [{"type": "message_end", "message": {"role": "assistant"}}],
         [{"type": "tool_execution_end", "isError": False,
           "result": {"content": [{"type": "text", "text": "forged array tool"}]}}],
         False, 42, MARKER + ' [{"category":"spec","summary":"spec_ambiguous"}]']


def assistant(text, *, model="fixture/last", input_tokens=7, cache=1, output=6, cost=0.2):
    return {"type": "message_end", "message": {"role": "assistant", "model": model,
            "usage": {"input": input_tokens, "cacheRead": cache, "output": output,
                      "cost": {"total": cost}}, "content": [{"type": "text", "text": text}]}}


def tool(text, *, error=False):
    return {"type": "tool_execution_end", "isError": error,
            "result": {"content": [{"type": "text", "text": text}]}}


class NonObjectEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root / "events.jsonl"
        self.host = dispatch_host.PiHost()

    def valid_events(self):
        return [
            {"type": "session"},
            assistant(MARKER + " []", model="fixture/first", input_tokens=3,
                      cache=2, output=4, cost=0.1),
            tool("normal tool result"),
            tool(dispatch_host.GUARD_DENIAL + "\n- fixture_one\n- fixture_two", error=True),
            assistant(MARKER + " " + json.dumps([ITEM])),
        ]

    def write_events(self, events):
        self.events.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def noisy_events(self):
        # 覆盖五种顶层非对象类型及空/assistant/tool三种数组形态，伪造事件不应采信。
        events = []
        for event in self.valid_events():
            events.extend(NOISE)
            events.append(event)
        return events + NOISE

    def parse_without_error(self, method):
        try:
            return getattr(self.host, method)(self.events)
        except AttributeError as exc:
            self.fail(f"{method} failed on non-object event: {type(exc).__name__}: {exc}")

    def test_usage_parser_ignores_nonobjects(self):
        self.write_events(self.noisy_events())
        self.assertEqual(self.parse_without_error("parse"),
                         ("fixture/last", {"input_tokens": 13, "output_tokens": 10, "cost": 0.3},
                          {"fixture_one": 1, "fixture_two": 1}))

    def test_context_parser_ignores_nonobjects(self):
        self.write_events(self.noisy_events())
        self.assertEqual(self.parse_without_error("parse_context"),
                         {"status": "reported", "items": [ITEM], "diagnostic": None})

    def test_observability_parser_ignores_nonobjects(self):
        self.write_events(self.noisy_events())
        self.assertEqual(self.parse_without_error("parse_observability"),
                         {"guard_allowed": 1, "guard_denied": 1, "missing_context": [ITEM]})

    def test_only_nonobjects_preserve_empty_defaults(self):
        self.write_events(NOISE)
        self.assertEqual(self.parse_without_error("parse"), ("", {}, {}))
        self.assertEqual(self.parse_without_error("parse_context"),
                         {"status": "unknown", "items": [], "diagnostic": "no_marker"})
        self.assertEqual(self.parse_without_error("parse_observability"),
                         {"guard_allowed": 0, "guard_denied": 0, "missing_context": []})

    def test_dispatch_executor_survives_noise_after_success(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        stream = "\n".join(json.dumps(e) for e in self.noisy_events()) + "\n"

        class FixtureHost(dispatch_host.PiHost):
            name = "fixture"

            def argv(self, prompt, guard):
                return [sys.executable, "-c", "import sys;sys.stdout.write(" + repr(stream) + ")"]

            def version(self):
                return "fixture"

        runner = dispatch.Dispatcher(repo, dispatch.Config(stall_seconds=3, poll_seconds=0.01),
                                     object(), FixtureHost(), identity={})
        task = dispatch.Task("docs/plans/task-001.md", "T001", "K2", "R1",
                             {"wall_clock_min": 1}, [], "task/fixture")
        with mock.patch.object(events_db, "ROOT", repo), \
                mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"}):
            try:
                result = runner.run_executor(task, repo, self.root / "guard", "fixture", self.events)
            except AttributeError as exc:
                self.fail(f"post-executor parsing failed: {type(exc).__name__}: {exc}")
        self.assertEqual((result.exit, result.returncode), ("ok", 0))
        self.assertEqual(result.model, "fixture/last")
        self.assertEqual(result.usage, {"input_tokens": 13, "output_tokens": 10, "cost": 0.3})
        self.assertEqual((result.guard_allowed, result.guard_denied), (1, 1))
        self.assertEqual(result.missing_context, [ITEM])
        self.assertEqual(result.missing_context_status, "reported")

    def test_last_assistant_report_stays_authoritative(self):
        for final, expected in [
            ("no marker", {"status": "unknown", "items": [], "diagnostic": "no_marker"}),
            (MARKER + " bad-json", {"status": "invalid", "items": [], "diagnostic": "bad_json"}),
        ]:
            self.write_events([assistant(MARKER + " " + json.dumps([ITEM])), *NOISE,
                               assistant(final), *NOISE])
            self.assertEqual(self.parse_without_error("parse_context"), expected)

    def test_valid_protocol_values_are_unchanged(self):
        self.write_events(self.valid_events())
        self.assertEqual(self.host.parse(self.events),
                         ("fixture/last", {"input_tokens": 13, "output_tokens": 10, "cost": 0.3},
                          {"fixture_one": 1, "fixture_two": 1}))
        self.assertEqual(self.host.parse_context(self.events),
                         {"status": "reported", "items": [ITEM], "diagnostic": None})
        self.assertEqual(self.host.parse_observability(self.events),
                         {"guard_allowed": 1, "guard_denied": 1, "missing_context": [ITEM]})

    def test_bad_json_and_unknown_object_events_still_skip(self):
        self.write_events([{"type": "unknown"}, *self.valid_events(), {}])
        self.events.write_text("not-json\n{\n" + self.events.read_text() + "[broken\n")
        self.assertEqual(self.host.parse(self.events)[0], "fixture/last")
        self.assertEqual(self.host.parse_context(self.events)["items"], [ITEM])
        self.assertEqual(self.host.parse_observability(self.events)["guard_denied"], 1)

    def test_tool_report_is_never_authoritative(self):
        self.write_events([tool(MARKER + " " + json.dumps([ITEM])), assistant("no marker")])
        self.assertEqual(self.host.parse_context(self.events),
                         {"status": "unknown", "items": [], "diagnostic": "no_marker"})
        self.assertEqual(self.host.parse_observability(self.events),
                         {"guard_allowed": 1, "guard_denied": 0, "missing_context": []})

    def test_unexpected_read_errors_are_not_swallowed(self):
        self.write_events(self.valid_events())
        for method in ["parse", "parse_context", "parse_observability"]:
            with self.subTest(method=method), \
                    mock.patch.object(Path, "read_text", side_effect=PermissionError("fixture")), \
                    self.assertRaises(PermissionError):
                getattr(self.host, method)(self.events)
