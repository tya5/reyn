"""Tier 1/2c: the axis contract's type-enforced continuation and its
production-gate blast-radius invariant (#2983 continuation).

Three things this file exists to pin, matching the module docstring of
``reyn.security.sandbox.axis_contract``:

1. The type-level forcing works: ``AxisException`` cannot be built without a
   ``boundary_probe``, and ``AxisContract`` cannot be built without stating
   ``exceptions`` — both a real ``TypeError`` at the call site, not merely a
   documented convention.
2. The migration-count guard works: exactly the axes named in
   ``_EXPECTED_MIGRATED_AXES`` read as migrated — now all three (write,
   spawn, network), a requirement stated explicitly rather than derived
   from the registry itself, so the check cannot pass vacuously the moment
   every axis happens to be migrated (see ``_EXPECTED_MIGRATED_AXES``'s own
   docstring in ``axis_contract.py``).
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
    read as is_migrated. Now that write/spawn/network are ALL migrated,
    this asserts the concrete claim "all three", not merely "the registry
    and the expectation happen to agree" — _EXPECTED_MIGRATED_AXES is a
    literal frozenset naming all three axes, not derived from the registry,
    so a future axis silently un-migrated (or a new axis added without
    updating this expectation) still fails here rather than passing
    vacuously."""
    migrated = {axis.name for axis in AXIS_REGISTRY if axis.is_migrated}
    assert migrated == _EXPECTED_MIGRATED_AXES
    assert migrated == {"write", "spawn", "network"}


def _assert_axis_fully_migrated(axis: AxisContract) -> None:
    """Shared assertion body: every one of the four legs names something
    real — no field silently still NOT_MIGRATED."""
    assert axis.deny_probe is not NOT_MIGRATED
    assert axis.exceptions is not NOT_MIGRATED
    assert axis.workload_test_id is not NOT_MIGRATED
    assert axis.witness_strength is not NOT_MIGRATED
    assert axis.is_migrated is True


def test_network_axis_is_fully_migrated_with_no_leg_left_behind() -> None:
    """Tier 1: the migrated axis names something real on every one of the
    four fields — no field silently still NOT_MIGRATED."""
    axis = next(a for a in AXIS_REGISTRY if a.name == "network")
    _assert_axis_fully_migrated(axis)
    exception_names = {exc.name for exc in axis.exceptions}
    assert "null_addr_socketpair_selfpipe" in exception_names


@pytest.mark.parametrize("axis_name", ["write", "spawn"])
def test_write_and_spawn_axes_are_fully_migrated_with_no_intentional_exceptions(
    axis_name: str,
) -> None:
    """Tier 1: write and spawn now name something real on every leg too —
    and their ``exceptions`` is an explicit empty tuple (a stated "no
    deliberate hole", not a forgotten field, which would instead have been
    a TypeError at construction — see test_axis_contract_requires_exceptions_field)."""
    axis = next(a for a in AXIS_REGISTRY if a.name == axis_name)
    _assert_axis_fully_migrated(axis)
    assert axis.exceptions == ()


def _resolve_workload_test(workload_test_id: str) -> object:
    """Load the module a workload_test_id points into and return the named
    test function, or None if it does not resolve.

    importlib.util rather than `import tests.<module>` — tests/ has no
    __init__.py (a deliberate namespace-package-free layout, and pyproject's
    pytest `pythonpath` only adds src/), so a plain package import is not
    guaranteed to resolve the same way pytest's own collection does. Loading
    by file path is robust to both.
    """
    path, _, func_name = workload_test_id.partition("::")
    repo_root = Path(__file__).resolve().parent.parent
    module_path = repo_root / path
    spec = importlib.util.spec_from_file_location("_workload_module_2983", module_path)
    assert spec is not None and spec.loader is not None
    workload_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workload_module)
    return getattr(workload_module, func_name, None)


def test_network_workload_test_id_resolves_to_a_real_test() -> None:
    """Tier 1: the workload leg's pytest node id names an EXISTING test
    function — #3060's real chunker-serving probe, reused rather than
    reimplemented (architect firm), not a stale or typo'd reference."""
    axis = next(a for a in AXIS_REGISTRY if a.name == "network")
    path, _, _ = axis.workload_test_id.partition("::")
    assert path == "tests/test_sandbox_seccomp_network_3030.py"

    func = _resolve_workload_test(axis.workload_test_id)
    assert func is not None, (
        f"{axis.workload_test_id} does not resolve — the network axis's "
        f"workload leg points at a test that no longer exists"
    )
    assert inspect.iscoroutinefunction(func)


