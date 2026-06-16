"""Tier 2: events-tab llm_request rendering (#1669).

#1669 surfaces the non-message LLM call params (reasoning_effort / temperature /
extra_body / …) in the events tab so the owner can verify what's sent to the
model. The backend (e2e) emits a P6 ``llm_request`` event; this is the TUI render
half: a per-kind colour, a DEDICATED filter group (isolatable while model-testing),
and a one-line hint (model + purpose + salient params; full set via Space-preview).

Pins the deterministic render pieces (the formatter string from a synthesized
event of the locked schema, the filter-group entry, the colour) — public module
surface, no running app needed. The live render + filter toggle are tmux-verified
separately (real-terminal, per the headless boundary).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.widgets.right_panel.events_tab import (  # noqa: E402
    _EVENT_COLORS,
    _FILTER_GROUPS,
    _event_hint,
)


def _llm_request_event(**data) -> dict:
    base = {"model": "gpt-5.4", "purpose": "main",
            "params": {"reasoning_effort": "high", "temperature": 0.7},
            "tools_count": 12}
    base.update(data)
    return {"type": "llm_request", "data": base}


def test_hint_renders_model_purpose_and_salient_params() -> None:
    """Tier 2: the one-line hint carries model + purpose + the salient params
    (reasoning_effort / temperature from the nested ``params``) + tools count."""
    hint = _event_hint(_llm_request_event())
    assert "gpt-5.4" in hint
    assert "main" in hint
    assert "reasoning_effort=high" in hint
    assert "temp=0.7" in hint
    assert "tools=12" in hint


def test_hint_omits_absent_params_no_none_noise() -> None:
    """Tier 2: params absent from the nested dict are omitted (no '=None' noise)
    — the #1669 schema says absent keys are simply absent."""
    hint = _event_hint(_llm_request_event(params={}, tools_count=0))
    assert "reasoning_effort" not in hint     # absent → omitted
    assert "temp" not in hint
    assert "tools=" not in hint               # 0 tools → omitted
    assert "None" not in hint
    assert "gpt-5.4" in hint                  # model still shown


def test_params_read_from_nested_key_not_top_level() -> None:
    """Tier 2: salient params are read from ``data['params']`` (the #1669 nested
    shape), NOT top-level — a top-level reasoning_effort must not be picked up."""
    ev = {"type": "llm_request",
          "data": {"model": "m", "purpose": "phase",
                   "reasoning_effort": "TOPLEVEL",   # wrong place — must be ignored
                   "params": {}}}
    hint = _event_hint(ev)
    assert "TOPLEVEL" not in hint


def test_llm_request_has_dedicated_filter_group() -> None:
    """Tier 2: a DEDICATED 'request' filter group isolates llm_request (owner's
    model-testing use case), separate from the all-LLM 'llm' group."""
    groups = dict(_FILTER_GROUPS)
    assert "request" in groups
    assert groups["request"] == frozenset({"llm_request"})
    # Not folded into "llm" (that would prevent isolating just request events).
    assert "llm_request" not in groups["llm"]


def test_llm_request_has_event_colour() -> None:
    """Tier 2: llm_request has a per-kind colour (so it's visually distinct, not
    the generic default)."""
    assert "llm_request" in _EVENT_COLORS
