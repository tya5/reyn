"""Tier 2/3: #2081 S2 — the delegate-signal + unbound-delegate default-deny.

When ``delegation.capability_default=deny``, an UNBOUND delegate (one spawned by
another agent's delegation — the A2A request path passes ``is_delegate=True``,
recursively) resolves to the restrictive built-in ``_delegate`` floor instead of
``(None, ∅)``. A topology capability_profile binding REPLACES the default (the
binding is the re-grant; composition is most-restrictive-wins and cannot re-grant).

The KEY safety property (laundering-prevention): the default-deny propagates
recursively, so a re-granted coordinator's UNBOUND sub-delegate is STILL
default-denied — being bound (re-granted) does not launder capability down to
sub-delegates.

No mocks: a real AgentRegistry + real on-disk topology/profile YAML; resolved via
the real ``resolved_profile_for`` and the real ``get_or_load`` → factory thread.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.capability_profile import _BUILTIN_UNTRUSTED_DENY
from reyn.security.permissions.effective import ContextualPermission

# A representative dangerous tool from the taxonomy (re-delegation class).
_REDELEGATE = "delegate_to_agent"


def _registry(tmp_path: Path, *, default: str = "deny") -> AgentRegistry:
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda profile: None,
        delegation_capability_default=default,
    )


def _bind_profile(tmp_path: Path, *, member: str, profile: str, body: str) -> None:
    """Bind ``member`` to ``profile`` in a network topology + write the profile YAML."""
    topo_dir = tmp_path / ".reyn" / "topologies"
    topo_dir.mkdir(parents=True, exist_ok=True)
    (topo_dir / "t.yaml").write_text(
        f"name: t\nkind: network\nmembers: [{member}, peer]\n"
        f"profiles:\n  {member}: {profile}\n",
        encoding="utf-8",
    )
    prof_dir = tmp_path / ".reyn" / "capability_profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)
    (prof_dir / f"{profile}.yaml").write_text(body, encoding="utf-8")


# ── the unbound-delegate default-deny (the core fork) ───────────────────────


def test_unbound_delegate_deny_gets_delegate_floor(tmp_path: Path) -> None:
    """Tier 2: deny + unbound + delegate → the restrictive _delegate floor (the full
    single-sourced dangerous-tool taxonomy is denied)."""
    contextual, excluded = _registry(tmp_path).resolved_profile_for("solo", is_delegate=True)
    assert isinstance(contextual, ContextualPermission)
    assert _REDELEGATE in contextual.tool_deny
    # the floor IS the single-sourced taxonomy (every dangerous class denied)
    assert _BUILTIN_UNTRUSTED_DENY <= contextual.tool_deny


def test_unbound_delegate_inherit_is_none(tmp_path: Path) -> None:
    """Tier 2: inherit (default policy) + unbound + delegate → (None, ∅) — byte-identical
    to pre-#2081 (the delegate inherits the spawner's surface)."""
    assert _registry(tmp_path, default="inherit").resolved_profile_for(
        "solo", is_delegate=True
    ) == (None, frozenset())


def test_top_level_deny_is_none(tmp_path: Path) -> None:
    """Tier 2: deny + unbound + NON-delegate (a top-level/root agent) → (None, ∅).
    A root is never default-denied — only delegates are."""
    assert _registry(tmp_path).resolved_profile_for("solo", is_delegate=False) == (
        None, frozenset(),
    )


# ── re-grant: a topology binding REPLACES the default ───────────────────────


def test_bound_delegate_regrant_replaces_default(tmp_path: Path) -> None:
    """Tier 2: deny + delegate but BOUND to a permissive profile → the binding REPLACES
    the _delegate default (re-grant). The permissive profile does NOT deny re-delegation,
    so the result does not carry the _delegate floor's deny."""
    _bind_profile(tmp_path, member="worker", profile="coordinator",
                  body="name: coordinator\ntool_deny: []\n")
    contextual, _excluded = _registry(tmp_path).resolved_profile_for("worker", is_delegate=True)
    # the bound (permissive) profile replaced the default → re-delegation is re-granted
    assert _REDELEGATE not in (contextual.tool_deny if contextual else frozenset())


