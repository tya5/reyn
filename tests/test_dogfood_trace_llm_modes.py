"""Tests for dogfood_trace.py llm-payloads / llm-detail / llm-tools-schema modes.

Tier 2: OS invariant — verifies that the LLM trace inspection CLI modes
correctly parse a JSONL trace file and surface the expected information.
Uses subprocess + hand-crafted JSONL fixtures (no reyn imports needed).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "dogfood_trace.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(args: list[str]) -> tuple[str, int]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True
    )
    return result.stdout + result.stderr, result.returncode


def _write_trace(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


# Minimal valid trace: 1 request + 1 response pair
REQUEST_ID_A = "aaaaaaaa-0000-0000-0000-000000000001"
REQUEST_ID_B = "bbbbbbbb-0000-0000-0000-000000000002"

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "run a skill",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "enum": ["skill_a", "skill_b"]},
                },
            },
        },
    }
]

SAMPLE_RECORDS = [
    {
        "kind": "request",
        "request_id": REQUEST_ID_A,
        "timestamp": "2026-05-04T10:00:00+00:00",
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "router",
        "messages": [
            {"role": "system", "content": "you are a helpful agent"},
            {"role": "user", "content": "call the run_skill tool"},
        ],
        "tools": SAMPLE_TOOLS,
        "tool_choice": "auto",
        "sampling_params": {"timeout": None, "max_retries": 1},
    },
    {
        "kind": "response",
        "request_id": REQUEST_ID_A,
        "timestamp": "2026-05-04T10:00:01+00:00",
        "content": None,
        "tool_calls": [
            {
                "id": "tc1",
                "type": "function",
                "function": {"name": "run_skill", "arguments": '{"skill": "skill_a"}'},
            }
        ],
        "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    },
    {
        "kind": "request",
        "request_id": REQUEST_ID_B,
        "timestamp": "2026-05-04T10:00:05+00:00",
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "phase:do_work",
        "messages": [
            {"role": "system", "content": "system prompt here"},
            {"role": "user", "content": "the user input"},
        ],
        "tools": None,
        "tool_choice": None,
        "sampling_params": {"timeout": 60.0, "max_retries": 3},
    },
    {
        "kind": "response",
        "request_id": REQUEST_ID_B,
        "timestamp": "2026-05-04T10:00:06+00:00",
        "content": '{"type": "decide", "control": {"type": "finish"}}',
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 200, "completion_tokens": 30},
    },
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLlmPayloadsMode:
    """Tier 2: llm-payloads mode lists request/response pairs in time order."""

    def test_lists_both_request_ids(self, tmp_path: Path) -> None:
        """Tier 2: llm-payloads mode shows both request_ids from the trace."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-payloads", "--trace", str(trace)])
        assert rc == 0
        assert REQUEST_ID_A in out
        assert REQUEST_ID_B in out

    def test_shows_model_and_caller(self, tmp_path: Path) -> None:
        """Tier 2: llm-payloads mode includes model name and caller_hint."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-payloads", "--trace", str(trace)])
        assert rc == 0
        assert "gemini-2.5-flash-lite" in out
        assert "router" in out
        assert "phase:do_work" in out

    def test_shows_token_counts(self, tmp_path: Path) -> None:
        """Tier 2: llm-payloads mode shows token_in/token_out from usage."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-payloads", "--trace", str(trace)])
        assert rc == 0
        # First pair: tokens_in=100, tokens_out=20
        assert "100" in out
        assert "20" in out

    def test_missing_trace_file_exits_1(self, tmp_path: Path) -> None:
        """Tier 2: llm-payloads mode exits with code 1 when trace file is missing."""
        out, rc = _run(["--mode", "llm-payloads", "--trace", str(tmp_path / "nonexistent.jsonl")])
        assert rc == 1
        assert "not found" in out.lower() or "trace file" in out.lower()

    def test_empty_trace_file_no_crash(self, tmp_path: Path) -> None:
        """Tier 2: llm-payloads mode handles empty trace file without crashing."""
        trace = tmp_path / "empty.jsonl"
        _write_trace(trace, [])

        out, rc = _run(["--mode", "llm-payloads", "--trace", str(trace)])
        assert rc == 0
        assert "no LLM request records" in out


