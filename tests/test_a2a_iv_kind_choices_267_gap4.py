"""Tier 2: A2A iv kind + choices coverage (issue #267 Gap 4).

Before this PR, the A2A peer-facing surface treated all interventions as
free-text ``ask_user``:

  - Outbound webhook payload exposed only ``question`` (= ``iv.prompt``),
    not ``kind`` or ``choices``. A peer received ``"Allow this permission?"``
    with no programmatic way to render the yes/no/always hotkeys.
  - Inbound ``_handle_answer_injection`` ignored ``choice_id``, always
    built ``InterventionAnswer(text=..., choice_id=None)``. Peer had to
    send free text and hope ``match_choice`` parsed it correctly.

This PR completes the round-trip: webhook payload carries the structured
prompt info, and ``_handle_answer_injection`` accepts ``choice_id`` at
top-level params OR inside ``message.metadata`` (= A2A-spec-conforming
structured channel).

Pins:

  1. Outbound webhook payload includes ``kind``, ``choices`` (= empty list
     for free-text), and ``detail`` (when non-empty).
  2. Round-trip for ``permission.confirm`` style iv with 3 choices.
  3. Inbound ``choice_id`` from ``params.choice_id`` (top-level).
  4. Inbound ``choice_id`` from ``params.message.metadata.choice_id``.
  5. Top-level ``choice_id`` wins when both are present.
  6. Free-text ``ask_user`` (= no ``choice_id``) still works unchanged.
  7. Backwards compat: ``choice_id`` absent → InterventionAnswer.choice_id
     is None (= pre-Gap-4 callers see no change).
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")

from reyn.user_intervention import (  # noqa: E402
    InterventionAnswer,
    InterventionChoice,
    UserIntervention,
)
from reyn.web.a2a_intervention import A2AInterventionBus  # noqa: E402
from reyn.web.run_registry import RunRegistry  # noqa: E402

# ── 1. Webhook payload enrichment ─────────────────────────────────────


def test_webhook_payload_includes_kind_and_choices(monkeypatch) -> None:
    """Tier 2: ``A2AInterventionBus.deliver`` posts a webhook payload
    that includes ``kind``, ``choices`` (full list with id/label/hotkey),
    and ``detail`` (when non-empty).
    """
    posted: list[tuple[str, dict]] = []

    async def _fake_post_webhook(url: str, payload: dict):  # noqa: ANN202
        posted.append((url, payload))
        from reyn.web.notifications import DeliveryOutcome, DeliveryResult
        return DeliveryResult(outcome=DeliveryOutcome.SUCCESS)

    import reyn.web.notifications as notifications_mod

    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post_webhook)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )
    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(
            kind="permission.confirm",
            prompt="Allow read access to ~/secrets?",
            detail="Path: ~/secrets/api.key",
            choices=[
                InterventionChoice(id="yes", label="[Y]es", hotkey="y"),
                InterventionChoice(id="always", label="[A]lways", hotkey="a"),
                InterventionChoice(id="no", label="[N]o", hotkey="n"),
            ],
        )
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    assert len(posted) == 1
    url, payload = posted[0]
    assert url == "https://peer.test/hook"
    assert payload["kind"] == "permission.confirm"
    assert payload["question"] == "Allow read access to ~/secrets?"
    assert payload["detail"] == "Path: ~/secrets/api.key"
    assert payload["choices"] == [
        {"id": "yes", "label": "[Y]es", "hotkey": "y"},
        {"id": "always", "label": "[A]lways", "hotkey": "a"},
        {"id": "no", "label": "[N]o", "hotkey": "n"},
    ]


def test_webhook_payload_omits_detail_when_empty(monkeypatch) -> None:
    """Tier 2: empty ``iv.detail`` is OMITTED from the payload (= keeps
    payload tight for the common case + no semantic difference between
    missing and empty string for peer renderers).
    """
    posted: list[dict] = []

    async def _fake_post_webhook(url: str, payload: dict):  # noqa: ANN202
        posted.append(payload)
        from reyn.web.notifications import DeliveryOutcome, DeliveryResult
        return DeliveryResult(outcome=DeliveryOutcome.SUCCESS)

    import reyn.web.notifications as notifications_mod

    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post_webhook)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )
    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(
            kind="ask_user",
            prompt="What's your name?",
            detail="",  # empty
        )
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    payload = posted[0]
    assert "detail" not in payload
    assert payload["choices"] == []
    assert payload["kind"] == "ask_user"


def test_webhook_payload_free_text_ask_user_has_empty_choices(monkeypatch) -> None:
    """Tier 2: a free-text ``ask_user`` iv (= no ``choices``) results in
    a payload with ``choices: []`` and ``kind: "ask_user"``. Backwards
    compat with peers that don't yet parse the new fields (= the old
    ``question`` / ``status`` fields are still present).
    """
    posted: list[dict] = []

    async def _fake_post_webhook(url: str, payload: dict):  # noqa: ANN202
        posted.append(payload)
        from reyn.web.notifications import DeliveryOutcome, DeliveryResult
        return DeliveryResult(outcome=DeliveryOutcome.SUCCESS)

    import reyn.web.notifications as notifications_mod

    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post_webhook)

    registry = RunRegistry()
    entry = registry.create(
        agent_name="demo", chain_id="chain-A",
        webhook_url="https://peer.test/hook",
    )
    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="Free text?")
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    payload = posted[0]
    # Existing fields preserved (= existing peers keep working).
    assert payload["run_id"] == entry.run_id
    assert payload["status"] == "input-required"
    assert payload["question"] == "Free text?"
    assert payload["agent_name"] == "demo"
    # New fields present + empty.
    assert payload["kind"] == "ask_user"
    assert payload["choices"] == []


# ── 2. Inbound choice_id extraction ───────────────────────────────────


class _FakeAgentRegistry:
    """Minimal stub — ``_handle_answer_injection`` only calls
    ``get_or_load(name)``. Returns a captured ChatSession-shape stub.
    """

    def __init__(self, session) -> None:
        self._session = session

    def get_or_load(self, name: str):  # noqa: ANN202
        return self._session


class _FakeChatSession:
    """Captures ``answer_pending_intervention`` calls. issue #292 (α):
    the iv future is owned by ChatSession; the router calls this method
    instead of ``RunRegistry.answer_intervention`` (= removed).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, InterventionAnswer]] = []
        # Configurable return for the "already answered" path.
        self.return_value = True

    async def answer_pending_intervention(
        self, run_id: str, answer: InterventionAnswer,
    ) -> bool:
        self.calls.append((run_id, answer))
        return self.return_value


