"""Tests for dogfood_trace.py multi-file --trace support.

Tier 2: OS invariant — verifies that the LLM trace inspection CLI modes
correctly accept multiple trace files via --trace a --trace b and
--trace a,b forms, merge them chronologically, and annotate each record
with its source file.

Uses subprocess + hand-crafted JSONL fixtures (no reyn imports needed).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "dogfood_trace.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(args: list[str]) -> tuple[str, int]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr, result.returncode


def _write_trace(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fixture data — two separate trace files with interleaved timestamps
# ---------------------------------------------------------------------------

REQUEST_ID_H1 = "h1aaaaaa-0000-0000-0000-000000000001"
REQUEST_ID_H2 = "h2bbbbbb-0000-0000-0000-000000000002"
REQUEST_ID_H3 = "h3cccccc-0000-0000-0000-000000000003"

# File A: two records at T=10:00:00 and T=10:00:10
RECORDS_FILE_A = [
    {
        "kind": "request",
        "request_id": REQUEST_ID_H1,
        "timestamp": "2026-05-04T10:00:00+00:00",
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "router",
        "messages": [{"role": "user", "content": "msg from h1"}],
        "tools": None,
        "tool_choice": None,
        "sampling_params": {},
    },
    {
        "kind": "response",
        "request_id": REQUEST_ID_H1,
        "timestamp": "2026-05-04T10:00:01+00:00",
        "content": "response h1",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    },
    {
        "kind": "request",
        "request_id": REQUEST_ID_H3,
        "timestamp": "2026-05-04T10:00:10+00:00",
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "phase:h3",
        "messages": [{"role": "user", "content": "msg from h3"}],
        "tools": None,
        "tool_choice": None,
        "sampling_params": {},
    },
    {
        "kind": "response",
        "request_id": REQUEST_ID_H3,
        "timestamp": "2026-05-04T10:00:11+00:00",
        "content": "response h3",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 20, "completion_tokens": 5},
    },
]

# File B: one record interleaved at T=10:00:05 (between H1 and H3)
RECORDS_FILE_B = [
    {
        "kind": "request",
        "request_id": REQUEST_ID_H2,
        "timestamp": "2026-05-04T10:00:05+00:00",
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "phase:h2",
        "messages": [{"role": "user", "content": "msg from h2"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "some_tool",
                    "description": "a tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": None,
        "sampling_params": {},
    },
    {
        "kind": "response",
        "request_id": REQUEST_ID_H2,
        "timestamp": "2026-05-04T10:00:06+00:00",
        "content": None,
        "tool_calls": [
            {
                "id": "tc_h2",
                "type": "function",
                "function": {"name": "some_tool", "arguments": "{}"},
            }
        ],
        "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 30, "completion_tokens": 10},
    },
]


def _make_traces(tmp_path: Path) -> tuple[Path, Path]:
    """Write both fixture files and return their paths."""
    file_a = tmp_path / "trace_h1.jsonl"
    file_b = tmp_path / "trace_h2.jsonl"
    _write_trace(file_a, RECORDS_FILE_A)
    _write_trace(file_b, RECORDS_FILE_B)
    return file_a, file_b


# ---------------------------------------------------------------------------
# (a) multi-flag form: --trace a --trace b
# ---------------------------------------------------------------------------


class TestMultiFlagForm:
    def test_multi_flag_shows_all_request_ids(self, tmp_path: Path) -> None:
        """Tier 2: --trace a --trace b shows records from both files."""
        file_a, file_b = _make_traces(tmp_path)
        out, rc = _run(
            ["--mode", "llm-payloads", "--trace", str(file_a), "--trace", str(file_b)]
        )
        assert rc == 0
        assert REQUEST_ID_H1 in out
        assert REQUEST_ID_H2 in out
        assert REQUEST_ID_H3 in out

    def test_multi_flag_total_record_count(self, tmp_path: Path) -> None:
        """Tier 2: --trace a --trace b produces output for records from all files."""
        file_a, file_b = _make_traces(tmp_path)
        # File A has 2 requests; File B has 1 request — expect 3 request lines
        out, rc = _run(
            ["--mode", "llm-payloads", "--trace", str(file_a), "--trace", str(file_b)]
        )
        assert rc == 0
        request_lines = [ln for ln in out.splitlines() if "request_id=" in ln and "response_id" not in ln]
        assert len(request_lines) == 3


# ---------------------------------------------------------------------------
# (b) comma-separated form: --trace a,b
# ---------------------------------------------------------------------------


class TestCommaSeparatedForm:
    def test_comma_separated_shows_all_request_ids(self, tmp_path: Path) -> None:
        """Tier 2: --trace a,b (comma-separated) shows records from both files."""
        file_a, file_b = _make_traces(tmp_path)
        combined = f"{file_a},{file_b}"
        out, rc = _run(["--mode", "llm-payloads", "--trace", combined])
        assert rc == 0
        assert REQUEST_ID_H1 in out
        assert REQUEST_ID_H2 in out
        assert REQUEST_ID_H3 in out

    def test_comma_separated_total_record_count(self, tmp_path: Path) -> None:
        """Tier 2: --trace a,b comma form produces output for all records."""
        file_a, file_b = _make_traces(tmp_path)
        combined = f"{file_a},{file_b}"
        out, rc = _run(["--mode", "llm-payloads", "--trace", combined])
        assert rc == 0
        request_lines = [ln for ln in out.splitlines() if "request_id=" in ln and "response_id" not in ln]
        assert len(request_lines) == 3


# ---------------------------------------------------------------------------
# (c) merge sort: records from different files appear in timestamp order
# ---------------------------------------------------------------------------


class TestMergeSort:
    def test_records_appear_in_timestamp_order(self, tmp_path: Path) -> None:
        """Tier 2: records from different files are merged in chronological order."""
        file_a, file_b = _make_traces(tmp_path)
        out, rc = _run(
            ["--mode", "llm-payloads", "--trace", str(file_a), "--trace", str(file_b)]
        )
        assert rc == 0
        # H1 (T+0.0s) must appear before H2 (T+5.0s), which must appear before H3 (T+10.0s)
        pos_h1 = out.index(REQUEST_ID_H1)
        pos_h2 = out.index(REQUEST_ID_H2)
        pos_h3 = out.index(REQUEST_ID_H3)
        assert pos_h1 < pos_h2 < pos_h3

    def test_relative_timestamps_are_consistent_across_files(self, tmp_path: Path) -> None:
        """Tier 2: T+ offsets use the oldest record's timestamp as the common base."""
        file_a, file_b = _make_traces(tmp_path)
        out, rc = _run(
            ["--mode", "llm-payloads", "--trace", str(file_a), "--trace", str(file_b)]
        )
        assert rc == 0
        # The first record is at T+0.0s; verify T+ notation appears
        assert "T+" in out


