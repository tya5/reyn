"""Tier 2: ChatLifecycleForwarder bridges session-level events → outbox (issue #162).

When ``CompactionController`` finishes collapsing N early-session turns
into a rolling summary, the conv pane previously showed nothing — users
had no signal that early turns had been replaced. This session-scoped
forwarder pushes a ``[↑ N turns compacted]`` system marker into the
outbox so the conversation pane's ``_render_system_message`` path can
display it.

Pins:
  1. ``compaction_completed`` event → ``OutboxMessage(kind="system",
     text="[↑ N turns compacted]")``.
  2. Pluralisation: ``N=1`` → "1 turn", ``N>1`` → "N turns".
  3. Missing ``new_turn_count`` falls back to a generic marker (=
     forward-compat with event-shape variation).
  4. Unrelated event types are dropped (= no spurious outbox writes).
  5. #5633: ``compaction_started`` gets the same treatment (marker,
     pluralisation, fallback) — before this, ``completed``/``failed`` both
     had a marker and ``started`` had none. This supersedes a prior pin
     here (``test_compaction_started_is_not_surfaced``, deleted in the
     same change) that argued a started-but-never-closed marker would
     mislead a reader after a mid-run abort. That risk does not hold:
     every abort after ``compaction_started`` fires is already followed
     by ``compaction_failed`` (``compaction_controller.py``'s caller
     wraps the engine's ``compact()`` in try/except and always emits
     ``compaction_failed`` on raise — see ``engine.py``'s own comment on
     ``compact()``'s raise path) — the started marker is never left
     dangling.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.schemas.models import Event


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def test_compaction_started_emits_system_marker() -> None:
    """Tier 2: #5633 — compaction_started with new_turn_count writes
    [⟳ compacting N turns]. Before this handler, the event existed
    (engine.py's own compact() emits it, verified) but nothing in
    src/reyn/interfaces/ ever consumed it — completed/failed both had a
    marker, started had none."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_started",
        data={"new_turn_count": 8, "covers_through_seq": 42, "had_previous": False},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[⟳ compacting 8 turns]"


def test_compaction_started_singular_turn_uses_singular_label() -> None:
    """Tier 2: pluralisation — 1 turn is singular, mirrors completed's own."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_started", data={"new_turn_count": 1}))
    msgs = _drain(q)
    assert msgs[0].text == "[⟳ compacting 1 turn]"


def test_compaction_started_missing_count_uses_generic_marker() -> None:
    """Tier 2: forward-compat fallback when new_turn_count is absent —
    mirrors compaction_completed's own established shape."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_started", data={}))
    msgs = _drain(q)
    (only,) = msgs
    assert only.text == "[⟳ compacting history]"


def test_compaction_started_zero_count_uses_generic_marker() -> None:
    """Tier 2: a 0-count event is treated as missing — never a spurious
    "[⟳ compacting 0 turns]"."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_started", data={"new_turn_count": 0}))
    msgs = _drain(q)
    assert msgs[0].text == "[⟳ compacting history]"


def test_compaction_completed_emits_system_marker() -> None:
    """Tier 2: compaction_completed with new_turn_count writes [↑ N turns compacted]."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_completed",
        data={"new_turn_count": 8, "covers_through_seq": 42},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[↑ 8 turns compacted]"


def test_compaction_singular_turn_uses_singular_label() -> None:
    """Tier 2: pluralisation — 1 turn is singular."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_completed",
        data={"new_turn_count": 1, "covers_through_seq": 5},
    ))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ 1 turn compacted]"


def test_compaction_missing_count_uses_generic_marker() -> None:
    """Tier 2: forward-compat fallback when new_turn_count is absent.

    Future event-shape variations (= compaction subtypes that don't
    expose a turn count) still surface a marker rather than silently
    dropping the signal.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_completed", data={}))
    msgs = _drain(q)
    (only,) = msgs
    assert only.text == "[↑ history compacted]"


def test_compaction_zero_count_uses_generic_marker() -> None:
    """Tier 2: a 0-count event is treated as missing (= no useful marker).

    Prevents spurious "[↑ 0 turns compacted]" if a future emit site
    fires with new_turn_count=0.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_completed", data={"new_turn_count": 0}))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ history compacted]"


def test_compaction_completed_shows_the_calls_real_spend() -> None:
    """Tier 2: #4703 axis① — owner's own complaint. The
    ``[↑ N turns compacted]`` marker already existed (this file's own
    pre-#4703 tests above); what was missing is that it never showed the
    real money the compaction LLM call spent. prompt_tokens/
    completion_tokens/cost_usd (CompactionController's own #4703 addition
    to the event payload) now render as ``· ↑<tokens> ↓<tokens> · $<cost>``
    — same ↑/↓ glyph convention as gutter.py's ReynTurnUsageGutter, so a
    reader who already knows that convention reads this marker the same
    way, no new vocabulary."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_completed",
        data={
            "new_turn_count": 8, "covers_through_seq": 42,
            "prompt_tokens": 8200, "completion_tokens": 340, "cost_usd": 0.05,
        },
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[↑ 8 turns compacted · ↑8.2k ↓340 · $0.05]"


def test_compaction_completed_without_usage_fields_degrades_to_the_bare_marker() -> None:
    """Tier 2: absent usage fields (pre-#4703-shape events, or usage that
    genuinely could not be read off the response) render the ORIGINAL
    bare marker — never a fabricated ``$0.00``. Backward-compatible with
    every pre-#4703 test in this file."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_completed",
        data={"new_turn_count": 8, "covers_through_seq": 42},
    ))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ 8 turns compacted]"


def test_compaction_completed_shows_tokens_even_when_cost_is_unpriced() -> None:
    """Tier 2: cost_usd and prompt/completion_tokens are independent —
    an unpriced model (estimate_cost returns None, see budget.py's own
    unpriced-call discipline) still shows the token spend, just no $
    clause, rather than losing the whole usage clause."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_completed",
        data={
            "new_turn_count": 2, "covers_through_seq": 10,
            "prompt_tokens": 500, "completion_tokens": 50, "cost_usd": None,
        },
    ))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ 2 turns compacted · ↑500 ↓50]"


