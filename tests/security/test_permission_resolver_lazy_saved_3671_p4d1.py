"""Tier 2: #3671 P4 item D-1 — `PermissionResolver`'s persisted-approvals
load is deferred to first actual use, not paid on every `reyn chat`
startup construction.

Before this fix, `__init__` unconditionally called `self._load_saved()` —
disk I/O + YAML parse, cost scaling with the number of persisted approval
entries — even for a turn that never checks or persists any permission
(the common case for most turns). The `_saved` property (single owner: the
ONE place `self.__saved` is read or written) now loads on first access,
whichever of `saved_get()` / `_persist()` / the internal read sites reaches
it first — there is no second call site that could forget to trigger it,
unlike an optional constructor kwarg (rejected pattern, #3681).

#5153: the load site is now `_ensure_folded` (folds the append-only
`approvals.jsonl` ledger, migrating a legacy `approvals.yaml` snapshot
first if present — see `approval_ledger.py`) rather than the old
`_load_saved`'s direct YAML read, but the SAME deferred-until-first-access
contract this issue established still holds; `_seed_approvals` below
seeds the legacy snapshot format, which the migration step reads exactly
as before.

Witnessed via a call-through spy on `_ensure_folded` (real behavior still
runs; only the call COUNT and its TIMING relative to construction are
observed) — not a private-state peek at `self.__saved` itself.

#3671 P4 D-1 review (lead-coder): the lazy `_saved` property is a
check-then-set, the same shape as the 6 races #3674 fixed — and ONE
`PermissionResolver` is genuinely shared across multiple `Session`s (PR10),
so it IS reachable from concurrent coroutines. Proven safe WITHOUT a lock
(no `await` inside the check-then-set body — see the property's own
docstring), not just asserted safe — see
`test_saved_property_consistent_under_concurrent_coroutines` below.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.security.permissions.permissions import PermissionResolver


def _seed_approvals(tmp_path, **entries) -> None:
    approvals_dir = tmp_path / ".reyn"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"{k}: {str(v).lower()}" for k, v in entries.items())
    (approvals_dir / "approvals.yaml").write_text(lines + "\n", encoding="utf-8")


def test_construction_does_not_read_approvals_yaml(tmp_path, monkeypatch):
    """Tier 2: #3671 P4 item D-1 core witness — `PermissionResolver(...)`
    itself never calls `_ensure_folded()`. A real (legacy-format)
    approvals.yaml is seeded so a call WOULD find real data if it fired."""
    _seed_approvals(tmp_path, **{"safe.key": True})
    calls = {"n": 0}
    orig = PermissionResolver._ensure_folded

    def _spy(self):
        calls["n"] += 1
        return orig(self)

    monkeypatch.setattr(PermissionResolver, "_ensure_folded", _spy)

    PermissionResolver({}, project_root=tmp_path)

    assert calls["n"] == 0, (
        f"_ensure_folded() was called {calls['n']} time(s) during construction — "
        "expected 0 (deferred to first access)"
    )


def test_first_saved_get_triggers_exactly_one_load(tmp_path, monkeypatch):
    """Tier 2: the FIRST read through any accessor triggers the load, and
    only once — a second call must not re-read the file."""
    _seed_approvals(tmp_path, **{"safe.key": True})
    calls = {"n": 0}
    orig = PermissionResolver._ensure_folded

    def _spy(self):
        calls["n"] += 1
        return orig(self)

    monkeypatch.setattr(PermissionResolver, "_ensure_folded", _spy)

    resolver = PermissionResolver({}, project_root=tmp_path)
    assert calls["n"] == 0

    assert resolver.saved_get("safe.key") is True
    assert calls["n"] == 1

    assert resolver.saved_get("safe.key") is True
    assert calls["n"] == 1, "a second read must reuse the cached dict, not re-load"


def test_persist_also_triggers_lazy_load_exactly_once(tmp_path, monkeypatch):
    """Tier 2: `_persist()` (the WRITE path, `self._saved[key] = approved`)
    is a DIFFERENT internal call site than `saved_get()` — confirms the
    property is the single owner for BOTH read and write sites, not just
    the one exercised above."""
    _seed_approvals(tmp_path, **{"other.key": False})
    calls = {"n": 0}
    orig = PermissionResolver._ensure_folded

    def _spy(self):
        calls["n"] += 1
        return orig(self)

    monkeypatch.setattr(PermissionResolver, "_ensure_folded", _spy)

    resolver = PermissionResolver({}, project_root=tmp_path)
    assert calls["n"] == 0

    resolver._persist("new.key", True)
    assert calls["n"] == 1
    # the persisted entry AND the pre-existing on-disk entry are both
    # visible — proves the load actually merged in, not just started
    # from an empty dict.
    assert resolver.saved_get("new.key") is True
    assert resolver.saved_get("other.key") is False
    assert calls["n"] == 1, "saved_get after _persist must not re-load"


def test_saved_get_value_correctness_unchanged(tmp_path):
    """Tier 2: no behavior change — real end-to-end value round-trip through
    the lazy path, no spies, matching what a caller actually observes."""
    _seed_approvals(tmp_path, **{"granted.key": True, "denied.key": False})
    resolver = PermissionResolver({}, project_root=tmp_path)

    assert resolver.saved_get("granted.key") is True
    assert resolver.saved_get("denied.key") is False
    assert resolver.saved_get("never.mentioned.key") is None


@pytest.mark.asyncio
async def test_saved_property_consistent_under_concurrent_coroutines(tmp_path):
    """Tier 2: #3671 P4 D-1 review (lead-coder) — `_saved` is a check-then-set
    (`self.__saved is None` → assign), the same SHAPE as the 6 races #3674
    fixed, and ONE `PermissionResolver` is genuinely shared across multiple
    `Session`s (PR10) — so this IS reachable from more than one concurrently
    -running coroutine. Correctness-under-concurrency check (like
    `_get_retryable_litellm_exceptions` in #3674): every concurrent accessor
    must observe the SAME dict object (not two independently-built dicts,
    which would mean a `_persist()` mutation on one could be invisible to a
    reader holding the other) — safe because `_ensure_folded()` contains no
    `await`, so the whole check-then-set body runs with no yield point a
    sibling coroutine could interleave into (asyncio's single-threaded
    cooperative scheduling — see the property's own docstring for the
    non-thread-safety caveat)."""
    _seed_approvals(tmp_path, **{"safe.key": True})
    resolver = PermissionResolver({}, project_root=tmp_path)

    async def _access():
        # yield once before touching _saved, so all 32 coroutines are
        # actually racing to be first, not serialized by construction order.
        await asyncio.sleep(0)
        return resolver._saved

    results = await asyncio.gather(*(_access() for _ in range(32)))

    first = results[0]
    assert all(r is first for r in results), (
        "every concurrent accessor must observe the SAME dict object — a "
        "torn check-then-set would let two coroutines each build and "
        "install their OWN dict, silently dropping whichever lost"
    )
