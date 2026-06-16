"""Tier 2: #1683 — the interactive chat path emits cost events so the TUI cost tab updates.

The chat path recorded cost to the in-memory recorder (→ header) but emitted NO
usage event, so the TUI cost tab (which reads `llm_called` then accumulates
tokens/cost on `llm_response_received` from the events log) stayed empty. The fix:
chat callers pass `emit_cost_events=True` to `recorded_acompletion`, which then
emits BOTH events via the #1669 ambient EventLog. The kernel/phase path leaves the
flag False (it emits these via LLMCallRecorder — emitting here too would
double-count).

No mocks: real `recorded_acompletion`, a real async fake for `litellm.acompletion`
(returning a usage-bearing response), a real `EventLog`, and the real `render_cost`
over a synthetic events tree.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import litellm
import pytest

from reyn.events.events import EventLog, set_llm_request_event_log
from reyn.interfaces.tui.widgets.right_panel.cost_tab import (
    render_cost,  # #2 moved chat/tui → reyn/tui
)
from reyn.llm.llm import recorded_acompletion


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    yield
    set_llm_request_event_log(None)


def _usage_response(prompt=100, completion=50):
    async def _fn(**_kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
            choices=[],
        )
    return _fn


def _call(monkeypatch, *, emit_cost_events):
    return asyncio.run(recorded_acompletion(
        model="openai/gpt-4o", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None, emit_cost_events=emit_cost_events,
    ))


# ── flag=True (chat) emits both events; flag=False (kernel) does not ─────────────


def test_chat_flag_emits_both_cost_events(monkeypatch) -> None:
    """Tier 2: #1683 — emit_cost_events=True emits llm_called (model) THEN
    llm_response_received (prompt/completion tokens + cost_usd) — the pair the cost
    tab needs."""
    monkeypatch.setattr(litellm, "acompletion", _usage_response(100, 50))
    log = EventLog()
    set_llm_request_event_log(log)

    _call(monkeypatch, emit_cost_events=True)

    kinds = [e.type for e in log.all()]
    # order matters: llm_called before llm_response_received (pending_model).
    assert kinds.index("llm_called") < kinds.index("llm_response_received")
    called = next(e for e in log.all() if e.type == "llm_called")
    assert called.data["model"] == "openai/gpt-4o"
    resp = next(e for e in log.all() if e.type == "llm_response_received")
    assert resp.data["prompt_tokens"] == 100
    assert resp.data["completion_tokens"] == 50
    assert "cost_usd" in resp.data  # present (value may be None for an unknown model)


def test_no_flag_does_not_emit_cost_events(monkeypatch) -> None:
    """Tier 2: #1683 — the default (emit_cost_events=False, the kernel/phase path)
    emits NEITHER cost event here, so the kernel's LLMCallRecorder emission is not
    double-counted."""
    monkeypatch.setattr(litellm, "acompletion", _usage_response())
    log = EventLog()
    set_llm_request_event_log(log)

    _call(monkeypatch, emit_cost_events=False)

    kinds = [e.type for e in log.all()]
    assert "llm_called" not in kinds
    assert "llm_response_received" not in kinds


# ── the emitted events accumulate in the cost tab ───────────────────────────────


def test_cost_tab_accumulates_chat_events(tmp_path) -> None:
    """Tier 2: #1683 — events of the shape this fix emits accumulate in render_cost.
    The BY-MODEL bucket populates ONLY when llm_response_received accumulates after
    a llm_called, so the model appearing in the render proves accumulation (vs the
    pre-fix empty bucket)."""
    events_dir = tmp_path / ".reyn" / "events" / "direct"
    events_dir.mkdir(parents=True)
    lines = [
        {"type": "llm_called", "data": {"model": "openai/uniqmodel-xyz"}},
        {"type": "llm_response_received",
         "data": {"prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.0012}},
    ]
    (events_dir / "2026-06-16.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8",
    )

    out = render_cost(tmp_path, content_width=120)

    # The model appears only if llm_response_received accumulated for it (the
    # per-model bucket keys on the preceding llm_called's model).
    assert "uniqmodel-xyz" in out
    assert "(no events yet)" not in out


def test_cost_tab_empty_without_events(tmp_path) -> None:
    """Tier 2: #1683 — regression baseline: no events → the cost tab is empty
    (the pre-fix chat state). Pairs with the accumulation test above."""
    (tmp_path / ".reyn" / "events").mkdir(parents=True)
    out = render_cost(tmp_path, content_width=120)
    assert "uniqmodel-xyz" not in out
