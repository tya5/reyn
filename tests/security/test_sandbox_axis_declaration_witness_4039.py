"""Tier 1/2c: #4039 D4 — declaration ↔ witness conformance.

A backend's ``enforced_axes`` (D1: the backend's OWN declaration, read by
the production predicate ``unenforced_axes()``, never probed) and
``axis_contract.AXIS_REGISTRY``'s ``witness_strength`` (the CI-conformance-only
REAL-execution record — see that module's own docstring for the two-layer
split, CLAUDE.md's hard rule) are two SEPARATE registries at two separate
layers. D4 is the CI-only bridge that keeps them from silently diverging:
whenever a backend declares ``ENFORCES`` for an axis this contract has
migrated, that backend must ALSO have a witness entry there — otherwise the
production declaration is an unverified claim (exactly the shape #4039
exists to close, one layer up).

**``DOES_NOT_ENFORCE`` needs no witness — its ABSENCE from ``witness_strength``
IS the correct state** (architect's explicit correction, #4039): a backend
that declares it does not enforce an axis has nothing to witness there, and
this file's own tests assert that absence is accepted, not flagged.

**Naming bridge**: ``AxisEnforcementDeclaration`` is keyed by
``SandboxPolicy`` FIELD names (``write_paths``/``network``/``deny_subprocess``/
...); ``AXIS_REGISTRY`` is keyed by axis_contract's own coarser
``write``/``spawn``/``network`` vocabulary (CLAUDE.md's 2-layer sandbox rule
— see ``backend.AxisEnforcementDeclaration``'s own docstring for why the two
vocabularies stay separate, not unified). Only the 3 migrated axis_contract
axes have a mapping to a policy field; the other 4 policy axes
(write_deny_paths/read_deny_paths/env_deny_names/allow_env_names) are simply
not covered by axis_contract's real-execution witnessing yet and are out of
this file's scope (D4 only bridges the axes BOTH registries know about).

**Landlock's witness key is "seccomp", not "landlock", for the network and
spawn axes** — this module's own docstring on ``LandlockBackend.enforced_axes``
explains why: Landlock's LSM has no net-port API, so network/subprocess
enforcement on the "landlock" backend actually comes from an
always-loaded seccomp-BPF filter INSIDE that same backend. ``witness_strength``
tracks THAT finer provenance detail; this file's mapping accounts for it
explicitly rather than asserting a witness key that will never exist.
"""
from __future__ import annotations

from reyn.environment.container_backend import DockerEnvironmentBackend
from reyn.security.sandbox.axis_contract import AXIS_REGISTRY
from reyn.security.sandbox.backend import AxisEnforcement
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
from reyn.security.sandbox.noop_backend import NoopBackend

#: {policy field name -> axis_contract axis name} — only the 3 axes BOTH
#: registries know about (see module docstring).
_POLICY_FIELD_TO_CONTRACT_AXIS: "dict[str, str]" = {
    "write_paths": "write",
    "deny_subprocess": "spawn",
    "network": "network",
}

#: {(backend Protocol .name, contract axis name) -> the witness_strength key
#: that actually appears for it} — the identity mapping EXCEPT Landlock's
#: network/spawn axes, witnessed under "seccomp" (see module docstring).
_WITNESS_KEY_OVERRIDES: "dict[tuple[str, str], str]" = {
    ("landlock", "spawn"): "seccomp",
    ("landlock", "network"): "seccomp",
}

#: Every concrete SandboxBackend this repo ships, keyed by its Protocol
#: ``.name`` — the population D4 checks. Docker included deliberately
#: (architect: the best evidence for D2's totality — it declares
#: DOES_NOT_ENFORCE on every axis, so it never triggers a witness
#: requirement here, which is itself the assertion worth making explicit).
_BACKENDS: "dict[str, type]" = {
    "noop": NoopBackend,
    "seatbelt": SeatbeltBackend,
    "landlock": LandlockBackend,
    "docker": DockerEnvironmentBackend,
}


def _witness_key(backend_name: str, contract_axis: str) -> str:
    return _WITNESS_KEY_OVERRIDES.get((backend_name, contract_axis), backend_name)


def test_every_enforces_declaration_on_a_migrated_axis_has_a_witness() -> None:
    """Tier 1: D4 — a backend that declares ENFORCES for a policy field
    mapping to a MIGRATED axis_contract axis must have a witness_strength
    entry there (under the resolved witness key). A declaration with no
    witness is an unverified production claim — the exact shape #4039
    exists to close, one layer up."""
    contracts_by_name = {c.name: c for c in AXIS_REGISTRY}

    missing: list[str] = []
    for backend_name, backend_cls in _BACKENDS.items():
        declared = backend_cls.enforced_axes.as_dict()
        for field_name, contract_axis in _POLICY_FIELD_TO_CONTRACT_AXIS.items():
            if declared[field_name] is not AxisEnforcement.ENFORCES:
                continue
            contract = contracts_by_name[contract_axis]
            if not contract.is_migrated:
                continue  # not yet migrated — nothing to check against
            key = _witness_key(backend_name, contract_axis)
            if key not in contract.witness_strength:
                missing.append(
                    f"{backend_name}.enforced_axes[{field_name}]=ENFORCES but "
                    f"AXIS_REGISTRY[{contract_axis!r}].witness_strength has no "
                    f"{key!r} entry"
                )
    assert not missing, "\n".join(missing)


def test_does_not_enforce_requires_no_witness() -> None:
    """Tier 1: D4's other half (architect's explicit correction) — a backend
    declaring DOES_NOT_ENFORCE for an axis is NOT required to have a
    witness_strength entry; its ABSENCE is the correct state. Docker is the
    concrete instance: it declares DOES_NOT_ENFORCE on write/network/spawn
    and has no witness_strength entry for "docker" anywhere in
    AXIS_REGISTRY — this must NOT be flagged by the check above."""
    contracts_by_name = {c.name: c for c in AXIS_REGISTRY}
    docker_declared = DockerEnvironmentBackend.enforced_axes.as_dict()

    for field_name, contract_axis in _POLICY_FIELD_TO_CONTRACT_AXIS.items():
        assert docker_declared[field_name] is AxisEnforcement.DOES_NOT_ENFORCE
        contract = contracts_by_name[contract_axis]
        assert "docker" not in contract.witness_strength

    # The check itself must accept this state (not raise/flag it) — run it
    # directly rather than only asserting the precondition above.
    test_every_enforces_declaration_on_a_migrated_axis_has_a_witness()


def test_landlocks_network_and_spawn_declarations_witness_under_seccomp() -> None:
    """Tier 1: the naming-bridge instance this file's docstring names —
    Landlock's Protocol name is "landlock", but its network/spawn
    enforcement is witnessed under "seccomp" (a different, finer-grained
    provenance key axis_contract.py tracks). Pins the override table
    doesn't silently go stale if AXIS_REGISTRY's witness keys change."""
    contracts_by_name = {c.name: c for c in AXIS_REGISTRY}
    assert "seccomp" in contracts_by_name["spawn"].witness_strength
    assert "seccomp" in contracts_by_name["network"].witness_strength
    assert "landlock" not in contracts_by_name["spawn"].witness_strength
    assert "landlock" not in contracts_by_name["network"].witness_strength
    # But landlock's WRITE axis is witnessed under its own name — no override.
    assert "landlock" in contracts_by_name["write"].witness_strength
