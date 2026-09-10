"""Tier 2: #5939 / #5851 stage (b) PR-1 — the shared cache-release step
(ladder step ②) and the ``process_memory_forensics`` diagnostic it emits.

Real caches throughout, driven through each owning module's own PUBLIC
production entry point (``estimate_tokens``, ``mint_ref``) wherever one
exists — never asserting on or resetting private state directly, per
this repo's own testing policy. The one exception (``status.py``'s
``_config_derived_fields``) has no fully public production entry point
reachable without a full ``AgentRegistry`` (see that test's own
docstring for why calling it directly is the pragmatic choice here,
matching the precedent ``test_thread_safety_startup_shared_state_3671.py``
already sets for this exact cache family).

The sandbox derivation cache is proven UNTOUCHED (not merely never
imported) — the exclusion this PR's own module docstring argues for is
falsified here, not just asserted in prose.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.data.workspace import artifact_ref
from reyn.interfaces.repl import status as status_module
from reyn.runtime.process_memory import ProcessMemoryGuard
from reyn.runtime.process_memory_release import (
    ProcessMemoryForensics,
    release_reconstructable_caches,
    run_cache_release_and_forensics,
)
from reyn.security.sandbox import _derivation_cache
from reyn.services.compaction import engine as compaction_engine
from tests._support.events import collect_events


def _fake_reader_sequence(values: "list[int | None]"):
    """A real, injectable callable — never a Mock — returning each of
    *values* in order, then repeating the last one (matches
    ``ProcessMemoryGuard.reader``'s own injection contract)."""
    it = iter(values)
    last = values[-1] if values else None

    def _read() -> "int | None":
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        return last

    return _read


# ── ① the release itself drops every in-scope cache ─────────────────────


def test_release_drops_the_token_cache_populated_through_the_real_public_path(
    monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: real ``estimate_tokens`` (the public production entry
    point) populates ``_token_cache`` — never touched directly. litellm's
    own ``token_counter`` is monkeypatched (a real third-party boundary
    swap, the SAME pattern ``test_thread_safety_startup_shared_state_
    3671.py`` already uses for this exact cache) so this test needs no
    network.

    Strip: comment out ``token_cache_clear()``'s own ``_token_cache.
    clear()`` call — this goes RED (``token_cache_size()`` stays > 0
    after release, performed during review)."""
    import litellm

    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    ensure_litellm_ready()
    monkeypatch.setattr(compaction_engine, "_token_counter_cooldown_until", 0.0)
    monkeypatch.setattr(litellm, "token_counter", lambda *, model, text: len(text))

    compaction_engine.estimate_tokens("release-pr1-a", "some-model")
    compaction_engine.estimate_tokens("release-pr1-b", "some-model")
    assert compaction_engine.token_cache_size() >= 2  # sanity: genuinely populated

    results = release_reconstructable_caches()

    assert compaction_engine.token_cache_size() == 0
    (matching,) = [r for r in results if r.name == "compaction_token_cache"]
    assert matching.entries_before >= 2
    assert matching.entries_after == 0


def test_release_drops_the_artifact_ref_table_cache_populated_through_mint_ref(
    tmp_path: Path,
):
    """Tier 2: real ``mint_ref`` (the public production entry point)
    populates ``_TABLE_CACHE`` via its own internal ``_load_table``
    call — never touched directly. The FIRST ``mint_ref`` on a brand new
    project only WRITES the table file (its own ``_load_table`` reads
    before the file exists, so nothing is cached that call); the SECOND
    call's own ``_load_table`` reads the now-real file and populates the
    cache — the realistic "同じ ref を再度引く" shape a real caller
    hits, not a synthetic direct write."""
    target = tmp_path / "some_file.txt"
    artifact_ref.mint_ref(tmp_path, "pr1-agent", target)
    artifact_ref.mint_ref(tmp_path, "pr1-agent", target)

    results = release_reconstructable_caches()

    (matching,) = [r for r in results if r.name == "artifact_ref_table_cache"]
    assert matching.entries_before >= 1
    assert matching.entries_after == 0
    # A fresh mint after release must re-derive from disk, not silently
    # find a stale hit — proves the drop was real, not just reported.
    second = artifact_ref.mint_ref(tmp_path, "pr1-agent", target)
    assert isinstance(second, str) and second


def test_release_drops_the_status_config_derived_cache():
    """Tier 2: ``status.py``'s own ``_config_derived_fields`` is the
    function that writes ``_CONFIG_DERIVED_CACHE`` — there is no fully
    public entry point reachable without a live ``AgentRegistry``
    (``_snapshot``/``_snapshot_for_session`` both need one attached).
    Calling it directly here matches the SAME precedent
    ``test_thread_safety_startup_shared_state_3671.py`` already sets for
    the sibling token cache (driving a module's own real population
    logic, not asserting on the dict's internals)."""

    class _FakeSession:
        """A real, weakref-able instance — `_CONFIG_DERIVED_CACHE` is a
        `WeakKeyDictionary` keyed by session identity; a bare `object()`
        has no `__weakref__` slot and cannot be a key here at all."""

    session = _FakeSession()
    status_module._config_derived_fields(session, None)

    results = release_reconstructable_caches()

    (matching,) = [r for r in results if r.name == "status_config_derived_cache"]
    assert matching.entries_before >= 1
    assert matching.entries_after == 0


# ── ② the sandbox derivation cache is explicitly, provably untouched ────


def test_release_never_touches_the_sandbox_derivation_cache():
    """Tier 2: this PR's own central judgment call — the sandbox
    derivation cache is EXCLUDED because its own refcount invariant
    means a present entry always has an outstanding checkout (see this
    module's own docstring). Falsified directly through the module's own
    public snapshot-style test hook (``_outstanding_checkout_count_for_
    tests``, added alongside this PR — the existing ``_cache_size_for_
    tests``/``_reset_cache_for_tests`` precedent), never the private
    ``_CACHE``/``_REFCOUNT`` dicts: populate a real entry via the real
    ``cached_derivation`` seam, run release, assert the checkout count
    is UNCHANGED — not merely "no exception raised"."""
    from reyn.security.sandbox.policy import SandboxPolicy

    policy = SandboxPolicy()
    computed = []

    def _compute():
        computed.append(1)
        return "derived-value"

    value = _derivation_cache.cached_derivation("pr1-backend", policy, _compute)
    assert value == "derived-value"
    before = _derivation_cache._outstanding_checkout_count_for_tests("pr1-backend", policy)
    assert before == 1  # sanity: genuinely checked out

    release_reconstructable_caches()

    after = _derivation_cache._outstanding_checkout_count_for_tests("pr1-backend", policy)
    assert after == before, (
        "release_reconstructable_caches must never touch the sandbox "
        "derivation cache -- an outstanding checkout would have its "
        "on-disk artifact evicted out from under a live consumer"
    )
    (only,) = computed  # compute() never re-ran as a side effect
    assert only == 1

    # cleanup, so this test does not itself leak into a sibling test
    _derivation_cache.release_derivation("pr1-backend", policy)


# ── ③ the forensics byproduct: 3-way output ─────────────────────────────


def test_run_cache_release_and_forensics_emits_the_audit_event():
    """Tier 2: the shared entry point emits ``process_memory_forensics``
    with the real before/after footprint and the real dropped-cache
    list — a real ``EventLog``, a real injected reader (never a Mock),
    never touching litellm/artifact_ref/status caches in THIS test (a
    clean, unpopulated state is a legitimate witness for the schema
    shape itself)."""
    events = EventLog()
    collected = collect_events(events)
    guard = ProcessMemoryGuard(reader=_fake_reader_sequence([1000, 400]))

    forensics = run_cache_release_and_forensics(guard, events, chain_id="chain-pr1")

    assert isinstance(forensics, ProcessMemoryForensics)
    assert forensics.footprint_before == 1000
    assert forensics.footprint_after == 400
    assert forensics.host is None
    assert forensics.top_history_rows is None

    (matching,) = [e for e in collected if e.type == "process_memory_forensics"]
    payload = matching.data
    assert payload["footprint_before"] == 1000
    assert payload["footprint_after"] == 400
    assert payload["chain_id"] == "chain-pr1"
    assert {d["name"] for d in payload["dropped"]} == {
        "compaction_token_cache",
        "artifact_ref_table_cache",
        "status_config_derived_cache",
    }


def test_run_cache_release_and_forensics_writes_to_the_real_stderr(
    monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: the stderr face writes to ``sys.__stderr__`` UNCONDITIONALLY
    — even when a ``reyn.log`` FileHandler is installed (proving this is
    NOT `stall_trace.default_log_stream`'s log-file-OR-stderr fallback
    shape, which this PR's own module docstring explicitly argues
    against). A real, in-memory stream stands in for the process's real
    stderr — never a Mock, a plain ``io.StringIO`` IS the collaborator
    ``sys.__stderr__`` names (a writable text stream).

    Strip: change ``sys.__stderr__`` to ``sys.stderr`` in
    ``_write_stderr_summary`` — this test still passes (both point at
    the same object here) but ``test_...bypasses_a_rebound_sys_stderr``
    below goes RED, which is the real witness for that specific claim."""
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", fake_stderr)
    events = EventLog()
    guard = ProcessMemoryGuard(reader=_fake_reader_sequence([500, 500]))

    run_cache_release_and_forensics(guard, events)

    output = fake_stderr.getvalue()
    assert "process_memory_forensics" in output
    assert output.count("\n") <= 5


def test_stderr_summary_bypasses_a_rebound_sys_stderr(monkeypatch: pytest.MonkeyPatch):
    """Tier 2: the exact witness the module docstring promises — a
    REBOUND ``sys.stderr`` (the reassignable name, e.g. a TUI/pytest
    capture manager owning it) must NOT receive this summary; only
    ``sys.__stderr__`` does. Two distinct real streams prove the
    function reads the right one."""
    real_stderr = io.StringIO()
    rebound_stderr = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", real_stderr)
    monkeypatch.setattr(sys, "stderr", rebound_stderr)
    events = EventLog()
    guard = ProcessMemoryGuard(reader=_fake_reader_sequence([500, 500]))

    run_cache_release_and_forensics(guard, events)

    assert "process_memory_forensics" in real_stderr.getvalue()
    assert rebound_stderr.getvalue() == ""


def test_run_cache_release_and_forensics_logs_to_reyn_log(caplog):
    """Tier 2: the ``reyn.log`` face — a WARNING-level log record carrying
    the same before/after numbers, independent of the audit-event and
    stderr faces (all 3 must fire even though this test only checks
    one)."""
    import logging

    events = EventLog()
    guard = ProcessMemoryGuard(reader=_fake_reader_sequence([777, 111]))

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.process_memory_release"):
        run_cache_release_and_forensics(guard, events)

    (matching,) = [r for r in caplog.records if r.name == "reyn.runtime.process_memory_release"]
    assert "777" in matching.getMessage()
    assert "111" in matching.getMessage()
