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
        task = asyncio.ensure_future(bus.deliver(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        iv.future.set_result(InterventionAnswer(text="yes", choice_id="yes"))
        await task

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
        task = asyncio.ensure_future(bus.deliver(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        iv.future.set_result(InterventionAnswer(text="alice"))
        await task

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
        task = asyncio.ensure_future(bus.deliver(iv))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        iv.future.set_result(InterventionAnswer(text="ok"))
        await task

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


def test_handle_answer_injection_extracts_top_level_choice_id() -> None:
    """Tier 2: ``params.choice_id`` (= top-level convenience) is extracted
    and passed into ``InterventionAnswer.choice_id``.
    """
    from reyn.web.routers.a2a import _handle_answer_injection

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")
    iv = UserIntervention(
        kind="permission.confirm",
        prompt="?",
        choices=[
            InterventionChoice(id="yes", label="[Y]", hotkey="y"),
            InterventionChoice(id="no", label="[N]", hotkey="n"),
        ],
    )
    registry.update(
        entry.run_id,
        status="input-required",
        pending_intervention=iv,
    )

    captured: list[InterventionAnswer] = []
    original_answer = registry.answer_intervention

    def _capture(task_id: str, answer: InterventionAnswer):  # noqa: ANN202
        captured.append(answer)
        return original_answer(task_id, answer)

    registry.answer_intervention = _capture  # type: ignore[method-assign]

    asyncio.run(
        _handle_answer_injection(
            req_id=1,
            task_id=entry.run_id,
            params={
                "task_id": entry.run_id,
                "choice_id": "yes",
                "message": {"parts": [{"type": "text", "text": "yes"}]},
            },
            run_registry=registry,
        )
    )

    assert len(captured) == 1
    assert captured[0].text == "yes"
    assert captured[0].choice_id == "yes"


def test_handle_answer_injection_extracts_message_metadata_choice_id() -> None:
    """Tier 2: ``params.message.metadata.choice_id`` (= A2A-spec-conforming
    structured channel) is extracted when top-level absent.
    """
    from reyn.web.routers.a2a import _handle_answer_injection

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")
    iv = UserIntervention(
        kind="permission.confirm", prompt="?",
        choices=[InterventionChoice(id="always", label="[A]", hotkey="a")],
    )
    registry.update(
        entry.run_id, status="input-required", pending_intervention=iv,
    )

    captured: list[InterventionAnswer] = []
    original_answer = registry.answer_intervention

    def _capture(task_id, answer):  # noqa: ANN202
        captured.append(answer)
        return original_answer(task_id, answer)

    registry.answer_intervention = _capture  # type: ignore[method-assign]

    asyncio.run(
        _handle_answer_injection(
            req_id=1,
            task_id=entry.run_id,
            params={
                "task_id": entry.run_id,
                "message": {
                    "parts": [{"type": "text", "text": "a"}],
                    "metadata": {"choice_id": "always"},
                },
            },
            run_registry=registry,
        )
    )

    assert captured[0].choice_id == "always"
    assert captured[0].text == "a"


def test_handle_answer_injection_top_level_wins_over_metadata() -> None:
    """Tier 2: when both top-level ``params.choice_id`` AND
    ``params.message.metadata.choice_id`` are present, top-level wins
    (= explicit-most call shape wins by convention).
    """
    from reyn.web.routers.a2a import _handle_answer_injection

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")
    iv = UserIntervention(
        kind="permission.confirm", prompt="?",
        choices=[
            InterventionChoice(id="yes", label="[Y]", hotkey="y"),
            InterventionChoice(id="no", label="[N]", hotkey="n"),
        ],
    )
    registry.update(
        entry.run_id, status="input-required", pending_intervention=iv,
    )

    captured: list[InterventionAnswer] = []
    original_answer = registry.answer_intervention

    def _capture(task_id, answer):  # noqa: ANN202
        captured.append(answer)
        return original_answer(task_id, answer)

    registry.answer_intervention = _capture  # type: ignore[method-assign]

    asyncio.run(
        _handle_answer_injection(
            req_id=1,
            task_id=entry.run_id,
            params={
                "task_id": entry.run_id,
                "choice_id": "yes",
                "message": {
                    "parts": [{"type": "text", "text": "yes"}],
                    "metadata": {"choice_id": "no"},
                },
            },
            run_registry=registry,
        )
    )

    assert captured[0].choice_id == "yes"


def test_handle_answer_injection_omits_choice_id_for_free_text() -> None:
    """Tier 2: free-text ``ask_user`` answer (= no ``choice_id`` in
    params) results in ``InterventionAnswer(text=..., choice_id=None)``
    — pre-Gap-4 behaviour preserved.
    """
    from reyn.web.routers.a2a import _handle_answer_injection

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")
    iv = UserIntervention(kind="ask_user", prompt="?")
    registry.update(
        entry.run_id, status="input-required", pending_intervention=iv,
    )

    captured: list[InterventionAnswer] = []
    original_answer = registry.answer_intervention

    def _capture(task_id, answer):  # noqa: ANN202
        captured.append(answer)
        return original_answer(task_id, answer)

    registry.answer_intervention = _capture  # type: ignore[method-assign]

    asyncio.run(
        _handle_answer_injection(
            req_id=1,
            task_id=entry.run_id,
            params={
                "task_id": entry.run_id,
                "message": {"parts": [{"type": "text", "text": "alice"}]},
            },
            run_registry=registry,
        )
    )

    assert captured[0].text == "alice"
    assert captured[0].choice_id is None


def test_handle_answer_injection_ignores_non_string_choice_id() -> None:
    """Tier 2: defensive — non-string ``choice_id`` (= e.g. peer sent
    a number or null) is ignored, falls through to None. Avoids
    propagating malformed peer input into the answer record.
    """
    from reyn.web.routers.a2a import _handle_answer_injection

    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")
    iv = UserIntervention(kind="ask_user", prompt="?")
    registry.update(
        entry.run_id, status="input-required", pending_intervention=iv,
    )

    captured: list[InterventionAnswer] = []
    original_answer = registry.answer_intervention

    def _capture(task_id, answer):  # noqa: ANN202
        captured.append(answer)
        return original_answer(task_id, answer)

    registry.answer_intervention = _capture  # type: ignore[method-assign]

    asyncio.run(
        _handle_answer_injection(
            req_id=1,
            task_id=entry.run_id,
            params={
                "task_id": entry.run_id,
                "choice_id": 42,  # non-string
                "message": {
                    "parts": [{"type": "text", "text": "x"}],
                    "metadata": {"choice_id": None},  # also non-string
                },
            },
            run_registry=registry,
        )
    )

    assert captured[0].choice_id is None
