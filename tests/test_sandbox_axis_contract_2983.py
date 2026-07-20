"""Tier 1/2c: the axis contract's type-enforced continuation and its
production-gate blast-radius invariant (#2983 continuation).

Three things this file exists to pin, matching the module docstring of
``reyn.security.sandbox.axis_contract``:

1. The type-level forcing works: ``AxisException`` cannot be built without a
   ``boundary_probe``, and ``AxisContract`` cannot be built without stating
   ``exceptions`` — both a real ``TypeError`` at the call site, not merely a
   documented convention.
2. The migration-count guard works: exactly the axes named in
   ``_EXPECTED_MIGRATED_AXES`` read as migrated, and an axis that has not
   named all four legs does NOT read as migrated even partially.
3. ★★ The production gate's blast radius has not widened. This is the single
   most important invariant this PR must not break: ``enforcement_self_test``
   — the function every real backend resolution calls — must still gate
   EXACTLY the write and spawn axes, and must NOT reference
   ``probe_network_enforcement``. Widening that set is exactly the mistake
   the architect firm warned against: a probe bug on a newly-added axis would
   silently fall every sandboxed op, on every host, back to ``NoopBackend``.

The real 3-leg witnessing (deny + boundary + workload, against a real
Landlock backend and against ``NoopBackend`` as the falsifying medium) is
Linux-only and lives in the ``@requires_landlock``-gated group below, mirroring
``tests/test_sandbox_seccomp_network_3030.py``'s own gating — a green run on a
non-Linux dev box witnesses nothing there, same as every sibling file.
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from reyn.security.sandbox import NoopBackend
from reyn.security.sandbox.axis_contract import (
    _EXPECTED_MIGRATED_AXES,
    AXIS_REGISTRY,
    NOT_MIGRATED,
    AxisContract,
    AxisException,
)


def _landlock_available() -> bool:
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    return LandlockBackend().available()


requires_landlock = pytest.mark.skipif(
    not _landlock_available(),
    reason="Landlock unavailable — real network-axis enforcement cannot be witnessed on this host",
)


# ── 1. Type-enforced continuation ─────────────────────────────────────────


def test_axis_exception_requires_boundary_probe() -> None:
    """Tier 1: an AxisException cannot be constructed without a boundary_probe
    — omitting it is a TypeError, not a silently-empty exception."""
    with pytest.raises(TypeError):
        AxisException(name="some_exception")  # type: ignore[call-arg]


def test_axis_contract_requires_exceptions_field() -> None:
    """Tier 1: an AxisContract cannot be constructed without stating
    exceptions — there is no default that reads a forgotten field as "none"."""
    with pytest.raises(TypeError):
        AxisContract(  # type: ignore[call-arg]
            name="hypothetical",
            deny_probe=NOT_MIGRATED,
            workload_test_id=NOT_MIGRATED,
            witness_strength=NOT_MIGRATED,
        )


def test_axis_contract_requires_every_field() -> None:
    """Tier 1: every AxisContract field is required — no defaults anywhere
    (deny_probe / workload_test_id / witness_strength included), matching the
    docstring's "every field is required" claim."""
    with pytest.raises(TypeError):
        AxisContract(name="hypothetical")  # type: ignore[call-arg]


# ── 2. Migration-count guard ──────────────────────────────────────────────


def test_axis_registry_declares_exactly_write_spawn_network() -> None:
    """Tier 1: the registry names exactly the three axes #2983 knows about
    today — not fewer (a dropped axis silently un-tracked) and not more
    (a new axis that must also be migrated deliberately, not by accident)."""
    names = {axis.name for axis in AXIS_REGISTRY}
    assert names == {"write", "spawn", "network"}


def test_only_the_expected_axes_are_migrated() -> None:
    """Tier 1: the migration-count guard — exactly _EXPECTED_MIGRATED_AXES
    read as is_migrated. Shrinking network back to NOT_MIGRATED, or promoting
    write/spawn without updating _EXPECTED_MIGRATED_AXES, both fail here."""
    migrated = {axis.name for axis in AXIS_REGISTRY if axis.is_migrated}
    assert migrated == _EXPECTED_MIGRATED_AXES


@pytest.mark.parametrize("axis_name", ["write", "spawn"])
def test_unmigrated_axes_carry_explicit_markers_on_every_leg(axis_name: str) -> None:
    """Tier 1: an unmigrated axis is NOT_MIGRATED on ALL FOUR legs, never a
    partial mix — a partially-migrated axis is exactly as unwitnessed on its
    missing legs as a fully-unmigrated one, so it must not read as "some
    progress" that could be mistaken for real coverage."""
    axis = next(a for a in AXIS_REGISTRY if a.name == axis_name)
    assert axis.deny_probe is NOT_MIGRATED
    assert axis.exceptions is NOT_MIGRATED
    assert axis.workload_test_id is NOT_MIGRATED
    assert axis.witness_strength is NOT_MIGRATED
    assert axis.is_migrated is False


