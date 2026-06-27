"""Tier 2: intervention typed-input region consumer (F2 mechanism).

A closed-set intervention (has choices) maps to an InterventionElement whose rows
are the choice labels and whose select delivers the chosen choice id; a free-text
intervention (no choices) maps to None (it keeps the input field). Also pins the
UserIntervention input_type field: explicit value wins, else inferred from shape,
and it round-trips through to_dict/from_dict.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.intervention_region import (
    InterventionElement,
    build_intervention_element,
)
from reyn.runtime.session import Session
from reyn.user_intervention import InterventionChoice, UserIntervention


def _iv(choices: list[tuple[str, str]]):
    return SimpleNamespace(
        id="iv1",
        choices=[SimpleNamespace(id=cid, label=label) for cid, label in choices],
    )


def test_element_rows_are_choice_labels() -> None:
    """Tier 2: the element's selectable rows are the choice labels."""
    el = InterventionElement("iv1", [("yes", "[y]es"), ("no", "[n]o")], lambda c: None)
    assert el.lines() == ["[y]es", "[n]o"]
    assert el.iv_id == "iv1"


def test_select_delivers_the_chosen_choice_id() -> None:
    """Tier 2: selecting a row fires on_choose with that row's choice id."""
    chosen: list[str] = []
    el = InterventionElement(
        "iv1", [("yes", "[y]es"), ("no", "[n]o")], chosen.append
    )
    el.on_select(1)
    assert chosen == ["no"]
    el.on_select(99)  # out of range → no delivery
    assert chosen == ["no"]


def test_build_element_for_closed_set_and_none_for_free_text() -> None:
    """Tier 2: a closed-set iv builds an element; a free-text iv (no choices)
    builds None (it uses the input field instead)."""
    el = build_intervention_element(_iv([("a", "[a]"), ("b", "[b]")]), lambda c: None)
    assert el is not None
    assert el.lines() == ["[a]", "[b]"]
    assert build_intervention_element(_iv([]), lambda c: None) is None


def test_effective_input_type_explicit_wins_else_inferred() -> None:
    """Tier 2: input_type explicit wins; else inferred (choices→select, none→text)."""
    text_iv = UserIntervention(kind="ask_user", prompt="q")
    assert text_iv.effective_input_type == "text"
    select_iv = UserIntervention(
        kind="permission.file", prompt="ok?",
        choices=[InterventionChoice(id="y", label="[y]es", hotkey="y")],
    )
    assert select_iv.effective_input_type == "select"
    explicit = UserIntervention(kind="ask_user", prompt="q", input_type="confirm")
    assert explicit.effective_input_type == "confirm"


def test_input_type_round_trips_through_dict() -> None:
    """Tier 2: a non-default input_type survives to_dict/from_dict."""
    iv = UserIntervention(kind="ask_user", prompt="q", input_type="grant-deny")
    restored = UserIntervention.from_dict(iv.to_dict())
    assert restored.input_type == "grant-deny"
    # default stays "" and is omitted from the dict (backward-compat)
    plain = UserIntervention(kind="ask_user", prompt="q")
    assert "input_type" not in plain.to_dict()
    assert UserIntervention.from_dict(plain.to_dict()).input_type == ""


@pytest.mark.asyncio
async def test_answer_oldest_intervention_choice_delivers_authoritatively(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the region's choice-id seam resolves the head intervention with
    the chosen id (authoritative — no text/hotkey match needed)."""
    monkeypatch.chdir(tmp_path)
    session = Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )
    session.register_intervention_listener("test")
    iv = UserIntervention(
        kind="permission.file", prompt="ok?",
        choices=[
            InterventionChoice(id="yes", label="[y]es", hotkey="y"),
            InterventionChoice(id="no", label="[n]o", hotkey="n"),
        ],
    )
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    for _ in range(200):  # wait until the iv is pending
        if session.interventions.list_active():
            break
        await asyncio.sleep(0.01)

    delivered = await session.answer_oldest_intervention_choice("no")
    assert delivered is True
    answer = await asyncio.wait_for(task, timeout=2.0)
    assert answer.choice_id == "no"
