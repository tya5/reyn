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


def _session_visibility_items(session) -> "list[dict] | None":
    """Read visibility state from the session (#2285 backend seam).

    Shape: ``[{kind, name, on, denied, denied_reason}, ...]``. ``on`` is the
    ``/visibility`` axis (not hidden_by_session); ``denied`` is the SEPARATE
    envelope/contextual axis. A denied row is not user-flippable, so it carries
    ``on=False, denied=True`` and a renderer must show it distinguishably from a
    plain ``off``.

    #3380 — ``denied_reason`` says WHICH narrowing, because the two differ in what
    the operator can do about them:

    - ``"envelope"`` (#3378 ``denied_by_envelope``) — a topology binding / delegate
      floor / per-session config / ⊆-parent cap. Durable for this session.
    - ``"turn_context"`` (#3380 ``denied_by_turn_context``) — the ephemeral
      ``_untrusted`` profile, live only while untrusted external content sits in the
      active context; it lifts itself when that entry compacts out.

    Both are re-read from the session on every snapshot (the #3338 per-frame pane
    rebuild), so neither is a latched "as of turn N" value.

    #3615 — a THIRD ``denied_reason``, ``"unknown"``: the read model's own
    ``envelope_unknown`` rows (no envelope source to test the capability against —
    see ``CapabilityVisibility.capability_visibility_state``'s docstring). Rendered
    the same non-flippable ``[--]`` way as ``"envelope"`` / ``"turn_context"`` — an
    operator cannot toggle a row whose authorization could not be determined any
    more than one the envelope actively denies — but with its own annotation, because
    the operator's next move differs from either: this is not "edit the profile" or
    "wait for context to clear", it is "the session was built without a way to check
    this; the report is not confirmed." Folding it into ``authorized`` (the pre-#3615
    behaviour) said the opposite of the truth.

    Returns **None** — not ``[]`` — when the seam is absent or raised (#3378
    requirement 4): the renderer must be able to tell "this session wires no
    visibility state" from "it wires state and nothing is narrowed", which an empty
    list conflates.
    """
    getter = getattr(session, "capability_visibility_state", None)
    if getter is None:
        return None
    try:
        state = getter()
        authorized = state.get("authorized") or []
        hidden = {(h["kind"], h["name"]) for h in (state.get("hidden_by_session") or [])}
        items = [
            {
                "kind": a["kind"],
                "name": a["name"],
                "on": (a["kind"], a["name"]) not in hidden,
                "denied": False,
                "denied_reason": None,
            }
            for a in authorized
        ]
        items += [
            {
                "kind": d["kind"], "name": d["name"], "on": False,
                "denied": True, "denied_reason": reason,
            }
            for key, reason in (
                ("denied_by_envelope", "envelope"),
                ("denied_by_turn_context", "turn_context"),
                ("unknown", "unknown"),
            )
            for d in (state.get(key) or [])
        ]
        return items
    except Exception:  # noqa: BLE001
        logger.warning("capability_visibility_state() raised; visibility panel degraded to unwired", exc_info=True)
        return None


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


def _session_mcp_subscriptions(session) -> list[dict]:
    """Read the MCP subscription read model from the session (#4686 backend
    seam). Shape: ``[{"server", "uris", "unhonored"}, ...]`` — see
    ``Session.mcp_subscription_state``'s own docstring for the field
    semantics. Mirrors ``_session_pipelines``'s defensiveness (a
    getattr + try/except around an accessor that's always constructed, not
    a "not wired yet" seam like ``visibility_items``/``hook_items``) — [] on
    any missing accessor or unexpected raise, never a crash of the whole
    status readout."""
    getter = getattr(session, "mcp_subscription_state", None)
    if getter is None:
        return []
    try:
        return list(getter() or [])
    except Exception:  # noqa: BLE001
        logger.warning("mcp_subscription_state() raised; mcp pane subscription rows degraded to []", exc_info=True)
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