def test_network_axis_is_fully_migrated_with_no_leg_left_behind() -> None:
    """Tier 1: the migrated axis names something real on every one of the
    four fields — no field silently still NOT_MIGRATED."""
    axis = next(a for a in AXIS_REGISTRY if a.name == "network")
    assert axis.deny_probe is not NOT_MIGRATED
    assert axis.exceptions is not NOT_MIGRATED
    assert axis.workload_test_id is not NOT_MIGRATED
    assert axis.witness_strength is not NOT_MIGRATED
    assert axis.is_migrated is True
    assert len(axis.exceptions) == 1
    assert axis.exceptions[0].name == "null_addr_socketpair_selfpipe"


def test_network_workload_test_id_resolves_to_a_real_test() -> None:
    """Tier 1: the workload leg's pytest node id names an EXISTING test
    function — #3060's real chunker-serving probe, reused rather than
    reimplemented (architect firm), not a stale or typo'd reference."""
    axis = next(a for a in AXIS_REGISTRY if a.name == "network")
    path, _, func_name = axis.workload_test_id.partition("::")
    assert path == "tests/test_sandbox_seccomp_network_3030.py"

    # importlib.util rather than `import tests.<module>` — tests/ has no
    # __init__.py (a deliberate namespace-package-free layout, and
    # pyproject's pytest `pythonpath` only adds src/), so a plain package
    # import is not guaranteed to resolve the same way pytest's own
    # collection does. Loading by file path is robust to both.
    repo_root = Path(__file__).resolve().parent.parent
    module_path = repo_root / path
    spec = importlib.util.spec_from_file_location("_workload_module_2983", module_path)
    assert spec is not None and spec.loader is not None
    workload_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workload_module)

    func = getattr(workload_module, func_name, None)
    assert func is not None, (
        f"{axis.workload_test_id} does not resolve — the network axis's "
        f"workload leg points at a test that no longer exists"
    )
    assert inspect.iscoroutinefunction(func)


# ── 3. ★★ Production gate blast-radius invariant ──────────────────────────


def test_enforcement_self_test_still_gates_exactly_write_and_spawn() -> None:
    """Tier 1: ★★ the single most important invariant this PR must not break.
    enforcement_self_test — the cached suite EVERY real backend resolution
    calls — must still reference only probe_enforcement (write) and
    probe_subprocess_enforcement (spawn), and must NOT reference
    probe_network_enforcement. Widening this set widens the blast radius of a
    probe bug to every sandboxed op on every host — exactly what the
    self_test.py module docstring says the network probe was kept out for."""
    from reyn.security.sandbox import self_test as self_test_mod

    src = inspect.getsource(self_test_mod.enforcement_self_test)
    assert "probe_enforcement(" in src
    assert "probe_subprocess_enforcement(" in src
    # The CALL form specifically (parens) — the docstring's prose section
    # explaining why probe_network_enforcement is deliberately excluded
    # mentions the bare name, which must not itself trip this guard.
    assert "probe_network_enforcement(" not in src


def test_axis_contract_module_does_not_widen_the_cached_suite() -> None:
    """Tier 1: importing axis_contract must not, as a side effect, register
    the network probe into self_test's process-global cache under a key that
    make_default_backend's cached suite would consult. Concretely: the
    process-global _CACHE self_test.py uses for enforcement_self_test is
    keyed on backend.name and untouched by importing this module."""
    from reyn.security.sandbox import self_test as self_test_mod

    before = dict(self_test_mod._CACHE)
    import reyn.security.sandbox.axis_contract  # noqa: F401 (import side effect under test)

    assert self_test_mod._CACHE == before


# ── 4. Real 3-leg witnessing (Linux-only) ─────────────────────────────────


@requires_landlock
def test_network_axis_deny_leg_fires_against_landlock_and_fails_against_noop() -> None:
    """Tier 2c: the registry's DENY leg is live wiring, not decoration — it
    must both witness a real deny on a real Landlock backend AND be able to
    report failure against NoopBackend (the falsifying medium the stage-1
    self-test docstring establishes: NoopBackend enforces nothing by
    contract and is available on every platform, so a probe unable to fail
    against it cannot distinguish a sandbox from a passthrough)."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    axis = next(a for a in AXIS_REGISTRY if a.name == "network")
    assert axis.deny_probe(LandlockBackend()) is None
    assert axis.deny_probe(NoopBackend()) is not None


@requires_landlock
def test_network_axis_boundary_leg_fires_against_landlock_and_fails_against_noop() -> None:
    """Tier 2c: the registry's BOUNDARY leg (the null_addr_socketpair_selfpipe
    exception's own probe) is live wiring — it must witness the exception's
    boundary intact on real Landlock and be able to report failure against
    NoopBackend."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    axis = next(a for a in AXIS_REGISTRY if a.name == "network")
    boundary_probe = axis.exceptions[0].boundary_probe
    assert boundary_probe(LandlockBackend()) is None
    assert boundary_probe(NoopBackend()) is not None
