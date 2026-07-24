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
  B. anchor tampering — ledger-side (same-size-or-larger unrelated content
     with a DIFFERENT leading-line identity, "invalid" status, #3201: no
     floor) and checkpoint-side (its own content_sha256 tampered, no floor,
     full re-scan) — each handled differently from the SAME-identity
     truncation case (governing rule below), which still floors.
  C. partial/corrupt checkpoint write -> full re-scan fallback.
  D. checkpoint deleted entirely -> full re-scan fallback (still reconstructs).
Plus a parity check (checkpoint-fast-path vs checkpoint-absent-full-scan
agree on totals for the same ledger).

Co-vet firm (governing rule for WHICH statuses floor), precision-refined by
#3201 (ledger IDENTITY discriminates "truncated" from "invalid", not file
SIZE): only an EXPLICIT operator action (archiving/deleting BOTH the ledger
and the checkpoint together) may LOWER a per-agent cap counter. The floor
is the DEFAULT for every non-"valid" status — "truncated" (same ledger
identity, anchor stale), "missing" (ledger absent/empty, no identity
derivable), and "identity_absent" (checkpoint predates #3201, or the
current ledger's identity is unreadable) all floor. The ONE exception is
"invalid": the ledger's leading-line identity is AFFIRMATIVELY computable
on BOTH sides and DIFFERS — a genuinely different ledger, whose past totals
are unrelated to this checkpoint's, so no floor applies. Reaching "invalid"
always requires positive proof, never merely the absence of proof — an
attacker who truncates the SAME ledger (leading line intact) still lands in
"truncated", not "invalid", however they pad the file's size. Additional
witnesses below cover: the floor firing is never silent (surfaced through
`/budget`'s actual rendered output, not just the checkpoint file), the
explicit-operator-action reset path genuinely does lower the counters (the
"can be lowered explicitly" side of the invariant — without this witness,
an implementation that could NEVER be lowered at all would also pass every
other test here), and a forged ``ledger_identity_sha256`` cannot spoof
either direction of the floor decision (caught by content_sha256).
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import (
    BudgetCheckpoint,
    BudgetLedger,
    BudgetTracker,
    CostConfig,
    CostLimitConfig,
    _default_checkpoint_path,
    format_budget_full,
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


def test_arm_B_ledger_replaced_different_identity_gets_no_floor(tmp_path):
    """Tier 2a: arm B (ledger-side, "invalid" status) — #3201. The ledger
    file is wholesale REPLACED with unrelated content that is the SAME SIZE
    OR LARGER than the stale checkpoint's anchor offset (defeats a
    size-only check) AND whose LEADING line differs from the checkpoint's
    stored ``ledger_identity_sha256``. This is now discriminated by
    IDENTITY, not size: the boundary-line content hash correctly rejects
    the fast path (not ``"valid"``), and the leading-line hash
    AFFIRMATIVELY proves this is a genuinely DIFFERENT ledger — so, unlike
    the pre-#3201 behavior, alpha's stale total does NOT floor here. A
    different ledger's past totals are simply not this checkpoint's
    business (see ``verify_anchor``'s docstring).

    FALSIFICATION (verified out-of-band with the hash check short-circuited
    to always-pass): the FAST tail-only path would incorrectly activate and
    mis-parse the new ledger's bytes from the stale anchor offset, corrupting
    ``beta``'s total (rather than just misclassifying the floor question) —
    the anchor hash check remains required for correctness of the fast
    path, independent of the floor question.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=4, tokens_each=10)  # alpha=40

    checkpoint_path = _default_checkpoint_path(ledger_path)
    old_offset = json.loads(checkpoint_path.read_text(encoding="utf-8"))["anchor"]["byte_offset"]

    # Wholesale replacement: unrelated agent, padded so the new file is AT
    # LEAST as large as the stale anchor's byte offset (defeats a
    # size-only check) while its actual bytes — INCLUDING its leading
    # line — differ from what the checkpoint's anchor/identity pin.
    ts = BudgetLedger._now_iso()
    pad = "p" * (old_offset + 16)
    rec1 = {"ts": ts, "agent": "beta", "model": "gpt-4", "tokens": 15, "cost_usd": 0.001, "pad": pad}
    rec2 = {"ts": ts, "agent": "beta", "model": "gpt-4", "tokens": 15, "cost_usd": 0.001}
    new_content = json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n"
    ledger_path.write_text(new_content, encoding="utf-8")
    assert ledger_path.stat().st_size >= old_offset, "replacement must be >= the stale anchor offset"

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    snap = bt2.snapshot()
    assert snap["agent_tokens"].get("alpha", 0) == 0, (
        "#3201: a ledger AFFIRMATIVELY proven different by leading-line "
        "identity must NOT floor alpha's stale total — a different "
        "ledger's history is unrelated to this checkpoint's"
    )
    assert snap["agent_tokens"].get("beta", 0) == 30, (
        "the new ledger's own records must still be counted correctly"
    )
    assert snap["budget_floor_applied"] is False, (
        "the actual cap-relevant total must reflect NO floor -- this is the "
        "witness the issue mandates: check the ACTUAL cap behavior, not "
        "just that an identity field got populated"
    )
    assert snap["budget_floor_reason"] is None


def test_same_identity_truncation_still_floors_even_at_old_anchor_size(tmp_path):
    """Tier 2a: #3201 positive witness for the precision fix itself. The
    ledger is truncated (real content lost) but then PADDED back up to at
    least the size of the stale anchor offset — the exact shape that a
    file-SIZE-based discriminator (the pre-#3201 implementation) would have
    misclassified as "invalid"/replaced. Because the padding preserves the
    ORIGINAL leading line byte-for-byte, ledger IDENTITY still matches the
    checkpoint's ``ledger_identity_sha256`` -- this must be classified
    ``"truncated"`` (same ledger, corrupted) and the floor must actually
    fire, holding alpha's cap-relevant total at its pre-truncation value
    despite the surviving ledger literally re-counting to less.

    This is the ACTUAL-cap-behavior witness for the "identity, not size"
    half of #3201: without identity, this same-size-or-larger shape would
    wrongly land in "invalid" and DROP the floor -- an under-count / silent
    cap lapse for an attacker who truncates-then-pads to dodge a
    size-only check.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=4, tokens_each=10)  # alpha=40

    checkpoint_path = _default_checkpoint_path(ledger_path)
    old_offset = json.loads(checkpoint_path.read_text(encoding="utf-8"))["anchor"]["byte_offset"]

    original_first_line = ledger_path.read_bytes().split(b"\n", 1)[0] + b"\n"

    # Truncate to keep ONLY the original leading line (real records lost,
    # under-count if trusted verbatim), then pad the tail with a harmless
    # comment-shaped JSON line so the file is >= the stale anchor's size --
    # defeating a pure size check while the leading line is untouched.
    pad_len = max(0, (old_offset + 32) - len(original_first_line))
    padding_line = json.dumps({"agent": "beta", "tokens": 0, "cost_usd": 0.0, "pad": "p" * pad_len})
    ledger_path.write_bytes(original_first_line + (padding_line + "\n").encode("utf-8"))
    assert ledger_path.stat().st_size >= old_offset, "padded ledger must be >= the stale anchor offset"

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    snap = bt2.snapshot()
    assert snap["agent_tokens"].get("alpha", 0) == 40, (
        "#3201: same-identity (leading line intact) truncation must still "
        "floor alpha's total even when padded to the old anchor's size or "
        "larger -- this is exactly the case file-size alone gets wrong"
    )
    assert snap["budget_floor_applied"] is True
    assert snap["budget_floor_reason"] == "truncated"


def test_checkpoint_predating_identity_feature_still_floors(tmp_path):
    """Tier 2a: #3201 backward-compat / negative-proof witness. A checkpoint
    written BEFORE this feature existed (``ledger_identity_sha256`` field
    entirely absent) must still floor when its anchor no longer verifies --
    absence of identity can never be treated as AFFIRMATIVE proof of a
    different ledger. Constructs such a checkpoint directly (an old-format
    payload with no identity key at all) to prove ``from_dict`` accepts it
    (content_sha256 computed without the field must still validate) and
    that ``hydrate`` routes it to "identity_absent" -> floors, never to
    "invalid" -> no floor.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    bt = _record_calls(ledger_path, state_path, n=4, tokens_each=10)  # alpha=40
    expected = bt.snapshot()["agent_tokens"]["alpha"]
    del bt

    # Build a pre-#3201-shaped checkpoint payload: same content as the
    # tracker's real checkpoint but with NO ``ledger_identity_sha256`` key
    # anywhere (simulating a file written by the old code).
    checkpoint = BudgetCheckpoint(
        agent_tokens={"alpha": expected},
        agent_cost_usd={"alpha": expected / 2},
        day_key=None, daily_tokens=0, daily_cost_usd=0.0,
        month_key=None, monthly_tokens=0, monthly_cost_usd=0.0,
        anchor_byte_offset=999_999_999,  # deliberately past the real ledger -> anchor invalid
        anchor_line_len=8,
        anchor_line_sha256="0" * 64,
        ledger_identity_sha256=None,
    )
    old_format_dict = checkpoint.to_dict()
    assert "ledger_identity_sha256" not in old_format_dict, (
        "sanity: the old-format payload must genuinely omit the identity "
        "key, not merely set it to null, to faithfully simulate a pre-#3201 "
        "checkpoint file"
    )
    checkpoint_path = _default_checkpoint_path(ledger_path)
    checkpoint_path.write_text(json.dumps(old_format_dict), encoding="utf-8")

    # Re-parse must succeed (old-format content_sha256 still validates) with
    # ledger_identity_sha256 == None.
    reparsed = BudgetCheckpoint.from_dict(json.loads(checkpoint_path.read_text(encoding="utf-8")))
    assert reparsed is not None, (
        "an old-format checkpoint (no identity field) must still parse -- "
        "identity absence is a legitimate historical state, not corruption"
    )
    assert reparsed.ledger_identity_sha256 is None

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    snap = bt2.snapshot()
    assert snap["agent_tokens"].get("alpha", 0) == expected, (
        "identity ABSENT (old-format checkpoint) must floor -- it can never "
        "AFFIRMATIVELY prove a different ledger, so it must not be routed "
        "to the no-floor branch"
    )
    assert snap["budget_floor_applied"] is True
    assert snap["budget_floor_reason"] == "identity_absent"


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


def test_tampered_ledger_identity_field_cannot_spoof_the_floor_decision(tmp_path):
    """Tier 2a: #3201 mandate — a forged ``ledger_identity_sha256`` must not
    be able to spoof the truncated-vs-different-ledger decision. Directly
    edits the on-disk checkpoint's ``ledger_identity_sha256`` field to an
    ATTACKER-CHOSEN value (leaving ``content_sha256`` as originally
    computed) — the ONLY way to make an identity-mismatch look like a
    match (or vice versa) without ``content_sha256`` catching it.

    This must fail ``BudgetCheckpoint.from_dict``'s content_sha256 check
    (the field is HASHED, per its docstring), so the checkpoint is
    rejected wholesale and ``hydrate`` falls back to a full ledger re-scan
    — the attacker cannot use a forged identity to either (a) fake a floor
    on a different ledger or (b) fake "different" to dodge a floor on the
    SAME ledger.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    _record_calls(ledger_path, state_path, n=4, tokens_each=10)  # 40 tokens

    checkpoint_path = _default_checkpoint_path(ledger_path)
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert "ledger_identity_sha256" in data, (
        "sanity: the real checkpoint must actually carry the new identity "
        "field for this tamper to be meaningful"
    )
    forged = "f" * 64
    assert data["ledger_identity_sha256"] != forged
    data["ledger_identity_sha256"] = forged  # tamper: forge the identity, content_sha256 untouched
    checkpoint_path.write_text(json.dumps(data), encoding="utf-8")

    reparsed = BudgetCheckpoint.from_dict(json.loads(checkpoint_path.read_text(encoding="utf-8")))
    assert reparsed is None, (
        "a forged ledger_identity_sha256 (content_sha256 left stale) must "
        "be caught by the content_sha256 check and rejected wholesale -- "
        "identity is tamper-evident, not a free-standing unverified field"
    )

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    assert bt2.snapshot()["agent_tokens"]["alpha"] == 40, (
        "with the tampered checkpoint rejected outright, hydrate falls back "
        "to a full re-scan of the (untouched) ledger and recovers the "
        "correct total regardless of the forged identity"
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


def test_floor_applied_is_visible_in_the_actual_budget_output(tmp_path):
    """Tier 2a: a floor firing must never be silent. Asserts against the
    ACTUAL operator-facing surface — ``format_budget_full()``, what `/budget`
    renders — not merely that the fact was recorded somewhere internal
    (checkpoint file / snapshot dict). Today's session repeatedly found
    "recorded in metadata but never reaches the consumer" bugs; this test
    is written specifically to not repeat that pattern.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    bt = _record_calls(ledger_path, state_path, n=3, tokens_each=10)  # alpha=30
    del bt

    # Truncate the ledger to 0 bytes (a "missing tail" truncation) so the
    # next hydrate must floor.
    with ledger_path.open("r+b") as f:
        f.truncate(0)

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    snap = bt2.snapshot()
    assert snap["agent_tokens"]["alpha"] == 30, "sanity: the floor must have fired"

    rendered = format_budget_full(snap, attached="alpha")
    assert "checkpoint" in rendered.lower(), (
        "the rendered /budget output must mention the checkpoint/floor "
        "explanation — the operator reads THIS text, not the snapshot dict "
        "or the checkpoint file directly"
    )
    # #3201: truncating all the way to 0 bytes destroys the ledger's leading
    # line too, so no identity is derivable at all -- this now classifies as
    # "missing" (ambiguous), not "truncated" (which #3201 reserves for a
    # ledger whose leading-line identity is still readable and matches).
    # Both floor; this assertion pins the SPECIFIC, now more precise reason.
    assert "missing" in rendered.lower(), (
        "the rendered output must include the SPECIFIC reason, not just a "
        "generic 'something happened'"
    )


def test_explicit_operator_reset_actually_lowers_the_counter(tmp_path):
    """Tier 2a: the "explicit operator action" side of the governing rule —
    archiving/deleting BOTH the ledger and the checkpoint together must
    actually reset the per-agent total to 0 (not merely "not increase").

    Without this witness, an implementation that floors unconditionally and
    NEVER lets the counter go down under any circumstance would also pass
    every other test in this file — this is the test that would catch that
    (a cap counter that can truly never be reset is its own kind of bug: an
    operator with a legitimate reason to reset spend has no way to do it).
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"
    bt = _record_calls(ledger_path, state_path, n=3, tokens_each=10)  # alpha=30
    del bt

    checkpoint_path = _default_checkpoint_path(ledger_path)
    assert ledger_path.is_file() and checkpoint_path.is_file()

    # The explicit, documented reset action: archive/delete BOTH files while
    # stopped (docs/reference/config/budget.md).
    ledger_path.unlink()
    checkpoint_path.unlink()

    bt2 = BudgetTracker(_cfg())
    bt2.hydrate(ledger_path)
    snap = bt2.snapshot()
    assert snap["agent_tokens"].get("alpha", 0) == 0, (
        "deleting BOTH files together is the explicit operator reset path — "
        "the per-agent total must actually drop to 0, not be floored forever"
    )
    assert snap["budget_floor_applied"] is False, (
        "no floor should be reported either — there was nothing left to "
        "floor with"
    )


if __name__ == "__main__":
    import sys

    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
