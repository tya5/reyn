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
from reyn.interfaces.inline.app import _deliver_intervention_choice, _submit
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
    el = InterventionElement(
        "iv1", [("yes", "[y]es"), ("no", "[n]o")], lambda c, lbl: None
    )
    assert el.lines() == ["[y]es", "[n]o"]
    assert el.iv_id == "iv1"


def test_select_delivers_the_chosen_choice_id_and_label() -> None:
    """Tier 2: selecting a row fires on_choose with that row's (id, label)."""
    chosen: list[tuple[str, str]] = []
    el = InterventionElement(
        "iv1", [("yes", "[y]es"), ("no", "[n]o")], lambda c, lbl: chosen.append((c, lbl))
    )
    el.on_select(1)
    assert chosen == [("no", "[n]o")]
    el.on_select(99)  # out of range → no delivery
    assert chosen == [("no", "[n]o")]


def test_build_element_for_closed_set_and_none_for_free_text() -> None:
    """Tier 2: a closed-set iv builds an element; a free-text iv (no choices)
    builds None (it uses the input field instead)."""
    el = build_intervention_element(_iv([("a", "[a]"), ("b", "[b]")]), lambda c, lbl: None)
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


class _AnsweringSession:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok
        self.delivered: list[str] = []

    async def answer_oldest_intervention_choice(self, choice_id: str) -> bool:
        self.delivered.append(choice_id)
        return self._ok


@pytest.mark.asyncio
async def test_deliver_choice_echoes_answer_to_scrollback() -> None:
    """Tier 2: a delivered choice is sent authoritatively AND a uniform
    'answered: <label>' system marker is put on the outbox (so every resolved
    intervention leaves a scrollback trace, not just permission's side effect)."""
    session = _AnsweringSession(ok=True)
    queue: asyncio.Queue = asyncio.Queue()
    registry = SimpleNamespace(attached_session=lambda: session, repl_outbox=queue)

    await _deliver_intervention_choice(registry, "just_path", "[j]ust this path always")

    assert session.delivered == ["just_path"]  # authoritative id delivered
    msg = queue.get_nowait()
    # kind="system" → dim · marker (persistent, not transient "status"/"trace").
    # "intervention" would also persist but renders with amber ◆ "needs-you" glyph
    # — semantically wrong for a resolved-answer confirmation.
    assert msg.kind == "system"
    assert "answered:" in msg.text
    assert "[j]ust this path always" in msg.text


@pytest.mark.asyncio
async def test_deliver_choice_no_echo_when_nothing_delivered() -> None:
    """Tier 2: no echo when delivery reports nothing resolved (no false trace)."""
    queue: asyncio.Queue = asyncio.Queue()
    registry = SimpleNamespace(
        attached_session=lambda: _AnsweringSession(ok=False), repl_outbox=queue
    )
    await _deliver_intervention_choice(registry, "x", "lbl")
    assert queue.empty()


@pytest.mark.asyncio
async def test_answer_oldest_intervention_text_delivers_free_text(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ``answer_oldest_intervention_text`` delivers the verbatim text
    to the head ask_user intervention and resolves its future.

    RED-verify: if ``_submit`` skips the ``head.kind == "ask_user"`` check
    and falls through to ``submit_user_text``, the intervention future is
    never resolved and ``asyncio.wait_for`` times out.
    """
    monkeypatch.chdir(tmp_path)
    session = Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )
    session.register_intervention_listener("test")
    iv = UserIntervention(kind="ask_user", prompt="What is your name?")
    iv.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    for _ in range(200):
        if session.interventions.list_active():
            break
        await asyncio.sleep(0.01)

    delivered = await session.answer_oldest_intervention_text("Alice")
    assert delivered is True
    answer = await asyncio.wait_for(task, timeout=2.0)
    assert answer.text == "Alice"


def _stub_registry(*, choices: list) -> SimpleNamespace:
    """Build a minimal registry stub with a head intervention of given choices."""
    answered: list[str] = []
    submitted: list[str] = []

    class _StubIntervention:
        pass

    iv = _StubIntervention()
    iv.choices = choices  # type: ignore[attr-defined]

    class _StubInterventions:
        def head(self):
            return iv

    class _StubSession:
        interventions = _StubInterventions()

        async def answer_oldest_intervention_text(self, text: str) -> bool:
            answered.append(text)
            return True

        async def submit_user_text(self, text: str) -> None:
            submitted.append(text)

    registry = SimpleNamespace(attached_session=lambda: _StubSession())
    return registry, answered, submitted


@pytest.mark.asyncio
async def test_submit_routes_to_intervention_bus_when_ask_user_pending() -> None:
    """Tier 2: ``_submit`` routes free-text to ``answer_oldest_intervention_text``
    (not ``submit_user_text``) when the head pending intervention has no choices
    (ask_user is the canonical case).

    RED-verify: replacing ``not head.choices`` with a kind-name check like
    ``head.kind == "ask_user"`` would pass for ask_user but fail for other
    free-text kinds (mcp_install.secret). Widened to choices-shape to match
    build_intervention_element logic.
    """
    registry, answered, submitted = _stub_registry(choices=[])
    await _submit(registry, "hello ask_user")

    assert answered == ["hello ask_user"], "text must reach intervention bus"
    assert submitted == [], "submit_user_text must NOT be called while free-text iv pending"


@pytest.mark.asyncio
async def test_submit_routes_to_intervention_bus_for_mcp_install_secret() -> None:
    """Tier 2: ``_submit`` routes free-text to the intervention bus for
    ``mcp_install.secret`` (choices=[]) — the same free-text class as ask_user.

    RED-verify: ``head.kind == "ask_user"`` silently misses mcp_install.secret
    and calls ``submit_user_text`` instead, leaving the MCP install awaiting a
    secret that never arrives.
    """
    registry, answered, submitted = _stub_registry(choices=[])
    await _submit(registry, "mysecretvalue")

    assert answered == ["mysecretvalue"], "secret must reach intervention bus"
    assert submitted == [], "submit_user_text must NOT be called for mcp_install.secret"