def _reported_snapshot_keys() -> "dict[str, bool]":
    """LOCAL's own full ``ChatReadModelCapabilities`` projection — a thin
    wrapper around ``read_model.py``'s ``reported_snapshot_keys`` (#5009)
    that carries the LAZY import (this module is imported BY
    ``read_model.py``'s own top level, so the reverse reference must not
    be). Generalized (#5009 closing pass) from 4 near-identical
    single-field wrappers to the ONE generic projection — see
    ``reported_snapshot_keys``'s own docstring for why."""
    from reyn.interfaces.repl.read_model import (  # noqa: PLC0415
        LOCAL_CHAT_READ_CAPABILITIES,
        reported_snapshot_keys,
    )

    return reported_snapshot_keys(LOCAL_CHAT_READ_CAPABILITIES)


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
    # #3695: how many of those calls had no price. Projected NEXT TO the figure
    # it qualifies, from the same accessor family, so a surface cannot show the
    # cost while being unaware that it is incomplete — which is what left the
    # owner reading a frozen number all day as though it were the amount spent.
    unpriced_calls = (
        registry.agent_unpriced_calls(registry.attached_name)
        if registry.attached_name else 0
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
    # #3283 ④: the KEYED per-turn lookup, stored UNCALLED (same shape as
    # ``ctx_compaction_status_fn`` above, for a different reason). The three
    # ``turn_*`` scalars below answer "the most recent turn"; a per-ROW surface
    # (the TUI's right gutter) has to ask about an arbitrary turn's chain_id,
    # once per rendered row — a snapshot scalar cannot carry that, and building
    # a whole snapshot per row would be absurd. Handing over the bound method
    # lets the row surface ask directly, and keeps the never-fabricate contract
    # in ONE place (``BudgetTracker.turn_usage`` returns None, not 0).
    turn_usage_fn = s.turn_usage
    # #5050: the SAME head ``ClientTransport.pending_intervention_head()``
    # (in_process.py) already returns for the in-process path
    # (``s.interventions.head()``), projected here to a JSON-safe dict so it
    # can also ride STATE_SNAPSHOT/STATE_DELTA — the source
    # ``RemoteReadModel.intervention_head()`` reads instead of an
    # unconditional None (read_model.py's own #5050 fix: that method was
    # conflating "unsupported" with "nothing pending now", the #4996-family
    # lying-None pattern, independent of whether choices already reach a
    # remote client some OTHER way — they do, via a separate AG-UI
    # frontend-tool encoding; that path is untouched here). Shape mirrors
    # ``intervention_handler._iv_meta``'s established choices convention
    # (id/label/hotkey per entry). ``None`` when nothing is pending — never a
    # fabricated placeholder.
    _head = s.interventions.head()
    pending_intervention_head = (
        {
            "id": _head.id,
            "prompt": _head.prompt,
            "detail": _head.detail,
            "choices": [
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in _head.choices
            ],
        }
        if _head is not None else None
    )
    return {
        # #5009 / #5009 closing pass: every ``*_reported`` declaration is
        # projected in ONE call, from ONE source
        # (``LOCAL_CHAT_READ_CAPABILITIES``) — see ``reported_snapshot_
        # keys``'s own docstring (read_model.py) for why a single
        # generic projection replaced 4 near-identical hand-typed
        # call sites.
        **_reported_snapshot_keys(),
        "pending_intervention_head": pending_intervention_head,
        "model": s.model,
        "model_active_class": s.active_model_class(),
        "model_classes": list(s.known_model_classes()),
        "agent_names": list(registry.loaded_names()),
        "attached_name": registry.attached_name,
        "session_tree": registry.session_tree(),
        # LOCAL genuinely measures the prompt/completion SPLIT below
        # (real Session state, u.prompt_tokens/u.completion_tokens) —
        # gated by ``usage_breakdown_reported`` above.
        "usage": (u.prompt_tokens, u.completion_tokens, u.total_tokens),
        "cost_usd": s.total_cost_usd,
        "cost_total": cost_total,
        "cost_agent": cost_agent,
        "cost_agent_unpriced_calls": unpriced_calls,
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
        # All three are None when THERE IS NO FIGURE — before this session's
        # first turn, or once that turn has been evicted from the tracker's
        # bounded per-turn buckets (#3283 ④ made this a KEYED read of this
        # session's own turn, so another session's turn no longer displaces it
        # — see Session.last_turn_usage). Deliberately not 0: a zero would be
        # indistinguishable from a turn that genuinely used nothing / cost
        # nothing and would render as fact, whereas None is loud in both
        # directions (drawn as "None", or a TypeError on any arithmetic).
        # `last_turn_usage` also carries a prompt/completion split (#3283 ④);
        # this bar shows the total, and the per-row TUI gutter shows the split
        # off `turn_usage_fn` below.
        "turn_chain_id": turn_usage["chain_id"],
        "turn_tokens": turn_usage["tokens"],
        "turn_cost_usd": turn_usage["cost_usd"],
        # #3283 ④: keyed per-turn lookup ``(chain_id) -> dict | None``, for a
        # surface that renders one figure PER ROW rather than one per session
        # (the TUI right gutter, which draws the prompt/completion split).
        # Deliberately NOT called here (see the assignment above).
        "turn_usage_fn": turn_usage_fn,
        "ctx_used": ctx_used,
        "ctx_window": ctx_window,
        "ctx_source": ctx_source,
        # LOCAL genuinely measures both cache figures below (the
        # `u`/`recent` reads are real Session state) — gated by
        # ``cache_usage_reported`` above.
        "session_cached_tokens": u.cached_tokens,
        "ctx_recent_usage": (recent.prompt_tokens, recent.cached_tokens),
        # LOCAL genuinely measures compaction status below (the bound
        # method is real ``Session.context_window_status``) — gated by
        # ``ctx_compaction_reported`` above.
        "ctx_compaction_status_fn": ctx_compaction_status_fn,
        # LOCAL genuinely measures cron config below (via
        # ``_extract_cron_jobs``) whenever a ``config`` is given — gated
        # by ``cron_jobs_reported`` above, declared True unconditionally:
        # an unattached/no-config caller already gets `[]` for
        # `cron_jobs` itself (same graceful-degrade value either way),
        # and this is LOCAL's capability declaration, not a per-call
        # state — the LOCAL implementation is always CAPABLE of
        # reporting cron config, whether or not one happens to be
        # loaded on THIS particular call.
        "cron_jobs": _extract_cron_jobs(config) if config is not None else [],
        "mcp_servers": _extract_mcp_servers(config) if config is not None else [],
        "hooks": _extract_hooks(config) if config is not None else [],
        "skills": _extract_skills(config) if config is not None else [],
        # #4194: the policy-tier unknown/renamed config-key count
        # (ReynConfig.unknown_config_key_count, set once at load_config()
        # time — see that field's own docstring in root.py). Read every
        # render tick like every other config-derived field above, so the
        # bottom-chrome indicator's own render logic stays a pure function
        # of this snapshot, same as everything else it draws from.
        "unknown_config_key_count": (
            getattr(config, "unknown_config_key_count", 0) if config is not None else 0
        ),
        # #4357: the full {key: hint} dict the count above is derived from
        # — kept alongside it in this snapshot for the same reason (a
        # future consumer of this snapshot dict gets the actionable detail
        # for free, not just the count).
        "unknown_config_keys": (
            getattr(config, "unknown_config_keys", {}) if config is not None else {}
        ),
        # #2285: session-scoped capability visibility + hook applicability toggles.
        # #3378: ``visibility_items`` is ``None`` when the session wires no visibility
        # seam (or it raised) and a possibly-empty LIST when it does — the renderer
        # needs that distinction to say "not wired" rather than "(none)".
        "visibility_items": _session_visibility_items(s),
        "hook_items": _session_hook_items(s),
        # #4686: per-server subscription read model for the mcp pane's
        # "subscribed"/"not honored"/"unconfirmed" rows. Always [] rather
        # than a not-wired seam — Session always owns an MCPConnectionService
        # (see Session.mcp_subscription_state's own docstring).
        "mcp_subscriptions": _session_mcp_subscriptions(s),
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
        # #2280: the durability-halt reason (``None`` while running), read
        # straight off Session.halted_reason — the operator-visible surface for
        # the fail-stop set in ``Session._fail_stop_if_durability_dead`` /
        # ``run_one_iteration``. Consumed by the TUI status line
        # (``chrome.status_line_text``) and threaded onto the wire for remote
        # parity (``agui.state._WIRE_KEYS`` / ``project_status``).
        "halted_reason": s.halted_reason,
    }
