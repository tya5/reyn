"""Tests for the --from-attractor mode in scripts/llm_replay.py.

Tier 2: OS invariant — verifies that --from-attractor correctly detects
attractors and replays each one, including heuristic filtering, first-N
limiting, zero-attractor graceful exit, summary output, and backward
compatibility of the non-attractor path.

Testing policy compliance:
- No MagicMock / AsyncMock / patch.  Real callable stubs only.
- No private-state assertions.
- No algorithm-level pins (sort order, dict iteration order, exact whitespace).
- Tier declared in each docstring's first line.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------
from pathlib import Path as _Path
from typing import Any

_SCRIPTS_DIR = _Path(__file__).parent.parent / "scripts"


def _import_replay():
    """Import scripts/llm_replay.py as a module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llm_replay", _SCRIPTS_DIR / "llm_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Real callable LLM stub (no mocks)
# ---------------------------------------------------------------------------


def _make_resp(
    content: str | None = "ok",
    tool_calls: list | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 100,
    completion_tokens: int = 10,
) -> Any:
    msg = type("_Msg", (), {
        "content": content,
        "tool_calls": tool_calls or [],
    })()
    choice = type("_Choice", (), {
        "message": msg,
        "finish_reason": finish_reason,
    })()
    usage = type("_Usage", (), {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    })()
    return type("_Resp", (), {"choices": [choice], "usage": usage})()


class _CountingLLM:
    """Real async callable stub that counts calls and returns a fixed response."""

    def __init__(
        self,
        content: str | None = "replay result",
        finish_reason: str = "stop",
        completion_tokens: int = 10,
    ) -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.completion_tokens = completion_tokens
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _make_resp(
            content=self.content,
            finish_reason=self.finish_reason,
            completion_tokens=self.completion_tokens,
        )


# ---------------------------------------------------------------------------
# Trace fixture builders
# ---------------------------------------------------------------------------


def _req_record(
    request_id: str,
    *,
    system_text: str = "",
    tools: list[dict] | None = None,
    timestamp: str = "2026-01-01T00:00:00+00:00",
) -> dict:
    messages: list[dict] = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": "go"})
    return {
        "kind": "request",
        "request_id": request_id,
        "timestamp": timestamp,
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "router",
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto" if tools else None,
        "sampling_params": {},
    }


def _resp_record(
    request_id: str,
    *,
    finish_reason: str = "stop",
    content: str | None = "all good",
    completion_tokens: int = 20,
    timestamp: str = "2026-01-01T00:00:01+00:00",
) -> dict:
    return {
        "kind": "response",
        "request_id": request_id,
        "timestamp": timestamp,
        "content": content,
        "tool_calls": [],
        "finish_reason": finish_reason,
        "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens},
    }


def _attractor_request(rid: str, *, timestamp: str = "2026-01-01T00:00:00+00:00") -> dict:
    """Build a request record that will trigger stop_with_must_rule detection."""
    return _req_record(
        rid,
        system_text="You MUST call describe_skill before invoke_skill.",
        timestamp=timestamp,
    )


