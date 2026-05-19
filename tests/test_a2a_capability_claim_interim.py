"""Tier 2: A2A Agent Card capability claim — issue #267 Gap 3 Z-b interim disclosure.

Pure-Python test that pins the interim ``False`` state of the
``streaming`` and ``pushNotifications`` capabilities without booting
FastAPI / TestClient. Calls ``_build_agent_card`` directly so the
check runs in any environment (= the broader test_a2a.py /
test_fp0001_a2a_endpoints.py tests need the optional ``fastapi``
extra; this file does not).

Pins:

  1. ``streaming`` is ``False`` (= issue #267 Gap 1 SSE producer not
     wired, history_events stays empty in production).
  2. ``pushNotifications`` is ``False`` (= issue #267 Gap 2 webhook
     trigger limited to completed / failed / input-required, claiming
     ``True`` would mislead spec-conformance-strict peers).
  3. ``stateTransitionHistory`` is ``False`` (= no plans to implement,
     unchanged from FP-0001).
  4. Required Agent Card fields stay shape-stable (= name / description
     / url / version / protocolVersion / capabilities / defaultInputModes
     / defaultOutputModes / skills) so the Gap 3 Z-b change is a
     **value flip only**, not a schema change.

Gap 3 Z-a (= flip ``True`` back) lands once Gap 1 + Gap 2 close; at
that point this test's expected values flip to ``True`` and reference
the Gap-completion PRs.
"""
from __future__ import annotations

import pytest


def _maybe_skip_if_router_unavailable() -> None:
    """Skip when ``reyn.web.routers.a2a`` can't import.

    The module pulls in fastapi at top-level (= ``from fastapi import
    APIRouter, ...``); environments without the optional extra simply
    skip. CI has fastapi installed; local dev may not. issue #253
    introduced the dependency.
    """
    try:
        import reyn.web.routers.a2a  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"reyn.web.routers.a2a unavailable: {exc}")


def test_agent_card_streaming_is_false_until_gap1_lands() -> None:
    """Tier 2: ``streaming`` capability is False (= issue #267 Gap 3 Z-b).

    SSE endpoint exists at GET /a2a/tasks/{run_id}/events but
    history_events has no in-tree producer, so streaming never delivers
    in-flight events. Claiming True would mislead spec-conformance peers.
    Gap 3 Z-a flips this back to True once Gap 1 wires the producer.
    """
    _maybe_skip_if_router_unavailable()
    from reyn.web.routers.a2a import _build_agent_card

    card = _build_agent_card("test_agent", "test role", "http://localhost/a2a")
    assert card["capabilities"]["streaming"] is False


def test_agent_card_push_notifications_is_false_until_gap2_lands() -> None:
    """Tier 2: ``pushNotifications`` capability is False (= issue #267
    Gap 3 Z-b).

    Webhook fires on exactly three triggers (completed / failed /
    input-required). Claiming True would imply support for arbitrary
    state-change notifications, which we don't deliver. Gap 3 Z-a
    flips this back to True once Gap 2 expands the trigger set.
    """
    _maybe_skip_if_router_unavailable()
    from reyn.web.routers.a2a import _build_agent_card

    card = _build_agent_card("test_agent", "test role", "http://localhost/a2a")
    assert card["capabilities"]["pushNotifications"] is False


def test_agent_card_state_transition_history_stays_false() -> None:
    """Tier 2: ``stateTransitionHistory`` capability remains False
    (= unchanged from FP-0001, no plans to implement).

    Catches any accidental flip during the Gap 3 Z-b adjustment.
    """
    _maybe_skip_if_router_unavailable()
    from reyn.web.routers.a2a import _build_agent_card

    card = _build_agent_card("test_agent", "test role", "http://localhost/a2a")
    assert card["capabilities"]["stateTransitionHistory"] is False


def test_agent_card_required_fields_shape_stable_through_gap3_zb() -> None:
    """Tier 2: Gap 3 Z-b is a **value flip only**, not a schema change.

    Required Agent Card fields stay present with their expected shape
    so peer clients reading the card parse it the same way. Pinning
    this guards against accidental schema drift bundled into the value
    flip.
    """
    _maybe_skip_if_router_unavailable()
    from reyn.web.routers.a2a import _build_agent_card

    card = _build_agent_card("test_agent", "test role", "http://localhost/a2a")

    # Identity + endpoint
    assert card["name"] == "test_agent"
    assert card["description"] == "test role"
    assert card["url"] == "http://localhost/a2a"

    # Version negotiation
    assert "version" in card
    assert "protocolVersion" in card

    # Capabilities map shape (3 boolean fields, all False post-Z-b)
    caps = card["capabilities"]
    assert isinstance(caps, dict)
    assert set(caps.keys()) == {
        "streaming",
        "pushNotifications",
        "stateTransitionHistory",
    }
    assert all(isinstance(v, bool) for v in caps.values())

    # Modes (= peers parse for IO negotiation)
    assert "text/plain" in card["defaultInputModes"]
    assert "text/plain" in card["defaultOutputModes"]

    # Skills (= A2A's outward capability, opaque to Reyn skill catalogue)
    assert isinstance(card["skills"], list)
    assert len(card["skills"]) >= 1
    chat_skill = next(s for s in card["skills"] if s["id"] == "chat")
    assert "name" in chat_skill
    assert "description" in chat_skill
