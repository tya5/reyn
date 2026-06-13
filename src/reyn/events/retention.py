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
    secondary axes a config may set; generation-count stays the clean primary.
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

    @classmethod
    def from_config(cls, cfg: dict | None) -> "RetentionPolicy":
        """Build from a reyn.yaml ``retention:`` block (or ``None`` → live)."""
        if not cfg:
            return cls()
        return cls(
            keep_generations=cfg.get("keep_generations"),
            keep_duration_secs=cfg.get("keep_duration_secs"),
            keep_bytes=cfg.get("keep_bytes"),
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

    **Rewind records are retained automatically** (no separate floor term): a
    rewind record at seq ``R`` abandons ``(N, R)``; for it to affect a retained
    seq ``S >= floor`` we need ``N < S < R``, hence ``R > S >= floor`` — so any
    rewind record whose abandoned interval touches the retained window has
    ``R >= floor`` and is kept (its ``target_n`` rides in the record's data).
    Rewind records below the floor only abandon intervals entirely below it, so
    dropping them cannot corrupt ``is_active`` for retained points. (This is why
    Q2's "oldest in-window rewind record" term is subsumed by the gen-base term;
    the reconstructability invariant test pins it.)
    """
    if policy.is_live or policy.keep_generations is None:
        return live_floor
    gens = sorted(checkpoint_seqs)
    if not gens:
        return live_floor
    n = max(1, policy.keep_generations)
    oldest_retained = gens[-n] if len(gens) >= n else gens[0]
    return min(live_floor, oldest_retained)
