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
"""
from __future__ import annotations

from reyn.security.sandbox.backend import SandboxBackend
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import SandboxPolicy

# The same 3 concrete classes get_default_backend() (security/sandbox/__init__.py)
# lazy-imports and dispatches to for backend="seatbelt"/"landlock"/"noop" — this
# list is not independently invented, it mirrors that function's own registry.
_ALL_BACKEND_CLASSES = (SeatbeltBackend, LandlockBackend, NoopBackend)


def test_the_concrete_backend_enumeration_has_not_silently_shrunk():
    """Tier 2: vacuity guard — the census this file's other tests iterate
    over names exactly the 3 concrete backends get_default_backend()
    dispatches to. If a future change removes a backend class from this
    enumeration (accidentally or via an import failure this file doesn't
    otherwise notice), this is the assertion that goes RED instead of every
    downstream test silently covering fewer backends and staying green
    regardless."""
    assert {cls.__name__ for cls in _ALL_BACKEND_CLASSES} == {
        "SeatbeltBackend", "LandlockBackend", "NoopBackend",
    }


def test_every_backend_implements_the_contract_and_conforms_to_the_protocol():
    """Tier 2: every concrete backend (a) satisfies the runtime-checkable
    SandboxBackend Protocol (which now includes
    session_artifact_outside_write_scope) and (b) returns a real bool, not
    None/NotImplemented, for a representative policy — a backend that
    forgot to override the Protocol method would fail (a).
    """
    policy = SandboxPolicy(write_paths=["/some/workspace"])
    for cls in _ALL_BACKEND_CLASSES:
        backend = cls()
        assert isinstance(backend, SandboxBackend), f"{cls.__name__} breaks Protocol conformance"
        result = backend.session_artifact_outside_write_scope(policy)
        assert isinstance(result, bool), (
            f"{cls.__name__}.session_artifact_outside_write_scope() returned "
            f"{result!r}, not a bool"
        )


def test_only_seatbelt_answers_non_vacuously_today():
    """Tier 2: names which backend's answer is a REAL derivation from
    *policy* (Seatbelt: materialises a file, so relocating write_paths onto
    the cache dir must flip its answer) versus VACUOUSLY True regardless of
    *policy* (Landlock, Noop: nothing is ever written to disk, so no
    write_paths value can make their answer False today). Distinguishing
    the two here is the point of this file — a vacuous True and a
    load-bearing True read identically as a bare boolean; this test is what
    tells them apart, so a future backend added to _ALL_BACKEND_CLASSES
    that silently returns a vacuous True while actually writing a file
    would be caught by test_seatbelt_cache_unsafe_when_write_scope_relocates_onto_the_cache_dir's
    OWN discipline being absent for it, not by this test — but this test at
    least documents which backends currently claim vacuity, so a reviewer
    adding a 4th backend has a checklist to extend."""
    from reyn.security.sandbox.backends.seatbelt import _seatbelt_cache_dir

    adversarial_policy = SandboxPolicy(write_paths=[str(_seatbelt_cache_dir())])
    assert SeatbeltBackend().session_artifact_outside_write_scope(adversarial_policy) is False

    # Landlock/Noop never touch disk, so even this adversarial policy (which
    # only means anything relative to SEATBELT's own cache dir) can't flip
    # them — their vacuous True is unconditional, which is exactly what
    # "vacuous" means here, not a gap in the check.
    assert LandlockBackend().session_artifact_outside_write_scope(adversarial_policy) is True
    assert NoopBackend().session_artifact_outside_write_scope(adversarial_policy) is True