def test_unrelated_event_is_dropped() -> None:
    """Tier 2: events with no matching on_<type> handler don't write to outbox.

    Lifecycle forwarder shares the EventLog subscriber slot with the
    session's per-skill audit events — it must NOT echo phase / llm /
    skill events into the outbox (those are the per-skill forwarder's
    job).
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="phase_started", data={"phase": "resolve"}))
    fwd(Event(type="llm_called", data={"model": "gemini-2.5-flash-lite"}))
    fwd(Event(type="user_message_received", data={"text": "hi"}))
    assert _drain(q) == []


# ── budget_warn (wave-5 C5) ──────────────────────────────────────────


def test_budget_warn_emits_lifecycle_marker_with_pct() -> None:
    """Tier 2: budget_warn → ``[↑ budget warn: <dim> (N%)]`` lifecycle marker.

    Without this forwarding path, ``budget_warn`` events only showed up
    in the Events tab (= side panel, default-closed). A user with the
    panel closed had no in-conv signal that the daily cap was being
    approached.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="budget_warn",
        data={
            "dimension": "daily_tokens",
            "agent": "default",
            "chain_id": "abc123",
            "current": 80000,
            "hard": 100000,
        },
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[↑ budget warn: daily_tokens (80%)]"


def test_budget_warn_without_numeric_context_drops_pct() -> None:
    """Tier 2: missing / non-numeric current / hard → no ``(N%)`` annotation.

    The marker still surfaces — pct just degrades to "no annotation"
    rather than failing the whole emit.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="budget_warn",
        data={"dimension": "rate_limit"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.text == "[↑ budget warn: rate_limit]"


def test_budget_warn_missing_dimension_uses_generic_label() -> None:
    """Tier 2: absent ``dimension`` falls back to the generic ``budget`` label."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="budget_warn", data={}))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ budget warn: budget]"


