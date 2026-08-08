"""Tier 2: #3793 stage 1 (ADR-0039 D4 conformance) — ``AttachedConnection``.

architect's design (issue #3793, owner-ratified 2026-08-08) introduces a
3-word vocabulary — ``attached`` (N-capable output-receiving set) /
``active`` (the one destination for un-addressed input) / ``addressed``
(id-carrying input, unaffected by this class) — as the shape a single
process-wide focus pointer will eventually be replaced by per-connection
instances (stage 2/3). Stage 1's own scope is narrower and explicitly
"zero behaviour change": wrap TODAY's single shared pointer in the new
``AttachedConnection`` type, with BOTH the local (TUI/REPL) and remote
(AG-UI) call sites continuing to share the ONE instance ``AgentRegistry``
constructs — so nothing observable changes yet (that split is stage 2).

This file covers:
- ``AttachedConnection``'s own contract in isolation (no ``AgentRegistry``).
- The zero-behaviour-change witness at the ``AgentRegistry`` level: the
  AG-UI-shaped caller (``registry.attach(...)`` called directly, mirroring
  ``agui/endpoint.py``) and the TUI-shaped caller
  (``registry.attach_session(...)``) still observably interfere with each
  other exactly as they did before stage 1 — i.e. stage 1 does NOT yet fix
  the N:N gap (that is explicitly stage 2's job); it only changes the
  REPRESENTATION underneath, not the behaviour.
"""
from __future__ import annotations

import pytest

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry, AttachedConnection
from tests._support.agent_session import make_session


def _registry(tmp_path):
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("beta")
    return reg


# ---------------------------------------------------------------------------
# AttachedConnection — isolated contract
# ---------------------------------------------------------------------------


def test_fresh_connection_has_nothing_attached_and_no_active() -> None:
    """Tier 2: a new connection starts empty on both axes."""
    conn = AttachedConnection()
    assert conn.attached_set() == frozenset()
    assert conn.active is None
    assert conn.attach_failed() is False


def test_switch_sets_both_attached_and_active() -> None:
    """Tier 2: stage 1 invariant — ``switch`` makes ``key`` both the sole
    member of ``attached_set()`` and the ``active`` value (attached and
    active coincide in stage 1; stage 2 is what lets them diverge)."""
    conn = AttachedConnection()
    key = ("alpha", _DEFAULT_SID)
    old = conn.switch(key)
    assert old is None
    assert conn.attached_set() == frozenset({key})
    assert conn.active == key
    assert conn.is_attached(key) is True


def test_switch_replaces_not_adds() -> None:
    """Tier 2: stage 1 invariant — a second ``switch`` REPLACES the first
    entry; ``attached_set()`` never grows past 1 in stage 1 (that growth is
    stage 2's whole point).

    Falsification (performed during review): changing ``switch`` to
    ``self._attached[key] = None`` WITHOUT the preceding ``.clear()`` makes
    this test go RED — ``attached_set()`` would contain both keys.
    """
    conn = AttachedConnection()
    key_a = ("alpha", _DEFAULT_SID)
    key_b = ("beta", _DEFAULT_SID)
    conn.switch(key_a)
    old = conn.switch(key_b)
    assert old == key_a
    assert conn.attached_set() == frozenset({key_b})
    assert conn.active == key_b
    assert conn.is_attached(key_a) is False


def test_switch_to_none_detaches() -> None:
    """Tier 2: ``switch(None)`` clears both axes (the ``detach()`` shape)."""
    conn = AttachedConnection()
    key = ("alpha", _DEFAULT_SID)
    conn.switch(key)
    old = conn.switch(None)
    assert old == key
    assert conn.attached_set() == frozenset()
    assert conn.active is None


def test_record_background_attach_error_round_trips() -> None:
    """Tier 2: the moved ``attach_failed``/``record_background_attach_error``
    pair behaves identically to the pre-move registry methods."""
    conn = AttachedConnection()
    assert conn.attach_failed() is False
    conn.record_background_attach_error("boom")
    assert conn.attach_failed() is True
    conn.record_background_attach_error(None)
    assert conn.attach_failed() is False


# ---------------------------------------------------------------------------
# Zero-behaviour-change witness at the AgentRegistry level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage1_still_shares_one_pointer_between_both_callers(tmp_path) -> None:
    """Tier 2: #3793 stage 1's OWN scope boundary — the AG-UI-shaped caller
    (``registry.attach``, mirroring ``agui/endpoint.py:171``) and the
    TUI-shaped caller (``registry.attach_session``, mirroring
    ``in_process.py``'s use) still observably interfere: attaching via one
    shape flips what the OTHER shape's own accessors report, because both
    still route through the SAME ``AgentRegistry._connection`` instance.

    This is deliberately a POSITIVE assertion that the interference still
    exists — proving stage 1 changed the representation, not the behaviour.
    Stage 2's own test (not this file) is what asserts the interference is
    GONE, on a design that gives AG-UI its own connection instance.
    """
    reg = _registry(tmp_path)
    reg.get_or_load("beta")  # attach_session requires the target to already exist

    await reg.attach("alpha")  # AG-UI-shaped call
    assert reg.attached_name == "alpha"

    await reg.attach_session("beta", _DEFAULT_SID)  # TUI-shaped call
    assert reg.attached_name == "beta", (
        "stage 1: the TUI-shaped call still overwrites what the AG-UI-shaped "
        "call saw — same shared connection, exactly as before this PR"
    )

    await reg.attach("alpha")  # back to AG-UI-shaped
    assert reg.attached_name == "alpha", (
        "and the AG-UI-shaped call still overwrites the TUI-shaped one back — "
        "confirms the single shared instance still cuts both ways"
    )


@pytest.mark.asyncio
async def test_attach_still_reports_via_the_new_connection(tmp_path) -> None:
    """Tier 2: ``AgentRegistry.attach`` populates the ``AttachedConnection``
    instance (not a stray, disconnected ``_attached`` attribute) — checked
    through the PUBLIC ``attached_name``/``attached_sid`` accessors, which
    themselves read ONLY ``self._connection.active`` post-move.

    Falsification (performed during review): reverting
    ``self._connection.switch(key)`` to the old ``self._attached = key`` in
    ``AgentRegistry.attach`` makes this test go RED — ``attached_name``/
    ``attached_sid`` both read ``self._connection.active``, which stays
    ``None`` when the attribute assignment silently creates an unrelated
    ``self._attached`` name instead of updating the connection object.
    """
    reg = _registry(tmp_path)
    await reg.attach("alpha")
    assert reg.attached_name == "alpha"
    assert reg.attached_sid == _DEFAULT_SID