def _run_answer_injection(*, params: dict, run_id: str) -> _FakeChatSession:
    """Drive ``_handle_answer_injection`` with a real RunRegistry and
    fake agent registry, return the captured FakeChatSession.
    """
    from reyn.web.routers.a2a import _handle_answer_injection

    run_registry = RunRegistry()
    # Inject a pre-existing RunEntry so the router can find the agent.
    run_registry.create(agent_name="demo", chain_id="chain-A")
    # Map run_id to the test's chosen value.
    list(run_registry._runs.values())[0].run_id = run_id  # noqa: SLF001
    run_registry._runs = {run_id: list(run_registry._runs.values())[0]}  # noqa: SLF001

    session = _FakeChatSession()
    agent_registry = _FakeAgentRegistry(session)

    asyncio.run(
        _handle_answer_injection(
            req_id=1,
            task_id=run_id,
            params=params,
            registry=agent_registry,
            run_registry=run_registry,
        )
    )
    return session


def test_handle_answer_injection_extracts_top_level_choice_id() -> None:
    """Tier 2: ``params.choice_id`` (= top-level convenience) is extracted
    and passed into ``InterventionAnswer.choice_id`` for the
    ChatSession-side delivery (issue #292 α).
    """
    session = _run_answer_injection(
        run_id="run-tlc",
        params={
            "task_id": "run-tlc",
            "choice_id": "yes",
            "message": {"parts": [{"type": "text", "text": "yes"}]},
        },
    )
    assert len(session.calls) == 1
    delivered_run_id, answer = session.calls[0]
    assert delivered_run_id == "run-tlc"
    assert answer.text == "yes"
    assert answer.choice_id == "yes"


def test_handle_answer_injection_extracts_message_metadata_choice_id() -> None:
    """Tier 2: ``params.message.metadata.choice_id`` is extracted when
    top-level absent.
    """
    session = _run_answer_injection(
        run_id="run-md",
        params={
            "task_id": "run-md",
            "message": {
                "parts": [{"type": "text", "text": "a"}],
                "metadata": {"choice_id": "always"},
            },
        },
    )
    assert session.calls[0][1].choice_id == "always"
    assert session.calls[0][1].text == "a"


def test_handle_answer_injection_top_level_wins_over_metadata() -> None:
    """Tier 2: when both are present, top-level wins."""
    session = _run_answer_injection(
        run_id="run-prec",
        params={
            "task_id": "run-prec",
            "choice_id": "yes",
            "message": {
                "parts": [{"type": "text", "text": "yes"}],
                "metadata": {"choice_id": "no"},
            },
        },
    )
    assert session.calls[0][1].choice_id == "yes"


def test_handle_answer_injection_omits_choice_id_for_free_text() -> None:
    """Tier 2: free-text ``ask_user`` answer → ``choice_id=None``."""
    session = _run_answer_injection(
        run_id="run-free",
        params={
            "task_id": "run-free",
            "message": {"parts": [{"type": "text", "text": "alice"}]},
        },
    )
    assert session.calls[0][1].text == "alice"
    assert session.calls[0][1].choice_id is None


def test_handle_answer_injection_ignores_non_string_choice_id() -> None:
    """Tier 2: non-string ``choice_id`` is ignored → choice_id=None."""
    session = _run_answer_injection(
        run_id="run-bad",
        params={
            "task_id": "run-bad",
            "choice_id": 42,  # non-string
            "message": {
                "parts": [{"type": "text", "text": "x"}],
                "metadata": {"choice_id": None},  # also non-string
            },
        },
    )
    assert session.calls[0][1].choice_id is None
