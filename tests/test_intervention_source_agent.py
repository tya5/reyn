"""Tier 2: ``OutboxMessage(kind="intervention").meta["source_agent"]`` opt-in.

Phase 4 follow-up (issue #261) — when ``Session.handle_intervention``
takes the ``parent_delegate`` branch, the downstream ``user_channel``
emit must stamp ``meta["source_agent"]`` with the delegating agent's
name so the TUI can render a ``[parent: <name>]`` badge.

Phase 2 commitment (issue #254 alignment, `test_outbox_intervention_
meta_shape_is_stable`) must still pass — ``source_agent`` is **opt-in**:
the key is omitted from meta when the delegation branch did not fire.

This test drives ``_iv_meta`` directly with the ``source_agent_var``
ContextVar set / unset, pinning the meta shape contract without
needing to spin up the full Phase 4 routing flow (= multi-session
agent factory, parent resolution policy, etc. — those are exercised
by ``test_intervention_agent_routing.py``).
"""
from __future__ import annotations

import sys
from contextvars import copy_context
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.services.intervention_handler import (
    _iv_meta,
    source_agent_var,
)
from reyn.user_intervention import UserIntervention


def test_iv_meta_omits_source_agent_when_var_is_default() -> None:
    """Tier 2: default (no delegation) → ``source_agent`` key not in meta."""
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    meta = _iv_meta(iv)
    assert "source_agent" not in meta, (
        f"source_agent must be omitted by default; got {meta!r}"
    )


def test_iv_meta_stamps_source_agent_when_var_is_set() -> None:
    """Tier 2: ``source_agent_var`` set → meta["source_agent"] == that value.

    Drive the test in a child context so the var change doesn't leak
    to sibling tests.
    """
    def _within_ctx() -> dict:
        source_agent_var.set("agent-A")
        return _iv_meta(UserIntervention(kind="ask_user", prompt="Q?"))

    ctx = copy_context()
    meta = ctx.run(_within_ctx)
    assert meta.get("source_agent") == "agent-A", (
        f"source_agent must be stamped when var is set; got {meta!r}"
    )


def test_iv_meta_does_not_leak_var_across_contexts() -> None:
    """Tier 2: setting the var in one context does NOT bleed to another.

    Defends against a regression that uses module-level state instead
    of ContextVar (= concurrent sessions / interleaved tests would
    cross-contaminate).
    """
    def _set_in_ctx() -> None:
        source_agent_var.set("agent-A")

    set_ctx = copy_context()
    set_ctx.run(_set_in_ctx)

    # Back on the outer context — var should still be at default.
    assert source_agent_var.get() is None, (
        f"var must not leak from child context; got {source_agent_var.get()!r}"
    )

    meta = _iv_meta(UserIntervention(kind="ask_user", prompt="Q?"))
    assert "source_agent" not in meta


def test_phase2_meta_shape_invariant_preserved_when_no_source_agent() -> None:
    """Tier 2: Phase 2 commitment — meta key set without ``source_agent``
    is unchanged from the pre-#261 shape.

    Pin the required-key set so a future refactor that turns
    ``source_agent`` into a required key (= regression) fails here.
    Mirrors ``test_outbox_intervention_meta_shape_is_stable`` in
    ``test_intervention_bus_protocols.py`` for the source_agent
    specific axis.
    """
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    meta = _iv_meta(iv)
    required = {"intervention_id", "intervention_kind", "prompt"}
    assert required.issubset(set(meta.keys())), (
        f"required Phase 2 keys missing; got {set(meta.keys())!r}"
    )
    # And the optional source_agent key is NOT present on the
    # default path.
    assert "source_agent" not in meta
