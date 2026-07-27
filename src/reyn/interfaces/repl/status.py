"""Shared status-line helpers for the plain-renderer path.

This module is the cohesive home for the status-snapshot + working/waiting
indicator helpers that the PLAIN renderer path shares — the status values read
off the attached session (:func:`_snapshot`, consumed by the client read-model
and the AG-UI transport endpoint) and the "what is the turn blocked on" working
indicator (:class:`WaitingOn` + :data:`_WAITING_ON_BY_EVENT` + :func:`working_line`,
consumed by :mod:`reyn.interfaces.repl.renderer` and
:mod:`reyn.interfaces.transport.frames`).

These lived in ``reyn.interfaces.inline.app`` alongside the old prompt_toolkit
inline TTY input driver. That driver was retired once the Textual chat app
(:mod:`reyn.interfaces.inline.textual_chat`) fully replaced it; these symbols
outlived it because the plain path imports them live, so they were extracted
here byte-identically and their consumers rewired.

The color/spinner constants live in :mod:`reyn.interfaces.repl.renderer`; the
renderer imports the working/waiting symbols back deferred (function-local) to
avoid a module-load cycle, exactly as the old ``app`` module was imported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from reyn.interfaces.repl.renderer import (
    _CC_ACCENT,
    _CC_DIM,
    _CC_WARN,
    _SPINNER,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaitingOn:
    """What the current turn is blocked on, for the working indicator.

    Owner: "Working… もっと状態細分化できないの?" — the intent was knowing WHAT
    is blocking progress (a slow tool? the model? a question only the user can
    answer?), not just "is something happening". Table-driven (see
    ``_WAITING_ON_BY_EVENT`` below) rather than growing an if/elif chain in the
    renderer — a new axis is one new table entry, not a new branch.

    Every ``turn`` in reyn funnels through exactly THREE await chokepoints
    (verified by reading the actual dispatch code, not assumed):
    ``call_llm_tools`` (the LLM call — the default/idle state below),
    ``dispatch_tool`` (any tool execution — sub-agent delegation / MCP / shell
    / web all go through this same call, and #2344's owner design decision
    made chat-axis tool_calls run SERIALLY in declaration order, so a single
    ``detail`` slot is correct, never a set), and ``intervention_bus.request``
    (ANY human-in-the-loop pause — ask_user, permission confirm, cost-warn,
    safety-limit checkpoint, MCP install confirm, elicitation, hook confirm
    all fan into this one primitive). This dataclass models exactly those
    three plus "reached via a mid-turn compaction pass" as a fourth, all
    optional/extensible via the table.
    """

    label: str
    detail: "str | None" = None
    # True → render as a static amber line matching the above-input
    # intervention region's visual weight, NOT the "the AI is busy" shimmer —
    # the ball is in the user's court, not the model's, and the shimmer
    # animation was actively misleading here (owner's original complaint was
    # exactly this: the spinner kept ticking through an ask_user pause).
    is_user_wait: bool = False

    def text(self) -> str:
        return f"{self.label} {self.detail}" if self.detail else self.label


_WAITING_ON_THINKING = WaitingOn(label="Thinking")  # default: LLM response in flight
_WAITING_ON_FOR_USER = WaitingOn(label="Waiting for you", is_user_wait=True)

# Event → WaitingOn transition table. tool_called's data is
# {caller_kind, caller_id, tool, chain_id, args, args_hash} (dispatch/dispatcher.py
# via lifecycle_forwarder.py's on_tool_called/on_tool_returned/on_tool_failed
# — the SAME events the scrollback's "▸ tool(...)"/"⎿ ..." trace lines already
# come from). Extending to a new axis (e.g. compaction, once desired — the
# compaction_check(outcome="forced_sync", candidate_count>0) / completed /
# failed events already exist and could bracket a "Compacting" state) is one
# new entry here, not a new branch in the renderer.
_WAITING_ON_BY_EVENT: "dict[str, Callable[[dict], WaitingOn]]" = {
    "tool_called": lambda d: WaitingOn(label="Running", detail=d.get("tool")),
    "tool_returned": lambda d: _WAITING_ON_THINKING,
    "tool_failed": lambda d: _WAITING_ON_THINKING,
}


def working_line(
    thinking: bool,
    think_start: float,
    now: float,
    *,
    cancelling: bool = False,
    waiting_on: "WaitingOn | None" = None,
    waiting_on_since: "float | None" = None,
) -> list:
    """Pure: working-row fragments while a turn runs (empty list when idle).

    The spinner frame derives from `now` so it advances smoothly regardless of
    refresh jitter. The label carries a shimmer — a bright crest sweeping
    left→right across the text (a moving light) over a dim base, also
    clock-driven so it animates on each refresh.

    ``waiting_on`` (default ``None`` → the "Thinking" default, byte-identical
    to every pre-existing caller/test) names WHAT is currently blocking
    progress — see ``WaitingOn``'s docstring. ``waiting_on_since`` is when
    THAT state began (defaults to ``think_start``, i.e. turn start, if not
    given) — elapsed seconds shown is time-in-THIS-state, not turn-total, so
    "Running grep_files… 45s" answers "where exactly is it stuck", not just
    "the turn has been going for a while".

    When ``cancelling=True`` (ctrl-c was pressed mid-turn), the shimmer is replaced
    by a static "Cancelling…" indicator — the cancel is cooperative so the turn
    completes at the next tool boundary; the indicator reassures the user it's noted.
    Takes priority over ``waiting_on`` (a cancel-in-progress is the one thing
    that always wins, regardless of what the turn happened to be doing).
    """
    if not thinking:
        return []
    if cancelling:
        return [(f"fg:{_CC_WARN}", " ✗ Cancelling…")]
    wo = waiting_on if waiting_on is not None else _WAITING_ON_THINKING
    since = waiting_on_since if waiting_on_since is not None else think_start
    elapsed = max(0, int(now - since))
    label = f"{wo.text()}… {elapsed}s"
    if wo.is_user_wait:
        return [(f"fg:{_CC_WARN}", f" ◆ {label} · ctrl-c to interrupt")]
    frame = _SPINNER[int(now * 8) % len(_SPINNER)]
    frags = [(f"fg:{_CC_ACCENT}", f" {frame} ")]
    # The crest sweeps across the label then pauses in a short trailing gap before
    # restarting, so the light reads as a repeating left→right pass.
    head = int(now * 16) % (len(label) + 6)
    for i, ch in enumerate(label):
        offset = head - i
        if offset == 0:
            frags.append((f"fg:{_CC_ACCENT} bold", ch))   # bright crest
        elif offset == 1:
            frags.append((f"fg:{_CC_ACCENT}", ch))         # trailing glow
        else:
            frags.append((f"fg:{_CC_DIM}", ch))            # dim base
    frags.append((f"fg:{_CC_DIM}", " · ctrl-c to interrupt"))
    return frags


def _extract_cron_jobs(config) -> list[dict]:
    """Extract cron job dicts from config. Returns [] on any missing/malformed section."""
    cron = getattr(config, "cron", None)
    jobs = getattr(cron, "jobs", None) if cron is not None else None
    if not jobs:
        return []
    result = []
    for j in jobs:
        try:
            result.append({
                "name": j.name,
                "schedule": j.schedule,
                "enabled": bool(j.enabled),
            })
        except Exception:  # noqa: BLE001
            pass
    return result


def _extract_mcp_servers(config) -> list[dict]:
    """Extract mcp server name dicts from config. Returns [] on any missing/malformed section."""
    mcp = getattr(config, "mcp", None)
    if mcp is None:
        return []
    # mcp may be a dict with a "servers" sub-key, or a flat {name: cfg} dict.
    if isinstance(mcp, dict):
        servers = mcp.get("servers", None)
        if isinstance(servers, dict):
            source = servers
        else:
            # Flat dict — values should be dicts (server configs).
            source = {k: v for k, v in mcp.items() if isinstance(v, dict)}
    else:
        return []
    return [{"name": name} for name in source]


def _extract_skills(config) -> list[dict]:
    """Extract skill name dicts from config. Returns [] on any missing/malformed section.

    Mirrors ``_extract_mcp_servers`` — the config-only fallback shown when the
    session hasn't wired ``visibility_items`` for kind="skill" yet."""
    skills = getattr(config, "skills", None)
    if not isinstance(skills, dict):
        return []
    entries = skills.get("entries")
    if not isinstance(entries, dict):
        return []
    return [{"name": name} for name in entries]