@pytest.mark.parametrize("axis_name", ["write", "spawn"])
def test_write_and_spawn_workload_test_ids_resolve_to_real_tests(axis_name: str) -> None:
    """Tier 1: write's and spawn's workload legs each name an EXISTING test
    function in THIS file (added by this PR, see section 5 below) — not a
    stale or typo'd reference."""
    axis = next(a for a in AXIS_REGISTRY if a.name == axis_name)
    path, _, _ = axis.workload_test_id.partition("::")
    assert path == "tests/test_sandbox_axis_contract_2983.py"

    func = _resolve_workload_test(axis.workload_test_id)
    assert func is not None, (
        f"{axis.workload_test_id} does not resolve — the {axis_name} axis's "
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


@requires_landlock
def test_write_axis_deny_leg_fires_against_landlock_and_fails_against_noop() -> None:
    """Tier 2c: the write axis's DENY leg — reused from stage 1's
    probe_enforcement (see _write_deny_probe) — is live wiring: it must
    witness a real deny on Landlock and be able to report failure against
    NoopBackend, the same falsifying medium every other axis's deny leg is
    checked against."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    axis = next(a for a in AXIS_REGISTRY if a.name == "write")
    assert axis.deny_probe(LandlockBackend()) is None
    assert axis.deny_probe(NoopBackend()) is not None


@requires_landlock
def test_spawn_axis_deny_leg_fires_against_landlock_and_fails_against_noop() -> None:
    """Tier 2c: the spawn axis's DENY leg — reused from stage 2's
    probe_subprocess_enforcement (see _spawn_deny_probe) — is live wiring:
    it must witness a real deny on Landlock (whose preexec loads the
    seccomp filter, #2983 stage 2) and be able to report failure against
    NoopBackend."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    axis = next(a for a in AXIS_REGISTRY if a.name == "spawn")
    assert axis.deny_probe(LandlockBackend()) is None
    assert axis.deny_probe(NoopBackend()) is not None


# ── 5. Workload legs — the real thing each axis exists to gate ────────────
#
# Unlike network's workload leg (#3060's pre-existing chunker-serving test,
# reused as-is), no equivalent pre-existing test was found for write or
# spawn's "reachable for purpose" claim, so these two are added here,
# minimally, and referenced by name from _WRITE_CONTRACT.workload_test_id /
# _SPAWN_CONTRACT.workload_test_id above.


@requires_landlock
@pytest.mark.asyncio
async def test_write_workload_grant_write_succeeds() -> None:
    """Tier 2c: WRITE axis workload leg — a write INSIDE a write_paths grant
    actually succeeds through the real Landlock backend (not the synthetic
    two-temp-dir policy probe_enforcement uses for its deny leg — this is
    the "reachable for purpose" claim, mirroring what #3060's chunker-
    serving test established for the network axis: the real workload, not
    just the deny, must reach its intended state)."""
    import tempfile
    from pathlib import Path as _Path

    from reyn.security.sandbox.backends.landlock import LandlockBackend
    from reyn.security.sandbox.policy import SandboxPolicy as _Policy

    backend = LandlockBackend()
    with tempfile.TemporaryDirectory() as granted_dir:
        target = _Path(granted_dir) / "granted-write.txt"
        policy = _Policy(
            read_paths=["/bin", "/usr/lib", "/lib"],
            write_paths=[granted_dir],
            network=False,
            env_passthrough=["PATH"],
            timeout_seconds=10,
        )
        result = await backend.run(["/bin/sh", "-c", f"echo ok > {target}"], policy)
        assert result.returncode == 0, (
            f"a write INSIDE the grant failed: rc={result.returncode}, "
            f"stderr={result.stderr!r}"
        )
        assert target.read_text() == "ok\n"


@requires_landlock
@pytest.mark.asyncio
async def test_spawn_workload_permitted_child_process_launches() -> None:
    """Tier 2c: SPAWN axis workload leg — a child process PERMITTED by
    allow_subprocess=True actually launches and runs through the real
    Landlock backend (whose preexec also loads the seccomp filter, #2983
    stage 2) — the "reachable for purpose" claim for this axis."""
    import tempfile
    from pathlib import Path as _Path

    from reyn.security.sandbox.backends.landlock import LandlockBackend
    from reyn.security.sandbox.policy import SandboxPolicy as _Policy

    backend = LandlockBackend()
    with tempfile.TemporaryDirectory() as granted_dir:
        marker = _Path(granted_dir) / "spawned"
        policy = _Policy(
            read_paths=["/bin", "/usr/lib", "/lib"],
            write_paths=[granted_dir],
            network=False,
            allow_subprocess=True,
            env_passthrough=["PATH"],
            timeout_seconds=10,
        )
        # A pipeline, not a bare command, so the shell must fork its
        # left-hand side (measured elsewhere in this repo: a shell asked to
        # run one simple command may exec it in place with no fork at all —
        # see probe_subprocess_enforcement's docstring in self_test.py).
        result = await backend.run(
            ["/bin/sh", "-c", f"touch {marker} | cat"], policy
        )
        assert result.returncode == 0, (
            f"a permitted child process did not launch: rc={result.returncode}, "
            f"stderr={result.stderr!r}"
        )
        assert marker.exists()
