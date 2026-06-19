"""reyn.config.chat — chat-session config: Reasoning/Chat/Loop/Compaction/Timeout/OnLimit/Safety. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# ── FP-0004: safety: section (user-facing unified schema) ──────────────────
# PR22: CostConfig + CostLimitConfig live in `reyn.runtime.budget` (re-exported here
# for ReynConfig typing). They include domain logic (warn_threshold etc.)
# that doesn't belong in the config-only module.
from reyn.runtime.budget.budget import CostConfig, CostLimitConfig  # noqa: E402


@dataclass
class LoopConfig:
    """`safety.loop:` — caps that catch repetitive / runaway behaviour.

    These are *loop-detection* limits (= "the agent is doing the same thing
    over and over"). Hitting one of these is normal during exploratory
    development; raising the cap is the right operator response when the
    workload genuinely needs more iterations.

    Fields:
        max_act_turns_per_phase:
            Global default for the per-phase ``max_act_turns`` (= LLM ↔ op
            volleys inside one phase visit). Skill / phase frontmatter still
            wins when set. ``0`` = unlimited.
        max_phase_visits:
            How many times any single phase may be entered in one skill run.
            ``0`` = unlimited.
        max_router_calls_per_turn:
            Cap on chat-router invocations within a single user turn.
            ``0`` = unlimited.
        max_agent_hops:
            Maximum delegation depth (= user → A → B → C is 3 hops).
        skill_calls_per_chain:
            Per-(chain, skill) spawn cap with warn + ask_on_exceed semantics.
            ``hard_limit=None`` = unlimited (default).
        skill_tokens_per_chain:
            Per-(chain, skill) token cap with warn semantics.
            ``hard_limit=None`` = unlimited (default).
        max_router_iterations:
            Maximum LLM tool-call iterations per chat-router invocation
            (= per user turn). ``0`` = unlimited. CLI ``--max-iterations``
            overrides this when provided. Run-once / autonomous contexts
            typically set this higher (e.g. 80) via CLI.
        max_tool_calls_per_turn:
            Cost-bound (#1666): the maximum number of ``tool_calls`` honoured
            from a SINGLE LLM completion. A degenerate (weak-model, long-context)
            completion can emit thousands of tool_calls — observed 3451 in one
            SWE-bench completion — each costing a tool-result message + token
            inflation. When a completion exceeds this cap the OS processes only
            the first ``max_tool_calls_per_turn`` calls, drops the overflow
            (un-executed, un-appended), and appends a single re-grounding notice.
            Default ``50`` is generous headroom over legitimate parallel tool use
            (observed < 10) yet ~70x below the runaway. ``0`` = unlimited.
    """

    max_act_turns_per_phase: int = 10
    max_phase_visits: int = 25
    max_router_calls_per_turn: int = 3
    max_agent_hops: int = 3
    max_router_iterations: int = 5
    max_tool_calls_per_turn: int = 50
    skill_calls_per_chain: CostLimitConfig = field(default_factory=CostLimitConfig)
    skill_tokens_per_chain: CostLimitConfig = field(default_factory=CostLimitConfig)

    # B51 NF-W6-3 fix: plan() tool call parse-error self-correction loop.
    #
    # When the router LLM emits ``plan(args={steps_json: <malformed>})``
    # and the plan tool returns ``{status: error, error: {kind:
    # plan_invalid, ...}}``, the router loop appends a user-role
    # directive carrying the error message + an "escape inner quotes"
    # hint and re-enters the LLM loop so the LLM gets a chance to
    # re-emit with valid JSON. ``0`` disables the retry (= the LLM
    # receives the plain error tool result and decides next step
    # itself, the pre-fix behaviour). ``1`` (= default) allows one
    # directive-driven correction per chat turn.
    #
    # Dedicated counter rather than reusing ``max_router_calls_per_turn``
    # so the operator can tune plan-revision attempts independently
    # from the broader router-call cap. The natural outer bounds
    # (``max_router_calls_per_turn`` + ``RouterLoop.max_iterations``)
    # still apply on top.
    plan_invalid_retries: int = 1


@dataclass
class TimeoutConfig:
    """`safety.timeout:` — wall-clock bounds.

    These are *timeout* limits (= "this is taking too long"). Hitting one
    almost always means a slow LLM, a stuck delegation, or an unbounded
    loop in user code. Raise the cap when the workload legitimately needs
    longer; investigate when it shouldn't.

    Fields:
        llm_call_seconds:
            Per-call timeout passed to ``litellm.acompletion``.
        llm_max_retries:
            Transient-error retry budget per call.
        phase_seconds:
            Soft wall-clock budget for one phase visit. ``0`` = unlimited.
        chain_seconds:
            How long a multi-agent pending chain waits for a delegate
            reply before the runtime synthesises an upstream error.
            ``0`` (or any non-positive value) disables.
    """

    llm_call_seconds: float = 60.0
    llm_max_retries: int = 3
    phase_seconds: float = 0.0
    chain_seconds: float = 60.0


ON_LIMIT_MODES = ("interactive", "unattended", "auto_extend")


@dataclass
class OnLimitConfig:
    """`safety.on_limit:` — what happens when a loop / timeout limit is hit
    (FP-0005).

    Reyn supports three behaviours when a safety limit fires:

    - ``interactive`` (= default): pause the run, prompt the user via
      ``ask_user`` for permission to continue. On approval the limit
      is extended by one increment; on refusal (or ask timeout) the
      run aborts with ``RunResult.partial_data`` populated. Default
      ``ask_timeout_seconds=0`` means "wait forever for a human
      reply" — silently discarding mid-run state on a 60s wall clock
      is a worse UX than holding the run open until the user returns.

    - ``unattended``: abort immediately on hit. Opt-in for CI / cron
      / scripted runs that genuinely cannot pause for a human, where
      a hung intervention prompt would be a worse outcome than a
      clean abort.

    - ``auto_extend``: auto-extend the limit ``auto_extend_times`` times
      without prompting, then fall through to ``unattended`` behaviour
      once the auto-extend budget is spent. Useful for trusted long-
      running tasks where the operator knows up front that ``N``
      extensions are acceptable.

    The mode applies to the user-facing limits listed in FP-0005 §
    "limit ごとの適用可否" (max_act_turns, max_phase_visits, router_cap,
    skill_calls_per_chain, max_agent_hops, phase_seconds, chain_seconds).
    LLM call timeouts already retry via litellm and are not part of this
    pipeline.

    ``ask_timeout_seconds`` bounds how long ``interactive`` mode waits
    for a user response. ``0`` (= default) means "wait forever";
    positive values abort with ``partial_data`` after the window
    elapses. Headless paths are still safe regardless of timeout:
    ``bus=None`` (= no intervention surface, e.g. dispatch_tool /
    scripted runs) short-circuits to abort via the ``no_bus`` reason
    in ``handle_limit_exceeded``, and ``StdinInterventionBus`` on a
    non-TTY raises ``EOFError`` immediately which the helper treats
    as a refusal.
    """

    mode: Literal["interactive", "unattended", "auto_extend"] = "interactive"
    auto_extend_times: int = 1
    ask_timeout_seconds: float = 0.0


@dataclass
class ThreatScanConfig:
    """`safety.threat_scan:` — content-layer threat scan + fence (FP-0050 / #1822).

    Complements the execution layer (permissions / sandbox): inspects untrusted
    content for prompt-injection before it enters the SP/context, and is the
    config surface for the fence + scan defense-in-depth.

    - ``enabled`` — master switch. Default-on: Class-A detect is non-blocking,
      low-risk telemetry; Class-B write seams block.
    - ``fail_open`` — scanner error → allow (a false-negative is tolerated over a
      false-positive that wedges a turn).
    - ``fence_enabled`` — Class-A structural fencing of untrusted content.
    - ``block_severity`` — minimum severity that BLOCKS at write seams (Class B).
      ``"block"`` (default) blocks only ``severity="block"`` patterns; ``"warn"``
      makes warn-severity block too (stricter).
    - ``custom_patterns`` — operator ``(regex, id, scope, severity)`` extension.
    """
    enabled: bool = True
    fail_open: bool = True
    fence_enabled: bool = True
    block_severity: str = "block"
    custom_patterns: list = field(default_factory=list)


@dataclass
class CostWarnConfig:
    """`cost_warn:` — high-cost model pre-selection awareness (#1830 / FP-0052).

    Surfaces a ``model_cost_warn`` event (and inline conv-pane marker) when the
    user selects a model whose input cost per 1M tokens exceeds the threshold.
    Fires at ``/model`` switch and at session startup — one warn per model per
    session (de-duped via the session's ``_cost_warned_models`` set).

    This is a *pre-selection awareness* layer, orthogonal to BudgetTracker
    (cumulative spend) and ContextBudgetAdvisor (token ceiling).

    - ``enabled`` — master switch; default True.
    - ``model_threshold_per_1m_input_usd`` — warn if input rate exceeds this
      value in USD per 1M tokens. Default 5.0: catches Opus-class (~$15/1M)
      without triggering on Sonnet-class (~$3/1M). User-overridable in reyn.yaml.
    """
    enabled: bool = True
    model_threshold_per_1m_input_usd: float = 5.0


@dataclass
class SafetyConfig:
    """`safety:` — unified, user-facing namespace for stop conditions.

    Reyn stops a run for one of three reasons: a loop was detected, a
    timeout fired, or the budget was exceeded. The first two are grouped
    under ``safety.loop`` / ``safety.timeout``; budget caps stay under
    ``cost:`` because they are financial knobs (per-agent / daily /
    monthly token + USD limits) rather than runaway-detection knobs.

    ``safety.loop.skill_calls_per_chain`` and
    ``safety.loop.skill_tokens_per_chain`` are hybrid caps: they live
    under ``safety.loop`` because they gate repeated skill spawns
    (loop-detection), but carry ``CostLimitConfig`` semantics (warn_ratio,
    ask_on_exceed, extension_calls) because the operator may want the
    user-approval flow on hit rather than an immediate abort.

    See ``docs/guide/for-skill-authors/understand-why-reyn-stops.md`` for
    the operator's mental model.

    ``on_limit`` (FP-0005) controls what happens when a loop / timeout
    limit fires: prompt the user (interactive), abort silently
    (unattended, legacy default), or auto-extend N times then abort
    (auto_extend).
    """

    loop: LoopConfig = field(default_factory=LoopConfig)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    on_limit: OnLimitConfig = field(default_factory=OnLimitConfig)
    threat_scan: ThreatScanConfig = field(default_factory=ThreatScanConfig)


@dataclass
class CompactionSectionCaps:
    """Per-section token budgets for chat_summary BODY."""
    topic_arc: int = 200
    decisions: int = 400
    pending: int = 400
    session_user_facts: int = 200
    artifacts_referenced: int = 300


@dataclass
class CompactionConfig:
    """`chat.compaction:` — Head/Body/Tail compaction policy.

    PR-N6 (FP-0008): budget allocation uses integer component_weights +
    section_weights, normalised at compute_budgets() time.  Weights are
    sum-arbitrary (any positive integers work; normalisation handles the rest).

    This REPLACES the PR-N3 ratio fields (head_ratio / body_ratio /
    tail_ratio / new_msg_ratio).  Those fields are REMOVED.

    **Breaking change from PR-N3**: YAML configs with ``head_ratio`` /
    ``body_ratio`` / ``tail_ratio`` / ``new_msg_ratio`` fields will have those
    keys silently ignored by _build_chat_config.  Operators must migrate to
    ``component_weights`` / ``section_weights`` dicts in reyn.yaml.  The old
    ratio sum <= 1.0 invariant is gone; the startup assertion now checks that
    all weight values are >= 0 and the total sum > 0.

    component_weights (PR-N6):
        Integer weights for each prompt component, normalised to sum to 1.0 at
        compute_budgets() time.  Keys: head / body / tail / new_msg /
        compaction_batch.

    section_weights (PR-N6 drift-mitigation):
        Integer weights for each compaction summary section, normalised to
        body_budget at compute_budgets() time.  Keys: topic_arc / decisions /
        pending / session_user_facts / artifacts_referenced.

    Tokeniser:
        use_chars4_estimate=False (default) -> litellm.token_counter per turn.
        use_chars4_estimate=True  -> len(text)//4 (latency-opt for large deploys).
    """
    # Integer weight-based budget allocation (PR-N6). Sum-arbitrary; normalised
    # at compute_budgets() time.
    component_weights: dict = field(default_factory=lambda: {
        "head":             10,
        "body":             5,
        "tail":             15,
        "new_msg":          10,
        "compaction_batch": 60,
    })
    section_weights: dict = field(default_factory=lambda: {
        "topic_arc":            5,    # abstract suppression
        "decisions":            40,   # specific data emphasis
        "pending":              25,
        "session_user_facts":   10,
        "artifacts_referenced": 35,   # path/line preservation
    })
    # section_caps_spec_tokens: static overhead budget for section_token_caps
    # serialisation in the compactor prompt.
    section_caps_spec_tokens: int = 100
    # Tokeniser opt-out (Axis 10): set True for latency-sensitive deployments.
    use_chars4_estimate: bool = False
    body_token_cap: int = 1500          # hard cap on summary body tokens (post-truncation)
    # #271 re-summarize (T2): max LLM re-compression passes when a produced
    # topic_arc overshoots body_budget, before the deterministic T3
    # hard_truncate floor. 1 = one judgment-based re-summary then floor; 0 =
    # skip T2 (straight to the floor, = pre-#271 behaviour).
    resummarize_passes: int = 1
    section_token_caps: CompactionSectionCaps = field(default_factory=CompactionSectionCaps)


@dataclass
class ReasoningConfig:
    """`chat.reasoning:` — model reasoning/thinking-text handling (#1652).

    Capture of the provider ``reasoning_content`` is always-on (not gated here).
    These knobs gate what happens to it afterwards; both default ON.

    ``continuity`` — persist reasoning to history and replay the recent turns'
      reasoning into the next turn's system prompt (cross-user-turn reasoning
      continuity, the #1212-mirror text-section). Opt-out to disable persist+replay.
    ``display`` — surface reasoning to the UI (TUI + chainlit, collapsible).
      Opt-out to hide it. Independent of ``continuity``.
    ``recent_turns`` — how many recent turns' reasoning to replay under
      ``continuity``. ``<= 0`` (e.g. 0 / -1) = unbounded (keep all). Bounding
      matters on gemini (no provider auto-filter → reasoning is billed in full).
    """
    continuity: bool = True
    display: bool = True
    recent_turns: int = 3


@dataclass
class ChatConfig:
    """`chat:` — chat-session-specific runtime knobs."""
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    reasoning: ReasoningConfig = field(default_factory=ReasoningConfig)


@dataclass
class PlannerStepCompactionConfig:
    """`plan.step_compaction:` — Plan step_results compaction policy (PR-N4).

    Mirrors CompactionConfig's ratio-based approach but scoped to the
    prior-step output accumulation that feeds each plan step's sub-loop
    system prompt.  When accumulated step_results would balloon the next
    step's sys_prompt, older entries are summarised using
    CompactionEngine and replaced with a single
    ``__compacted_step_summary__`` entry.

    Fields
    ------
    recent_step_results_raw:
        Keep the last N step_results verbatim; compact older ones.
    summarize_older_threshold_tokens:
        Total token threshold above which older step_results are compacted.
        ``None`` uses the CompactionEngine's ``effective_trigger`` from
        ``ComputedBudgets`` (= derived from the router model context window).
    step_results_ratio:
        Fraction of ``main_pool`` (= T_max - T_SP) allocated for the
        step_results portion of the next step's sys_prompt.  Sibling to
        CompactionConfig.component_weights["body"].
    use_chars4_estimate:
        When True, use len(text)//4 for token estimation instead of
        litellm.token_counter (latency opt-out, mirrors CompactionConfig).
    """
    recent_step_results_raw: int = 3
    summarize_older_threshold_tokens: int | None = None
    step_results_ratio: float = 0.50
    use_chars4_estimate: bool = False


@dataclass
class PhaseActResultsCompactionConfig:
    """`phase.act_results_compaction:` — phase act-loop control_ir_results
    compaction policy. Sibling to CompactionConfig (chat) and
    PlannerStepCompactionConfig (planner step).

    When accumulated ``control_ir_results`` in a phase's act loop would push
    the next prompt over the model's effective context budget, older results
    (outside the ``recent_act_turns_raw`` window) are summarised by
    ``CompactionEngine`` using a phase-specific system prompt that preserves
    op-kind structured data (paths, line numbers, exit codes, etc.).

    Fields
    ------
    recent_act_turns_raw:
        Keep the last N act-turn results verbatim; compact older ones.
        Higher than PlannerStepCompactionConfig.recent_step_results_raw (= 3)
        because phase ops carry specific data the LLM needs to plan next ops.
        Default 5.
    control_ir_results_ratio:
        Fraction of ``main_pool`` (= T_max - T_SP) allocated for the
        control_ir_results portion of the act-loop context. Sibling to
        CompactionConfig.component_weights["body"].  Default 0.50.
    summarize_older_threshold_tokens:
        Total token threshold above which older results are compacted.
        ``None`` uses ``control_ir_results_ratio × main_pool`` derived from the
        engine's ComputedBudgets.
    use_chars4_estimate:
        When True, use len(text)//4 for token estimation instead of
        litellm.token_counter (latency opt-out, mirrors CompactionConfig).
    """
    recent_act_turns_raw: int = 5
    control_ir_results_ratio: float = 0.50
    summarize_older_threshold_tokens: int | None = None
    use_chars4_estimate: bool = False


def _build_reasoning_config(raw: object) -> ReasoningConfig:
    """#1652: parse ``chat.reasoning`` (continuity / display / recent_turns)."""
    defaults = ReasoningConfig()
    if not isinstance(raw, dict):
        return defaults
    return ReasoningConfig(
        continuity=bool(raw.get("continuity", defaults.continuity)),
        display=bool(raw.get("display", defaults.display)),
        # recent_turns: <=0 = unbounded (keep-all). int() coerces YAML scalars.
        recent_turns=int(raw.get("recent_turns", defaults.recent_turns)),
    )


def _build_chat_config(raw: object) -> ChatConfig:
    if not isinstance(raw, dict):
        return ChatConfig()
    # #1652: reasoning parses independently of compaction (a chat: block with
    # only `reasoning:` and no `compaction:` must still honour it).
    reasoning = _build_reasoning_config(raw.get("reasoning"))
    compaction_raw = raw.get("compaction") or {}
    if not isinstance(compaction_raw, dict):
        return ChatConfig(reasoning=reasoning)
    # #1128: head_size/tail_size (step 3) + trigger_total_tokens/min_compact_batch
    # (PR-a, axis-1 removal) were removed — head/tail sizing is token-budget via
    # component_weights and auto-compaction is window-relative (no turn-count
    # limit, no 30K-absolute background trigger). Warn on all four removed keys
    # so operators clean up their YAML symmetrically.
    _removed_compaction_keys = (
        "head_size", "tail_size", "trigger_total_tokens", "min_compact_batch",
    )
    if any(k in compaction_raw for k in _removed_compaction_keys):
        import warnings
        warnings.warn(
            "chat.compaction.head_size/tail_size/trigger_total_tokens/"
            "min_compact_batch are deprecated and ignored — removed in #1128. "
            "head/tail sizing is now token-budget via component_weights, and "
            "auto-compaction is window-relative. Remove these keys.",
            DeprecationWarning, stacklevel=2,
        )
    section_raw = compaction_raw.get("section_token_caps") or {}
    if not isinstance(section_raw, dict):
        section_raw = {}
    defaults_section = CompactionSectionCaps()
    section = CompactionSectionCaps(
        topic_arc=int(section_raw.get("topic_arc", defaults_section.topic_arc)),
        decisions=int(section_raw.get("decisions", defaults_section.decisions)),
        pending=int(section_raw.get("pending", defaults_section.pending)),
        session_user_facts=int(
            section_raw.get("session_user_facts", defaults_section.session_user_facts)
        ),
        artifacts_referenced=int(
            section_raw.get("artifacts_referenced", defaults_section.artifacts_referenced)
        ),
    )
    defaults = CompactionConfig()

    # PR-N6: parse component_weights dict (integer weights, sum-arbitrary).
    # YAML: chat.compaction.component_weights: {head: 10, body: 5, ...}
    raw_cw = compaction_raw.get("component_weights")
    if isinstance(raw_cw, dict):
        component_weights = {
            k: int(v) for k, v in raw_cw.items()
            if isinstance(v, (int, float))
        }
        # Fill any missing keys from defaults.
        for k, v in defaults.component_weights.items():
            component_weights.setdefault(k, v)
    else:
        component_weights = dict(defaults.component_weights)

    # PR-N6: parse section_weights dict.
    # YAML: chat.compaction.section_weights: {decisions: 40, ...}
    raw_sw = compaction_raw.get("section_weights")
    if isinstance(raw_sw, dict):
        section_weights = {
            k: int(v) for k, v in raw_sw.items()
            if isinstance(v, (int, float))
        }
        for k, v in defaults.section_weights.items():
            section_weights.setdefault(k, v)
    else:
        section_weights = dict(defaults.section_weights)

    compaction = CompactionConfig(
        component_weights=component_weights,
        section_weights=section_weights,
        section_caps_spec_tokens=int(
            compaction_raw.get("section_caps_spec_tokens", defaults.section_caps_spec_tokens)
        ),
        use_chars4_estimate=bool(
            compaction_raw.get("use_chars4_estimate", defaults.use_chars4_estimate)
        ),
        body_token_cap=int(compaction_raw.get("body_token_cap", defaults.body_token_cap)),
        resummarize_passes=int(
            compaction_raw.get("resummarize_passes", defaults.resummarize_passes)
        ),
        section_token_caps=section,
    )
    return ChatConfig(compaction=compaction, reasoning=reasoning)


def _build_phase_act_results_compaction_config(
    raw: object,
) -> "PhaseActResultsCompactionConfig":
    """Parse ``phase.act_results_compaction:`` sub-block.

    Missing / non-dict block returns defaults.  Unknown keys are ignored
    (forward-compat).
    """
    defaults = PhaseActResultsCompactionConfig()
    if not isinstance(raw, dict):
        return defaults

    recent_raw = raw.get("recent_act_turns_raw")
    try:
        recent = int(recent_raw) if recent_raw is not None else defaults.recent_act_turns_raw
    except (TypeError, ValueError):
        recent = defaults.recent_act_turns_raw
    if recent < 0:
        recent = defaults.recent_act_turns_raw

    threshold_raw = raw.get("summarize_older_threshold_tokens")
    if threshold_raw is None:
        threshold: int | None = None
    else:
        try:
            threshold = int(threshold_raw)
            if threshold <= 0:
                threshold = None
        except (TypeError, ValueError):
            threshold = None

    ratio_raw = raw.get("control_ir_results_ratio")
    try:
        ratio = float(ratio_raw) if ratio_raw is not None else defaults.control_ir_results_ratio
    except (TypeError, ValueError):
        ratio = defaults.control_ir_results_ratio
    if not (0.0 < ratio <= 1.0):
        ratio = defaults.control_ir_results_ratio

    use_chars4 = bool(raw.get("use_chars4_estimate", defaults.use_chars4_estimate))

    return PhaseActResultsCompactionConfig(
        recent_act_turns_raw=recent,
        control_ir_results_ratio=ratio,
        summarize_older_threshold_tokens=threshold,
        use_chars4_estimate=use_chars4,
    )


def _build_plan_step_compaction_config(raw: object) -> "PlannerStepCompactionConfig":
    """Parse ``plan.step_compaction:`` sub-block.

    Missing / non-dict block returns defaults.  Unknown keys are ignored
    (forward-compat).
    """
    defaults = PlannerStepCompactionConfig()
    if not isinstance(raw, dict):
        return defaults

    recent_raw = raw.get("recent_step_results_raw")
    try:
        recent = int(recent_raw) if recent_raw is not None else defaults.recent_step_results_raw
    except (TypeError, ValueError):
        recent = defaults.recent_step_results_raw
    if recent < 0:
        recent = defaults.recent_step_results_raw

    threshold_raw = raw.get("summarize_older_threshold_tokens")
    if threshold_raw is None:
        threshold: int | None = None
    else:
        try:
            threshold = int(threshold_raw)
            if threshold <= 0:
                threshold = None
        except (TypeError, ValueError):
            threshold = None

    ratio_raw = raw.get("step_results_ratio")
    try:
        ratio = float(ratio_raw) if ratio_raw is not None else defaults.step_results_ratio
    except (TypeError, ValueError):
        ratio = defaults.step_results_ratio
    if not (0.0 < ratio <= 1.0):
        ratio = defaults.step_results_ratio

    use_chars4 = bool(raw.get("use_chars4_estimate", defaults.use_chars4_estimate))

    return PlannerStepCompactionConfig(
        recent_step_results_raw=recent,
        summarize_older_threshold_tokens=threshold,
        step_results_ratio=ratio,
        use_chars4_estimate=use_chars4,
    )


# ── FP-0004: safety: section parsers ───────────────────────────────────────


def _build_safety_config(raw: object) -> SafetyConfig:
    """Parse the user-facing ``safety:`` section.

    Empty / missing returns full defaults. Unknown / malformed values
    fall back to defaults silently — config-level errors should not
    abort startup (logger.warning is the convention used elsewhere).
    """
    if not isinstance(raw, dict):
        return SafetyConfig()
    loop_raw = raw.get("loop") or {}
    if not isinstance(loop_raw, dict):
        loop_raw = {}
    timeout_raw = raw.get("timeout") or {}
    if not isinstance(timeout_raw, dict):
        timeout_raw = {}
    on_limit_raw = raw.get("on_limit") or {}
    if not isinstance(on_limit_raw, dict):
        on_limit_raw = {}

    loop_defaults = LoopConfig()
    timeout_defaults = TimeoutConfig()

    loop = LoopConfig(
        max_act_turns_per_phase=int(loop_raw.get(
            "max_act_turns_per_phase", loop_defaults.max_act_turns_per_phase,
        )),
        max_phase_visits=int(loop_raw.get(
            "max_phase_visits", loop_defaults.max_phase_visits,
        )),
        max_router_calls_per_turn=int(loop_raw.get(
            "max_router_calls_per_turn", loop_defaults.max_router_calls_per_turn,
        )),
        max_agent_hops=int(loop_raw.get(
            "max_agent_hops", loop_defaults.max_agent_hops,
        )),
        max_router_iterations=int(loop_raw.get(
            "max_router_iterations", loop_defaults.max_router_iterations,
        )),
        max_tool_calls_per_turn=int(loop_raw.get(
            "max_tool_calls_per_turn", loop_defaults.max_tool_calls_per_turn,
        )),
        skill_calls_per_chain=_build_cost_limit(
            loop_raw.get("skill_calls_per_chain")
        ),
        skill_tokens_per_chain=_build_cost_limit(
            loop_raw.get("skill_tokens_per_chain")
        ),
        plan_invalid_retries=int(loop_raw.get(
            "plan_invalid_retries", loop_defaults.plan_invalid_retries,
        )),
    )
    timeout = TimeoutConfig(
        llm_call_seconds=float(timeout_raw.get(
            "llm_call_seconds", timeout_defaults.llm_call_seconds,
        )),
        llm_max_retries=int(timeout_raw.get(
            "llm_max_retries", timeout_defaults.llm_max_retries,
        )),
        phase_seconds=float(timeout_raw.get(
            "phase_seconds", timeout_defaults.phase_seconds,
        )),
        chain_seconds=float(timeout_raw.get(
            "chain_seconds", timeout_defaults.chain_seconds,
        )),
    )
    on_limit_defaults = OnLimitConfig()
    mode_raw = str(on_limit_raw.get("mode", on_limit_defaults.mode))
    if mode_raw not in ON_LIMIT_MODES:
        import logging
        logging.getLogger(__name__).warning(
            "safety.on_limit.mode=%r is not one of %s; using %r",
            mode_raw, ON_LIMIT_MODES, on_limit_defaults.mode,
        )
        mode_raw = on_limit_defaults.mode
    auto_extend_times_raw = on_limit_raw.get(
        "auto_extend_times", on_limit_defaults.auto_extend_times,
    )
    try:
        auto_extend_times = int(auto_extend_times_raw)
        if auto_extend_times < 0:
            auto_extend_times = on_limit_defaults.auto_extend_times
    except (TypeError, ValueError):
        auto_extend_times = on_limit_defaults.auto_extend_times
    ask_timeout_seconds_raw = on_limit_raw.get(
        "ask_timeout_seconds", on_limit_defaults.ask_timeout_seconds,
    )
    try:
        ask_timeout_seconds = float(ask_timeout_seconds_raw)
        if ask_timeout_seconds < 0:
            ask_timeout_seconds = on_limit_defaults.ask_timeout_seconds
    except (TypeError, ValueError):
        ask_timeout_seconds = on_limit_defaults.ask_timeout_seconds
    on_limit = OnLimitConfig(
        mode=mode_raw,  # type: ignore[arg-type]
        auto_extend_times=auto_extend_times,
        ask_timeout_seconds=ask_timeout_seconds,
    )
    threat_scan_raw = raw.get("threat_scan") or {}
    if not isinstance(threat_scan_raw, dict):
        threat_scan_raw = {}
    ts_defaults = ThreatScanConfig()
    custom_patterns_raw = threat_scan_raw.get("custom_patterns", ts_defaults.custom_patterns)
    threat_scan = ThreatScanConfig(
        enabled=bool(threat_scan_raw.get("enabled", ts_defaults.enabled)),
        fail_open=bool(threat_scan_raw.get("fail_open", ts_defaults.fail_open)),
        fence_enabled=bool(threat_scan_raw.get("fence_enabled", ts_defaults.fence_enabled)),
        block_severity=str(threat_scan_raw.get("block_severity", ts_defaults.block_severity)),
        custom_patterns=list(custom_patterns_raw) if isinstance(custom_patterns_raw, list) else list(ts_defaults.custom_patterns),
    )
    return SafetyConfig(
        loop=loop, timeout=timeout, on_limit=on_limit, threat_scan=threat_scan,
    )


def _build_cost_warn_config(raw: object) -> "CostWarnConfig":
    """Parse the ``cost_warn:`` section (#1830 / FP-0052).

    Missing or malformed → full defaults (enabled=True, threshold=$5/1M).
    """
    if not isinstance(raw, dict):
        return CostWarnConfig()
    defaults = CostWarnConfig()
    enabled = raw.get("enabled", defaults.enabled)
    threshold = raw.get(
        "model_threshold_per_1m_input_usd",
        defaults.model_threshold_per_1m_input_usd,
    )
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = defaults.model_threshold_per_1m_input_usd
    return CostWarnConfig(
        enabled=bool(enabled),
        model_threshold_per_1m_input_usd=threshold,
    )


def _build_cost_limit(raw: object) -> CostLimitConfig:
    if not isinstance(raw, dict):
        return CostLimitConfig()
    hard = raw.get("hard_limit")
    if hard is not None:
        try:
            hard = float(hard)
        except (TypeError, ValueError):
            hard = None
    warn_ratio = raw.get("warn_ratio", 0.8)
    try:
        warn_ratio = float(warn_ratio)
    except (TypeError, ValueError):
        warn_ratio = 0.8
    # FP-0003: opt-in user-approval flow on hard-limit hit.
    ask_on_exceed = bool(raw.get("ask_on_exceed", False))
    extension_calls_raw = raw.get("extension_calls", 0)
    try:
        extension_calls = int(extension_calls_raw)
    except (TypeError, ValueError):
        extension_calls = 0
    if extension_calls < 0:
        extension_calls = 0
    return CostLimitConfig(
        hard_limit=hard,
        warn_ratio=warn_ratio,
        ask_on_exceed=ask_on_exceed,
        extension_calls=extension_calls,
    )
def _build_cost_config(raw: object) -> CostConfig:
    if not isinstance(raw, dict):
        return CostConfig()
    rate_raw = raw.get("rate_limit_per_minute") or {}
    rate: dict[str, int] = {}
    if isinstance(rate_raw, dict):
        for k, v in rate_raw.items():
            try:
                rate[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    warn_ratio = raw.get("rate_limit_warn_ratio", 0.8)
    try:
        warn_ratio = float(warn_ratio)
    except (TypeError, ValueError):
        warn_ratio = 0.8
    return CostConfig(
        per_agent_tokens=_build_cost_limit(raw.get("per_agent_tokens")),
        per_agent_cost_usd=_build_cost_limit(raw.get("per_agent_cost_usd")),
        rate_limit_per_minute=rate,
        rate_limit_warn_ratio=warn_ratio,
        # PR25: persistent daily / monthly quota
        daily_tokens=_build_cost_limit(raw.get("daily_tokens")),
        daily_cost_usd=_build_cost_limit(raw.get("daily_cost_usd")),
        monthly_tokens=_build_cost_limit(raw.get("monthly_tokens")),
        monthly_cost_usd=_build_cost_limit(raw.get("monthly_cost_usd")),
    )