def _extract_hooks(config) -> list[dict]:
    """Extract hook label dicts from config. Returns [] on any missing/malformed section."""
    hooks_raw = getattr(config, "hooks", None)
    if not hooks_raw:
        return []
    result = []
    _HOOK_EVENT_KEYS = frozenset({
        "event", "hook", "on", "trigger", "type", "name", "hook_point",
    })
    for i, entry in enumerate(hooks_raw):
        try:
            if isinstance(entry, dict):
                # Best-effort label: prefer a hook-point/event-ish key.
                label_key = next(
                    (k for k in _HOOK_EVENT_KEYS if k in entry), None
                )
                if label_key is None:
                    label_key = next(iter(entry), None)
                label = str(entry[label_key])[:40] if label_key else f"hook {i}"
            else:
                label = str(entry)[:40]
        except Exception:  # noqa: BLE001
            label = f"hook {i}"
        result.append({"label": label})
    return result


def _session_visibility_items(session) -> list[dict]:
    """Read visibility toggle state from the session (#2285 backend seam).

    Returns [] until e2e lands ``capability_visibility_state`` on the Session.
    Shape when available: [{kind, name, on}, ...] where on = not hidden_by_session.
    """
    getter = getattr(session, "capability_visibility_state", None)
    if getter is None:
        return []
    try:
        state = getter()
        authorized = state.get("authorized") or []
        hidden = {(h["kind"], h["name"]) for h in (state.get("hidden_by_session") or [])}
        return [
            {"kind": a["kind"], "name": a["name"], "on": (a["kind"], a["name"]) not in hidden}
            for a in authorized
        ]
    except Exception:  # noqa: BLE001
        logger.warning("capability_visibility_state() raised; visibility panel degraded to []", exc_info=True)
        return []