# ---------------------------------------------------------------------------
# (d) _source_file field in output
# ---------------------------------------------------------------------------


class TestSourceFileAnnotation:
    def test_file_annotation_appears_in_multi_file_output(self, tmp_path: Path) -> None:
        """Tier 2: multi-file llm-payloads output includes [file=...] annotation."""
        file_a, file_b = _make_traces(tmp_path)
        out, rc = _run(
            ["--mode", "llm-payloads", "--trace", str(file_a), "--trace", str(file_b)]
        )
        assert rc == 0
        assert "file=" in out

    def test_file_annotation_contains_basename(self, tmp_path: Path) -> None:
        """Tier 2: [file=...] annotation uses the basename of each trace file."""
        file_a, file_b = _make_traces(tmp_path)
        out, rc = _run(
            ["--mode", "llm-payloads", "--trace", str(file_a), "--trace", str(file_b)]
        )
        assert rc == 0
        assert "trace_h1.jsonl" in out
        assert "trace_h2.jsonl" in out

    def test_single_file_no_file_annotation(self, tmp_path: Path) -> None:
        """Tier 2: single --trace file does not add [file=...] noise to output."""
        file_a, _ = _make_traces(tmp_path)
        out, rc = _run(["--mode", "llm-payloads", "--trace", str(file_a)])
        assert rc == 0
        assert "file=" not in out


