"""Tier 2: SandboxBackend.session_artifact_outside_write_scope contract (#4434
stage 1) — every concrete backend bears this contract (owner ruling: the
sandbox abstraction means every backend needs the abstract contract, not just
the one implementation that currently has real content to protect).

This file's job is the CROSS-backend piece two single-backend test files
(test_sandbox_seatbelt.py, test_landlock_exec_shim_1344e.py) cannot cover on
their own: that the full concrete-backend set is actually exercised, and that
a backend answering "vacuously True" (nothing on disk to protect) does so
self-consciously rather than by accident (e.g. the enumeration silently
shrinking to fewer backends, or a backend inheriting an unimplemented default
that happens to return something truthy).

#4439 CI (this arc's own PR) caught TWO successive shapes of this census
being wrong, in order:
  1. a hand-typed 3-tuple missed ``DockerEnvironmentBackend`` (lives under
     ``environment/``, a different directory from the other 3).
  2. the fix for (1) — an AST scan of ``src/reyn`` rooted at
     ``Path(__file__).resolve().parents[N]`` — tripped
     ``file-depth-reference-gate.yml`` (lead-coder correction): that kind of
     reference breaks the moment the SCANNING file itself moves to a
     different depth under ``tests/``, and doing a directory walk from a
     test to answer "who implements this Protocol" duplicates work a
     stable source should do once.

The actual fix (both times) is structural, not "patch the list": the census
now reads ``reyn.security.sandbox.backend.all_concrete_backend_classes()`` —
a single, explicit registry living in the Protocol's own home module — so a
future backend author has one place to update, discoverable at the same
place they implement the Protocol, and no test needs to re-derive it via a
filesystem/AST scan of any kind.
"""
from __future__ import annotations

from reyn.environment.container_backend import DockerEnvironmentBackend
from reyn.security.sandbox.backend import SandboxBackend, all_concrete_backend_classes
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import SandboxPolicy


def test_the_concrete_backend_enumeration_has_not_silently_shrunk():
    """Tier 2: vacuity guard — ``all_concrete_backend_classes()`` names
    exactly the 4 real implementers. If a future backend is added (or one
    is accidentally removed / renamed in the registry), this assertion is
    what goes RED — not a test silently covering fewer backends and
    staying green regardless."""
    found = all_concrete_backend_classes()
    assert {cls.__name__ for cls in found} == {
        "SeatbeltBackend", "LandlockBackend", "NoopBackend", "DockerEnvironmentBackend",
    }


def test_every_backend_implements_the_contract_and_conforms_to_the_protocol():
    """Tier 2: every concrete backend (a) satisfies the runtime-checkable
    SandboxBackend Protocol (which now includes
    session_artifact_outside_write_scope) and (b) returns a real bool, not
    None/NotImplemented, for a representative policy. Scoped to the 3
    no-arg-constructible backends; DockerEnvironmentBackend has its own
    dedicated test below (its constructor requires real arguments)."""
    policy = SandboxPolicy(write_paths=["/some/workspace"])
    for cls in (SeatbeltBackend, LandlockBackend, NoopBackend):
        backend = cls()
        assert isinstance(backend, SandboxBackend), f"{cls.__name__} breaks Protocol conformance"
        result = backend.session_artifact_outside_write_scope(policy)
        assert isinstance(result, bool), (
            f"{cls.__name__}.session_artifact_outside_write_scope() returned "
            f"{result!r}, not a bool"
        )


def test_docker_backend_implements_the_contract():
    """Tier 2: DockerEnvironmentBackend (#4439 CI's own catch — see module
    docstring) is IN the registry and answers
    session_artifact_outside_write_scope, vacuously True (docker exec never
    writes a policy-derived file to the host)."""
    assert DockerEnvironmentBackend in all_concrete_backend_classes()

    backend = DockerEnvironmentBackend(container="reyn-test-container", repo_dir="/repo")
    assert isinstance(backend, SandboxBackend)
    result = backend.session_artifact_outside_write_scope(SandboxPolicy(write_paths=["/repo"]))
    assert result is True


def test_only_seatbelt_answers_non_vacuously_today():
    """Tier 2: names which backend's answer is a REAL derivation from
    *policy* (Seatbelt: materialises a file, so relocating write_paths onto
    the cache dir must flip its answer) versus VACUOUSLY True regardless of
    *policy* (Landlock, Noop, Docker: nothing is ever written to disk, so no
    write_paths value can make their answer False today)."""
    from reyn.security.sandbox.backends.seatbelt import _seatbelt_cache_dir

    adversarial_policy = SandboxPolicy(write_paths=[str(_seatbelt_cache_dir())])
    assert SeatbeltBackend().session_artifact_outside_write_scope(adversarial_policy) is False

    # Landlock/Noop never touch disk, so even this adversarial policy (which
    # only means anything relative to SEATBELT's own cache dir) can't flip
    # them — their vacuous True is unconditional, which is exactly what
    # "vacuous" means here, not a gap in the check.
    assert LandlockBackend().session_artifact_outside_write_scope(adversarial_policy) is True
    assert NoopBackend().session_artifact_outside_write_scope(adversarial_policy) is True
