"""Tier 2a: #2945 — ``BudgetTracker.hydrate`` must not re-parse the whole,
monotonically-growing ``budget_ledger.jsonl`` on every startup (a blocking
startup-path bug, not a "make it faster" optimization).

The fix compacts per-agent lifetime totals into a ``BudgetCheckpoint`` file
anchored to an exact ledger byte position; ``hydrate`` re-parses only the
tail after that anchor when it verifies. The core invariant under test: the
checkpoint must always be safe to delete/tamper/corrupt WITHOUT losing or
under-counting any durable spend — any ambiguity about its validity falls
back to a full ledger re-scan (P3, over-count-safe).

Covers the CLAUDE.md recovery-feature PR gate's 4-arm truncate-falsify
requirement:
  A. main: set X, truncate the ledger past X's own records, reconstruct, X survives.
  B. anchor tampering (ledger-side byte/hash mismatch, and separately the
     checkpoint's own counted-value content hash) -> full re-scan fallback.
  C. partial/corrupt checkpoint write -> full re-scan fallback.
  D. checkpoint deleted entirely -> full re-scan fallback (still reconstructs).
Plus a parity check (checkpoint-fast-path vs checkpoint-absent-full-scan
agree on totals for the same ledger).
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import (
    BudgetLedger,
    BudgetTracker,
    CostConfig,
    CostLimitConfig,
    _default_checkpoint_path,
)


def _cfg() -> CostConfig:
    return CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=100_000))


def _record_calls(ledger_path: Path, state_path: Path, n: int, tokens_each: int = 10) -> BudgetTracker:
    """Hydrate a tracker against *ledger_path* and record *n* LLM calls for
    agent "alpha", refreshing the checkpoint on every call (throttle=0)."""
    bt = BudgetTracker(_cfg())
    bt.hydrate(ledger_path)
    bt.set_state_path(state_path, throttle_secs=0.0)
    for _ in range(n):
        bt.record_llm(
            model="gpt-4", agent="alpha",
            usage=TokenUsage(tokens_each // 2, tokens_each // 2),
        )
    return bt


def test_checkpoint_created_after_hydrate(tmp_path):
    """Tier 2a: hydrate() leaves a checkpoint file behind (deletable-by-design
    cache artifact) so the NEXT hydrate is bounded, not just the ledger scan."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=3)

    checkpoint_path = _default_checkpoint_path(ledger_path)
    assert checkpoint_path.is_file(), "hydrate must leave a compacted checkpoint behind"
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert data["agent_tokens"]["alpha"] == 30
    assert "anchor" in data and "line_sha256" in data["anchor"]