def _session_hook_items(session) -> list[dict]:
    """Read hook applicability state from the session (#2285 backend seam).

    Returns [] until e2e lands ``hook_state`` on the Session.
    Shape when available: [{name, scope, on}, ...].
    """
    getter = getattr(session, "hook_state", None)
    if getter is None:
        return []
    try:
        return [
            {"name": h["name"], "scope": h.get("scope", ""), "on": h.get("enabled", True)}
            for h in (getter() or [])
        ]
    except Exception:  # noqa: BLE001
        logger.warning("hook_state() raised; hooks panel degraded to []", exc_info=True)
        return []


def _session_pipelines(session) -> list[dict]:
    """Read registered pipeline names + descriptions from the session's
    PipelineRegistry — always constructed at Session.__init__ (never a "not
    wired yet" seam like visibility_items/hook_items); the try/except is
    defensive against an unexpected attribute-shape drift, not a feature gate.
    Shape: [{name, description}, ...]."""
    getter = getattr(session, "pipeline_registry", None)
    if getter is None:
        return []
    try:
        return [{"name": name, "description": desc} for name, desc in getter.entries()]
    except Exception:  # noqa: BLE001
        logger.warning("pipeline_registry.entries() raised; pipe panel degraded to []", exc_info=True)
        return []


def _snapshot(registry, config=None):
    """Read live status values off the attached session via sync accessors."""
    s = registry.attached_session()
    if s is None:
        return None
    u = s.total_usage
    # Cost breakdown (all via registry.agent_cost_usd — the single source of
    # truth for per-agent cost aggregation across all sids).
    cost_total = sum(registry.agent_cost_usd(name) for name in registry.loaded_names())
    cost_agent = (
        registry.agent_cost_usd(registry.attached_name)
        if registry.attached_name else s.total_cost_usd
    )
    agent_tokens = (
        registry.agent_tokens(registry.attached_name)
        if registry.attached_name else u.total_tokens
    )
    # Headline figure: the single most recent LLM call's prompt_tokens against
    # the model's REAL context window (get_max_input_tokens) — "how close to
    # the model's hard limit am I", matching the Claude Code-style % owners
    # expect. last_call_usage (NOT total_usage or a turn-summed figure) — a
    # turn can make several LLM calls via tool-loop iterations, each re-
    # sending nearly the same growing context, so summing them would wildly
    # overstate current occupancy. raw_context_window() is a cheap dict
    # lookup, safe to call every render frame (_snapshot runs on every frame).
    raw_window = s.raw_context_window()
    ctx_window = raw_window["window"]
    ctx_source = raw_window["source"]
    recent = s.last_call_usage
    ctx_used = recent.prompt_tokens
    # Supplementary figure: the compaction subsystem's OWN lightweight estimate
    # (history only, excl. system prompt/tools) against ITS internal trigger
    # threshold (already SP/head/tail-adjusted, not the model's real window) —
    # answers "when will auto-compaction fire", a different question from the
    # headline one above. Keeping both avoids collapsing two distinct
    # measurements into one ambiguous number (the original "used" bug).
    #
    # UNLIKE raw_context_window, Session.context_window_status() is NOT cheap
    # (json.dumps + token-estimate of the full router-view history) — do not
    # call it eagerly here, since _snapshot() runs on every render frame
    # regardless of whether the ctx dropdown is even open. Store the bound
    # method itself; _ctx_expansion's lines() calls it lazily, only while the
    # dropdown is actually open (and only once per redraw of THAT dropdown,
    # not the whole app).
    ctx_compaction_status_fn = s.context_window_status
    # #3339: per-turn token/cost aggregate (see Session.last_turn_usage) — a
    # cheap dict read off the durable tracker, safe on every render frame.
    turn_usage = s.last_turn_usage
    return {
        "model": s.model,
        "model_active_class": s.active_model_class(),
        "model_classes": list(s.known_model_classes()),
        "agent_names": list(registry.loaded_names()),
        "attached_name": registry.attached_name,
        "session_tree": registry.session_tree(),
        "usage": (u.prompt_tokens, u.completion_tokens, u.total_tokens),
        "cost_usd": s.total_cost_usd,
        "cost_total": cost_total,
        "cost_agent": cost_agent,
        "agent_tokens": agent_tokens,
        # Cost-panel breakdown (#cost-panel-breakdown): per-scope CostBreakdown
        # (Input/Output/Saved/Saved% rows) mirroring the 3 $ totals above.
        "cost_breakdown_session": s.total_cost_breakdown,
        "cost_breakdown_agent": (
            registry.agent_cost_breakdown(registry.attached_name)
            if registry.attached_name else s.total_cost_breakdown
        ),
        "cost_breakdown_project": registry.project_cost_breakdown(),
        # #3339: the CURRENT (or most recent) turn's real token/cost total —
        # every LLM call the turn made, summed under its chain_id by the
        # durable tracker. Distinct from BOTH `usage`/`cost_usd` (session
        # cumulative) and `ctx_used` (a single call). A call the OS could not
        # attribute to a turn is counted in no turn's total, so these figures
        # are never a difference of cumulative counters.
        # All three are None when THERE IS NO FIGURE (before the first turn,
        # or when the process-shared tracker's latest turn is a different one
        # — see Session.last_turn_usage). Deliberately not 0: a zero would be
        # indistinguishable from a real zero-cost turn and would render as
        # fact, whereas None is loud in both directions (drawn as "None", or
        # a TypeError on any arithmetic).
        "turn_chain_id": turn_usage["chain_id"],
        "turn_tokens": turn_usage["tokens"],
        "turn_cost_usd": turn_usage["cost_usd"],
        "ctx_used": ctx_used,
        "ctx_window": ctx_window,
        "ctx_source": ctx_source,
        "session_cached_tokens": u.cached_tokens,
        "ctx_recent_usage": (recent.prompt_tokens, recent.cached_tokens),
        "ctx_compaction_status_fn": ctx_compaction_status_fn,
        "cron_jobs": _extract_cron_jobs(config) if config is not None else [],
        "mcp_servers": _extract_mcp_servers(config) if config is not None else [],
        "hooks": _extract_hooks(config) if config is not None else [],
        "skills": _extract_skills(config) if config is not None else [],
        # #2285: session-scoped capability visibility + hook applicability toggles.
        # Populated once e2e lands the backend; graceful fallback to [] until then.
        "visibility_items": _session_visibility_items(s),
        "hook_items": _session_hook_items(s),
        # Always available (Session owns a PipelineRegistry from __init__) —
        # not a "not wired yet" seam like the two lines above.
        "pipelines": _session_pipelines(s),
        # #3300 P2a: server-authoritative sent-queue state — the undispatched
        # inbox queue + whether a turn is currently dispatched. Read straight
        # off the session's public accessors (Session.queued_user_messages /
        # Session.turn_active); this dict is the SAME snapshot both the local
        # (in-process, read live every render tick) and remote (agui, via
        # project_status -> STATE_SNAPSHOT/STATE_DELTA) clients derive their
        # queue view from, so local ≡ remote by construction.
        "queue": s.queued_user_messages(),
        "turn_active": s.turn_active,
        # The seq-gate token (#3300 P2a design-pass pin D) — a client merging
        # the granular `user_submitted`/`turn_started` queue deltas seeds its
        # last-applied-seq from this value on snapshot, so a stale delta
        # (seq <= this) already reflected here can never resurrect a
        # dispatched item regardless of arrival order.
        "queue_seq": s.queue_seq,
    }
