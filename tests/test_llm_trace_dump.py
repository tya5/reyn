"""Tests for LLM payload trace dump via REYN_LLM_TRACE_DUMP env var.

Tier 2: OS invariant — verifies the dump infrastructure's public behaviour:
file creation, JSONL parseability, record structure, caller_hint routing,
and complete no-op when the env var is absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers: minimal real-callable LLM stub (allowed by testing policy)
# ---------------------------------------------------------------------------

def _make_fake_litellm_response(content: str = '{"type":"decide","control":{"type":"finish","decision":"finish","next_phase":null,"confidence":0.9,"reason":{"summary":"ok"}},"artifact":{"type":"test","data":{}},"ops":[]}'):
    """Build a minimal response object that matches the litellm API surface."""
    msg = type("_Msg", (), {"content": content, "tool_calls": None})()
    choice = type("_Choice", (), {
        "message": msg,
        "finish_reason": "stop",
    })()
    usage = type("_Usage", (), {"prompt_tokens": 10, "completion_tokens": 5})()
    return type("_Resp", (), {"choices": [choice], "usage": usage})()


class _ScriptedLLM:
    """Real callable stub — allowed by testing policy (not MagicMock)."""

    def __init__(self, response_content: str = "{}"):
        self._response_content = response_content
        self.call_count = 0

    async def __call__(self, **kwargs):
        self.call_count += 1
        return _make_fake_litellm_response(self._response_content)


def _minimal_frame():
    from reyn.testing.replay import REPLAY_DATETIME
    from reyn.schemas.models import ContextFrame
    return ContextFrame(
        current_phase="test",
        instructions="Reply with a minimal valid JSON decide turn.",
        input_artifact={},
        candidate_outputs=[],
        output_language="en",
        current_datetime=REPLAY_DATETIME,
    )


def _minimal_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "run_skill",
                "description": "run a skill",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


MODEL = "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------------
# Tier 2 invariants
# ---------------------------------------------------------------------------

class TestTraceDumpDisabledByDefault:
    """Tier 2: when REYN_LLM_TRACE_DUMP is not set, no file is created."""

    def test_call_llm_no_dump_without_env_var(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: call_llm with env var absent leaves no trace file."""
        import asyncio
        import litellm

        # Ensure env var is absent
        monkeypatch.delenv("REYN_LLM_TRACE_DUMP", raising=False)

        # Reload llm module so _LLM_TRACE_DUMP_PATH picks up the cleared env var
        import reyn.llm.llm as llm_mod
        monkeypatch.setattr(llm_mod, "_LLM_TRACE_DUMP_PATH", None)

        stub = _ScriptedLLM('{"type":"decide","control":{"type":"finish","decision":"finish","next_phase":null,"confidence":0.9,"reason":{"summary":"ok"}},"artifact":{"type":"t","data":{}},"ops":[]}')
        monkeypatch.setattr(litellm, "acompletion", stub)

        dummy_trace = tmp_path / "should_not_exist.jsonl"
        frame = _minimal_frame()
        asyncio.run(llm_mod.call_llm(MODEL, frame, prompt_cache_enabled=False))

        assert not dummy_trace.exists(), "No trace file should be created when env var is absent"
        assert stub.call_count >= 1

    def test_call_llm_tools_no_dump_without_env_var(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: call_llm_tools with env var absent leaves no trace file."""
        import asyncio
        import litellm

        monkeypatch.delenv("REYN_LLM_TRACE_DUMP", raising=False)

        import reyn.llm.llm as llm_mod
        monkeypatch.setattr(llm_mod, "_LLM_TRACE_DUMP_PATH", None)

        stub = _ScriptedLLM()
        monkeypatch.setattr(litellm, "acompletion", stub)

        dummy_trace = tmp_path / "should_not_exist.jsonl"
        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
            )
        )

        assert not dummy_trace.exists()
        assert stub.call_count >= 1


class TestTraceDumpEnabled:
    """Tier 2: when REYN_LLM_TRACE_DUMP is set, call_llm writes paired records."""

    def test_call_llm_creates_jsonl_with_request_and_response(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: call_llm writes 1 request + 1 response entry when env var is set."""
        import asyncio
        import litellm

        trace_file = tmp_path / "trace.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        import reyn.llm.llm as llm_mod
        monkeypatch.setattr(llm_mod, "_LLM_TRACE_DUMP_PATH", str(trace_file))

        stub = _ScriptedLLM('{"type":"decide","control":{"type":"finish","decision":"finish","next_phase":null,"confidence":0.9,"reason":{"summary":"ok"}},"artifact":{"type":"t","data":{}},"ops":[]}')
        monkeypatch.setattr(litellm, "acompletion", stub)

        frame = _minimal_frame()
        asyncio.run(llm_mod.call_llm(MODEL, frame, prompt_cache_enabled=False))

        assert trace_file.exists(), "Trace file must be created"
        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 2, f"Expected 1 request + 1 response, got {len(records)}"

        req = next((r for r in records if r.get("kind") == "request"), None)
        resp = next((r for r in records if r.get("kind") == "response"), None)
        assert req is not None, "Must have a 'request' record"
        assert resp is not None, "Must have a 'response' record"

        # request_id must pair them
        assert req["request_id"] == resp["request_id"]

        # Required fields on request
        assert "model" in req
        assert "messages" in req
        assert "timestamp" in req
        assert "caller_hint" in req

        # Required fields on response
        assert "content" in resp
        assert "finish_reason" in resp
        assert "usage" in resp

    def test_call_llm_tools_creates_jsonl_with_tools_schema(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: call_llm_tools includes tools schema in request record."""
        import asyncio
        import litellm

        trace_file = tmp_path / "trace_tools.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        import reyn.llm.llm as llm_mod
        monkeypatch.setattr(llm_mod, "_LLM_TRACE_DUMP_PATH", str(trace_file))

        stub = _ScriptedLLM()
        monkeypatch.setattr(litellm, "acompletion", stub)

        tools = _minimal_tools()
        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=tools,
            )
        )

        assert trace_file.exists()
        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 2

        req = next(r for r in records if r.get("kind") == "request")
        assert req.get("tools") == tools, "Tools schema must be included verbatim in request"
        assert req.get("tool_choice") is not None


class TestCallerHint:
    """Tier 2: trace_caller is reflected in the dump as caller_hint."""

    def test_caller_hint_in_call_llm(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: trace_caller kwarg arrives as caller_hint in the request record."""
        import asyncio
        import litellm

        trace_file = tmp_path / "trace_caller.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        import reyn.llm.llm as llm_mod
        monkeypatch.setattr(llm_mod, "_LLM_TRACE_DUMP_PATH", str(trace_file))

        stub = _ScriptedLLM('{"type":"decide","control":{"type":"finish","decision":"finish","next_phase":null,"confidence":0.9,"reason":{"summary":"ok"}},"artifact":{"type":"t","data":{}},"ops":[]}')
        monkeypatch.setattr(litellm, "acompletion", stub)

        frame = _minimal_frame()
        asyncio.run(
            llm_mod.call_llm(MODEL, frame, prompt_cache_enabled=False, trace_caller="phase:my_phase")
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        req = next(r for r in records if r.get("kind") == "request")
        assert req["caller_hint"] == "phase:my_phase"

    def test_caller_hint_defaults_to_unknown(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: when trace_caller is not passed, caller_hint defaults to 'unknown'."""
        import asyncio
        import litellm

        trace_file = tmp_path / "trace_default.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        import reyn.llm.llm as llm_mod
        monkeypatch.setattr(llm_mod, "_LLM_TRACE_DUMP_PATH", str(trace_file))

        stub = _ScriptedLLM('{"type":"decide","control":{"type":"finish","decision":"finish","next_phase":null,"confidence":0.9,"reason":{"summary":"ok"}},"artifact":{"type":"t","data":{}},"ops":[]}')
        monkeypatch.setattr(litellm, "acompletion", stub)

        frame = _minimal_frame()
        asyncio.run(
            llm_mod.call_llm(MODEL, frame, prompt_cache_enabled=False)
            # No trace_caller kwarg
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        req = next(r for r in records if r.get("kind") == "request")
        assert req["caller_hint"] == "unknown"

    def test_caller_hint_in_call_llm_tools(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: trace_caller kwarg arrives as caller_hint in call_llm_tools request record."""
        import asyncio
        import litellm

        trace_file = tmp_path / "trace_caller_tools.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        import reyn.llm.llm as llm_mod
        monkeypatch.setattr(llm_mod, "_LLM_TRACE_DUMP_PATH", str(trace_file))

        stub = _ScriptedLLM()
        monkeypatch.setattr(litellm, "acompletion", stub)

        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
                trace_caller="router",
            )
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        req = next(r for r in records if r.get("kind") == "request")
        assert req["caller_hint"] == "router"


class TestMultipleCallsAccumulate:
    """Tier 2: consecutive calls append records, request_ids pair correctly."""

    def test_two_calls_produce_four_records(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: 2 consecutive call_llm calls produce 4 records (2 req + 2 resp) with distinct request_ids."""
        import asyncio
        import litellm

        trace_file = tmp_path / "trace_multi.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        import reyn.llm.llm as llm_mod
        monkeypatch.setattr(llm_mod, "_LLM_TRACE_DUMP_PATH", str(trace_file))

        stub = _ScriptedLLM('{"type":"decide","control":{"type":"finish","decision":"finish","next_phase":null,"confidence":0.9,"reason":{"summary":"ok"}},"artifact":{"type":"t","data":{}},"ops":[]}')
        monkeypatch.setattr(litellm, "acompletion", stub)

        frame = _minimal_frame()

        async def _run_two():
            await llm_mod.call_llm(MODEL, frame, prompt_cache_enabled=False, trace_caller="call1")
            await llm_mod.call_llm(MODEL, frame, prompt_cache_enabled=False, trace_caller="call2")

        asyncio.run(_run_two())

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 4, f"Expected 4 records, got {len(records)}"

        requests = [r for r in records if r.get("kind") == "request"]
        responses = [r for r in records if r.get("kind") == "response"]
        assert len(requests) == 2
        assert len(responses) == 2

        # request_ids must be distinct
        req_ids = {r["request_id"] for r in requests}
        assert len(req_ids) == 2, "Each call must produce a distinct request_id"

        # Each response must be paired with a request
        resp_ids = {r["request_id"] for r in responses}
        assert req_ids == resp_ids, "Response request_ids must match request request_ids"