def test_fast_path_ignores_ledger_corruption_before_the_anchor(tmp_path):
    """Tier 2a: proves the checkpoint's TAIL-ONLY fast path is genuinely
    exercised — not merely that hydrate() "produces the right answer via
    some fallback". Corrupts EVERY ledger byte strictly BEFORE the verified
    anchor (breaking JSON parsing for all of alpha's real records) while
    leaving the anchor's own boundary line untouched, so the anchor still
    verifies as "valid".

    If the fast tail-only path is live, those corrupted bytes are never
    re-read and the correct (checkpoint-seeded) total comes back unharmed.
    If the checkpoint/fast-path mechanism were removed entirely (hydrate
    always fully re-scanning from byte 0 regardless of any checkpoint), this
    corruption would silently swallow the corrupted records (broken JSON
    lines are skipped, per ``iter_records``) and the recovered total would
    come back wrong (fewer tokens than were really spent).

    This closes a gap the other arm tests cannot: arm B/C/D each assert
    "hydrate falls back to a correct full re-scan when the checkpoint is
    untrustworthy" — an invariant that would ALSO hold if the checkpoint
    feature did not exist at all (verified by disabling the fast path AND
    the truncation floor together: those tests stay green). This test is
    the one that actually requires the fast path to exist.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=5, tokens_each=10)  # 50 tokens

    checkpoint_path = _default_checkpoint_path(ledger_path)
    anchor = json.loads(checkpoint_path.read_text(encoding="utf-8"))["anchor"]
    offset, line_len = anchor["byte_offset"], anchor["line_len"]
    prefix_end = offset - line_len
    assert prefix_end > 0, "need a non-empty prefix strictly before the anchor to corrupt"

    with ledger_path.open("r+b") as f:
        f.seek(0)
        f.write(b"~" * prefix_end)  # garbage: every prior line fails JSON parsing

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    assert bt2.snapshot()["agent_tokens"]["alpha"] == 50, (
        "the fast path must never re-read bytes before the verified anchor; "
        "if the checkpoint mechanism were absent (always full re-scan from "
        "byte 0), this corrupted prefix would silently lose those tokens"
    )


def test_parity_checkpoint_fastpath_vs_full_scan(tmp_path):
    """Tier 2a: hydrating WITH a valid checkpoint (fast tail-only path) and
    hydrating with the checkpoint deleted (full re-scan path) against the
    SAME ledger produce IDENTICAL totals — the compaction must not change the
    reconstructed value, only the amount of ledger it re-reads."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=7, tokens_each=12)

    checkpoint_path = _default_checkpoint_path(ledger_path)
    assert checkpoint_path.is_file()

    # Fast path: checkpoint present and valid.
    bt_fast = BudgetTracker(_cfg())
    bt_fast.hydrate(ledger_path)
    snap_fast = bt_fast.snapshot()

    # Full-scan path: same ledger, checkpoint removed first (arm D shape).
    checkpoint_path.unlink()
    bt_full = BudgetTracker(_cfg())
    bt_full.hydrate(ledger_path)
    snap_full = bt_full.snapshot()

    assert snap_fast["agent_tokens"] == snap_full["agent_tokens"]
    assert snap_fast["agent_cost_usd"] == snap_full["agent_cost_usd"]
    assert snap_fast["daily_tokens"] == snap_full["daily_tokens"]
    assert snap_fast["monthly_tokens"] == snap_full["monthly_tokens"]