def _attractor_response(rid: str, *, timestamp: str = "2026-01-01T00:00:01+00:00") -> dict:
    """Build a response record that matches the attractor pattern (empty stop)."""
    return _resp_record(
        rid,
        finish_reason="stop",
        content="",
        completion_tokens=0,
        timestamp=timestamp,
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# (a) --from-attractor replays all attractors
# ---------------------------------------------------------------------------


class TestFromAttractorAll:
    """Tier 2: --from-attractor replays all detected attractors."""

    def test_all_attractors_replayed(self, tmp_path: Path) -> None:
        """Tier 2: with 2 attractors and n=1, the LLM stub receives exactly 2 calls."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid1 = "attractor-001"
        rid2 = "attractor-002"
        _write_jsonl(trace, [
            _attractor_request(rid1, timestamp="2026-01-01T00:00:00+00:00"),
            _attractor_response(rid1, timestamp="2026-01-01T00:00:01+00:00"),
            _attractor_request(rid2, timestamp="2026-01-01T00:00:02+00:00"),
            _attractor_response(rid2, timestamp="2026-01-01T00:00:03+00:00"),
        ])

        stub = _CountingLLM()

        asyncio.run(mod._run_from_attractor(
            trace_path=trace,
            attractor_heuristics=None,
            attractor_first=None,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        # 2 attractors × n=1 → 2 calls
        (call_0, call_1) = stub.calls

    def test_all_attractors_n3(self, tmp_path: Path) -> None:
        """Tier 2: with 2 attractors and n=3, the LLM stub receives 6 calls total."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid1 = "multi-001"
        rid2 = "multi-002"
        _write_jsonl(trace, [
            _attractor_request(rid1, timestamp="2026-01-01T00:00:00+00:00"),
            _attractor_response(rid1, timestamp="2026-01-01T00:00:01+00:00"),
            _attractor_request(rid2, timestamp="2026-01-01T00:00:02+00:00"),
            _attractor_response(rid2, timestamp="2026-01-01T00:00:03+00:00"),
        ])

        stub = _CountingLLM()

        asyncio.run(mod._run_from_attractor(
            trace_path=trace,
            attractor_heuristics=None,
            attractor_first=None,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=3,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        # 2 attractors × n=3 → 6 calls
        (c0, c1, c2, c3, c4, c5) = stub.calls


# ---------------------------------------------------------------------------
# (b) --attractor-heuristics filters detections
# ---------------------------------------------------------------------------


class TestAttractorHeuristicsFilter:
    """Tier 2: --attractor-heuristics filters which heuristic detections are replayed."""

    def test_filter_to_stop_must_only(self, tmp_path: Path) -> None:
        """Tier 2: heuristic filter 'stop_with_must_rule' only replays that attractor type."""
        mod = _import_replay()

        # Build one stop_with_must_rule attractor (will match) and
        # one enum_violation attractor (must be excluded by filter).
        rid_must = "must-attractor"
        tool_def = [{
            "type": "function",
            "function": {
                "name": "invoke_skill",
                "description": "run",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": ["skill_a"]},
                    },
                    "required": ["name"],
                },
            },
        }]
        rid_enum = "enum-attractor"

        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, [
            # stop_with_must_rule attractor
            _attractor_request(rid_must, timestamp="2026-01-01T00:00:00+00:00"),
            _attractor_response(rid_must, timestamp="2026-01-01T00:00:01+00:00"),
            # enum_violation attractor: tool call with illegal value
            {
                "kind": "request",
                "request_id": rid_enum,
                "timestamp": "2026-01-01T00:00:02+00:00",
                "model": "gemini-2.5-flash-lite",
                "caller_hint": "router",
                "messages": [{"role": "user", "content": "go"}],
                "tools": tool_def,
                "tool_choice": "auto",
                "sampling_params": {},
            },
            {
                "kind": "response",
                "request_id": rid_enum,
                "timestamp": "2026-01-01T00:00:03+00:00",
                "content": None,
                "tool_calls": [{
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": "invoke_skill", "arguments": '{"name":"skill_z"}'},
                }],
                "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        ])

        stub = _CountingLLM()

        asyncio.run(mod._run_from_attractor(
            trace_path=trace,
            attractor_heuristics=["stop_with_must_rule"],
            attractor_first=None,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        # Only the stop_must attractor should be replayed (1 call)
        (only_call,) = stub.calls


# ---------------------------------------------------------------------------
# (c) --attractor-first limits the number of attractors
# ---------------------------------------------------------------------------


class TestAttractorFirst:
    """Tier 2: --attractor-first N limits replay to the first N attractors."""

    def test_first_1_of_3(self, tmp_path: Path) -> None:
        """Tier 2: with 3 attractors and first=1, only 1 is replayed."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        records: list[dict] = []
        for idx in range(3):
            rid = f"first-limit-{idx:03d}"
            ts_req = f"2026-01-01T00:00:{idx * 2:02d}+00:00"
            ts_res = f"2026-01-01T00:00:{idx * 2 + 1:02d}+00:00"
            records.append(_attractor_request(rid, timestamp=ts_req))
            records.append(_attractor_response(rid, timestamp=ts_res))
        _write_jsonl(trace, records)

        stub = _CountingLLM()

        asyncio.run(mod._run_from_attractor(
            trace_path=trace,
            attractor_heuristics=None,
            attractor_first=1,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        # Only 1 attractor replayed despite 3 being present
        (only_call,) = stub.calls

    def test_first_2_of_3(self, tmp_path: Path) -> None:
        """Tier 2: with 3 attractors and first=2, exactly 2 are replayed."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        records: list[dict] = []
        for idx in range(3):
            rid = f"first2-limit-{idx:03d}"
            ts_req = f"2026-01-01T00:00:{idx * 2:02d}+00:00"
            ts_res = f"2026-01-01T00:00:{idx * 2 + 1:02d}+00:00"
            records.append(_attractor_request(rid, timestamp=ts_req))
            records.append(_attractor_response(rid, timestamp=ts_res))
        _write_jsonl(trace, records)

        stub = _CountingLLM()

        asyncio.run(mod._run_from_attractor(
            trace_path=trace,
            attractor_heuristics=None,
            attractor_first=2,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        (call_0, call_1) = stub.calls


# ---------------------------------------------------------------------------
# (d) zero attractors → graceful exit (no error, no LLM call)
# ---------------------------------------------------------------------------


class TestZeroAttractors:
    """Tier 2: a trace with no attractors causes graceful exit with no LLM calls."""

    def test_no_attractors_no_calls(self, tmp_path: Path, capsys) -> None:
        """Tier 2: clean trace produces zero LLM calls and prints an informational message."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "clean-001"
        _write_jsonl(trace, [
            _req_record(rid, system_text="Use the tools."),
            _resp_record(rid, finish_reason="stop", content="done"),
        ])

        stub = _CountingLLM()

        # Must not raise
        asyncio.run(mod._run_from_attractor(
            trace_path=trace,
            attractor_heuristics=None,
            attractor_first=None,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        assert not stub.calls, "No LLM calls expected when no attractors found"
        out = capsys.readouterr().out
        # Informational message must appear
        assert "No attractors" in out or "nothing" in out.lower()


# ---------------------------------------------------------------------------
# (e) summary output contains expected table sections
# ---------------------------------------------------------------------------


class TestSummaryOutput:
    """Tier 2: multi-attractor summary contains required structural sections."""

    def test_summary_sections_present(self, tmp_path: Path, capsys) -> None:
        """Tier 2: summary output contains 'Multi-attractor replay summary' and 'By heuristic'."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "summary-attractor-001"
        _write_jsonl(trace, [
            _attractor_request(rid),
            _attractor_response(rid),
        ])

        stub = _CountingLLM()

        asyncio.run(mod._run_from_attractor(
            trace_path=trace,
            attractor_heuristics=None,
            attractor_first=None,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=2,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "Multi-attractor replay summary" in out
        assert "Total attractors replayed" in out
        assert "Total LLM calls" in out
        assert "By heuristic" in out

    def test_summary_counts_correct(self, tmp_path: Path, capsys) -> None:
        """Tier 2: summary total-calls count matches attractors × n."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "summary-count-001"
        _write_jsonl(trace, [
            _attractor_request(rid),
            _attractor_response(rid),
        ])

        stub = _CountingLLM()
        n = 3

        asyncio.run(mod._run_from_attractor(
            trace_path=trace,
            attractor_heuristics=None,
            attractor_first=None,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=n,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        # 1 attractor × 3 = 3 LLM calls; "3" must appear in summary
        assert "3" in out


# ---------------------------------------------------------------------------
# (f) backward compat: direct request_id path still works
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Tier 2: the existing request_id-direct path is unaffected by --from-attractor additions."""

    def test_direct_request_id_still_works(self, tmp_path: Path) -> None:
        """Tier 2: _run() with a direct request_id issues exactly n LLM calls (no change)."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "compat-rid-001"
        _write_jsonl(trace, [
            _req_record(rid),
            _resp_record(rid),
        ])

        stub = _CountingLLM()

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=2,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        (call_0, call_1) = stub.calls
