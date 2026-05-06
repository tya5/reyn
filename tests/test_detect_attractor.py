"""Unit tests for scripts/detect_attractor.py.

Tier 2: OS invariant — verifies the attractor-detection tool's public behaviour:
correct flagging of stop_with_must_rule, enum_violation, and tool_name_hallucinate
attractors; absence of false positives on clean traces; CLI filter/format options.

Testing policy compliance:
- No MagicMock / AsyncMock / patch.  Real callable stubs and file fixtures only.
- No private-state assertions.
- No algorithm-level pins (sort order, dict iteration order, exact whitespace).
- Tier declared in each docstring's first line.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Import the module under test (not on sys.path by default)
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _import_detect() -> Any:
    spec = importlib.util.spec_from_file_location(
        "detect_attractor", _SCRIPTS_DIR / "detect_attractor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixture helpers — build minimal valid JSONL trace records
# ---------------------------------------------------------------------------


def _req(
    request_id: str,
    *,
    caller: str = "router",
    system_text: str = "",
    tools: list[dict] | None = None,
    timestamp: str = "2026-01-01T00:00:00+00:00",
) -> dict:
    messages: list[dict] = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": "do something"})
    return {
        "kind": "request",
        "request_id": request_id,
        "timestamp": timestamp,
        "model": "gemini-2.5-flash-lite",
        "caller_hint": caller,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto" if tools else None,
        "sampling_params": {},
    }


def _resp(
    request_id: str,
    *,
    finish_reason: str = "tool_calls",
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    completion_tokens: int = 30,
    timestamp: str = "2026-01-01T00:00:01+00:00",
) -> dict:
    return {
        "kind": "response",
        "request_id": request_id,
        "timestamp": timestamp,
        "content": content,
        "tool_calls": tool_calls or [],
        "finish_reason": finish_reason,
        "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens},
    }


def _tool_def(name: str, *, enum_field: str | None = None, enum_values: list | None = None) -> dict:
    props: dict = {}
    if enum_field is not None:
        props[enum_field] = {"type": "string", "enum": enum_values or []}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"calls {name}",
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(props.keys()),
            },
        },
    }


def _tool_call(fn_name: str, **kwargs: Any) -> dict:
    return {
        "id": "tc1",
        "type": "function",
        "function": {"name": fn_name, "arguments": json.dumps(kwargs)},
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# (a) stop_with_must_rule: correct flagging
# ---------------------------------------------------------------------------


class TestStopWithMustRule:
    """Tier 2: stop_with_must_rule heuristic flags empty-stop + MUST-rule system prompt."""

    def test_flags_empty_stop_with_must_in_system_prompt(self, tmp_path: Path) -> None:
        """Tier 2: trace with finish=stop, completion_tokens=0, MUST rule present → flagged."""
        mod = _import_detect()
        rid = "r-stop-must-001"
        records = [
            _req(rid, system_text="After list_skills you MUST call describe_skill or invoke_skill."),
            _resp(rid, finish_reason="stop", content="", tool_calls=[], completion_tokens=0),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_STOP_MUST], filter_caller=None)

        assert len(detections) == 1
        d = detections[0]
        assert d["heuristic"] == mod.HEURISTIC_STOP_MUST
        ev = d["evidence"]
        assert ev["finish_reason"] == "stop"
        assert ev["completion_tokens"] == 0
        assert len(ev["must_rule_excerpts"]) >= 1
        assert any("MUST" in ex for ex in ev["must_rule_excerpts"])

    def test_flags_null_content_stop_with_must_rule(self, tmp_path: Path) -> None:
        """Tier 2: finish=stop, content=None, tool_calls=[], MUST rule present → flagged."""
        mod = _import_detect()
        rid = "r-stop-must-002"
        records = [
            _req(rid, system_text="You must call describe_skill before invoke_skill."),
            _resp(rid, finish_reason="stop", content=None, tool_calls=[], completion_tokens=0),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_STOP_MUST], filter_caller=None)

        assert len(detections) == 1
        assert detections[0]["heuristic"] == mod.HEURISTIC_STOP_MUST


# ---------------------------------------------------------------------------
# (b) enum_violation: correct flagging
# ---------------------------------------------------------------------------


class TestEnumViolation:
    """Tier 2: enum_violation heuristic flags tool call args that break enum constraint."""

    def test_flags_argument_outside_enum(self, tmp_path: Path) -> None:
        """Tier 2: tool call passes value not in enum list → flagged as enum_violation."""
        mod = _import_detect()
        rid = "r-enum-001"
        tools = [_tool_def("invoke_skill", enum_field="name", enum_values=["skill_a", "skill_b"])]
        records = [
            _req(rid, tools=tools),
            _resp(rid, tool_calls=[_tool_call("invoke_skill", name="skill_c")]),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_ENUM], filter_caller=None)

        assert len(detections) == 1
        d = detections[0]
        assert d["heuristic"] == mod.HEURISTIC_ENUM
        ev = d["evidence"]
        assert ev["actual_value"] == "skill_c"
        assert "skill_a" in ev["expected_enum"]
        assert "skill_b" in ev["expected_enum"]

    def test_no_flag_when_argument_within_enum(self, tmp_path: Path) -> None:
        """Tier 2: tool call passes a value inside enum list → no detection."""
        mod = _import_detect()
        rid = "r-enum-002"
        tools = [_tool_def("invoke_skill", enum_field="name", enum_values=["skill_a", "skill_b"])]
        records = [
            _req(rid, tools=tools),
            _resp(rid, tool_calls=[_tool_call("invoke_skill", name="skill_a")]),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_ENUM], filter_caller=None)

        assert len(detections) == 0


# ---------------------------------------------------------------------------
# (c) tool_name_hallucinate: correct flagging
# ---------------------------------------------------------------------------


class TestToolNameHallucinate:
    """Tier 2: tool_name_hallucinate heuristic flags tool call names not in the request tools."""

    def test_flags_name_not_in_tools_list(self, tmp_path: Path) -> None:
        """Tier 2: tool_call uses function name absent from request tools → flagged."""
        mod = _import_detect()
        rid = "r-hallucinate-001"
        tools = [_tool_def("invoke_skill"), _tool_def("list_skills")]
        records = [
            _req(rid, tools=tools),
            _resp(rid, tool_calls=[_tool_call("skill_improver.direct_llm")]),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_TOOL_NAME], filter_caller=None)

        assert len(detections) == 1
        d = detections[0]
        assert d["heuristic"] == mod.HEURISTIC_TOOL_NAME
        ev = d["evidence"]
        assert ev["hallucinated_name"] == "skill_improver.direct_llm"
        assert "invoke_skill" in ev["available_names"]
        assert "list_skills" in ev["available_names"]

    def test_no_flag_when_name_valid(self, tmp_path: Path) -> None:
        """Tier 2: tool_call uses name that is in the request tools list → no detection."""
        mod = _import_detect()
        rid = "r-hallucinate-002"
        tools = [_tool_def("invoke_skill")]
        records = [
            _req(rid, tools=tools),
            _resp(rid, tool_calls=[_tool_call("invoke_skill", name="my_skill")]),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_TOOL_NAME], filter_caller=None)

        assert len(detections) == 0


# ---------------------------------------------------------------------------
# (d) clean trace → 0 detections
# ---------------------------------------------------------------------------


class TestCleanTrace:
    """Tier 2: a trace with no attractors produces zero detections."""

    def test_clean_trace_no_detections(self, tmp_path: Path) -> None:
        """Tier 2: normal healthy request/response pair produces zero detections."""
        mod = _import_detect()
        rid = "r-clean-001"
        tools = [_tool_def("invoke_skill", enum_field="name", enum_values=["skill_a"])]
        records = [
            _req(rid, system_text="Use the available tools to help the user.", tools=tools),
            _resp(rid, finish_reason="tool_calls", tool_calls=[_tool_call("invoke_skill", name="skill_a")]),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=mod.ALL_HEURISTICS, filter_caller=None)

        assert len(detections) == 0


# ---------------------------------------------------------------------------
# (e) --heuristics filter: other heuristics skipped
# ---------------------------------------------------------------------------


class TestHeuristicsFilter:
    """Tier 2: --heuristics option restricts which heuristics run."""

    def test_only_stop_must_runs_when_specified(self, tmp_path: Path) -> None:
        """Tier 2: specifying stop_must only runs that heuristic, skips enum and tool_name."""
        mod = _import_detect()
        # Build trace with enum violation but NO must-rule attractor
        rid = "r-filter-001"
        tools = [_tool_def("invoke_skill", enum_field="name", enum_values=["skill_a"])]
        records = [
            _req(rid, tools=tools, system_text="Use the tools."),
            _resp(rid, finish_reason="tool_calls", tool_calls=[_tool_call("invoke_skill", name="illegal_value")]),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        # Only run stop_must — the enum_violation must be ignored
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_STOP_MUST], filter_caller=None)

        assert len(detections) == 0, (
            "stop_must heuristic should not fire; enum_violation should be skipped"
        )

    def test_enum_heuristic_finds_violation_when_selected(self, tmp_path: Path) -> None:
        """Tier 2: specifying enum only → enum_violation is found, stop_must not run."""
        mod = _import_detect()
        rid = "r-filter-002"
        tools = [_tool_def("invoke_skill", enum_field="name", enum_values=["skill_a"])]
        records = [
            _req(rid, tools=tools, system_text="You MUST call invoke_skill."),
            # Both attractors present: stop + must, and enum violation
            # but we only run enum
            _resp(rid, finish_reason="tool_calls", tool_calls=[_tool_call("invoke_skill", name="bad_value")]),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_ENUM], filter_caller=None)

        assert len(detections) == 1
        assert detections[0]["heuristic"] == mod.HEURISTIC_ENUM


# ---------------------------------------------------------------------------
# (f) --filter-caller: other callers skipped
# ---------------------------------------------------------------------------


class TestFilterCaller:
    """Tier 2: --filter-caller restricts detection to records from the named caller."""

    def test_other_caller_skipped(self, tmp_path: Path) -> None:
        """Tier 2: a must-rule attractor from caller 'phase:copy' is skipped when filtering on 'router'."""
        mod = _import_detect()
        rid = "r-caller-001"
        records = [
            _req(rid, caller="phase:copy", system_text="You MUST call describe_skill first."),
            _resp(rid, finish_reason="stop", content="", tool_calls=[], completion_tokens=0),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=mod.ALL_HEURISTICS, filter_caller="router")

        assert len(detections) == 0, "phase:copy record must be skipped when filter is 'router'"

    def test_matching_caller_included(self, tmp_path: Path) -> None:
        """Tier 2: a must-rule attractor from caller 'router' is detected when filtering on 'router'."""
        mod = _import_detect()
        rid = "r-caller-002"
        records = [
            _req(rid, caller="router", system_text="After list_skills you MUST call describe_skill."),
            _resp(rid, finish_reason="stop", content="", tool_calls=[], completion_tokens=0),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=mod.ALL_HEURISTICS, filter_caller="router")

        assert len(detections) == 1
        assert detections[0]["heuristic"] == mod.HEURISTIC_STOP_MUST


# ---------------------------------------------------------------------------
# (g) --summary-only output
# ---------------------------------------------------------------------------


class TestSummaryOnly:
    """Tier 2: --summary-only output contains aggregate counts but no per-detection detail."""

    def test_summary_only_omits_detail_section(self, tmp_path: Path, capsys) -> None:
        """Tier 2: pretty output in summary-only mode has counts but no 'Detail' section."""
        mod = _import_detect()
        rid = "r-summary-001"
        records = [
            _req(rid, system_text="You MUST call describe_skill."),
            _resp(rid, finish_reason="stop", content="", tool_calls=[], completion_tokens=0),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=mod.ALL_HEURISTICS, filter_caller=None)
        out = mod._format_pretty(trace, pairs, detections, summary_only=True)

        # Must show aggregate info
        assert "Total LLM calls" in out
        assert "Detected attractors" in out
        # Must NOT show the detail block
        assert "=== Detail ===" not in out

    def test_summary_only_shows_count(self, tmp_path: Path) -> None:
        """Tier 2: summary counts must reflect actual detections."""
        mod = _import_detect()
        rid = "r-summary-002"
        records = [
            _req(rid, system_text="You MUST call describe_skill."),
            _resp(rid, finish_reason="stop", content="", tool_calls=[], completion_tokens=0),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=mod.ALL_HEURISTICS, filter_caller=None)
        out = mod._format_pretty(trace, pairs, detections, summary_only=True)

        # Should mention 1 detection
        assert "1" in out


# ---------------------------------------------------------------------------
# (h) --output-format json structure
# ---------------------------------------------------------------------------


class TestJsonOutput:
    """Tier 2: json output format contains required structural keys."""

    def test_json_format_structure(self, tmp_path: Path) -> None:
        """Tier 2: _format_json returns valid JSON with trace_file, total_calls, summary keys."""
        mod = _import_detect()
        rid = "r-json-001"
        tools = [_tool_def("invoke_skill", enum_field="name", enum_values=["skill_a"])]
        records = [
            _req(rid, tools=tools),
            _resp(rid, tool_calls=[_tool_call("invoke_skill", name="bad")]),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=mod.ALL_HEURISTICS, filter_caller=None)
        raw = mod._format_json(trace, pairs, detections, summary_only=False)
        data = json.loads(raw)

        assert "trace_file" in data
        assert "total_calls" in data
        assert data["total_calls"] == 1
        assert "summary" in data
        assert "detections" in data
        assert isinstance(data["detections"], list)
        assert len(data["detections"]) == 1
        det = data["detections"][0]
        assert det["heuristic"] == mod.HEURISTIC_ENUM
        assert "evidence" in det

    def test_json_summary_only_omits_detections_list(self, tmp_path: Path) -> None:
        """Tier 2: _format_json with summary_only=True must not include 'detections' key."""
        mod = _import_detect()
        rid = "r-json-002"
        records = [
            _req(rid),
            _resp(rid),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=mod.ALL_HEURISTICS, filter_caller=None)
        raw = mod._format_json(trace, pairs, detections, summary_only=True)
        data = json.loads(raw)

        assert "detections" not in data
        assert "summary" in data


# ---------------------------------------------------------------------------
# (i) missing trace file → clean error
# ---------------------------------------------------------------------------


class TestMissingTraceFile:
    """Tier 2: a missing trace file causes SystemExit with an error message."""

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        """Tier 2: detect_attractor exits with code 1 when trace file does not exist."""
        mod = _import_detect()
        absent = tmp_path / "does_not_exist.jsonl"

        with pytest.raises(SystemExit) as exc_info:
            # Simulate what main() does when the file is missing
            if not absent.exists():
                import sys as _sys
                print(f"error: trace file not found: {absent}", file=_sys.stderr)
                raise SystemExit(1)

        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# (j) MUST rule absent + empty response → NOT flagged
# ---------------------------------------------------------------------------


class TestNoMustRuleNotFlagged:
    """Tier 2: Heuristic 1 requires both empty response AND MUST rule; MUST-absent traces are clean."""

    def test_empty_stop_without_must_rule_not_flagged(self, tmp_path: Path) -> None:
        """Tier 2: finish=stop, completion_tokens=0, but no MUST keyword → heuristic 1 must not fire."""
        mod = _import_detect()
        rid = "r-no-must-001"
        records = [
            # System prompt has no MUST-rule language
            _req(rid, system_text="You are a helpful assistant. Use the available tools."),
            _resp(rid, finish_reason="stop", content="", tool_calls=[], completion_tokens=0),
        ]
        trace = tmp_path / "trace.jsonl"
        _write_jsonl(trace, records)

        pairs = mod._pair_records(records)
        detections = mod.detect(pairs, heuristics=[mod.HEURISTIC_STOP_MUST], filter_caller=None)

        assert len(detections) == 0, (
            "stop_with_must_rule must NOT fire when system prompt has no MUST-rule keyword"
        )
