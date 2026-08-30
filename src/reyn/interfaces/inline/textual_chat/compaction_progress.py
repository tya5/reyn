"""Shrink-flow (compaction/overflow-recovery) progress display — pure render
layer (#5588).

Two readers, one display, three layered lines (architect design, #5588
issue thread — quoted verbatim in this module's own docstrings below):

- Line 1 answers the general user's ONLY real question: "should I wait, or
  is this stuck?" — always sayable, since the ladder's own termination is a
  PREDICATE (same input -> same result), never a probability.
- Line 2 answers "is it actually doing something right now" — distinguishes
  "waiting on a slow LLM response" from "genuinely stalled", so a slow
  provider never gets misread as a hang and triggers an unnecessary restart
  (the exact failure this display exists to prevent — architect: "健全な
  回復を再起動させます").
- Line 3 is the owner's analysis line: which rung of the ladder is active,
  and how much headroom each rung has left. Never collapsed into a single
  percentage — the rungs use different, individually-honest units (a
  monotone fraction, a raw value that can legitimately grow, a binary
  in/out, another monotone fraction) and folding them into one number would
  either lie (a value that increases reads as "regressed") or hide the
  figure that matters (architect: "段の単位が違い、②は増えもします").

#5588 skeleton-first note (lead-coder ruling, issue thread): the real
``rung``/``levers_left``/lap/call-count PRODUCER fields land in #5592
(e2e-coder), not landed in this tree as of this module's creation — see
that issue. Deriving them here from engine internals instead would create
a SECOND counting site for the same fact (architect, #5350's own family:
"2か所で数えるとズレます") — this module never does that. Every optional
field below defaults to ``None`` and the render functions degrade
gracefully (never fabricating a number), matching CLAUDE.md's own rule.

Only ``is_compacting`` (via ``Session.is_compacting`` / #5588) and
``terminal`` (via the ``router_context_overflow_unrecovered`` event's own
new field, also #5588) are wired to something real as of this module's
introduction — both landed in THIS PR, no producer dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.services.compaction.engine import RetryLoopTerminal


@dataclass(frozen=True)
class CompactionProgressSnapshot:
    """The full designed shape (#5588) — every field beyond ``is_compacting``
    is optional and ``None`` until its producer lands (see module docstring).
    A caller with only ``is_compacting=True`` still gets a correct, honest
    line 1; lines 2/3 render only the pieces they actually have data for.
    """

    is_compacting: bool = False

    #: Line 2 — which real LLM call this pass is currently awaiting a
    #: response from ("summary" | "main_call"), or ``None`` when nothing is
    #: currently in flight (a between-calls moment, not a stall — see
    #: :func:`compaction_progress_lines`'s own "no fabricated conflation"
    #: note). Elapsed seconds, DISPLAY ONLY (CLAUDE.md: never used for a
    #: machine timeout decision) — omitted entirely when ``None``, the same
    #: "never coerce an unknown clock to 0s" rule ``activity_row.py`` already
    #: follows.
    waiting_for: "str | None" = None
    waiting_elapsed_s: "float | None" = None

    #: Line 3 — rung① spill: consumed / total un-spilled candidates
    #: (``levers_left``, #5592 — the population NEVER shrinks by
    #: redefinition, per owner ruling; only consumption moves this).
    spill_done: "int | None" = None
    spill_total: "int | None" = None

    #: rung② slice: the CURRENT mid-turn compact-attempt length (can legally
    #: grow — binary search doubles on success — never rendered as a
    #: fraction/percent).
    slice_len: "int | None" = None

    #: rung③ refill: which of head/tail still has non-summary content left
    #: to move into the middle candidate pool (a 2-value in/out fact, not a
    #: count).
    head_available: "bool | None" = None
    tail_available: "bool | None" = None

    #: rung④ budget: halvings already taken / halvings until the room floor
    #: (a real fraction — the floor IS fixed, so this ratio is honest).
    budget_halvings_done: "int | None" = None
    budget_halvings_max: "int | None" = None

    #: Which rung is CURRENTLY active ("spill" | "slice" | "refill" |
    #: "budget") — drives the ``← 今 ①`` marker. #5592.
    active_rung: "str | None" = None

    #: Monotonically-increasing episode counter (#5588, architect's final
    #: addition) — the ONE figure that is never allowed to look like a
    #: regression even when rung① is refilled to a taller-looking fraction
    #: at the start of a new episode. #5592.
    lap: "int | None" = None

    #: Total LLM calls made across this recovery so far — MUST be shown
    #: alongside spill_done/spill_total, never alone (architect, #5588
    #: correction: "片方だけでは今回の事故がまた見えません" — candidates
    #: alone hides a disproportionate call count; calls alone hides how much
    #: further there is to go). #5592.
    call_count: "int | None" = None


_RUNG_LABELS = {"spill": "①", "slice": "②", "refill": "③", "budget": "④"}


def compaction_progress_lines(snap: CompactionProgressSnapshot) -> "list[str]":
    """Render the (up to) 3 chrome lines for *snap* — ``[]`` when nothing is
    running (the accept/deny pair from the issue's own acceptance criteria:
    this display is invisible whenever ``is_compacting`` is False, never a
    persistent/toggled row).

    Each line renders independently of whether the OTHER lines have data —
    a caller with only ``is_compacting=True`` still gets a correct line 1.
    """
    if not snap.is_compacting:
        return []

    lines = ["⟳ 文脈を縮めています（自動で終わります）"]

    if snap.waiting_for is not None:
        # #5588: "応答待ち" (waiting) vs "進捗なし" (no progress) are
        # DELIBERATELY different words — this function never says "no
        # progress" for anything; the only state it knows how to name
        # honestly, from a real signal, is "waiting for X" (an in-flight
        # call). A genuinely-stalled state (no call in flight AND no
        # forward motion) has no producer signal yet and is correctly
        # rendered as line 2 simply absent, never guessed at.
        target = {"summary": "要約", "main_call": "本文"}.get(snap.waiting_for, snap.waiting_for)
        elapsed = ""
        if snap.waiting_elapsed_s is not None:
            elapsed = f" {int(snap.waiting_elapsed_s)}秒"
        lines.append(f"  {target}の応答を待っています{elapsed}")

    rung_parts = []
    if snap.spill_done is not None and snap.spill_total is not None:
        call_suffix = "" if snap.call_count is None else f"  呼び出し {snap.call_count}"
        rung_parts.append(f"① 退避 {snap.spill_done}/{snap.spill_total}{call_suffix}")
    if snap.slice_len is not None:
        rung_parts.append(f"② 分割 {snap.slice_len}")
    if snap.head_available is not None or snap.tail_available is not None:
        avail = []
        if snap.head_available:
            avail.append("head")
        if snap.tail_available:
            avail.append("tail")
        rung_parts.append(f"③ 補充 {'/'.join(avail) if avail else '—'}")
    if snap.budget_halvings_done is not None and snap.budget_halvings_max is not None:
        rung_parts.append(f"④ 予算 {snap.budget_halvings_done}/{snap.budget_halvings_max}")

    if rung_parts:
        line3 = "  " + "  ".join(rung_parts)
        if snap.lap is not None:
            line3 += f"  · 周回 {snap.lap}"
        if snap.active_rung is not None and snap.active_rung in _RUNG_LABELS:
            line3 += f"     ← 今 {_RUNG_LABELS[snap.active_rung]}"
        lines.append(line3)

    return lines


#: architect's own exact wording (#5588 issue thread) — never derived from
#: RetryLoopTerminal's own member NAME/value string (that would be the same
#: "parse the reason text" pattern this design explicitly forbids elsewhere;
#: instead this is a closed dict keyed on the ENUM MEMBER itself).
def compaction_failure_text(terminal: "RetryLoopTerminal") -> str:
    """The end-of-recovery FAILURE line, naming WHICH impossibility fired —
    never a parse of ``UnrecoveredError``'s own ``reason``/``repr()`` text
    (architect ruling, #5588: "reason 文字列を解析しないこと"). Keyed on the
    ``RetryLoopTerminal`` enum member itself, imported lazily by the caller
    (this module takes no hard dependency on ``reyn.services.compaction``)."""
    from reyn.services.compaction.engine import RetryLoopTerminal as _T

    return {
        _T.MID_FLOOR: "1つのやり取りが単独で大きすぎます",
        _T.ROOM_FLOOR: "最新のメッセージだけで窓に入りません",
    }[terminal]