def test_budget_warn_zero_hard_drops_pct_safely() -> None:
    """Tier 2: ``hard=0`` would divide by zero — pct degrades, no crash."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="budget_warn",
        data={"dimension": "daily_tokens", "current": 100, "hard": 0},
    ))
    msgs = _drain(q)
    assert msgs[0].text == "[↑ budget warn: daily_tokens]"


# ── model_cost_block (#1867 / FP-0052 S4) ────────────────────────────────────


def test_model_cost_block_declined_emits_marker() -> None:
    """Tier 2: model_cost_block with reason=declined → [✗ model switch declined:] marker.

    Without this handler, a user who says No to the high-cost confirm gets no
    feedback — the model chip stays unchanged but nothing explains why.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="model_cost_block",
        data={"model": "gpt-4o", "model_class": "gpt4o", "reason": "declined"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "model switch declined" in only.text
    assert "gpt-4o" in only.text


def test_model_cost_block_approved_emits_nothing() -> None:
    """Tier 2: model_cost_block with reason=approved → no outbox message.

    The status-bar chip updates to the new model; no extra marker is needed.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="model_cost_block",
        data={"model": "gpt-4o", "reason": "approved"},
    ))
    assert _drain(q) == []


def test_model_cost_block_non_interactive_emits_nothing() -> None:
    """Tier 2: model_cost_block with reason=non_interactive_fail_closed → no message.

    No human present; the operator discovers the block via the calling exception.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="model_cost_block",
        data={"model": "gpt-4o", "reason": "non_interactive_fail_closed"},
    ))
    assert _drain(q) == []


def test_model_cost_block_missing_reason_emits_nothing() -> None:
    """Tier 2: model_cost_block with no reason field → no message (forward-compat).

    Future event-shape additions must not accidentally trigger the declined marker.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="model_cost_block", data={"model": "gpt-4o"}))
    assert _drain(q) == []


# ── config hot-reload (#2073) ─────────────────────────────────────────────────


def test_config_reloaded_with_components_emits_marker() -> None:
    """Tier 2: config_reloaded with changed components → [↻ config reloaded: <names>] marker.

    Without this handler, a user who ran /reload gets no confirmation that the
    reload completed or which components changed — only the /reload "scheduled"
    message from earlier in the turn.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="config_reloaded",
        data={"components": ["hooks", "mcp"], "failed": [], "source": "operator"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "config reloaded" in only.text
    assert "hooks" in only.text
    assert "mcp" in only.text


def test_config_reloaded_with_no_changes_emits_nothing() -> None:
    """Tier 2: config_reloaded with empty components+failed → no outbox marker.

    A reload that touched nothing is already confirmed by the /reload reply;
    a redundant "nothing changed" marker would be noise.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="config_reloaded",
        data={"components": [], "failed": [], "source": "operator"},
    ))
    assert _drain(q) == []


def test_config_reloaded_with_failed_seams_includes_failure_note() -> None:
    """Tier 2: config_reloaded with failed seams → marker includes failure names.

    A seam failure is otherwise silently logged; surfacing it in the conv pane
    lets the user know the reload was partial.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="config_reloaded",
        data={"components": ["hooks"], "failed": ["cron"], "source": "operator"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert "config reloaded" in only.text
    assert "hooks" in only.text
    assert "cron" in only.text
    assert "failed" in only.text


def test_config_reload_rejected_emits_error_marker() -> None:
    """Tier 2: config_reload_rejected → [✗ config reload rejected: <reason>] marker.

    Without this event the user sees the /reload "scheduled" confirmation but
    then nothing when the validate-before-apply step rejects the malformed
    IN-set — the next turn silently runs under the old config.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="config_reload_rejected",
        data={"reason": "cron.jobs must be a list", "source": "operator"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "config reload rejected" in only.text
    assert "cron.jobs must be a list" in only.text


def test_config_reload_rejected_missing_reason_uses_fallback() -> None:
    """Tier 2: config_reload_rejected with no reason field → generic fallback text."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="config_reload_rejected", data={}))
    msgs = _drain(q)
    (only,) = msgs
    assert "config reload rejected" in only.text
    assert "malformed config" in only.text


# ── compaction_failed ────────────────────────────────────────────────────────


def test_compaction_failed_emits_error_marker() -> None:
    """Tier 2: compaction_failed → [✗ compaction failed: <reason>] marker.

    CompactionController emits this when the summarisation LLM call raises.
    Without a handler the user sees the compaction spinner clear but gets no
    feedback that context pressure is still unrelieved.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="compaction_failed",
        data={"error": "LLM returned empty"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "compaction failed" in only.text
    assert "LLM returned empty" in only.text


def test_compaction_failed_missing_error_uses_fallback() -> None:
    """Tier 2: compaction_failed with no error field → generic fallback text."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_failed", data={}))
    msgs = _drain(q)
    (only,) = msgs
    assert "compaction failed" in only.text
    assert "unknown error" in only.text


