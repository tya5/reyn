"""Audit-event schema registries — the kind vocabulary, and the fields per kind.

Two registries with two DIFFERENT responsibilities live here. Keeping them in
one module is deliberate (one place to look for "what is an audit-event"), but
they are not interchangeable and one is not derived from the other:

1. ``AUDIT_EVENT_KINDS`` — **the vocabulary**. Which audit-event kinds exist at
   all. This is the closed set (#3410): an audit-event's ``type`` is a public
   interface, because reyn is not the only consumer of ``.reyn/events``, and an
   external subscriber must be able to enumerate the kinds it may receive. An
   open namespace makes that impossible in principle, so the namespace is
   closed here and gated in ``tests/core/test_audit_event_kind_vocabulary_3410.py``
   in both directions (nothing emits an undeclared kind; nothing is declared
   without a producer).

2. ``EVENT_AUDIT_REQUIREMENTS`` — **the field requirements**. Given a kind, what
   its payload must carry (FP-0021). It covers a SUBSET of the vocabulary and
   says nothing about which kinds exist — the distinction that let
   ``mcp_search_invoked`` / ``mcp_tool_loaded`` sit here constraining a code
   path that no production run reaches (see the decision record next to their
   former entries), and let ``mcp_resources_listed`` / ``mcp_prompts_listed``
   ship without appearing here at all.

``KIND_EMIT_SEAMS`` and ``DYNAMIC_KIND_EMIT_SITES`` support (1): they declare
where kinds enter the log, and where the AST census that guards the vocabulary
is structurally blind.

Neither registry is enforced at ``emit()`` runtime (production overhead stays
zero) — the gates are CI-side.

P7 note: kind names here are OS-level event kinds, not domain-specific
identifiers, so this file stays within the OS layer's allowed vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── RETIRED_PHASE_FIELD — decision record (#2696) ────────────────────────────
# Several audit-events below declare a mandatory ``phase`` field. The phase
# engine that populated it was deleted (#2434 / #2438).
#
# DECISION: the field is RETAINED, and its value is ALWAYS the empty string.
#   - Retained because it is a persisted, CI-checked audit-event schema:
#     dropping it from ``EVENT_AUDIT_REQUIREMENTS`` would make every event file
#     reyn has already written fail to replay against the current schema, and
#     ``reyn events replay`` is the audit trail (P6 / band member).
#   - Always empty because there is nothing left to name: the #2696 drift-audit
#     also deleted ``OpContext.current_phase`` (which defaulted to ``""`` and
#     was never passed a non-empty value by any caller in ``src/``), so emit
#     sites now pass THIS constant literally rather than reading a field whose
#     existence implied a live phase concept.
#
# Do NOT "wire this up" — there is no producer to reconnect to. A non-empty
# value would require a NEW runtime concept, which is a design decision, not a
# repair. If such a concept ever lands, name it accurately instead of reviving
# ``phase``.
#
# Name collision, not the same field: ``interfaces/transport/agui/protocol.py``
# (``_encode_event``, the RUN_STARTED/RUN_FINISHED branch) also writes a
# ``"phase"`` key — that one is the AG-UI wire protocol's own standard field,
# live and populated with the run-lifecycle type string. It is unrelated to
# THIS field and cannot be renamed either (it's the AG-UI spec's own
# vocabulary). Two different "phase"s that happen to share a name — do not
# read one as explaining the other.
RETIRED_PHASE_FIELD = ""

# Events that must carry these audit fields (FP-0021)
EVENT_AUDIT_REQUIREMENTS: dict[str, frozenset[str]] = {
    # LLM cost events (llm.py _emit_chat_cost_events — cost-tab observability).
    # Minimal fields: llm_called carries model, llm_response_received carries the
    # usage/cost figures. agent is derived from the events file path (not an event
    # field), and run_id is not threaded on this path.
    "llm_called": frozenset({"model"}),
    # ``usage_source`` (#3351) is MANDATORY, not optional: a provider-reported
    # count and a ``litellm.token_counter`` estimate are the same int, and the
    # audit trail is where an estimated turn has to be identifiable after the
    # fact. Requiring it here means the cost events cannot report figures
    # without reporting their origin.
    "llm_response_received": frozenset(
        {"prompt_tokens", "completion_tokens", "cost_usd", "usage_source"}
    ),
    # Permission events (op_runtime/__init__.py). ``phase`` is mandatory for
    # replay compatibility and always ``RETIRED_PHASE_FIELD`` — see the decision
    # record at the top of this module before touching it.
    "permission_granted": frozenset({"run_id", "actor", "phase"}),
    "permission_denied": frozenset({"run_id", "actor", "phase"}),
    # #5065: a management operation on the SAVED-approvals store
    # (.reyn/approvals.yaml, permissions.py's REST router), not an in-run
    # permission decision — a different event from permission_granted/
    # permission_denied above (which ARE in-run, hence run_id/actor/phase).
    # This happens outside any run, so it carries no run_id/actor/phase —
    # fabricating those would violate the band's "reconstructible", not
    # "fields present", requirement (architect ruling, #5065). Two typed
    # kinds rather than one kind + an "operation" string field, because
    # their answerable fields genuinely differ (a single revoke names the
    # key it removed; a bulk clear has no single key to name).
    "permission_approval_revoked": frozenset({"key", "surface"}),
    "permission_approvals_cleared": frozenset({"count", "surface"}),
    # #5236: the grant half of the pair above — a management operation on
    # the SAVED-approvals store, same non-in-run reasoning (no run_id/
    # actor/phase). Same field shape as permission_approval_revoked
    # (key, surface) — the value the approval covers is deliberately never
    # in the payload, matching that kind's own choice.
    "permission_approval_granted": frozenset({"key", "surface"}),
    # #5296 PR-2: fires once per same-turn recovery attempt after a
    # BYTE-limited (HTTP 413) unrecovered overflow — spill and/or durable
    # compaction reduced the wire payload and the turn is being re-sent
    # WITHOUT re-triggering user_submitted/turn_started (same chain_id).
    # `attempt` is the 1-based retry count within this turn.
    "payload_reduced": frozenset({"chain_id", "attempt"}),
    # #5316: retry_loop's byte-axis measurement (#4944①) — wire_bytes is
    # the total, accepted names whether this size was SENT and SUCCEEDED
    # (True, a lower bound on the real limit) or SENT and REJECTED (False,
    # an upper bound). #5316 splits that total into its 5 components (byte
    # counts only, never content — company environment) so a reader can
    # tell WHICH part of the payload dominated, closing the "measure-and-
    # emit only, not yet consumed" gap #4944① left open.
    "compaction_wire_bytes_measured": frozenset({
        "wire_bytes", "accepted",
        "sp_bytes", "head_bytes", "summary_bytes", "tail_bytes", "new_msg_bytes",
    }),
    # #5531 PR-2 (owner acceptance: "下限を割ったことが見える" — visible
    # with the shipped config, not just inferable from a shrunk wire).
    # retry_loop's own reservation-based halving ladder lowers head/tail
    # below what `component_weights` configured whenever T_max itself
    # gets halved (both the byte-limit and, since PR-2, the token-
    # overflow path reach this) — previously silent (byte-path only,
    # and even there nothing recorded it). `configured_head_budget`/
    # `configured_tail_budget` are the UNCHANGED, entry-time values (the
    # floor this ladder is lowering BELOW); `head_min_tokens`/
    # `tail_min_tokens` are what this halving pass just derived instead.
    "compaction_floor_lowered": frozenset({
        "t_max_override", "head_min_tokens", "tail_min_tokens",
        "configured_head_budget", "configured_tail_budget", "saw_byte_limit",
    }),
    # #5367①: two distinct mechanisms (tool_result_cap.TRIGGER_CAP — write-time
    # size gate; TRIGGER_OVERFLOW — reactive same-turn spill, #5296 PR-2) emit
    # this SAME kind through one shared emit site. `trigger` is mandatory so a
    # reader distinguishing the two never has to infer which one fired from
    # context (chain_id presence, call ordering, ...).
    "tool_result_offloaded": frozenset({"trigger"}),
    # #5438 (architect ruling — "compute, don't store"): a spilled entry's
    # own backing file is confirmed missing on read-back
    # (history_content_resolve.resolve's own "lost" kind, checked FRESH
    # every serialise — never a persisted "lost" ledger). `reason` is
    # derived at THIS read, not stored: `never_persisted` when the entry's
    # own LOST_REASON_META_KEY already says so (the write-time cap refused
    # the offload outright), else `gc` (eviction is reyn's only deleter of
    # an already-persisted ref — a file missing for any other reason still
    # reads as `gc`, disclosed in the reader's own docstring, never a
    # claim this event can tell the two apart). `ref_sha256` (architect
    # review: not `content_hash` — that name reads as a hash of the LOST
    # content itself, which by definition isn't available to hash) is a
    # stable, ref-STRING-derived identifier so an operator can correlate
    # repeated reads of the SAME missing file.
    "offloaded_content_unavailable": frozenset({"ref", "reason", "ref_sha256"}),
    # #5514 §5/§8 (architect ruling, 2026-08-30): a spillability=never hook
    # push that exceeds its own declared spillability_max_chars is REJECTED
    # outright — never truncated (a partial frame is worse than no frame;
    # NEVER's own definition is "losing it changes the remaining meaning",
    # and truncation loses it silently in part) and never offloaded (NEVER
    # forbids spill by definition, so there is no ref to keep it lossless
    # with — tool_result_offloaded's own lossless+ref shape does not apply
    # here). History is left byte-unchanged; this event plus a WARNING log
    # line are the only trace. `hook_name`/`declared_max_chars`/
    # `actual_chars` are mandatory so an operator can act without reading
    # source: which hook, what it promised, what it actually produced.
    "hook_push_rejected_oversized": frozenset({
        "hook_name", "declared_max_chars", "actual_chars",
    }),
    # #5531 §9.6 (owner acceptance, 2026-08-30): "candidate 0" is
    # AMBIGUOUS by itself — a population that is genuinely all-NEVER
    # (correct, nothing to spill) and a population-construction path
    # silently broken (a real bug) produce the SAME zero-candidate
    # observation with no other signal. Fired whenever `_spill_fn`
    # (router_loop_driver.py) scans a non-empty `raw_middle` and finds
    # no usable candidate — `population`/`first_choice_count`/
    # `last_resort_count`/`never_count` say WHY: if the three counts
    # sum to `population`, the population was built correctly and is
    # legitimately exhausted; if they don't, the construction path
    # itself is the bug (a candidate with no `spillability` key at all,
    # or a non-string content, falls into neither eligible bucket NOR
    # `never_count` — this event's own consumer can compute that gap).
    "spill_candidate_population_exhausted": frozenset({
        "population", "first_choice_count", "last_resort_count", "never_count",
    }),
    # #5067: same shape as the two above, on the OTHER band pairing
    # (cost-budget x audit-events, not permission x audit-events) — a
    # management operation on the live BudgetTracker's hard caps
    # (budget.py's REST router, ``PATCH /api/budget/caps``), reachable
    # only via ``project_root`` (no live Session, no run_id/actor/phase).
    # ONE kind (not a typed union like #5065's revoked/cleared pair) since
    # there is only one operation shape here: a PATCH may touch several
    # cap fields at once, so ``changes`` carries only the fields the
    # request actually set (never a fabricated entry for a field the
    # caller left ``None`` / unchanged), each as its own
    # ``{"from": <old hard_limit>, "to": <new hard_limit>}`` pair —
    # lead-coder's measurement: unlike #5065's approvals.yaml, this change
    # is not even persisted (a restart silently reverts it), so this
    # audit-event is the ONLY record either value ever existed.
    "budget_caps_updated": frozenset({"changes", "surface"}),
    # User intervention (op_runtime/ask_user.py)
    "user_intervention_requested": frozenset({"run_id", "actor", "intervention_id"}),
    "user_intervention_received": frozenset({"run_id", "actor", "intervention_id"}),
    # #5729: fired from InterventionHandler.announce — the ONE choke point
    # ALL 6 intervention_bus.request() callers share (ask_user/permissions/
    # limit_handler/mcp_install/elicitation/hooks-shell_runner), unlike
    # user_intervention_requested above (ask_user.py only, verified).
    # Exists so a subscriber (the #5729 registry status fan-out) can learn
    # "an intervention was just enqueued" via subscribe_audit_events alone,
    # without also subscribing to the session's outbox (which would force
    # every session's OutboxHub drain task to start eagerly — a real,
    # measured regression: it silently began consuming session.outbox
    # before any real UI ever subscribed, starving direct outbox.get_nowait()
    # readers elsewhere in the test suite).
    "intervention_announced": frozenset({"intervention_id", "kind"}),
    # (#3410) ``mcp_search_invoked`` / ``mcp_tool_loaded`` were declared here for
    # the FP-0024 tool_search meta-tool and REMOVED. The reason is narrower than
    # it looks, and getting it right matters more than the removal does — see
    # "when FP-0024 is switched back on" below.
    #
    # ★ REASON: the FP-0024 path DOES NOT RUN. Not "nobody wrote the emitter",
    # not "reyn cannot observe it" — the mechanism exists and is switched off:
    #
    #   - ``router_tools.MCP_SEARCH_THRESHOLD`` is ``0``, and ``build_tools()``
    #     takes the tool_search branch only when ``mcp_search_threshold > 0``.
    #     FP-0032 lowered the default from 30 (the meta-tool is Anthropic-API-
    #     specific, which collides with reyn's provider-agnostic stance);
    #     removing it outright is tracked as FP-0033.
    #   - ``git grep "mcp_search_threshold=" -- src`` → **0 hits**. No production
    #     caller passes a non-default value; the ``ReynConfig`` field that could
    #     have was fold-removed in #3218. Only tests opt in, by passing the
    #     parameter directly to ``build_tools()``.
    #
    # So these were required fields for kinds on a branch no production run
    # reaches. Keeping them would have made the vocabulary describe a code path
    # that is dormant — the #3357 defect (a consumer waits forever) with an
    # extra layer of indirection.
    #
    # ★ WHEN FP-0024 IS SWITCHED BACK ON, re-decide both kinds SEPARATELY. They
    # are not the same case, and a single "we removed these" would lose that:
    #
    #   - ``mcp_tool_loaded`` — "a specific MCP tool was loaded from a search
    #     result" is **reyn's own act**: reyn is the one loading. An observation
    #     point exists. RE-ADD IT.
    #   - ``mcp_search_invoked`` — "the LLM called tool_search" may resolve
    #     inside the provider, in which case reyn has nothing to observe.
    #     CHECK before re-adding; do not assume either way from this comment.
    #
    # ★ COROLLARY (the line that decides the above): the audit log records what
    # the OS DID, not what happened elsewhere. But handing control to a
    # provider-internal mechanism is itself an OS act, and IS auditable. So the
    # right shape when this is re-enabled is not "the model searched" (not
    # observable) but "reyn advertised the meta-tool" / "reyn loaded tool X" —
    # which is also the shape that matches what the OS is accountable for.
    #
    # ★ Contrast with the ``_mcp_list_*`` decision this same change made in the
    # other direction (session.py, #3410): those four listing paths RUN on every
    # production turn that touches MCP, so "emitting is the recoverable
    # direction" applies and the fix was to add the missing emitters. The
    # difference is reachability, not observability.
    # FP-0034 Phase 3: Universal catalog routing decision (Self-improvement Loop)
    # #3455: emitted by RouterLoop._dispatch_resolved — the single dispatch
    # chokepoint every catalog action call funnels through, regardless of
    # entry surface (invoke_action wrapper / ARS-salvaged direct call /
    # flat bare-name dispatch when universal wrappers are off). Previously
    # emitted from a run_loop-local block gated on `if _univ_enabled:`,
    # which meant the opt-out config (an operator setting
    # `action_retrieval.universal_wrappers_enabled: false` in reyn.yaml)
    # never emitted it at all even though catalog routing was happening.
    # action_name: the resolved action_name (e.g. "agent.peer__alice")
    # source: how the routing happened
    #   ("invoke_action" | "ars_direct")
    # outcome: "success" | "error" based on the tool result status
    # chain_id: for cross-agent tracing (P6)
    "routing_decided": frozenset({"action_name", "source", "outcome", "chain_id"}),
    # FP-0034 B28-Q2 Case A: Inline LLM reply (no catalog dispatch in this turn).
    # Emitted by RouterLoop when the turn ends with a text reply and no
    # routing_decided event was emitted in the same turn.  Mutually exclusive
    # with routing_decided per turn.
    # decision: "inline_reply" — LLM answered conversationally without invoking
    #   any catalog-dispatched tool.  "clarification_asked" and "decline" are
    #   reserved in the schema for future analytics but NOT emitted by the router
    #   yet (both collapse to "inline_reply" at emit time).
    # tool_calls_attempted: count of tool_call rounds in earlier iterations of
    #   this turn where the LLM did try a non-catalog tool (e.g. an unknown tool name).
    # chain_id: for cross-agent tracing (P6)
    "chat_turn_completed_inline": frozenset({"chain_id", "decision", "tool_calls_attempted"}),
    # #1800 slice 5a: session + turn lifecycle events (P6 audit — hook dispatch
    # points added in slice 5b).
    #
    # session_started / session_completed: emitted in Session.run() alongside
    #   chat_started / chat_stopped. Marks the boundary of the session's
    #   resource scope (F in the lifecycle hook design).
    # agent_name: the session's agent identity (same field as chat_started).
    "session_started": frozenset({"agent_name"}),
    "session_completed": frozenset({"agent_name"}),
    "hook_shell_executed": frozenset({"command", "mode", "returncode", "denial_class", "cwd", "origin"}),
    # turn_started: emitted in Session.run_one_iteration() after the trigger
    #   is consumed from the inbox and before dispatch to _handle_*.
    # kind: the inbox message kind that triggered this turn — a value of the
    #   CLOSED reyn.runtime.turn_origin.TurnOrigin vocabulary, which is where
    #   the members and their reasons live. Lets subscribers distinguish human
    #   triggers from automated ones without parsing the payload.
    # skipped_over (#5647, MID-TURN injections only — absent on the ordinary
    #   turn-boundary emit): what the injection looked PAST in the inbox to
    #   reach the operator's message, as [{"kind", "msg_id"}] in arrival
    #   order; empty list when it looked past nothing. #3792 originally
    #   stopped a mid-turn peek at the first ineligible head because skipping
    #   "would leave no trace anywhere" — this field IS that trace, which is
    #   what let #5647 lift the stop. Enumerated, not counted: a reader has to
    #   be able to tell WHICH work was overtaken, not just how much.
    "turn_started": frozenset({"kind"}),
    # turn_completed: emitted in Session._run_router_loop() immediately after
    #   RouterLoopDriver.run_turn() returns — the router loop has reached a
    #   terminal condition (the turn's response is complete). One emit per
    #   turn, independent of routing path. This is the hook point for
    #   the turn_end lifecycle hook (slice 5b). chain_id matches the turn's
    #   chain_id for cross-agent tracing.
    "turn_completed": frozenset({"chain_id"}),
    # turn_settled: emitted in Session.run_one_iteration()'s finally for EVERY
    #   turn kind (including slash / intervention short-circuits that return
    #   before the router). Unlike turn_completed (router path only), this is the
    #   reliable "the turn is done" signal for UI working-indicators. kind mirrors
    #   turn_started; chain_id may be absent for non-user triggers.
    "turn_settled": frozenset({"kind"}),
    # #5103 ④: emitted around the #4405 REYN_STALL_TRACE bracket in
    # Session._run_turn_body — armed on entry (before run_turn dispatch),
    # disarmed in the finally (after run_turn returns or raises). Gives
    # ordering-only tests a public, append-only way to observe "the stall
    # bracket really wraps run_turn" (join on chain_id against turn_started,
    # index order = time order) instead of a private run_turn replacement
    # that asserts inside a monkeypatched closure — architect's own #5103
    # ruling: append-only order beats a counter (no content) or a snapshot
    # (overwrites between polls) because only a monotonically growing series
    # can be waited on without a duration. Only emitted when
    # REYN_STALL_TRACE is set (mirrors arm/disarm's own off-by-default gate,
    # #4405) — the pair is fully absent from a run with the env var unset,
    # itself the noise/cost control test_4405_stall_trace_wiring.py already
    # asserted before this PR.
    "stall_trace_armed": frozenset({"chain_id", "seconds"}),
    "stall_trace_disarmed": frozenset({"chain_id"}),
    # #5221: emitted by the `emit_behavior_anomaly_verdict` tool, the ONLY
    # producer — called from a registered pipeline's own `tool` step after its
    # judge `agent` step returns a schema-constrained verdict (never by a
    # live agent directly: the tool is gates.router="deny"). `verdict` is the
    # closed clean|suspicious vocabulary (`reyn.tools.emit_behavior_anomaly_verdict`
    # is the SSoT for the two literal values). `chain_id` joins this back to
    # the turn's own `turn_settled`/`turn_completed` — a chain_id with
    # `turn_settled` but NO matching `behavior_anomaly_judged` means "the
    # judge did not run" (below the escalation threshold, or the pipeline
    # itself failed), a THIRD state from "judged clean" — never conflate the
    # two (asymmetric trust: `verdict="clean"` means "the judge did not flag
    # it", not "verified clean"; an absent record means the judge never
    # looked at all). `anomalous_op_count` is the sensitive-op tally
    # (reyn.runtime.turn_behavior_tally) that triggered escalation, so a
    # reader can see WHY the judge ran without re-deriving it.
    "behavior_anomaly_judged": frozenset({"verdict", "chain_id", "anomalous_op_count"}),
}


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE VOCABULARY — which audit-event kinds exist (#3410)
# ═══════════════════════════════════════════════════════════════════════════
#
# CLOSED SET. An audit-event's ``type`` is a public interface: ``.reyn/events``
# is consumed outside reyn, and an external subscriber has to be able to
# enumerate every kind it might receive. That is impossible against an open
# namespace, so the namespace is closed here.
#
# Adding an audit-event kind is a three-line change: emit it, add it here, add
# it to the enumeration in ``docs/reference/runtime/events.md``. The gate
# (``tests/core/test_audit_event_kind_vocabulary_3410.py``) fails on any two of the
# three without the third — in BOTH directions, so a kind cannot be emitted
# without being declared, and cannot be declared without a producer.
#
# This list is NOT a hand-maintained mirror of the source: the gate derives the
# emitted set by AST census and asserts equality, so the only way to edit this
# list correctly is to make the code agree with it (or vice versa). What it adds
# over the census is reviewability — a diff here is a change to a public
# interface, visible as such in a PR.
AUDIT_EVENT_KINDS: frozenset[str] = frozenset({
    "agent_delta",
    "agent_message_refused",
    "agent_message_sent",
    "agent_request_received",
    "agent_response_committed",
    "agent_response_received",
    "asyncio_unhandled_exception",
    "behavior_anomaly_judged",
    "body_summary_hard_truncated",
    "budget_caps_updated",
    "budget_reset",
    "bus_subscriber_dropped",
    "canonical_degraded",
    "canonical_fallback_used",
    "chain_peer_discarded",
    "chain_timeout",
    "chain_timeout_extended",
    "chat_started",
    "chat_stopped",
    "chat_turn_completed_inline",
    "client_attached",
    "client_detached",
    "client_seized",
    # #5717 (lead-coder review of #5712/PR #5716): fires from
    # Session._compact_now_for_op when the attached ExecutionDriver has no
    # spill mechanism at all (e.g. PipelineExecutorDriver — it carries no
    # RouterHistoryBuffer) — distinguishes "no spill capability exists"
    # from "spill ran and found nothing eligible" in the audit trail, the
    # same distinction shrink_pool_after_overflow's own
    # spill_capability_present kwarg carries into UnrecoveredError.
    "compact_now_spill_capability_absent",
    "compact_op_completed",
    "compact_op_failed",
    "compact_op_requested",
    "compact_op_unavailable",
    "compaction_batch_cap_below_head_tail_budget",
    "compaction_check",
    "compaction_completed",
    "compaction_failed",
    "compaction_floor_lowered",
    "compaction_schema_invalid",
    "compaction_shrink_recovered",
    "compaction_started",
    "compaction_wire_bytes_measured",
    "composer_dropped",
    "composer_fired",
    "config_reload_rejected",
    "config_reloaded",
    "control_ir_failed",
    "control_ir_skipped",
    "cron_fired",
    "direct_alias_call_salvaged",
    "embed_attempts",
    "embed_cancelled",
    "embed_secret_redacted",
    "embedding_index_build_complete",
    "embedding_index_build_error",
    "embedding_index_build_progress",
    "embedding_index_build_started",
    "exec_threat_blocked",
    "exec_threat_match",
    "file_changed",
    "file_read_media_denied",
    "file_read_media_write_unavailable",
    "force_close_triggered",
    "hook_changed",
    "hook_drain_task_died",
    "hook_event_emitted",
    "hook_push_fired",
    "hook_push_rejected_oversized",
    "hook_shell_executed",
    "spill_candidate_population_exhausted",
    "hooks_layer_rejected",
    "inbox_cancel",
    "index_dropped",
    "index_update_cost_warning",
    "index_updated",
    "ingress_bridge_dropped",
    "intervention_announced",
    "intervention_answer_submitted",
    "intervention_denied",
    "intervention_routed",
    "limit_denied",
    "llm_call_retry",
    "llm_call_retry_exhausted",
    "llm_called",
    "llm_request",
    "llm_request_error",
    "llm_response_received",
    "mcp_called",
    "mcp_cancelled",
    "mcp_client_close_leaked",
    "mcp_completed",
    "mcp_elicitation_answered",
    "mcp_elicitation_auto_declined",
    "mcp_elicitation_requested",
    "mcp_elicitation_timed_out",
    "mcp_failed",
    "mcp_hook_subscribe_not_applied",
    "mcp_initialized",
    "mcp_install_cancelled",
    "mcp_install_probe_failed",
    "mcp_install_threat_blocked",
    "mcp_install_threat_match",
    "mcp_media_denied",
    "mcp_media_write_unavailable",
    "mcp_progress",
    "mcp_prompt_get",
    "mcp_prompt_get_cancelled",
    "mcp_prompt_get_completed",
    "mcp_prompt_get_failed",
    "mcp_prompt_list_changed",
    "mcp_prompts_listed",
    "mcp_reconnect_failed",
    "mcp_resource_read",
    "mcp_resource_read_cancelled",
    "mcp_resource_read_completed",
    "mcp_resource_read_failed",
    "mcp_resource_subscribe",
    "mcp_resource_subscribe_cancelled",
    "mcp_resource_subscribe_failed",
    "mcp_resource_subscribed",
    "mcp_resource_templates_listed",
    "mcp_resource_unsubscribe",
    "mcp_resource_unsubscribe_cancelled",
    "mcp_resource_unsubscribe_failed",
    "mcp_resource_unsubscribed",
    "mcp_resource_updated",
    "mcp_resources_listed",
    "mcp_server_install_skipped",
    "mcp_server_installed",
    "mcp_server_removed",
    "mcp_tool_list_changed",
    "mcp_tool_probe_degraded",
    "mcp_tools_listed",
    "memory_deleted",
    "memory_saved",
    "model_budget_fallback",
    "model_cost_block",
    "model_cost_warn",
    "network_ssl_verify_disabled",
    "new_msg_exceeds_budget",
    "oauth_login_completed",
    "oauth_login_started",
    "offloaded_content_unavailable",
    "payload_reduced",
    "peer_reply_failed_surfaced",
    "pending_intervention_claimed",
    "pending_intervention_discarded",
    "permission_approval_granted",
    "permission_approval_revoked",
    "permission_approvals_cleared",
    "permission_denied",
    "permission_granted",
    "pipeline_install_skipped",
    "pipeline_install_threat_blocked",
    "pipeline_install_threat_match",
    "pipeline_installed",
    "pipeline_load_failed",
    "pipeline_run_attached",
    "pipeline_step_completed",
    "pipeline_step_started",
    "plan_step_llm_memoized",
    "plugin_install_completed",
    "plugin_install_copied",
    "plugin_install_reconciled",
    "plugin_install_registered",
    "plugin_install_started",
    "plugin_install_token_vocabulary_mismatch",
    "plugin_uninstall_completed",
    "plugin_uninstall_registry_dropped",
    "plugin_uninstall_started",
    "presentation_install_blocked",
    "presentation_installed",
    "presentation_load_failed",
    "presented",
    "process_marker_reaped",
    "project_context_changed",
    # #5732 (architect ruling): the textual chat pump's own bounded
    # diagnostic for a swallowed per-frame exception (/copy sentinel,
    # /rewind sentinel, __open_artifact__ sentinel, _ingest_frame) --
    # one event per FIRST
    # occurrence of a (frame_kind, exception type) pair, never per
    # occurrence (a broken call site fails every frame; the running
    # count is exposed separately via PumpSwallowStats.count, which is
    # complete -- this event is bounded, not a duplicate of the count).
    "pump_exception_swallowed",
    "recovery_summary_persisted",
    "repo_ingest_files_skipped",
    "resource_cap_exceeds_budget_trigger",
    "router_context_overflow_detected",
    "router_context_overflow_unrecovered",
    "router_empty_response_detected",
    "router_empty_response_retry_injected",
    "router_loop_terminated_by_exception",
    "router_represent_round",
    "router_retry_exhausted",
    "routing_decided",
    "safety_limit_checkpoint",
    "sandbox_axis_unenforced",
    "sandbox_policy_narrowed",
    "sandbox_policy_not_applied",
    "sandboxed_exec_cancelled",
    "sandboxed_exec_completed",
    "sandboxed_exec_started",
    "secret_cleared",
    "secret_rotated",
    "secret_set",
    "semantic_search_complete",
    "semantic_search_embed_failed",
    "semantic_search_started",
    "session_completed",
    "session_halted",
    "session_restored",
    # #5694 stage 2 (architect ruling): the registry's own single
    # done-callback funnel for every (name, sid) session.run() background
    # task (AgentRegistry._on_session_run_task_done) — closes the gap
    # where a session dying was consumed by nothing more specific than
    # Python's own generic unhandled-exception path, with no (name, sid).
    "session_run_task_finished",
    # #5694 stage 2 disposition (architect ruling): a request-driven caller
    # (ensure_running/ensure_session_running/attach/attach_session) found
    # a PRIOR (name, sid) run-task already done and is replacing it — the
    # moment a session's death used to be silently consumed as a mere
    # restart trigger. NOT a new restart policy (the restart already
    # happened, request-driven, before this event existed) — only makes
    # it recorded. Named "rediscovered_dead", not "restarted": the
    # event's own timestamp is an upper bound on when the task actually
    # died (same property as process_marker_reaped's own observed_at),
    # never asserted as the death time itself.
    "session_run_task_rediscovered_dead",
    "session_started",
    "skill_body_loaded",
    "skill_body_threat_blocked",
    "skill_body_threat_match",
    "skill_install_skipped",
    "skill_install_threat_blocked",
    "skill_install_threat_match",
    "skill_installed",
    "skill_invoke_body_loaded",
    "skill_invoke_collision",
    "stall_trace_armed",
    "stall_trace_disarmed",
    "state_change_notified",
    "summary_resummarize_failed",
    "summary_resummarized",
    "task_settle_undelivered",
    "threat_block",
    "threat_scan_match",
    "token_refresh_failed",
    "token_refreshed",
    "tool_call_cap_exceeded",
    "tool_call_deduped",
    "tool_called",
    "tool_cycle_kept_whole_over_budget",
    "tool_executed",
    "tool_failed",
    "tool_result_offloaded",
    "tool_result_write_unavailable",
    "tool_returned",
    "turn_cancelled",
    "turn_completed",
    "turn_settled",
    "turn_started",
    "turn_too_large_truncated",
    "untrusted_narrowing_engaged",
    "untrusted_narrowing_lifted",
    "user_answered_intervention",
    "user_intervention_received",
    "user_intervention_requested",
    "user_message_received",
    "user_submitted",
    "visibility_changed",
    "web_fetch_completed",
    "web_fetch_failed",
    "web_fetch_media_denied",
    "web_fetch_media_write_unavailable",
    "web_fetch_ssrf_blocked",
    "web_fetch_started",
    "web_fetch_too_large",
    "web_fetch_too_many_redirects",
    "web_search_completed",
    "web_search_failed",
    "web_search_started",
    "webhook_received",
    "workspace_updated",
})


# ── Where kinds enter the log ────────────────────────────────────────────────
# The call shapes the vocabulary census reads. Name → the KEYWORD carrying the
# kind, or ``None`` when the kind is the first positional argument.
#
# Most entries are ``EventLog.emit``-shaped ``(kind, **data)`` callables; the
# ``_emit*`` / ``*_sink`` names are injected sinks and thin forwarders that take
# the kind from their own caller. Registering a name is what makes the kinds
# flowing through it visible to the census — which is why the gate does not
# trust this list to be complete on its own: it also derives, from the AST,
# every function that forwards a parameter into a seam's kind slot, and fails if
# such a function is not registered here. A hand-written registry covers the
# seams someone remembered; the derivation covers the rest.
KIND_EMIT_SEAMS: dict[str, str | None] = {
    # The primary seam: ``EventLog.emit(kind, **data)``.
    "emit": None,
    # ``emit_cli_event(kind, **payload)`` — audit emit from a CLI entry point
    # that has no session (writes to ``.reyn/events/direct/cli``).
    "emit_cli_event": None,
    # ``emit_direct_event(kind, *, surface, reyn_root, **payload)`` (#5065) —
    # the general "no live Session" seam ``emit_cli_event`` above is a thin
    # wrapper over; writes to ``.reyn/events/direct/<surface>``.
    "emit_direct_event": None,
    # ``Session.emit_audit_event(event_type, **data)`` — the narrow public seam
    # the AG-UI transport records surface-lifecycle attribution through.
    "emit_audit_event": None,
    # Injected ``(kind, **data)`` sinks: the hook bus / dispatcher / composer and
    # the MCP connection service + message handler are handed a callable rather
    # than an EventLog, so their emits never name ``emit`` at the call site.
    "emit_event": None,
    "_emit_event": None,
    "emit_sink": None,
    "_emit_sink": None,
    # Local best-effort wrappers around one of the above (index coordinator, MCP
    # elicitation / message handler, hook composer). ``_emit`` is also the name
    # of an unrelated tree-walk helper in ``interfaces/common/branch_tree.py``;
    # that collision is declared in DYNAMIC_KIND_EMIT_SITES rather than special-
    # cased here, so it stays visible.
    "_emit": None,
    "_audit": None,
    # ``RouterHostAdapter._mcp_list_via_gateway(..., event_kind=...)`` — the
    # shared MCP listing seam (#3447: folded off Session, same function name).
    # The kind rides a keyword and every call site passes a literal (#3410);
    # before that it was assembled as ``f"mcp_{noun}_listed"``, which no
    # census could read.
    "_mcp_list_via_gateway": "event_kind",
}


@dataclass(frozen=True)
class DynamicEmitSite:
    """One audit-emit call site whose kind the vocabulary census cannot read.

    A gate cannot close a namespace through a call site that builds its kind at
    runtime. Declaring each such site — with a classification and a reason —
    turns "the census might be blind somewhere" into an enumerable list that
    cannot grow without a decision.

    Each classification names a different way the entry can turn out to be
    WRONG — which is the point of classifying at all. An entry is a claim about
    the census's coverage, and a claim you cannot refute is not worth recording:

    - ``FORWARDER`` — the enclosing function is itself a registered seam, so the
      kind came from a caller the census already reads and none is minted here.
      **Breaks when** a caller of that seam passes a computed kind instead of a
      literal: that kind reaches the log without ever being censused, and the
      vocabulary silently stops being closed for this path. Refute by finding
      such a caller — the gate will already be RED on the caller's own site.
    - ``SINK_BINDING`` — a lambda/closure binding a registered sink parameter to
      a real ``EventLog``. Same claim one injection level further out: the kinds
      are censused at the consuming component's own emit sites. **Breaks when**
      the sink is handed to a component whose emits are NOT literal, which the
      census cannot see from this end at all.
    - ``KIND_FAMILY`` — the kind is genuinely assembled at runtime, so the
      census cannot expand it. These are the real holes; each needs a reason it
      still exists AND the condition under which it should stop existing.
      **Breaks when** the reason's premise changes (a producer appears, a
      provider starts accepting the sink) — at which point kinds flow through a
      site no gate reads.
    - ``NOT_AN_AUDIT_EMIT`` — a name collision with a seam name; the call never
      touches an event log. **Breaks when** the colliding helper is rewired onto
      a real event log, or the seam registry drops the name: the entry then
      reads as a granted exemption, and the gate would count a genuine dynamic
      emit as already-declared. This is the only classification that can turn a
      RED into a silent GREEN, so verify the call really does not reach a log
      before using it.
    """

    module: str
    function: str
    seam: str
    classification: str
    reason: str

    CLASSIFICATIONS = ("FORWARDER", "SINK_BINDING", "KIND_FAMILY", "NOT_AN_AUDIT_EMIT")


# ── The census's blind spots, enumerated ─────────────────────────────────────
# Keyed by (module, enclosing function, seam) — not by line number, which is not
# an identifier across a moving ``main``.
DYNAMIC_KIND_EMIT_SITES: tuple[DynamicEmitSite, ...] = (
    DynamicEmitSite(
        module="src/reyn/core/events/events.py",
        function="emit_cli_event",
        seam="emit_direct_event",
        classification="FORWARDER",
        reason=(
            "``emit_cli_event`` is itself a registered seam (#5065: now a thin "
            "wrapper over ``emit_direct_event``); its own call sites pass "
            "literal kinds and are censused there."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/core/events/events.py",
        function="emit_direct_event",
        seam="emit",
        classification="FORWARDER",
        reason=(
            "#5065: ``emit_direct_event`` is itself a registered seam (the "
            "general 'no live Session' emit path ``emit_cli_event`` above "
            "wraps); its own callers (``emit_cli_event`` and, e.g., the "
            "``/api/permissions`` REST router) pass literal kinds and are "
            "censused there."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/data/index/coordinator.py",
        function="_emit",
        seam="emit",
        classification="FORWARDER",
        reason=(
            "IndexCoordinator's best-effort wrapper (never raises, so a broken "
            "sink cannot fail a build). Registered seam; its five call sites "
            "pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/hooks/composer.py",
        function="_audit",
        seam="_emit_event",
        classification="FORWARDER",
        reason=(
            "Composer's metadata-only audit wrapper over the injected hook "
            "sink. Registered seam; its call sites pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/mcp/elicitation.py",
        function="_emit",
        seam="emit_sink",
        classification="FORWARDER",
        reason=(
            "The elicitation handler's local emit closure over the injected "
            "sink. Registered seam; its call sites pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/mcp/message_handler.py",
        function="_emit",
        seam="_emit_sink",
        classification="FORWARDER",
        reason=(
            "MCP message handler's guarded wrapper over the injected sink. "
            "Registered seam; its call sites pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/session.py",
        function="emit_audit_event",
        seam="emit",
        classification="FORWARDER",
        reason=(
            "The public Session seam the AG-UI transport emits through. "
            "Registered seam; its call sites pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/services/router_host_adapter.py",
        function="_mcp_list_via_gateway",
        seam="emit",
        classification="FORWARDER",
        reason=(
            "The shared MCP listing seam (#3447: folded off Session onto "
            "RouterHostAdapter, byte-identical function name/seam). "
            "Registered with ``event_kind`` as its kind keyword; all four "
            "gateway-backed ``mcp_list_*`` call sites pass a literal (#3410)."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/session.py",
        function="_build_hook_event_bundle",
        seam="emit",
        classification="SINK_BINDING",
        reason=(
            "``lambda et, **d: self._audit_events.emit(et, **d)`` handed to the "
            "HookBus / HookDispatcher / Composer as their ``emit_event`` sink. "
            "The kinds are censused at those components' own emit sites."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/session.py",
        function="_build_mcp_connection_service",
        seam="emit",
        classification="SINK_BINDING",
        reason=(
            "The same lambda shape handed to MCPConnectionService as its "
            "``emit_sink``; censused at that component's own emit sites."
        ),
    ),
    # #3438: the ``_embedding_event_sink`` KIND_FAMILY entry that used to live
    # here (``f"embedding_{kind}"`` in src/reyn/runtime/session.py) was
    # deleted along with the sink itself and its whole seven-hop wire
    # (Session -> OpContext -> the `embed` op -> provider). It had no
    # producer — no embedding provider in the repo ever accepted the
    # ``event_sink`` kwarg ``get_provider`` conditionally forwarded — and no
    # comment/ADR/issue recorded an intent to keep it for a future provider,
    # so the hole is closed by removing the wire rather than by keeping this
    # registry entry.
    # Two entries for one helper: the recursion inside it, and the call that
    # starts the recursion — different enclosing functions, so different keys.
    DynamicEmitSite(
        module="src/reyn/interfaces/common/branch_tree.py",
        function="_emit",
        seam="_emit",
        classification="NOT_AN_AUDIT_EMIT",
        reason=(
            "A local recursive tree-walk helper that happens to be named "
            "``_emit``; it appends rendered rows to a list and never touches an "
            "event log (verified: its only sink is the enclosing ``rows`` "
            "list). Declared rather than filtered out by a receiver-name "
            "heuristic, so the collision stays visible. ★ If this helper is "
            "ever rewired onto an event log, DELETE this entry first — leaving "
            "it turns a genuine unreadable kind into a gate the census counts "
            "as already-declared."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/interfaces/common/branch_tree.py",
        function="build_branch_tree_rows",
        seam="_emit",
        classification="NOT_AN_AUDIT_EMIT",
        reason=(
            "The call that seeds the tree-walk helper above. Same collision, "
            "same non-audit call, same deletion condition."
        ),
    ),
)
