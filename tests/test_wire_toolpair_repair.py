"""Tier 2: wire-format tool_call ↔ tool_result pairing repair (the safety-net guarantee).

`repair_tool_call_pairing` is a pure, full-list repair applied at the single provider-call
choke-point (`recorded_acompletion`). It guarantees the assistant.tool_calls ↔ role=tool pairing
invariant on the FINAL wire payload — so a split from compaction/decompose (a dangling tool_call
whose result was elided, or an orphan result whose call was elided) can never reach the provider
as a 400. Full-list (not per-segment) pairing means an intact pair split only across a segment
boundary is left untouched.

Levels: (1) the pure function on assembled lists; (2) the choke-point wiring — the repair is
applied inside `recorded_acompletion` before litellm; (3) the real decompose+assemble path
producing the split.
"""
from __future__ import annotations

import json

import pytest

from reyn.llm.wire_format import _INTERRUPTED_TOOL_RESULT, repair_tool_call_pairing


def _tc(id: str, name: str = "some_tool") -> dict:
    return {"id": id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _assistant(tool_call_ids: list[str]) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [_tc(i) for i in tool_call_ids]}


def _tool(id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": id, "content": content}


def _user(text: str = "hi") -> dict:
    return {"role": "user", "content": text}


def _is_interrupted(m: dict) -> bool:
    try:
        return json.loads(m["content"]).get("error", {}).get("kind") == "interrupted"
    except Exception:
        return False


def _declared(msgs: list[dict]) -> set[str]:
    return {
        tc["id"] for m in msgs
        if m.get("role") == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    }


def _answered(msgs: list[dict]) -> set[str]:
    return {m["tool_call_id"] for m in msgs if m.get("role") == "tool"}


# ── Level 1: pure function ───────────────────────────────────────────────────


def test_no_tool_calls_passthrough():
    """Tier 2: messages with no tool_calls are returned unchanged."""
    msgs = [_user("hello"), {"role": "assistant", "content": "hi"}]
    assert repair_tool_call_pairing(msgs) == msgs


def test_fully_answered_pair_untouched():
    """Tier 2: an assistant tool_call with its matching result → no injection, no drop."""
    msgs = [_assistant(["id-1"]), _tool("id-1")]
    assert repair_tool_call_pairing(msgs) == msgs


def test_dangling_tool_call_synthesizes_interrupted_result():
    """Tier 2: an assistant tool_call with NO result anywhere → a synthetic interrupted result is
    injected immediately after the assistant."""
    msgs = [_user(), _assistant(["dangling"]), _user("next")]
    out = repair_tool_call_pairing(msgs)
    # the synthetic result is right after the assistant, answers the dangling id, marked interrupted.
    assert out[2] == {"role": "tool", "tool_call_id": "dangling", "content": _INTERRUPTED_TOOL_RESULT}
    assert _is_interrupted(out[2])
    assert _declared(out) <= _answered(out), "every tool_call answered post-repair"


def test_orphan_result_is_dropped():
    """Tier 2: a role=tool whose tool_call_id is declared by NO assistant → dropped (the call was
    elided; it cannot be synthesized)."""
    msgs = [_user(), {"role": "assistant", "content": "summary"}, _tool("orphan"), _user("next")]
    out = repair_tool_call_pairing(msgs)
    assert _tool("orphan") not in out, "orphan result must be dropped"
    assert _answered(out) == set(), "no tool result survives (its call is gone)"
    assert out == [_user(), {"role": "assistant", "content": "summary"}, _user("next")]


def test_intact_cross_segment_pair_untouched():
    """Tier 2: THE PRIMARY case (the owner's observed failure, tui-confirmed) — build_history's
    elide path yields ``[assistant+tool_calls(tc-1), bridge(assistant), role=tool(tc-1)]``: an INTACT
    pair separated by the bridge. #2287's adjacency repair BREAKS it (looks ahead, sees the bridge,
    synthesizes a DUPLICATE result for tc-1 → the real result orphaned → 400). The full-list repair
    leaves it EXACTLY as-is: tc-1 ∈ declared ∧ ∈ answered → neither dangling nor orphan → untouched,
    no duplicate. RED against #2287's per-segment/adjacency logic."""
    msgs = [
        _assistant(["tc-1"]),                                   # call in "head"
        {"role": "assistant", "content": "bridge summary of elided middle"},  # the bridge between
        _tool("tc-1", "the real result"),                       # its REAL result in "tail"
    ]
    out = repair_tool_call_pairing(msgs)
    assert out == msgs, "the intact cross-segment pair must be untouched (no dup synth, no drop)"
    # explicit no-duplicate check: exactly ONE result for tc-1 (not the synth + the real).
    assert sum(1 for m in out if m.get("tool_call_id") == "tc-1") == 1


# ── Logging (observability — a repair firing = a split reached the wire) ──────────────────────


def test_dangling_synth_logs_warning(caplog):
    """Tier 2: synthesizing a dangling result logs a WARNING with the id (owner observability)."""
    import logging
    with caplog.at_level(logging.WARNING, logger="reyn.llm.wire_format"):
        repair_tool_call_pairing([_assistant(["d"]), _user("x")])
    assert any(
        "synthesized an interrupted result for dangling tool_call d" in r.message
        for r in caplog.records
    ), "a dangling synth must log a WARNING"


def test_orphan_drop_logs_warning(caplog):
    """Tier 2: dropping an orphan result logs a WARNING with the id (owner observability)."""
    import logging
    with caplog.at_level(logging.WARNING, logger="reyn.llm.wire_format"):
        repair_tool_call_pairing([{"role": "assistant", "content": "s"}, _tool("orph")])
    assert any(
        "dropped orphan tool_result orph" in r.message for r in caplog.records
    ), "an orphan drop must log a WARNING"


def test_intact_cross_segment_pair_does_not_log(caplog):
    """Tier 2: the intact cross-segment pair triggers NO repair → NO log. Secondary over-repair
    guard: if the repair wrongly touched an intact pair, this WARNING would fire."""
    import logging
    with caplog.at_level(logging.WARNING, logger="reyn.llm.wire_format"):
        repair_tool_call_pairing(
            [_assistant(["tc-1"]), {"role": "assistant", "content": "bridge"}, _tool("tc-1")]
        )
    assert [r for r in caplog.records if r.name == "reyn.llm.wire_format"] == [], (
        "an intact pair must produce NO repair and NO log"
    )


def test_mixed_dangling_orphan_and_intact():
    """Tier 2: dangling synth + orphan drop + intact pair, all in one list, resolved together."""
    msgs = [
        _assistant(["dangle"]),          # dangling (no result)
        {"role": "assistant", "content": "summary"},
        _tool("orphan"),                 # orphan (no declaring call)
        _assistant(["keep"]), _tool("keep"),  # intact
    ]
    out = repair_tool_call_pairing(msgs)
    d, a = _declared(out), _answered(out)
    assert d == a, f"declared/answered must match exactly post-repair; declared={d} answered={a}"
    assert "dangle" in a and "keep" in a, "dangling synthesized, intact kept"
    assert "orphan" not in a, "orphan dropped"


def test_partial_answer_synthesizes_only_missing():
    """Tier 2: an assistant with two calls, one answered → only the UNANSWERED one is synthesized;
    the answered one keeps its real result (not re-synthesized)."""
    msgs = [_assistant(["a", "b"]), _tool("a", "the real result")]
    out = repair_tool_call_pairing(msgs)
    assert _answered(out) == {"a", "b"}, "both calls answered post-repair"
    b_results = [m for m in out if m.get("tool_call_id") == "b"]
    assert b_results and all(_is_interrupted(m) for m in b_results), (
        "the unanswered id 'b' is answered by an interrupted synthetic result"
    )
    a_results = [m for m in out if m.get("tool_call_id") == "a"]
    assert a_results and not any(_is_interrupted(m) for m in a_results), (
        "the already-answered id 'a' keeps its real result — not re-synthesized"
    )


def test_empty_list():
    """Tier 2: empty input → empty output."""
    assert repair_tool_call_pairing([]) == []


# ── Level 2: choke-point wiring (the repair is applied inside recorded_acompletion) ──────────


@pytest.mark.asyncio
async def test_recorded_acompletion_repairs_before_provider_call(monkeypatch):
    """Tier 2: `recorded_acompletion` applies the pairing repair to the FINAL wire messages before
    the litellm call — a dangling call AND an orphan result in the assembled payload never reach the
    provider. RED if the repair is not wired at the choke-point (the payload passes through raw)."""
    import litellm

    from reyn.llm.llm import recorded_acompletion

    captured: dict = {}

    class _StopBeforeProvider(Exception):
        pass

    async def _capture(*, model, messages, **kwargs):
        captured["messages"] = messages
        raise _StopBeforeProvider()

    monkeypatch.setattr(litellm, "acompletion", _capture)

    # Assembled payload (head + summary + tail shape) with BOTH a dangling call and an orphan result
    # plus an intact pair that must survive.
    assembled = [
        _user("q"),
        _assistant(["call-head"]),                            # dangling: result elided
        {"role": "assistant", "content": "summary of elided middle"},
        _tool("orphan-id", "leftover"),                       # orphan: its call elided
        _assistant(["call-tail"]), _tool("call-tail"),        # intact pair
    ]
    with pytest.raises(_StopBeforeProvider):
        await recorded_acompletion(
            model="openai/gpt-4o", messages=assembled, purpose="main", routing={},
        )

    wire = captured["messages"]
    declared, answered = _declared(wire), _answered(wire)
    assert declared == answered, (
        f"the wire payload must satisfy the pairing invariant; declared={declared} answered={answered}"
    )
    assert "orphan-id" not in answered, "orphan result must be dropped before the provider"
    assert "call-head" in answered, "dangling call must be synthesized before the provider"
    assert "call-tail" in answered, "intact pair preserved"