# ── #5588: started/completed/failed carry the episode-marker absorption tag ──


def test_compaction_started_carries_the_episode_marker_meta() -> None:
    """Tier 2: #5588 — app.py's own single shrink-flow-episode entry absorbs
    this marker (TUI-local) via ``meta["compaction_episode_marker"]``; the
    frame itself is UNCHANGED for a surface with no absorption mechanism
    (e.g. AG-UI)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_started", data={"new_turn_count": 3}))
    (only,) = _drain(q)
    assert only.meta.get("compaction_episode_marker") is True
    assert only.text == "[⟳ compacting 3 turns]", "the marker text itself is unchanged"


def test_compaction_completed_carries_the_episode_marker_meta() -> None:
    """Tier 2: same tag on the success marker — architect's own "existing
    [↑ N turns compacted] text unchanged" ruling means only meta is added,
    never the text."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_completed", data={"new_turn_count": 5}))
    (only,) = _drain(q)
    assert only.meta.get("compaction_episode_marker") is True
    assert only.text == "[↑ 5 turns compacted]"


def test_compaction_failed_carries_the_episode_marker_meta() -> None:
    """Tier 2: same tag on the (per-compact()-call) failure marker — a
    single failed attempt inside an active episode may still be recovered
    by #5719's own shrink-retry ladder, so it absorbs into the episode
    entry too rather than scattering its own line."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="compaction_failed", data={"error": "boom"}))
    (only,) = _drain(q)
    assert only.meta.get("compaction_episode_marker") is True


# ── #5588: router_context_overflow_unrecovered (the TRUE end-of-episode failure) ──


def test_router_context_overflow_unrecovered_names_mid_floor() -> None:
    """Tier 2: #5588 architect ruling — the true end-of-ladder failure is
    named via the RetryLoopTerminal member itself, never a parse of
    error's own repr() text."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="router_context_overflow_unrecovered",
        data={"error": "UnrecoveredError(...)", "terminal": "mid_floor"},
    ))
    (only,) = _drain(q)
    assert only.kind == "system"
    assert "1つのやり取りが単独で大きすぎます" in only.text
    assert "shrink flow failed" in only.text


