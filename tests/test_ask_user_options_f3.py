"""Tier 2: ask_user can carry selectable options (F3 region-framework slice).

An ask_user op with ``options`` becomes a closed-set (select) intervention: each
option maps to a choice (id = the option text, label = "[N] option", hotkey = the
number). The inline region renders it as a selector; the answer is the chosen
option. Empty options keep the free-text behaviour (no choices).
"""
from __future__ import annotations

import pytest

from reyn.core.op_runtime.ask_user import _options_to_choices, handle
from reyn.core.op_runtime.context import OpContext
from reyn.schemas.models import AskUserIROp
from reyn.user_intervention import InterventionAnswer


def test_options_map_to_numbered_choices() -> None:
    """Tier 2: each option → (id=text, label='[N] text', hotkey='N')."""
    choices = _options_to_choices(["yes", "no", "maybe"])
    assert [(c.id, c.label, c.hotkey) for c in choices] == [
        ("yes", "[1] yes", "1"),
        ("no", "[2] no", "2"),
        ("maybe", "[3] maybe", "3"),
    ]
    assert _options_to_choices([]) == []


class _RecordingEvents:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, name: str, **kw) -> None:
        self.emitted.append((name, kw))


class _RecordingBus:
    def __init__(self, answer: InterventionAnswer) -> None:
        self._answer = answer
        self.requested: list = []

    async def request(self, iv):
        self.requested.append(iv)
        return self._answer


def _ctx(bus) -> OpContext:
    # ask_user's handler only reads events / intervention_bus / skill_name /
    # run_id / current_phase, so workspace + permission_decl are unused dummies.
    return OpContext(
        workspace=None, events=_RecordingEvents(),
        permission_decl=None, intervention_bus=bus,
    )


@pytest.mark.asyncio
async def test_ask_user_with_options_is_a_select_intervention() -> None:
    """Tier 2: options → a select intervention with choices; the answer is the
    chosen option id."""
    bus = _RecordingBus(InterventionAnswer(choice_id="no"))
    op = AskUserIROp(kind="ask_user", question="Pick?", options=["yes", "no"])
    result = await handle(op, _ctx(bus))

    iv = bus.requested[0]
    assert iv.input_type == "select"
    assert [c.id for c in iv.choices] == ["yes", "no"]
    assert result["answer"] == "no"  # the selected option, via choice_id


@pytest.mark.asyncio
async def test_ask_user_without_options_stays_free_text() -> None:
    """Tier 2: no options → free-text (no choices), answer is the typed text."""
    bus = _RecordingBus(InterventionAnswer(text="some text"))
    op = AskUserIROp(kind="ask_user", question="Free?")
    result = await handle(op, _ctx(bus))

    iv = bus.requested[0]
    assert iv.choices == []
    assert iv.input_type == ""
    assert result["answer"] == "some text"
