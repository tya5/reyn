"""Tier 2: A2A Agent Card capability claim — issue #267 Gap 3 Z-c re-elevation.

With Gap 1 (= SSE producer wiring, PR #288) and Gap 2 (= webhook
trigger expansion, PR #286) both landed, ``streaming`` +
``pushNotifications`` flip back to ``True`` (= reversing PR #272's
Gap 3 Z-b interim disclosure). Each claim is now pinned to the
in-source wire that backs it, mirroring PR #284's MCP capability/wire
AST-pin pattern (= prevents the #267 Z-b "claim/reality mismatch"
regression by construction).

Calibration constraint (= same as PR #284 M3): every declared
``True`` capability must derive from a concrete in-source wire. Tests
below pin BOTH the declaration AND the wire, so a future PR that
removes one without the other fails immediately.

Pins:

  1. ``streaming`` is ``True`` + backed by ``_A2AProgressBridge``
     appending to ``run_registry.history_events`` (Gap 1 PR #288).
  2. ``pushNotifications`` is ``True`` + backed by the lifecycle
     webhook fire path (Gap 2 PR #286) + the original 3 terminal
     triggers in ``_handle_async_mode._run`` + ``A2AInterventionBus``.
  3. ``stateTransitionHistory`` stays ``False`` (= unchanged from
     FP-0001, no plans to implement).
  4. Required Agent Card fields stay shape-stable.

Pure-Python tests — call ``_build_agent_card`` directly so the check
runs in any environment.
"""
from __future__ import annotations

import inspect

import pytest


def _maybe_skip_if_router_unavailable() -> None:
    """Skip when ``reyn.web.routers.a2a`` can't import (= optional
    fastapi extra missing). CI has it; local dev may not.
    """
    try:
        import reyn.web.routers.a2a  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"reyn.web.routers.a2a unavailable: {exc}")


# ── 1. streaming = True + backed by producer wire ────────────────────


def test_agent_card_streaming_is_true_after_gap1_lands() -> None:
    """Tier 2: ``streaming`` capability is True (= issue #267 Gap 3
    Z-c re-elevation). Backed by ``_A2AProgressBridge``'s SSE sink
    landed in PR #288.
    """
    _maybe_skip_if_router_unavailable()
    from reyn.web.routers.a2a import _build_agent_card

    card = _build_agent_card("test_agent", "test role", "http://localhost/a2a")
    assert card["capabilities"]["streaming"] is True


def test_streaming_claim_backed_by_append_event_call_site() -> None:
    """Tier 2: pin the in-source wire backing ``streaming=True``. The
    producer call site is ``_A2AProgressBridge._send`` (= PR #288)
    calling ``run_registry.append_event(...)``. AST-search confirms
    the wire still exists so a refactor removing the producer fails
    this test alongside the Z-c claim test.
    """
    _maybe_skip_if_router_unavailable()
    from reyn.web.routers import a2a as a2a_router

    bridge_src = inspect.getsource(a2a_router._A2AProgressBridge)
    assert "append_event" in bridge_src, (
        "_A2AProgressBridge no longer calls run_registry.append_event — "
        "the streaming capability claim is now unbacked (= #267 Z-b "
        "style claim/reality mismatch). Either restore the wire or "
        "flip the claim back to False."
    )


# ── 2. pushNotifications = True + backed by webhook fire wires ───────


def test_agent_card_push_notifications_is_true_after_gap2_lands() -> None:
    """Tier 2: ``pushNotifications`` capability is True (= issue #267
    Gap 3 Z-c re-elevation). Backed by the lifecycle webhook fire
    path landed in PR #286 + the original 3 terminal triggers.
    """
    _maybe_skip_if_router_unavailable()
    from reyn.web.routers.a2a import _build_agent_card

    card = _build_agent_card("test_agent", "test role", "http://localhost/a2a")
    assert card["capabilities"]["pushNotifications"] is True


def test_push_notifications_claim_backed_by_webhook_post_call_sites() -> None:
    """Tier 2: pin the in-source wires backing ``pushNotifications=True``.
    Two surfaces must call ``post_webhook``:

      - ``_A2AProgressBridge._send`` (= PR #286 lifecycle fire)
      - ``_handle_async_mode._run`` (= original completed / failed
        terminal fires)
      - ``A2AInterventionBus.deliver`` (= original input-required
        terminal fire, in src/reyn/web/a2a_intervention.py)

    AST-search for ``post_webhook`` references confirms the wires still
    exist. A refactor removing them fails this test alongside the Z-c
    claim test.
    """
    _maybe_skip_if_router_unavailable()
    from pathlib import Path

    repo_root = Path(__file__).parent.parent / "src" / "reyn" / "web"
    a2a_router_src = (repo_root / "routers" / "a2a.py").read_text(
        encoding="utf-8",
    )
    bus_src = (repo_root / "a2a_intervention.py").read_text(encoding="utf-8")

    assert "post_webhook" in a2a_router_src, (
        "src/reyn/web/routers/a2a.py no longer calls post_webhook — "
        "pushNotifications claim is unbacked."
    )
    assert "post_webhook" in bus_src, (
        "src/reyn/web/a2a_intervention.py no longer calls post_webhook "
        "— pushNotifications claim is unbacked for the iv surface."
    )


# ── 3. stateTransitionHistory unchanged ─────────────────────────────


def test_agent_card_state_transition_history_stays_false() -> None:
    """Tier 2: ``stateTransitionHistory`` capability remains False
    (= unchanged from FP-0001, no plans to implement).

    Catches any accidental flip during the Z-c adjustment.
    """
    _maybe_skip_if_router_unavailable()
    from reyn.web.routers.a2a import _build_agent_card

    card = _build_agent_card("test_agent", "test role", "http://localhost/a2a")
    assert card["capabilities"]["stateTransitionHistory"] is False


# ── 4. Card shape stability ──────────────────────────────────────────


def test_agent_card_required_fields_shape_stable_through_gap3_zc() -> None:
    """Tier 2: Z-c re-elevation is a **value flip only**, not a schema
    change. Required Agent Card fields stay present with their
    expected shape so peer clients reading the card parse it the same
    way.
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

    # Capabilities map shape (3 boolean fields)
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
