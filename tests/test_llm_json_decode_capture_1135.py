"""Tier 2: FP-0008 #1135 sibling — capture raw LLM output on pre-parse JSON-decode failure.

`call_llm` previously raised a bare ValueError (only the first 800 chars of the
raw in its message) when the model's output failed to parse after the JSON-repair
retry — so the failure (e.g. run3's malformed-escape case in an apply file edit)
was undiagnosable from the always-on events log; the raw lived only in the
opt-in REYN_LLM_TRACE_DUMP.

Per the canonical #1135 contract (opt A, inline-cap, llm layer): emit an additive
`llm_output_json_decode_failed` event carrying the raw (truncated to a window
around the decode position). No offload (the llm layer has no state_dir).

Unit tests of the truncation helper + a behavioral test driving the real
`call_llm` JSON-decode-fail path with a stubbed `litellm.acompletion` (a real
async callable — allowed by testing policy, not MagicMock). Docstring opens "Tier 2:".
"""
from __future__ import annotations

import pytest

from reyn.llm.llm import _JSON_DECODE_RAW_CAP, _truncate_json_for_event

# ── unit: truncation helper ───────────────────────────────────────────────────


def test_truncate_returns_raw_unchanged_when_within_cap() -> None:
    """Tier 2: raw ≤ cap is returned verbatim."""
    raw = '{"k": "v"}'
    assert _truncate_json_for_event(raw, pos=3) == raw


def test_truncate_windows_around_pos_when_oversized() -> None:
    """Tier 2: oversized raw → a cap-sized window centered on the error position, with markers."""
    raw = "A" * 5000 + "X" + "B" * 5000  # > cap, malformation 'X' at pos 5000
    out = _truncate_json_for_event(raw, pos=5000, cap=100)
    assert len(out) < len(raw)
    assert "X" in out, "the window must include the error position"
    assert "bytes before]" in out and "bytes after]" in out
    # the marker byte-accounting must sum to the original length
    import re
    before = int(re.search(r"\[(\d+) bytes before\]", out).group(1))
    after = int(re.search(r"\[(\d+) bytes after\]", out).group(1))
    assert before + 100 + after == len(raw)


def test_truncate_head_when_pos_unknown() -> None:
    """Tier 2: oversized raw with no position → head slice + truncation marker."""
    raw = "z" * 20000
    out = _truncate_json_for_event(raw, pos=None, cap=8192)
    assert out.startswith("z" * 8192)
    assert "truncated" in out and str(20000 - 8192) in out


def test_cap_default_is_8192() -> None:
    """Tier 2: the inline cap constant is 8192 (canonical contract)."""
    assert _JSON_DECODE_RAW_CAP == 8192


# ── behavioral: call_llm emits the event on a JSON-decode failure ─────────────


def _fake_litellm_response(content: str):
    msg = type("_Msg", (), {"content": content, "tool_calls": None})()
    choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
    usage = type("_Usage", (), {"prompt_tokens": 10, "completion_tokens": 5})()
    return type("_Resp", (), {"choices": [choice], "usage": usage})()


class _ScriptedBadJSON:
    """Real async callable stub returning unparseable JSON (allowed by testing policy)."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.call_count = 0

    async def __call__(self, **kwargs):
        self.call_count += 1
        return _fake_litellm_response(self._content)


@pytest.mark.asyncio
async def test_call_llm_emits_json_decode_failed_event(monkeypatch) -> None:
    """Tier 2: a JSON-decode failure in call_llm emits llm_output_json_decode_failed with the raw.

    Drives the real call_llm path; litellm.acompletion is a real async stub
    returning genuinely malformed JSON (an unclosed object) — unrecoverable by
    every lenient tier including the D6 invalid-escape repair, so the failure
    path still fires. Asserts the additive event fires with failure_kind + error
    + raw_output, and that the run still raises (behavior preserved).
    """
    import litellm

    from reyn.core.events.events import EventLog
    from reyn.dev.testing.replay import REPLAY_DATETIME
    from reyn.llm.llm import call_llm
    from reyn.schemas.models import ContextFrame

    # Unclosed object (missing final `}`): genuinely malformed, so neither the
    # escape-repair (D6) nor raw_decode can recover a complete leading value.
    bad = '{"type": "act", "ops": [{"path": "x"}]'  # unclosed → JSONDecodeError
    monkeypatch.setattr(litellm, "acompletion", _ScriptedBadJSON(bad))

    events = EventLog()
    frame = ContextFrame(
        current_phase="apply",
        instructions="edit",
        input_artifact={},
        candidate_outputs=[],
        output_language="en",
        current_datetime=REPLAY_DATETIME,
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        await call_llm("gemini-2.5-flash-lite", frame, timeout=5, max_retries=0, event_log=events)

    evs = [e for e in events.all() if e.type == "llm_output_json_decode_failed"]
    assert evs, "a JSON-decode failure must emit llm_output_json_decode_failed"
    d = evs[-1].data
    assert d["failure_kind"] == "json_decode"
    assert d["error"]  # the JSONDecodeError message
    assert "ops" in d["raw_output"], "the raw model output must be captured"
    assert d["raw_output_ref"] is None if "raw_output_ref" in d else True  # opt A: inline only