# ── THE KEY SAFETY PROPERTY: recursive default-deny (no laundering) ─────────


def test_recursive_subdelegate_of_regranted_coordinator_still_denied(tmp_path: Path) -> None:
    """Tier 2: laundering-prevention (the property lead verifies hardest). A re-granted
    coordinator (BOUND, so it CAN re-delegate) does NOT launder capability to its
    UNBOUND sub-delegate: the sub-delegate — reached via the A2A request path, so
    is_delegate=True recursively — is STILL default-denied.

    Both halves in one test: the coordinator is re-granted (re-delegation allowed) AND
    its unbound sub-delegate is default-denied (re-delegation + the whole taxonomy)."""
    _bind_profile(tmp_path, member="coordinator", profile="coord_profile",
                  body="name: coord_profile\ntool_deny: []\n")
    reg = _registry(tmp_path)

    # the re-granted coordinator CAN re-delegate (the binding replaced the default)
    coord_perm, _ = reg.resolved_profile_for("coordinator", is_delegate=True)
    assert _REDELEGATE not in (coord_perm.tool_deny if coord_perm else frozenset())

    # its UNBOUND sub-delegate is STILL default-denied (no laundering)
    sub_perm, _ = reg.resolved_profile_for("sub_worker", is_delegate=True)
    assert isinstance(sub_perm, ContextualPermission)
    assert _REDELEGATE in sub_perm.tool_deny
    assert _BUILTIN_UNTRUSTED_DENY <= sub_perm.tool_deny


# ── the is_delegate thread: get_or_load → transient → factory resolution ────


def test_get_or_load_is_delegate_threads_through_factory(tmp_path: Path) -> None:
    """Tier 3a: the spawn-reason signal reaches the factory's resolution WITHOUT a
    factory-signature change. A factory that calls resolved_profile_for(profile.name)
    (the None-arg call the real factories make) sees the _delegate floor under
    get_or_load(is_delegate=True), and (None, ∅) under a normal load — proving the
    _construct_session transient threads is_delegate to the construction-time call."""
    seen: dict = {}

    def _factory(profile):
        # mirrors the real factory: resolves WITHOUT passing is_delegate explicitly.
        seen[profile.name] = reg.resolved_profile_for(profile.name)
        return None

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, delegation_capability_default="deny",
    )
    reg.create("as_delegate")
    reg.create("as_root")

    reg.get_or_load("as_delegate", is_delegate=True)
    reg.get_or_load("as_root")  # default is_delegate=False

    delegate_perm, _ = seen["as_delegate"]
    assert isinstance(delegate_perm, ContextualPermission)
    assert _REDELEGATE in delegate_perm.tool_deny       # delegate → default-denied
    assert seen["as_root"] == (None, frozenset())        # root → unaffected


def test_transient_restored_after_real_delegate_construction(tmp_path: Path) -> None:
    """Tier 3a: the is_delegate transient is restored after a REAL delegate
    construction — a later non-delegate resolution is not contaminated.

    Exercises the actual get_or_load(is_delegate=True) → _construct_session →
    factory path (not an explicit-arg shortcut, which would bypass the transient):
    the factory's resolved_profile_for(name) sees the floor DURING construction;
    afterwards a direct resolved_profile_for(other) must be (None, ∅). Removing the
    finally-restore in _construct_session makes this RED (the transient stays True)."""
    seen: dict = {}

    def _factory(profile):
        seen[profile.name] = reg.resolved_profile_for(profile.name)  # None-arg → transient
        return None

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, delegation_capability_default="deny",
    )
    reg.create("worker")
    reg.get_or_load("worker", is_delegate=True)  # a REAL delegate construction

    # DURING construction the transient was True (the floor was resolved)
    worker_perm, _ = seen["worker"]
    assert _REDELEGATE in worker_perm.tool_deny
    # AFTER construction the transient is restored → a direct resolution is clean
    assert reg.resolved_profile_for("other_unbound") == (None, frozenset())