def test_arm_A_truncate_destroys_X_source_records_checkpoint_floor_survives(tmp_path):
    """Tier 2a: arm A (main, corrected per co-vet finding on the PR) — X's OWN
    contributing ledger records are destroyed by truncation (not merely
    records written AFTER X), and X still survives ONLY because the
    checkpoint's (content-hash-verified) total is used as a floor.

    The CLAUDE.md recovery-feature gate's definition is "truncate past X's
    SOURCE events -> X survives". An earlier version of this test truncated
    AFTER X's records (leaving them intact in the ledger) — that is
    trivially true even with NO checkpoint at all (a plain ledger re-scan
    would recover X unaided), so it never actually exercised this PR's fix.
    This version truncates the ledger down to a point BEFORE alpha's records
    exist at all, so a bare ledger re-scan of the survivor would see alpha=0
    — the only way X (30) can still come back is the checkpoint's own
    all-time total, kept as a floor because ``verify_anchor`` classifies a
    shortened ledger as "truncated" (never "invalid") and #2945's fix merges
    the checkpoint in via max() rather than discarding it (P3: ambiguity
    from truncation must resolve to over-count, never under-count).

    FALSIFICATION (verified out-of-band with the truncation floor-merge
    removed): this test goes RED at 0 == 30 — the truncated ledger has no
    alpha records left and nothing recovers X.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"

    # A different agent's activity comes FIRST so there is a non-empty
    # ledger prefix to truncate down to that contains NONE of alpha's
    # records.
    bt = BudgetTracker(_cfg())
    bt.hydrate(ledger_path)
    bt.set_state_path(state_path, throttle_secs=0.0)
    bt.record_llm(model="gpt-4", agent="prelude", usage=TokenUsage(3, 3))
    truncate_to = ledger_path.stat().st_size  # boundary BEFORE any alpha record

    # Now X = 30 for alpha, checkpointed on every call (throttle=0).
    for _ in range(3):
        bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(5, 5))
    x_expected = bt.snapshot()["agent_tokens"]["alpha"]
    assert x_expected == 30
    del bt

    # Destroy X's own source records: truncate back to BEFORE any of them.
    with ledger_path.open("r+b") as f:
        f.truncate(truncate_to)

    # Sanity: the surviving ledger genuinely has no alpha record left — a
    # bare re-scan (no checkpoint at all) could not recover X. Checked by
    # temporarily moving the checkpoint aside (NOT deleting it — the real
    # recovery step below still needs it) and restoring it afterward.
    checkpoint_path = _default_checkpoint_path(ledger_path)
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint_path.unlink()
    bt_bare = BudgetTracker(_cfg())
    bt_bare.hydrate(ledger_path)
    assert bt_bare.snapshot()["agent_tokens"].get("alpha", 0) == 0, (
        "sanity check: the truncated ledger must contain NO alpha record — "
        "otherwise this test doesn't actually require the checkpoint floor"
    )
    # bt_bare.hydrate() just wrote its OWN fresh (correct-for-the-truncated-
    # ledger, alpha-less) checkpoint over the path — restore the original
    # pre-truncation checkpoint so the real recovery step below has it.
    checkpoint_path.write_bytes(checkpoint_bytes)

    # Real recovery: WITH the (still-valid, content-hash-verified) checkpoint
    # from before truncation, X must survive via the floor merge.
    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    assert bt2.snapshot()["agent_tokens"]["alpha"] == x_expected, (
        "X's own source records were destroyed by truncation; only the "
        "checkpoint's floor-merged total can recover X — a bare re-scan "
        "would see 0"
    )


def test_arm_B_ledger_replaced_same_or_larger_size_falls_back(tmp_path):
    """Tier 2a: arm B (ledger-side) — the ledger file is wholesale REPLACED
    (e.g. an operator manually archives/rotates it and a fresh one begins)
    with unrelated content that is the SAME SIZE OR LARGER than the stale
    checkpoint's anchor offset. A byte-size-only check (``size >= offset``)
    would wrongly accept the stale checkpoint and misinterpret the new
    file's bytes as "the tail since last checkpoint" — leaking the old
    agent's stale total and/or mis-parsing the new agent's records. The
    boundary-line content hash must catch the mismatch and fall back to a
    full re-scan of the NEW ledger only.

    FALSIFICATION (verified out-of-band with the hash check short-circuited
    to always-pass): this test goes RED — the stale ``alpha`` total (40)
    leaks into the snapshot even though the ledger no longer contains any
    ``alpha`` record at all.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=4, tokens_each=10)  # alpha=40

    checkpoint_path = _default_checkpoint_path(ledger_path)
    old_offset = json.loads(checkpoint_path.read_text(encoding="utf-8"))["anchor"]["byte_offset"]

    # Wholesale replacement: unrelated agent, padded so the new file is AT
    # LEAST as large as the stale anchor's byte offset (defeats a
    # size-only check) while its actual bytes differ from what the
    # checkpoint's anchor pins.
    ts = BudgetLedger._now_iso()
    pad = "p" * (old_offset + 16)
    rec1 = {"ts": ts, "agent": "beta", "model": "gpt-4", "tokens": 15, "cost_usd": 0.001, "pad": pad}
    rec2 = {"ts": ts, "agent": "beta", "model": "gpt-4", "tokens": 15, "cost_usd": 0.001}
    new_content = json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n"
    ledger_path.write_text(new_content, encoding="utf-8")
    assert ledger_path.stat().st_size >= old_offset, "replacement must be >= the stale anchor offset"

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    snap = bt2.snapshot()["agent_tokens"]
    assert snap.get("alpha", 0) == 0, (
        "the stale checkpoint's 'alpha' total must not leak once the ledger "
        "it was anchored to has been replaced"
    )
    assert snap.get("beta", 0) == 30