class TestLlmDetailMode:
    """Tier 2: llm-detail mode pretty-prints a single request's full payload."""

    def test_shows_model_and_caller_hint(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail outputs model and caller_hint for the given request_id."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-detail", REQUEST_ID_A, "--trace", str(trace)])
        assert rc == 0
        assert "gemini-2.5-flash-lite" in out
        assert "router" in out

    def test_shows_message_roles(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail shows message roles (system, user)."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-detail", REQUEST_ID_A, "--trace", str(trace)])
        assert rc == 0
        assert "system" in out
        assert "user" in out

    def test_shows_tool_names_without_full(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail shows tool names by default (no --full)."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-detail", REQUEST_ID_A, "--trace", str(trace)])
        assert rc == 0
        assert "run_skill" in out

    def test_full_flag_expands_tools_schema(self, tmp_path: Path) -> None:
        """Tier 2: --full flag makes llm-detail output the full tools schema JSON."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-detail", REQUEST_ID_A, "--trace", str(trace), "--full"])
        assert rc == 0
        # With --full, the full enum values should appear
        assert "skill_a" in out or "skill_b" in out

    def test_shows_response_usage(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail shows usage (prompt/completion tokens) from response."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-detail", REQUEST_ID_A, "--trace", str(trace)])
        assert rc == 0
        # Response for A: prompt_tokens=100, completion_tokens=20
        assert "100" in out

    def test_shows_finish_reason(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail shows finish_reason from response."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-detail", REQUEST_ID_A, "--trace", str(trace)])
        assert rc == 0
        assert "tool_calls" in out  # finish_reason for REQUEST_ID_A

    def test_absent_request_id_exits_1(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail exits 1 and reports error for an unknown request_id."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-detail", "nonexistent-id", "--trace", str(trace)])
        assert rc == 1
        assert "not found" in out.lower()

    def test_missing_request_id_arg_exits_1(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail without a request_id argument exits with code 1."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-detail", "--trace", str(trace)])
        assert rc == 1

    def test_system_prompt_truncated_by_default(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail truncates long system prompts unless --full is given."""
        long_system = "A" * 1000
        records = [
            {
                "kind": "request",
                "request_id": REQUEST_ID_A,
                "timestamp": "2026-05-04T10:00:00+00:00",
                "model": "gemini-2.5-flash-lite",
                "caller_hint": "phase:x",
                "messages": [{"role": "system", "content": long_system}],
                "tools": None,
                "tool_choice": None,
                "sampling_params": {},
            },
            {
                "kind": "response",
                "request_id": REQUEST_ID_A,
                "timestamp": "2026-05-04T10:00:01+00:00",
                "content": "ok",
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 50, "completion_tokens": 5},
            },
        ]
        trace = tmp_path / "trace_long.jsonl"
        _write_trace(trace, records)

        out, rc = _run(["--mode", "llm-detail", REQUEST_ID_A, "--trace", str(trace)])
        assert rc == 0
        # The full 1000-char system prompt should NOT appear without --full
        assert len(out) < 1000 + 500  # allow some overhead, but not 1000 chars of As

        out_full, rc_full = _run(["--mode", "llm-detail", REQUEST_ID_A, "--trace", str(trace), "--full"])
        assert rc_full == 0
        assert "A" * 100 in out_full  # full content appears with --full


class TestLlmToolsSchemaMode:
    """Tier 2: llm-tools-schema mode outputs full tools JSON for a request_id."""

    def test_outputs_valid_json(self, tmp_path: Path) -> None:
        """Tier 2: llm-tools-schema output is valid JSON for the tools array."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-tools-schema", REQUEST_ID_A, "--trace", str(trace)])
        assert rc == 0
        # Output should be parseable JSON
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["function"]["name"] == "run_skill"

    def test_shows_enum_constraints(self, tmp_path: Path) -> None:
        """Tier 2: llm-tools-schema output includes enum constraints in parameter schema."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-tools-schema", REQUEST_ID_A, "--trace", str(trace)])
        assert rc == 0
        assert "skill_a" in out
        assert "skill_b" in out

    def test_no_tools_request_prints_message(self, tmp_path: Path) -> None:
        """Tier 2: llm-tools-schema for a request with no tools prints informational message."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        # REQUEST_ID_B has tools=None
        out, rc = _run(["--mode", "llm-tools-schema", REQUEST_ID_B, "--trace", str(trace)])
        assert rc == 0
        assert "no tools" in out.lower()

    def test_absent_request_id_exits_1(self, tmp_path: Path) -> None:
        """Tier 2: llm-tools-schema exits 1 for an unknown request_id."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-tools-schema", "nonexistent-id", "--trace", str(trace)])
        assert rc == 1
        assert "not found" in out.lower()

    def test_missing_request_id_arg_exits_1(self, tmp_path: Path) -> None:
        """Tier 2: llm-tools-schema without a request_id argument exits with code 1."""
        trace = tmp_path / "trace.jsonl"
        _write_trace(trace, SAMPLE_RECORDS)

        out, rc = _run(["--mode", "llm-tools-schema", "--trace", str(trace)])
        assert rc == 1
