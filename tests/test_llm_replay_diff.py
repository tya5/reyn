"""Tests for the --diff mode in scripts/llm_replay.py.

Tier 2: OS invariant — verifies diff computation, output formatting,
N-shot aggregation, --patch + --diff combination, and end-to-end
integration with a real fake LLM.

Testing policy compliance:
- No MagicMock / AsyncMock / patch.  Real callable stubs only.
- No private-state assertions.
- No algorithm-level pins (sort order, dict iteration order).
- Tier declared in each docstring's first line.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Import helper — same pattern as the other llm_replay test files
# ---------------------------------------------------------------------------
from pathlib import Path as _Path
from typing import Any

_SCRIPTS_DIR = _Path(__file__).parent.parent / "scripts"


def _import_replay():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llm_replay", _SCRIPTS_DIR / "llm_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Real callable stubs (no mocks)
# ---------------------------------------------------------------------------


def _make_tool_call_stub(name: str, arguments: str = "{}") -> Any:
    fn = type("_Fn", (), {"name": name, "arguments": arguments})()
    return type("_TC", (), {"id": "tc1", "function": fn})()


def _make_resp(
    content: str | None = None,
    tool_calls: list | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
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


class _FixedLLM:
    """Real async callable stub — returns a configurable fixed response."""

    def __init__(
        self,
        content: str | None = "replay result",
        tool_calls: list | None = None,
        finish_reason: str = "stop",
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.finish_reason = finish_reason
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _make_resp(
            content=self.content,
            tool_calls=self.tool_calls,
            finish_reason=self.finish_reason,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


class _RotatingLLM:
    """Real async callable stub — rotates through a list of response specs."""

    def __init__(self, responses: list[dict]) -> None:
        """Each entry: {content, tool_calls, finish_reason}."""
        self._responses = responses
        self._idx = 0
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        spec = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        tc_stubs = []
        for tc in spec.get("tool_calls") or []:
            tc_stubs.append(_make_tool_call_stub(tc["name"], tc.get("args", "{}")))
        return _make_resp(
            content=spec.get("content"),
            tool_calls=tc_stubs,
            finish_reason=spec.get("finish_reason", "stop"),
        )


# ---------------------------------------------------------------------------
# Trace builder helpers
# ---------------------------------------------------------------------------


def _write_trace(
    path: Path,
    request_id: str,
    *,
    orig_content: str | None = "original response",
    orig_tool_calls: list[dict] | None = None,
    orig_finish_reason: str = "stop",
    model: str = "gemini-2.5-flash-lite",
    include_response: bool = True,
) -> None:
    """Write a minimal request + optional response JSONL trace."""
    req = {
        "kind": "request",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "model": model,
        "caller_hint": "test",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": None,
        "tool_choice": None,
        "sampling_params": {},
    }
    tc_list = orig_tool_calls or []
    resp = {
        "kind": "response",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:01+00:00",
        "content": orig_content,
        "tool_calls": tc_list,
        "finish_reason": orig_finish_reason,
        "usage": {"prompt_tokens": 80, "completion_tokens": 20},
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(req) + "\n")
        if include_response:
            f.write(json.dumps(resp) + "\n")


def _write_trace_with_tools(
    path: Path,
    request_id: str,
    *,
    orig_tool_calls: list[dict],
    orig_finish_reason: str = "tool_calls",
) -> None:
    """Write a trace where original response has tool calls."""
    tools = [{"type": "function", "function": {"name": "invoke_skill", "description": "run", "parameters": {}}}]
    req = {
        "kind": "request",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "router",
        "messages": [{"role": "user", "content": "do something"}],
        "tools": tools,
        "tool_choice": "auto",
        "sampling_params": {},
    }
    resp = {
        "kind": "response",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:01+00:00",
        "content": None,
        "tool_calls": orig_tool_calls,
        "finish_reason": orig_finish_reason,
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(req) + "\n")
        f.write(json.dumps(resp) + "\n")


# ---------------------------------------------------------------------------
# (a) exact match: original == replay → match=exact
# ---------------------------------------------------------------------------


class TestDiffExactMatch:
    def test_exact_match(self) -> None:
        """Tier 2: _compute_diff returns match=exact when content + tool_calls + finish_reason all match."""
        mod = _import_replay()
        original = {
            "content": "hello world",
            "tool_calls": [],
            "finish_reason": "stop",
        }
        replay = {
            "content": "hello world",
            "tool_calls": [],
            "finish_reason": "stop",
        }
        d = mod._compute_diff(original, replay)
        assert d["match"] == "exact"
        assert d["content_diff"] is None
        assert d["finish_reason_match"] is True

    def test_exact_match_end_to_end(self, tmp_path: Path, capsys) -> None:
        """Tier 2: --diff with identical original and replay produces match=exact in output."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-a-001"
        _write_trace(path, rid, orig_content="hello world", orig_finish_reason="stop")

        stub = _FixedLLM(content="hello world", finish_reason="stop")
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "exact" in out


# ---------------------------------------------------------------------------
# (b) content differs → match=different + content_diff present
# ---------------------------------------------------------------------------