def test_router_context_overflow_unrecovered_names_room_floor() -> None:
    """Tier 2: the OTHER RetryLoopTerminal member gets its own distinct
    text — the two are never collapsed into one."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="router_context_overflow_unrecovered",
        data={"error": "ContextOverflowError(...)", "terminal": "room_floor"},
    ))
    (only,) = _drain(q)
    assert "最新のメッセージだけで窓に入りません" in only.text


def test_router_context_overflow_unrecovered_without_terminal_degrades_gracefully() -> None:
    """Tier 2: a plain ContextOverflowError (no ladder-terminal distinction
    at all — never fabricated) still emits a marker, generic rather than
    naming an impossibility it was never told."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="router_context_overflow_unrecovered",
        data={"error": "ContextOverflowError(...)"},
    ))
    (only,) = _drain(q)
    assert only.text == "[✗ shrink flow failed]"
    # Never absorbed — this is the ONE terminal signal, always its own line.
    assert only.meta.get("compaction_episode_marker") is None


# ── limit_denied (router cap) ────────────────────────────────────────────────


def test_limit_denied_emits_cap_marker_with_counts() -> None:
    """Tier 2: limit_denied with count+cap → [✗ router cap hit: N ops (limit L)].

    session.py emits this when the loop's op-count exceeds the operator-configured
    router cap. Without a handler the user only sees LLM wrap-up text — no inline
    marker signals that the cap is WHY the turn ended early.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="limit_denied",
        data={"kind": "router_cap", "count": 42, "cap": 40, "chain_id": "abc"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "router cap hit" in only.text
    assert "42" in only.text
    assert "40" in only.text


def test_limit_denied_missing_counts_uses_generic_marker() -> None:
    """Tier 2: limit_denied without count/cap → generic [✗ router cap hit] marker.

    Forward-compat: future limit_denied sub-kinds that omit numeric fields still
    surface a marker rather than silently dropping the event.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="limit_denied", data={"kind": "router_cap"}))
    msgs = _drain(q)
    (only,) = msgs
    assert "router cap hit" in only.text
    assert "ops" not in only.text


def test_limit_denied_max_iterations_shows_iteration_limit_not_router_cap() -> None:
    """Tier 2: limit_denied kind='max_iterations' → 'iteration limit hit', not 'router cap hit'.

    router_loop.py emits limit_denied with kind='max_iterations' and limit=N when the
    iteration ceiling is reached. Previously on_limit_denied always said 'router cap hit'
    regardless of kind, conflating two distinct stop reasons — a user who hit the iteration
    limit saw the same marker as one who hit the op-count cap.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="limit_denied",
        data={"kind": "max_iterations", "limit": 20, "chain_id": "xyz"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "iteration limit" in only.text, "must say 'iteration limit', not 'router cap'"
    assert "20" in only.text
    assert "router cap" not in only.text, "must not conflate with router cap"


def test_limit_denied_max_iterations_without_limit_uses_generic() -> None:
    """Tier 2: limit_denied kind='max_iterations' without limit field → generic marker.

    Forward-compat: still surfaces a marker even if limit is missing.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="limit_denied", data={"kind": "max_iterations"}))
    msgs = _drain(q)
    (only,) = msgs
    assert "iteration limit" in only.text
    assert "router cap" not in only.text


# ── summary_resummarize_failed ───────────────────────────────────────────────


def test_summary_resummarize_failed_emits_error_marker() -> None:
    """Tier 2: summary_resummarize_failed → [✗ summary re-compress failed: <reason>].

    CompactionEngine emits this when the T2 re-summarise LLM call raises.
    Without a handler the user sees compaction_completed as if everything
    succeeded, but the stored summary may overshoot its body-budget — degrading
    future compaction quality silently.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(
        type="summary_resummarize_failed",
        data={"error": "context length exceeded"},
    ))
    msgs = _drain(q)
    (only,) = msgs
    assert only.kind == "system"
    assert "summary re-compress failed" in only.text
    assert "context length exceeded" in only.text


def test_summary_resummarize_failed_missing_error_uses_fallback() -> None:
    """Tier 2: summary_resummarize_failed with no error field → generic fallback text."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type="summary_resummarize_failed", data={}))
    msgs = _drain(q)
    (only,) = msgs
    assert "summary re-compress failed" in only.text
    assert "unknown error" in only.text
