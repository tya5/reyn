"""Retention policy + truncation floor clamp (ADR-0038 Stage 1e, D5).

Two retention windows: a fine **WAL** window and a coarse **generation** window.
The user-facing knob is generation-count (`keep_generations` = "undo back N
checkpoints"); the WAL fine-window is *derived* (keep WAL back to the oldest
retained generation's base). `keep_duration` / `keep_bytes` are optional
secondary axes. **Default = live**: no deeper retention, the floor is the current
live floor (`min(watermark)+1`) — fully backward compatible.

The clamp consolidates the retention knobs into one policy and guarantees the
compaction floor never rises past what `reconstruct` needs for any *retained*
point — the concrete form of the Stage 1c-1 `maybe_truncate` caveat.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionPolicy:
    """How deep rewind/reconstruct can reach (ADR-0038 D5).

    ``keep_generations`` is the primary, user-facing axis ("undo back N
    checkpoints"). ``None`` on every field = **live** (current behaviour, no
    deeper retention). ``keep_duration_secs`` / ``keep_bytes`` are optional
    secondary axes; generation-count stays the clean primary.

    No ``from_config`` constructor, deliberately (#3987, 2026-08-11) — this is
    NOT an oversight, so don't add one back on reflex because a class with
    tunable knobs and no config reader looks unfinished:

    1. **What's missing**: a way to build a non-live policy from
       ``reyn.yaml`` (a config reader, i.e. the removed ``from_config``). Every
       non-default construction in the repo today is in ``tests/`` — production
       always passes ``retention_policy=None`` (live).
    2. **Why removed rather than left dead**: measured (#3987) that the WAL/
       generation GC already runs correctly at the live floor — throttled,
       every chat-turn boundary (``registry.py``'s ``truncate_wal_if_eligible``
       via ``maybe_truncate_for_size``) — with no unbounded-disk-growth risk.
       ``from_config`` sat unwired since ADR-0038 declared this "Implemented"
       with zero operator complaints about the missing config path.
    3. **When to reconsider**: a real request for a retention window deeper
       than live (an operator wanting to keep undo reach past the current
       compaction floor). Absent that, this class's only two real callers are
       its own clamp math (``compute_retention_floor``, used unconditionally)
       and ``AgentRegistry``'s constructor-arg escape hatch (currently
       test-only) — reintroducing ``from_config`` before a concrete need
       exists would just recreate the unwired state this note explains.
    """

    keep_generations: int | None = None
    keep_duration_secs: float | None = None
    keep_bytes: int | None = None

    @property
    def is_live(self) -> bool:
        """True when no deeper retention is configured (current behaviour)."""
        return (
            self.keep_generations is None
            and self.keep_duration_secs is None
            and self.keep_bytes is None
        )


def compute_retention_floor(
    policy: RetentionPolicy,
    *,
    live_floor: int,
    checkpoint_seqs: list[int],
) -> int:
    """Lowest seq that must remain so the retention window is reconstructable.

    ``floor = min(live_floor, oldest_retained_generation_base)``. **Live policy →
    ``live_floor``** (no clamp). With ``keep_generations = N``, the oldest
    retained checkpoint is the N-th most recent in ``checkpoint_seqs`` (its seq is
    the gen base WAL replay starts from); the floor is clamped *down* to it so the
    last N checkpoints stay reconstructable.

    **Rewind records and the floor**: a rewind record at seq ``R`` abandons
    ``(N, R)``; for it to affect a retained seq ``S >= floor`` we need
    ``N < S < R``, hence ``R > S >= floor`` — so any rewind record whose
    abandoned interval touches the retained window has ``R >= floor`` and is kept.
    This argument is correct for runtime-state reconstruction (WAL replay uses
    only seqs >= floor), but ``history.jsonl`` is append-only apart from the
    narrow #5759 stage 2 GC (which only ever removes content already folded
    into a compaction summary, never a rewind-record/branch-anchor line — see
    ``reyn.runtime.history_tail_reader.rewrite_history_dropping``):
    ``_active_branch_history`` tests the branch model
    (``build_active_predicate``) against ``wal_seq`` anchors from
    ``history.jsonl`` that may be below the floor (abandoned-branch turns).
    Dropping a rewind record below the floor therefore lets abandoned
    conversation turns reappear in the LLM context. Fix: callers of
    ``truncate_below`` pass ``always_keep_kinds=frozenset({REWIND_KIND})``
    (``snapshot_generations.REWIND_KIND``) so reset-records survive truncation
    regardless of the floor.

    Note that this protection is what makes the record survive; it is NOT what
    makes the branch model notice. The model's rewind-record index is
    incremental (#2939), so it re-reads the WAL only as it grows — a rewrite is
    caught by file identity and forces a rebuild. Truncation and the index are
    therefore independent: keeping a record here keeps its abandoned interval,
    and dropping one drops it, either way from the file that now exists.
    """
    if policy.is_live or policy.keep_generations is None:
        return live_floor
    gens = sorted(checkpoint_seqs)
    if not gens:
        return live_floor
    n = max(1, policy.keep_generations)
    oldest_retained = gens[-n] if len(gens) >= n else gens[0]
    return min(live_floor, oldest_retained)