class TestDiffContentChanged:
    def test_content_diff_produced(self) -> None:
        """Tier 2: _compute_diff returns non-None content_diff when content differs."""
        mod = _import_replay()
        original = {
            "content": "line one\nline two",
            "tool_calls": [],
            "finish_reason": "stop",
        }
        replay = {
            "content": "line one\nline THREE",
            "tool_calls": [],
            "finish_reason": "stop",
        }
        d = mod._compute_diff(original, replay)
        assert d["match"] in ("different", "partial")
        assert d["content_diff"] is not None
        assert "line THREE" in d["content_diff"] or "line two" in d["content_diff"]

    def test_content_diff_in_output(self, tmp_path: Path, capsys) -> None:
        """Tier 2: content diff appears in pretty output when content differs."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-b-001"
        _write_trace(path, rid, orig_content="original text", orig_finish_reason="stop")

        stub = _FixedLLM(content="changed text", finish_reason="stop")
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "Diff" in out or "diff" in out.lower()
        # content change must be reflected
        assert "different" in out or "partial" in out or "Content" in out


# ---------------------------------------------------------------------------
# (c) tool_calls name same, args differ → match=partial
# ---------------------------------------------------------------------------


class TestDiffToolCallsArgsChanged:
    def test_args_change_is_partial(self) -> None:
        """Tier 2: same tool name but different args → match=partial."""
        mod = _import_replay()
        original = {
            "content": None,
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "invoke_skill", "arguments": '{"name":"skill_a"}'}}],
            "finish_reason": "tool_calls",
        }
        replay = {
            "content": None,
            "tool_calls": [{"id": "t2", "type": "function", "function": {"name": "invoke_skill", "arguments": '{"name":"skill_b"}'}}],
            "finish_reason": "tool_calls",
        }
        d = mod._compute_diff(original, replay)
        assert d["match"] == "partial"
        tc_diff = d["tool_calls_diff"]
        assert tc_diff is not None
        assert len(tc_diff["changed"]) == 1
        assert tc_diff["changed"][0]["name"] == "invoke_skill"


# ---------------------------------------------------------------------------
# (d) tool_calls name differs → match=different
# ---------------------------------------------------------------------------


class TestDiffToolCallsNameChanged:
    def test_different_name_is_different(self) -> None:
        """Tier 2: different tool call name → match=different."""
        mod = _import_replay()
        original = {
            "content": None,
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "invoke_skill", "arguments": "{}"}}],
            "finish_reason": "tool_calls",
        }
        replay = {
            "content": None,
            "tool_calls": [{"id": "t2", "type": "function", "function": {"name": "list_skills", "arguments": "{}"}}],
            "finish_reason": "tool_calls",
        }
        d = mod._compute_diff(original, replay)
        assert d["match"] == "different"
        tc_diff = d["tool_calls_diff"]
        assert tc_diff is not None
        removed_names = [tc["function"]["name"] for tc in tc_diff["removed"]]
        added_names = [tc["function"]["name"] for tc in tc_diff["added"]]
        assert "invoke_skill" in removed_names
        assert "list_skills" in added_names


# ---------------------------------------------------------------------------
# (e) finish_reason differs → reflected in diff output
# ---------------------------------------------------------------------------


class TestDiffFinishReasonChanged:
    def test_finish_reason_mismatch_reflected(self) -> None:
        """Tier 2: differing finish_reason sets finish_reason_match=False."""
        mod = _import_replay()
        original = {
            "content": "x",
            "tool_calls": [],
            "finish_reason": "stop",
        }
        replay = {
            "content": "x",
            "tool_calls": [],
            "finish_reason": "tool_calls",
        }
        d = mod._compute_diff(original, replay)
        assert d["finish_reason_match"] is False
        assert "stop" in d["summary_line"] or "tool_calls" in d["summary_line"]

    def test_finish_reason_mismatch_in_output(self, tmp_path: Path, capsys) -> None:
        """Tier 2: finish_reason change appears in pretty output."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-e-001"
        _write_trace(path, rid, orig_content="x", orig_finish_reason="stop")

        # replay returns same content but different finish_reason
        stub = _FixedLLM(content="x", finish_reason="length")
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "Finish reason" in out


# ---------------------------------------------------------------------------
# (f) original response absent → warning emitted, no crash
# ---------------------------------------------------------------------------


