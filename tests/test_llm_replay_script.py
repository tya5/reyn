"""Unit tests for scripts/llm_replay.py (llm_replay CLI tool).

Tier 2: OS invariant — verifies the replay tool's public behaviour:
trace record lookup, payload construction, model override, N-shot call count,
sampling overrides, missing request_id error path, and output formatting.

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
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Minimal litellm response stub — real callable, testing-policy compliant
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
        self.calls: list[dict] = []  # record kwargs for assertion

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _make_resp(
            content=self.content,
            tool_calls=self.tool_calls,
            finish_reason=self.finish_reason,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


# ---------------------------------------------------------------------------
# Helper to build a minimal trace file
# ---------------------------------------------------------------------------


def _write_trace(path: Path, request_id: str, *, model: str = "gemini-2.5-flash-lite") -> None:
    req = {
        "kind": "request",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "model": model,
        "caller_hint": "test",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": None,
        "tool_choice": None,
        "sampling_params": {"timeout": 30, "max_retries": 1},
    }
    resp = {
        "kind": "response",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:01+00:00",
        "content": "original response",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 80, "completion_tokens": 20},
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(req) + "\n")
        f.write(json.dumps(resp) + "\n")


def _write_trace_with_tools(
    path: Path, request_id: str, *, model: str = "gemini-2.5-flash-lite"
) -> None:
    tools = [{"type": "function", "function": {"name": "invoke_skill", "description": "run", "parameters": {}}}]
    req = {
        "kind": "request",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "model": model,
        "caller_hint": "router",
        "messages": [{"role": "user", "content": "do something"}],
        "tools": tools,
        "tool_choice": "auto",
        "sampling_params": {},
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(req) + "\n")


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from pathlib import Path as _Path

_SCRIPTS_DIR = _Path(__file__).parent.parent / "scripts"


def _import_replay():
    """Import scripts/llm_replay.py as a module (not on sys.path by default)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llm_replay", _SCRIPTS_DIR / "llm_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBasicReplay:
    """Tier 2: basic replay reads trace and calls litellm with correct payload."""

    def test_payload_constructed_from_trace(self, tmp_path: Path) -> None:
        """Tier 2: _run() reads the trace record and submits the expected model + messages to litellm."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-001"
        _write_trace(trace, rid, model="gemini-2.5-flash-lite")

        stub = _FixedLLM()

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        assert len(stub.calls) == 1, "Expected exactly one litellm call"
        call = stub.calls[0]
        # model must be the one from the trace (proxy-stripped if needed)
        assert "gemini-2.5-flash-lite" in call["model"]
        assert call["messages"] == [{"role": "user", "content": "hello"}]

    def test_tools_passed_when_present(self, tmp_path: Path) -> None:
        """Tier 2: when trace record contains tools, they are forwarded to litellm."""
        mod = _import_replay()

        trace = tmp_path / "trace_tools.jsonl"
        rid = "test-request-id-tools"
        _write_trace_with_tools(trace, rid)

        stub = _FixedLLM()

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert "tools" in call, "tools must be forwarded when present in trace"
        assert call["tools"][0]["function"]["name"] == "invoke_skill"
        assert call.get("tool_choice") == "auto"


class TestModelOverride:
    """Tier 2: --model override replaces the model field submitted to litellm."""

    def test_model_override_applied(self, tmp_path: Path) -> None:
        """Tier 2: model_override replaces the original model in the litellm call."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-002"
        _write_trace(trace, rid, model="gemini-2.5-flash-lite")

        stub = _FixedLLM()

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override="claude-sonnet",
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        assert len(stub.calls) == 1
        call = stub.calls[0]
        # Override model must be used, not the original
        assert call["model"] == "claude-sonnet", (
            f"Expected override model 'claude-sonnet', got '{call['model']}'"
        )

    def test_model_override_shown_in_output(self, tmp_path: Path, capsys) -> None:
        """Tier 2: output includes both original and override model when --model is used."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-003"
        _write_trace(trace, rid, model="gemini-2.5-flash-lite")

        stub = _FixedLLM()

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override="claude-sonnet",
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "claude-sonnet" in out
        assert "gemini-2.5-flash-lite" in out or "original" in out.lower()


class TestNShot:
    """Tier 2: --n N causes exactly N litellm calls."""

    def test_n_calls_issued(self, tmp_path: Path) -> None:
        """Tier 2: _run() with n=3 issues exactly 3 litellm calls with identical payload."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-004"
        _write_trace(trace, rid)

        stub = _FixedLLM()

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=3,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        assert len(stub.calls) == 3, f"Expected 3 calls, got {len(stub.calls)}"
        # All calls must use the same messages
        for call in stub.calls:
            assert call["messages"] == [{"role": "user", "content": "hello"}]

    def test_nshot_summary_in_output(self, tmp_path: Path, capsys) -> None:
        """Tier 2: N-shot output contains distribution table markers."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-005"
        _write_trace(trace, rid)

        stub = _FixedLLM(finish_reason="stop")

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=3,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "N-shot replay" in out
        assert "Finish reasons" in out
        # Token stats appear when usage is present
        assert "Tokens" in out


class TestMissingRequestId:
    """Tier 2: a missing request_id produces a clear error message and exits."""

    def test_missing_id_exits_with_error(self, tmp_path: Path) -> None:
        """Tier 2: request_id not found in trace causes SystemExit with error output."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "exists-id"
        _write_trace(trace, rid)

        stub = _FixedLLM()

        with pytest.raises(SystemExit):
            asyncio.run(mod._run(
                request_id="does-not-exist",
                trace_path=trace,
                model_override=None,
                temperature_override=None,
                max_tokens_override=None,
                n=1,
                full=False,
                output_format="pretty",
                acompletion_fn=stub,
            ))

        # The stub must NOT have been called since lookup failed first
        assert len(stub.calls) == 0