# ---------------------------------------------------------------------------
# (e) llm-detail cross-file lookup
# ---------------------------------------------------------------------------


class TestDetailCrossFileLookup:
    def test_detail_finds_request_in_second_file(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail with multiple --trace files finds a request_id in file B."""
        file_a, file_b = _make_traces(tmp_path)
        out, rc = _run(
            [
                "--mode", "llm-detail", REQUEST_ID_H2,
                "--trace", str(file_a),
                "--trace", str(file_b),
            ]
        )
        assert rc == 0
        assert "phase:h2" in out

    def test_tools_schema_finds_request_in_second_file(self, tmp_path: Path) -> None:
        """Tier 2: llm-tools-schema with multiple --trace files finds a request_id in file B."""
        file_a, file_b = _make_traces(tmp_path)
        out, rc = _run(
            [
                "--mode", "llm-tools-schema", REQUEST_ID_H2,
                "--trace", str(file_a),
                "--trace", str(file_b),
            ]
        )
        assert rc == 0
        # REQUEST_ID_H2 has 'some_tool' in its tools list
        assert "some_tool" in out


# ---------------------------------------------------------------------------
# (f) backward compat: single --trace <path> still works
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_single_trace_flag_still_works(self, tmp_path: Path) -> None:
        """Tier 2: single --trace <path> (legacy form) continues to work unchanged."""
        file_a, _ = _make_traces(tmp_path)
        out, rc = _run(["--mode", "llm-payloads", "--trace", str(file_a)])
        assert rc == 0
        assert REQUEST_ID_H1 in out
        assert REQUEST_ID_H3 in out
        # File B records should NOT appear when only file A is given
        assert REQUEST_ID_H2 not in out

    def test_single_trace_detail_still_works(self, tmp_path: Path) -> None:
        """Tier 2: llm-detail with single --trace still locates a request_id."""
        file_a, _ = _make_traces(tmp_path)
        out, rc = _run(
            ["--mode", "llm-detail", REQUEST_ID_H1, "--trace", str(file_a)]
        )
        assert rc == 0
        assert "router" in out


# ---------------------------------------------------------------------------
# (g) absent file path => clear error
# ---------------------------------------------------------------------------


class TestAbsentFilePath:
    def test_absent_trace_file_exits_nonzero(self, tmp_path: Path) -> None:
        """Tier 2: a non-existent file path in --trace causes a non-zero exit."""
        out, rc = _run(
            ["--mode", "llm-payloads", "--trace", str(tmp_path / "ghost.jsonl")]
        )
        assert rc != 0

    def test_absent_trace_file_error_message(self, tmp_path: Path) -> None:
        """Tier 2: a non-existent file path in --trace produces a 'not found' message."""
        missing = tmp_path / "ghost.jsonl"
        out, rc = _run(["--mode", "llm-payloads", "--trace", str(missing)])
        assert rc != 0
        assert "not found" in out.lower() or "ghost.jsonl" in out

    def test_second_trace_file_absent_exits_nonzero(self, tmp_path: Path) -> None:
        """Tier 2: even when the first --trace file exists, a missing second file exits non-zero."""
        file_a, _ = _make_traces(tmp_path)
        out, rc = _run(
            [
                "--mode", "llm-payloads",
                "--trace", str(file_a),
                "--trace", str(tmp_path / "also_ghost.jsonl"),
            ]
        )
        assert rc != 0