class TestDiffMissingOriginalResponse:
    def test_no_response_record_emits_warning(self, tmp_path: Path, capsys) -> None:
        """Tier 2: --diff with no response record in trace emits a warning and runs without crash."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-f-001"
        _write_trace(path, rid, orig_content="x", include_response=False)

        stub = _FixedLLM(content="y")
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            diff=True,
            acompletion_fn=stub,
        ))

        # should produce a warning, not a crash
        err = capsys.readouterr().err
        assert "warning" in err.lower() or "no response record" in err.lower() or "diff" in err.lower()
        assert len(stub.calls) == 1  # LLM was still called


# ---------------------------------------------------------------------------
# (g) N-shot diff: aggregated table appears with match distribution
# ---------------------------------------------------------------------------


class TestDiffNShot:
    def test_nshot_diff_summary_produced(self, tmp_path: Path, capsys) -> None:
        """Tier 2: --diff --n 3 produces N-shot diff summary with match counts."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-g-001"
        _write_trace(path, rid, orig_content="original", orig_finish_reason="stop")

        # Alternate: first run exact, second different, third exact
        stub = _RotatingLLM([
            {"content": "original", "finish_reason": "stop"},
            {"content": "changed",  "finish_reason": "stop"},
            {"content": "original", "finish_reason": "stop"},
        ])

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=3,
            full=False,
            output_format="pretty",
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        # N-shot diff summary must appear
        assert "N-shot diff summary" in out
        assert "match=exact" in out or "exact" in out
        assert "Finish reason matches" in out

    def test_nshot_diff_counts_correct(self, tmp_path: Path, capsys) -> None:
        """Tier 2: N-shot diff match counts reflect actual per-run results."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-g-002"
        _write_trace(path, rid, orig_content="abc", orig_finish_reason="stop")

        # All 4 runs return different content → all should be different
        stub = _FixedLLM(content="xyz", finish_reason="stop")

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=4,
            full=False,
            output_format="pretty",
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        # At least the different count should appear as 4 (100%)
        assert "4" in out


# ---------------------------------------------------------------------------
# (h) --patch + --diff: patched result diffed against original
# ---------------------------------------------------------------------------


class TestDiffWithPatch:
    def test_patch_and_diff_combined(self, tmp_path: Path, capsys) -> None:
        """Tier 2: --patch changes payload; --diff shows difference from original response."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-h-001"
        _write_trace(path, rid, orig_content="original text", orig_finish_reason="stop")

        stub = _FixedLLM(content="patched replay", finish_reason="stop")
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=['messages[0].content="patched input"'],
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        # Patch summary must appear
        assert "Applied patches" in out
        # Diff must appear (content changed from original)
        assert "Diff" in out or "diff" in out.lower()
        # LLM was called with patched payload
        assert stub.calls[0]["messages"][0]["content"] == "patched input"


# ---------------------------------------------------------------------------
# (i) --output-format json + --diff → machine-readable diff
# ---------------------------------------------------------------------------


class TestDiffJsonFormat:
    def test_json_output_format_with_diff(self, tmp_path: Path, capsys) -> None:
        """Tier 2: --output-format json --diff outputs machine-readable diff dict."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-i-001"
        _write_trace(path, rid, orig_content="original", orig_finish_reason="stop")

        stub = _FixedLLM(content="changed", finish_reason="stop")
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="json",
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        # Output must contain at least one parseable JSON block with "match" key
        parsed_objects = []
        for line in out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
        # Try to parse the full output as JSON objects separated by blank lines
        raw = out.strip()
        # The output may contain two JSON objects (result + diff) or interleaved;
        # try parsing the first complete JSON object
        depth = 0
        start = None
        for idx, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start = idx
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(raw[start:idx + 1])
                        parsed_objects.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start = None

        # At least one of the parsed JSON objects must have a "match" key
        match_keys = [obj for obj in parsed_objects if "match" in obj]
        assert match_keys, f"No JSON object with 'match' key found. Output was:\n{out}"
        diff_obj = match_keys[0]
        assert diff_obj["match"] in ("exact", "partial", "different")
        assert "finish_reason_match" in diff_obj


# ---------------------------------------------------------------------------
# (j) integration: end-to-end with fixture trace containing both record types
# ---------------------------------------------------------------------------


class TestDiffIntegration:
    def test_integration_full_trace(self, tmp_path: Path, capsys) -> None:
        """Tier 2: end-to-end with a fixture trace; --diff produces correct output."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-j-001"

        # Write a realistic trace with tool_calls in original
        orig_tc = {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "invoke_skill", "arguments": '{"name":"skill_router"}'},
        }
        _write_trace_with_tools(path, rid, orig_tool_calls=[orig_tc])

        # Replay returns same tool call name but different args
        tc_stub = _make_tool_call_stub("invoke_skill", '{"name":"skill_improver"}')
        stub = _FixedLLM(content=None, tool_calls=[tc_stub], finish_reason="tool_calls")

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        # Must show diff section
        assert "Diff" in out
        # Match should be partial (same name, different args)
        assert "partial" in out
        # finish_reason info must appear
        assert "Finish reason" in out

    def test_exact_tool_call_match(self, tmp_path: Path, capsys) -> None:
        """Tier 2: identical tool_calls in original and replay → match=exact."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "diff-j-002"

        orig_tc = {
            "id": "call_xyz",
            "type": "function",
            "function": {"name": "invoke_skill", "arguments": '{"name":"skill_a"}'},
        }
        _write_trace_with_tools(path, rid, orig_tool_calls=[orig_tc])

        tc_stub = _make_tool_call_stub("invoke_skill", '{"name":"skill_a"}')
        stub = _FixedLLM(content=None, tool_calls=[tc_stub], finish_reason="tool_calls")

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            diff=True,
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "exact" in out