class TestSamplingOverrides:
    """Tier 2: temperature and max_tokens overrides are reflected in litellm call kwargs."""

    def test_temperature_override_forwarded(self, tmp_path: Path) -> None:
        """Tier 2: temperature_override is included in litellm call kwargs."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-006"
        _write_trace(trace, rid)

        stub = _FixedLLM()

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override=None,
            temperature_override=0.7,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        assert len(stub.calls) == 1
        assert stub.calls[0].get("temperature") == pytest.approx(0.7)

    def test_max_tokens_override_forwarded(self, tmp_path: Path) -> None:
        """Tier 2: max_tokens_override is included in litellm call kwargs."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-007"
        _write_trace(trace, rid)

        stub = _FixedLLM()

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override=None,
            temperature_override=None,
            max_tokens_override=512,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        assert len(stub.calls) == 1
        assert stub.calls[0].get("max_tokens") == 512


class TestNShotTableFormat:
    """Tier 2: N-shot summary table contains expected section labels."""

    def test_table_contains_tool_call_section(self, tmp_path: Path, capsys) -> None:
        """Tier 2: N-shot output with tool_calls response contains 'Tool calls' section."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-008"
        _write_trace(trace, rid)

        tc_stub = _make_tool_call_stub("invoke_skill", '{"name":"my_skill"}')
        stub = _FixedLLM(
            content=None,
            tool_calls=[tc_stub],
            finish_reason="tool_calls",
        )

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=3,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "Tool calls" in out
        assert "invoke_skill" in out
        assert "Finish reasons" in out


class TestCrossModelDiff:
    """Tier 2: cross-model diff output contains both original and override model names."""

    def test_diff_output_contains_both_models(self, tmp_path: Path, capsys) -> None:
        """Tier 2: with model_override, output includes original and override model in diff section."""
        mod = _import_replay()

        trace = tmp_path / "trace.jsonl"
        rid = "test-request-id-009"
        _write_trace(trace, rid, model="gemini-2.5-flash-lite")

        stub = _FixedLLM(content="override response")

        asyncio.run(mod._run(
            request_id=rid,
            trace_path=trace,
            model_override="openai/gpt-4o",
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        # Both original and override model must appear somewhere in the output
        assert "gemini-2.5-flash-lite" in out or "original" in out.lower()
        assert "gpt-4o" in out or "openai" in out