def test_arm_B_tampered_checkpoint_content_falls_back_to_full_scan(tmp_path):
    """Tier 2a: arm B (content variant) — the ledger-side anchor only proves
    the checkpoint is pinned to an unmodified ledger position — it says
    nothing about whether the checkpoint's OWN counted values were hand-
    edited afterward. A direct edit to ``agent_tokens`` (anchor left intact)
    must be caught by ``content_sha256`` and fall back to a full re-scan."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=4, tokens_each=10)  # 40 tokens

    checkpoint_path = _default_checkpoint_path(ledger_path)
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    applied = 0
    if data["agent_tokens"]["alpha"] != 999_999:
        data["agent_tokens"]["alpha"] = 999_999  # tamper: inflate the stored total
        applied += 1
    assert applied == 1, "tamper must actually change the stored agent_tokens value"
    checkpoint_path.write_text(json.dumps(data), encoding="utf-8")

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    assert bt2.snapshot()["agent_tokens"]["alpha"] == 40, (
        "a checkpoint with tampered counted values (anchor otherwise intact) "
        "must not be trusted; full re-scan must still recover the correct total"
    )


def test_arm_C_partial_checkpoint_write_falls_back_to_full_scan(tmp_path):
    """Tier 2a: arm C — a checkpoint file truncated mid-write (simulating a
    crash during the checkpoint's own write) is unparseable JSON — hydrate
    must treat it as absent and fall back to a full re-scan, not raise or
    silently under-count."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=5, tokens_each=10)  # 50 tokens

    checkpoint_path = _default_checkpoint_path(ledger_path)
    raw = checkpoint_path.read_text(encoding="utf-8")
    applied = 0
    truncated = raw[: len(raw) // 2]
    try:
        json.loads(truncated)
    except json.JSONDecodeError:
        checkpoint_path.write_text(truncated, encoding="utf-8")  # partial write
        applied += 1
    assert applied == 1, (
        "the partial-write strip must actually produce unparseable JSON "
        "(behavioral check, not a length pin)"
    )

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)  # must not raise on unparseable JSON
    assert bt2.snapshot()["agent_tokens"]["alpha"] == 50


def test_arm_D_checkpoint_deleted_reconstructs_fully(tmp_path):
    """Tier 2a: arm D (strongest witness) — deleting the checkpoint entirely
    must be a no-op for correctness — the checkpoint carries no fact the
    ledger doesn't already durably hold. hydrate falls back to a full
    ledger re-scan and reconstructs the exact same total."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    bt = _record_calls(ledger_path, state_path, n=6, tokens_each=10)  # 60 tokens
    expected = bt.snapshot()["agent_tokens"]["alpha"]

    checkpoint_path = _default_checkpoint_path(ledger_path)
    assert checkpoint_path.is_file()
    checkpoint_path.unlink()  # "it is always safe to delete" — the design's core invariant
    assert not checkpoint_path.is_file()

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    assert bt2.snapshot()["agent_tokens"]["alpha"] == expected


def test_checkpoint_write_failure_does_not_block_startup(tmp_path):
    """Tier 2a: the checkpoint is DERIVED/cache
    (docs/reference/runtime/reyn-dir-layout.md) — a cache write failure must
    never make the application unable to start. Makes ``.reyn/cache/``
    read-only (simulating a permissions problem) BEFORE any checkpoint has
    ever been written, so ``hydrate`` cannot create the checkpoint file at
    all; it must still return the correct totals (via full re-scan) rather
    than raise.

    FALSIFICATION (verified out-of-band with the checkpoint-write try/except
    removed): this test goes RED with an unhandled ``PermissionError``
    propagating out of ``hydrate``.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    cache_dir = _default_checkpoint_path(ledger_path).parent

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.chmod(0o500)  # read + execute only, no write
    try:
        bt = BudgetTracker(_cfg())
        bt.hydrate(ledger_path)  # must not raise even though it can't write a checkpoint
        bt.set_state_path(state_path, throttle_secs=0.0)
        bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(5, 5))  # must not raise either
        assert bt.snapshot()["agent_tokens"]["alpha"] == 10

        bt2 = BudgetTracker(_cfg())
        bt2.hydrate(ledger_path)  # still no checkpoint was ever written; full re-scan must work
        assert bt2.snapshot()["agent_tokens"]["alpha"] == 10
    finally:
        cache_dir.chmod(0o700)  # restore so tmp_path cleanup can remove it


if __name__ == "__main__":
    import sys

    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
