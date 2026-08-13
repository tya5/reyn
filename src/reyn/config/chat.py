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
        max_router_calls_per_turn:
            Cap on chat-router invocations within a single user turn.
            ``0`` = unlimited.
        max_agent_hops:
            Maximum delegation depth (= user → A → B → C is 3 hops).
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
        max_hook_driven_turns:
            #1800 slice 7: the loop valve. Caps hook self-continuation — an
            ``E`` (wake=true) hook firing at ``turn_end`` triggers a new turn,
            which can fire another, … This bounds that chain: each
            hook-originated (``kind="hook"``) turn counts 1; the counter resets
            on each human user turn (``kind="user"`` re-arms the budget). When
            the count would exceed the cap the next hook turn hits the
            ``safety.on_limit`` checkpoint (warn → ask_user → abort) instead of
            running. A backstop only — does NOT obstruct intentional
            loop-engineering (the operator raises the cap). ``0`` = unlimited.
    """

    max_router_calls_per_turn: int = 3
    max_agent_hops: int = 3
    max_router_iterations: int = 5
    max_tool_calls_per_turn: int = 50
    max_hook_driven_turns: int = 25


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
        chain_seconds:
            How long a multi-agent pending chain waits for a delegate
            reply before the runtime synthesises an upstream error.
            ``0`` (or any non-positive value) disables.
        mcp_probe_seconds:
            #3475: per-server timeout for the MCP tools-list probe
            (`RouterHostAdapter.ensure_mcp_tools_cached` / the CLI's
            `reyn mcp refresh`). A server slower than this is NOT cached
            at all (#3520: a timed-out probe measured nothing, so there is
            no answer to cache — it used to be recorded as an empty tool
            list, which the model then read as "this server has no tools"
            for the rest of the session and beyond) and an
            `mcp_tool_probe_degraded` audit-event is emitted naming which
            server and why. The server is re-probed on the next turn;
            raising this value is how you stop paying that cost every turn
            on a legitimately slow server. THE default (``5.0``) — the two call sites
            derive their own defaults from this field rather than
            repeating the literal, so raising this one number is the only
            operator action needed to widen the budget under co-located
            CPU load; it does not itself change the default.
    """

    llm_call_seconds: float = 60.0
    llm_max_retries: int = 3
    chain_seconds: float = 60.0
    mcp_probe_seconds: float = 5.0


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
    "limit ごとの適用可否" (router_cap, max_agent_hops, chain_seconds).
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


# ``safety.threat_scan.capability_narrowing`` — the untrusted-content CAPABILITY
# narrowing, as one ordered ladder (#3501):
#
# - ``off`` (default) — the narrowing never engages. An agent keeps the
#   capabilities it started the session with, whatever enters its context.
# - ``turn`` — while external content is live in the active context, the
#   ``_untrusted`` profile is applied, resolved once per turn.
# - ``iteration`` — as ``turn``, and re-resolved at every router-loop iteration, so
#   external content arriving in round N narrows dispatch in round N+1 of the SAME
#   turn (closes the same-turn injection window). Monotonic within a turn: a
#   compaction that evicts the tainted entry mid-turn does not restore the
#   capability until the turn ends, so the taint cannot be laundered away.
#
# ONE setting, not an enable flag plus a granularity flag (#3501): two booleans can
# express "re-narrow every iteration, but do not narrow", which is not a state the
# runtime has. The ladder is strictly increasing in strictness, so an operator
# picking a level never has to reason about interaction.
#
# ``off`` is the default because the narrowing removes capabilities MID-SESSION for
# a reason nothing tells the agent — the owner-reported symptom was "it worked at
# the start of the session and then suddenly did not, and the LLM could not explain
# why". Predictability is the default; the hardening is opted into.
CAPABILITY_NARROWING_MODES = ("off", "turn", "iteration")


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
    - ``capability_narrowing`` — the CAPABILITY half of the same defense (the
      ``_untrusted`` profile), one ladder of three settings. See
      ``CAPABILITY_NARROWING_MODES`` below.
    """
    enabled: bool = True
    fail_open: bool = True
    fence_enabled: bool = True
    block_severity: str = "block"
    custom_patterns: list = field(default_factory=list)
    capability_narrowing: str = "off"

    def __post_init__(self) -> None:
        if self.capability_narrowing not in CAPABILITY_NARROWING_MODES:
            raise ValueError(
                "safety.threat_scan.capability_narrowing must be one of "
                f"{list(CAPABILITY_NARROWING_MODES)}, got "
                f"{self.capability_narrowing!r}"
            )

    def narrowing_engaged(self) -> bool:
        """Whether the untrusted-content capability narrowing runs at all."""
        return self.capability_narrowing != "off"

    def narrowing_per_iteration(self) -> bool:
        """Whether the narrowing is re-resolved every router-loop iteration.

        A second predicate rather than a second flag: ``iteration`` implies
        ``turn``, so one ordered setting cannot express the contradiction "narrow
        every iteration but do not narrow"."""
        return self.capability_narrowing == "iteration"


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
    - ``block_on_high_cost`` — #1867 (S4) opt-in: when True, a ``/model`` switch
      to a high-cost model is held for an interactive confirm via the unified
      safety framework (``handle_limit_exceeded``); the switch applies only on
      approval. Default False (warn-only — S1–S3 behaviour). A non-interactive
      session (no TTY) fail-closes (the switch is denied) since it cannot
      confirm. Session-startup stays warn-only regardless of this flag.
    """
    enabled: bool = True
    model_threshold_per_1m_input_usd: float = 5.0
    block_on_high_cost: bool = False


@dataclass
class OffloadConfig:
    """`offload:` — opt-in switch for the tool-result size gates
    (tool-result-schema-redesign §5).

    **Default OFF.** Offloading a large tool result to a file ref only helps if
    the model reads the ref back, and mid-tier models often don't — they act on
    the truncated preview, degrading the result. So by default every tool result
    is delivered to the model in full and the format (frontmatter + text) is the
    same either way. Opt in with ``enabled: true`` when you want the cost
    reduction of capping/offloading large tool results (e.g. when a single tool
    result is very large, opting in also prevents an oversized turn).

    ``enabled: true`` turns on all three gates: the text token cap
    (``cap_tool_result_content``), the structured inline gate
    (``STRUCTURED_INLINE_MAX_CHARS`` in ``build_offload_body``), and the media
    follow-up budget bound (``media_followup_budget`` — included so enabling
    the flag isn't confounded by media starvation from an un-gated budget).

    **The size bounds are operator-tunable (#3580).** They used to be module
    constants, so an operator who opted in got one fixed shape of capping and
    no way to say "cap, but not that aggressively" — the only lever was the
    boolean. Each field below keeps its previous constant as the default, so
    an existing ``enabled: true`` config behaves exactly as before.

    Fields (all apply only while ``enabled: true``):

    ``max_inline_bytes``
        Absolute ceiling on the inline preview left in the conversation after a
        text result is offloaded. Also the value the turn-budget's
        ``offload_cap`` reserve is derived from.
    ``preview_head_chars`` / ``preview_tail_chars``
        How much of the head and tail of the body that preview keeps. The body
        itself is never lost — it is stored and referenced.
    ``cap_ceil_tokens`` / ``cap_alpha``
        The per-turn token cap is ``min(cap_ceil_tokens, cap_alpha × effective_trigger)``.
        ``cap_alpha`` is the budget-relative term (so a small-context model still
        gets a compactable turn); ``cap_ceil_tokens`` clamps it so a large-context
        model does not get a huge inline.
    ``structured_inline_max_chars`` / ``structured_preview_chars``
        The same two questions for a STRUCTURED (dict/list) result: the size at
        which it goes to its own ref, and how much of it stays inline.

    #4381's resource-vs-budget ROLE split (see ``ReadCapConfig`` above): every
    field here is BUDGET-role — ``cap_ceil_tokens``/``cap_alpha`` are TOKENS,
    model-derived via ``effective_trigger``; ``max_inline_bytes``,
    ``preview_head_chars``/``preview_tail_chars``, and
    ``structured_inline_max_chars``/``structured_preview_chars`` name their
    own unit already (bytes/chars) but are still budget-role — they bound
    what stays in the conversation the model reads, not a resource-role
    physical ceiling like ``ReadCapConfig``'s. This is stated explicitly
    (#4431 follow-up) because the unit being IMPLICIT is exactly the shape
    #4381's loop happened in: two caps compared without either side naming
    its own ROLE or unit. Any cross-ROLE comparison against a resource-role
    value MUST go through ``context_builder.INLINE_CAP_BYTES_PER_TOKEN``,
    never a second independently-derived ratio.
    """
    enabled: bool = False
    max_inline_bytes: int = 16_384
    preview_head_chars: int = 6_000
    preview_tail_chars: int = 2_000
    cap_ceil_tokens: int = 4_096
    cap_alpha: float = 0.5
    structured_inline_max_chars: int = 2_000
    structured_preview_chars: int = 600


def _build_offload_config(raw: object) -> "OffloadConfig":
    """Parse the ``offload:`` section. Missing/malformed -> defaults (enabled=False, opt-in).

    Each size field falls back to its own default independently, so a config
    that sets only one of them keeps the shipped value for the rest.
    """
    if not isinstance(raw, dict):
        return OffloadConfig()
    d = OffloadConfig()

    def _int(key: str, fallback: int) -> int:
        try:
            return int(raw.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _float(key: str, fallback: float) -> float:
        try:
            return float(raw.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return OffloadConfig(
        enabled=bool(raw.get("enabled", d.enabled)),
        max_inline_bytes=_int("max_inline_bytes", d.max_inline_bytes),
        preview_head_chars=_int("preview_head_chars", d.preview_head_chars),
        preview_tail_chars=_int("preview_tail_chars", d.preview_tail_chars),
        cap_ceil_tokens=_int("cap_ceil_tokens", d.cap_ceil_tokens),
        cap_alpha=_float("cap_alpha", d.cap_alpha),
        structured_inline_max_chars=_int(
            "structured_inline_max_chars", d.structured_inline_max_chars
        ),
        structured_preview_chars=_int(
            "structured_preview_chars", d.structured_preview_chars
        ),
    )


@dataclass
class RenderTemplateConfig:
    """`render_template:` — operator-tunable output bounds for the ``render_template``
    op (FP-0055 / #2679).

    The op caps output DURING generation (a ``SandboxedEnvironment`` blocks SSTI but
    not resource exhaustion — a bounded loop like ``{% for i in range(10**9) %}``
    still floods). Safe defaults + a per-op-context override seam already ship; this
    section exposes the two bounds to operator yaml, mirroring ``offload:`` /
    ``cost_warn:``.

    - ``max_output_chars`` — the streaming char budget; the render truncates the
      moment cumulative output exceeds it. Default 256_000.
    - ``wall_clock_seconds`` — the elapsed-time backstop (Jinja2 exposes no iteration
      count, so wall-clock bounds a runaway loop that emits little text per step).
      Default 5.0.

    The defaults mirror ``op_runtime.render_template.RenderTemplateBounds`` (the
    in-handler fallback). Generous enough for real reports / configs, tight enough
    that a runaway generator stops quickly; an operator raises them for a large
    report or lowers them to harden a shared host.
    """
    max_output_chars: int = 256_000
    wall_clock_seconds: float = 5.0


def _build_render_template_config(raw: object) -> "RenderTemplateConfig":
    """Parse the ``render_template:`` section (#2679).

    Missing or malformed → full defaults (256_000 chars / 5.0s). A non-numeric or
    non-positive value for either bound falls back to that field's default (an
    operator typo must not silently disable the cap → a zero/negative bound would
    truncate everything or never fire).
    """
    if not isinstance(raw, dict):
        return RenderTemplateConfig()
    defaults = RenderTemplateConfig()

    max_output_chars = raw.get("max_output_chars", defaults.max_output_chars)
    try:
        max_output_chars = int(max_output_chars)
        if max_output_chars <= 0:
            max_output_chars = defaults.max_output_chars
    except (TypeError, ValueError):
        max_output_chars = defaults.max_output_chars

    wall_clock_seconds = raw.get("wall_clock_seconds", defaults.wall_clock_seconds)
    try:
        wall_clock_seconds = float(wall_clock_seconds)
        if wall_clock_seconds <= 0:
            wall_clock_seconds = defaults.wall_clock_seconds
    except (TypeError, ValueError):
        wall_clock_seconds = defaults.wall_clock_seconds

    return RenderTemplateConfig(
        max_output_chars=max_output_chars,
        wall_clock_seconds=wall_clock_seconds,
    )


@dataclass
class ReadCapConfig:
    """`read_cap:` — the RESOURCE-BOUND per-result inline cap (#4381 PR-5,
    architect design).

    Shares ONE cap value across ``file.py``'s read op and ``load_skill.py``
    (both call ``context_builder.control_ir_inline_cap`` — architect: "同じ
    整理が当たり... read_file と同じ扱いへ一緒に移る").

    **Unit is BYTES, not characters** — the architect's own correction:
    "文字は資源境界の単位として使わないこと。多バイト文字で8192文字≒24KBに
    なり、資源を守る量として3倍ぶれる." A resource bound protects memory/
    transfer/disk, which are byte-denominated regardless of encoding; a
    char-denominated cap drifts against that by up to ~3x for non-ASCII
    content (exactly the drift #4381 traced).

    **Model-INDEPENDENT** (owner ruling) — this used to scale with the
    resolved model's context window (#1209's window-derive), but a resource
    bound protects a fixed physical resource, not a model-relative budget;
    scaling it by model window conflated the two ROLEs #4381's design
    closes (resource bound = bytes/model-independent/config;
    budget bound = tokens/model-derived — see ``context_builder.
    INLINE_CAP_BYTES_PER_TOKEN``, the ONE named conversion point between
    them, consulted by ``router_history_buffer._check_resource_within_
    budget``, PR-1/#4451).

    **Why 10 KiB is the shipped default, not the prior 8 KB floor's own
    number carried forward** (architect, #4381): a cap's affordable size is
    a function of whether a truncated read has a way BACK IN, not of
    "what other tools use." Both consumers gained a resume mechanism the
    same night this design was written — ``read_file`` via ``char_offset``
    (#4432 wired the schema all the way to the op layer) and
    ``load_skill`` via deferring to ``read_file(path, offset=next_offset)``
    instead of inventing its own offset (#4441) — so a truncated read here
    loses at most ONE round-trip, not the rest of the content. That is the
    property that justifies a SMALL cap; it is checkable in THIS repo's
    own code (both resume paths exist and are tested), not an external
    claim like "another tool uses N" that a reader could dispute without
    being able to verify it against reyn's own behaviour.
    """
    inline_bytes: int = 10_240   # 10 KiB


def _build_read_cap_config(raw: object) -> "ReadCapConfig":
    """Parse the `read_cap:` section (#4381 PR-5).

    Missing or malformed -> default (10_240 bytes). A non-numeric or
    non-positive value falls back to the default -- same discipline as
    ``_build_render_template_config``: an operator typo must not silently
    disable the cap (zero/negative would truncate everything or never fire).
    """
    if not isinstance(raw, dict):
        return ReadCapConfig()
    defaults = ReadCapConfig()
    inline_bytes = raw.get("inline_bytes", defaults.inline_bytes)
    try:
        inline_bytes = int(inline_bytes)
        if inline_bytes <= 0:
            inline_bytes = defaults.inline_bytes
    except (TypeError, ValueError):
        inline_bytes = defaults.inline_bytes
    return ReadCapConfig(inline_bytes=inline_bytes)


@dataclass
class HistoryResidentConfig:
    """`history_resident:` — a RESOURCE-BOUND cap on ``Session.history``'s
    in-memory footprint (#4387 Phase B ③, applying #4431's role split).

    **Unit is BYTES** — the resource role (#4431: "資源 role = bytes, model
    非依存, config で可変"), matching ``ReadCapConfig.inline_bytes``'s own
    unit for the identical reason: a resource bound protects a fixed
    physical quantity (process memory), which is byte-denominated
    regardless of any model's context window.

    **Deliberately NOT the same axis as ``_HISTORY_HYDRATE_MIN_LINES``**
    (``session.py``'s on-demand backward-read granularity, in LINES) — #4387's
    own architect review named conflating the two as the risk to avoid
    ("窓の定義が2つになる...混ぜると3つ目の単位が増える"). This cap bounds
    how much stays RESIDENT; the hydrate window bounds how much is read PER
    on-demand fetch. Two different questions, two different units, kept
    separate on purpose.

    **What this does NOT claim**: owner reported a ~6GB memory ceiling: this
    config gives ``self.history`` a bound where it previously had none
    (six-question checklist ⑤ — nobody gave it a limit), but whether
    ``history`` was the ~6GB's actual majority contributor was explicitly
    left UNMEASURED (#4387's own body says so) and remains so. This closes
    the "unbounded growth" defect on its own terms, independent of that
    open question.

    Default is generous (256 MiB) — the whole point (per #4387/#4431's
    architect derivation) is that eviction is NOT information loss: anything
    evicted stays durable in ``history.jsonl`` and reloads on demand via the
    already-shipped backward-hydrate path (#4400/#4411), so a large default
    costs nothing but memory an operator can already afford, while a too-small
    one would make ordinary scrollback/rewind pay for reloads more often than
    necessary.
    """
    max_bytes: int = 256 * 1024 * 1024  # 256 MiB


def _build_history_resident_config(raw: object) -> "HistoryResidentConfig":
    """Parse the `history_resident:` section (#4387 Phase B ③).

    Missing or malformed -> default (256 MiB). A non-numeric or non-positive
    value falls back to the default — same discipline as
    ``_build_read_cap_config``: an operator typo must not silently disable
    the cap (zero/negative would evict everything or never fire)."""
    if not isinstance(raw, dict):
        return HistoryResidentConfig()
    defaults = HistoryResidentConfig()
    max_bytes = raw.get("max_bytes", defaults.max_bytes)
    try:
        max_bytes = int(max_bytes)
        if max_bytes <= 0:
            max_bytes = defaults.max_bytes
    except (TypeError, ValueError):
        max_bytes = defaults.max_bytes
    return HistoryResidentConfig(max_bytes=max_bytes)


@dataclass
class ImageConfig:
    """`image:` — operator-tunable inline image render bounds (#4474).

    ``row_height_cells`` — the FIXED height, in terminal rows, every
    `present`-rendered image (reyn's own ``HalfBlockImage`` renderable) is
    shown at — width is derived FROM this height and the image's own
    pixel aspect ratio (see ``interfaces/repl/present_renderer.py``'s
    ``decode_image_body``); ``HalfBlockImage`` takes an explicit
    width/height in cells with no aspect-ratio derivation of its own, so a
    fixed height is what makes aspect-ratio-correct rendering possible at
    all.

    **Why this is operator-configurable, not a bare constant** (owner's
    standing rule — no unjustified number embedded without either a
    reasoning comment or a user-facing override): the "right" row count is
    a function of the OPERATOR'S OWN terminal height and how much
    scrollback real estate they want a photo to occupy — a property this
    repo cannot decide FOR every operator's environment. 20 is a shipped
    default (tall enough to show real photo detail, short enough not to
    dominate a typical terminal's scrollback), not a measured "correct"
    number — the config key exists specifically so an operator on a
    short terminal (or one who wants larger previews) can change it
    without a code edit.
    """
    row_height_cells: int = 20


def _build_image_config(raw: object) -> "ImageConfig":
    """Parse the `image:` section (#4474).

    Missing or malformed -> default (20 rows). A non-numeric or
    non-positive value falls back to the default -- same discipline as
    ``_build_read_cap_config``: an operator typo must not silently produce
    a zero/negative row height (which would either draw nothing or invert
    the layout math downstream).
    """
    if not isinstance(raw, dict):
        return ImageConfig()
    defaults = ImageConfig()
    row_height_cells = raw.get("row_height_cells", defaults.row_height_cells)
    try:
        row_height_cells = int(row_height_cells)
        if row_height_cells <= 0:
            row_height_cells = defaults.row_height_cells
    except (TypeError, ValueError):
        row_height_cells = defaults.row_height_cells
    return ImageConfig(row_height_cells=row_height_cells)


@dataclass
class SpawnConfig:
    """`safety.spawn:` — operator bounds on the LLM spawn tree (#2103 C3).

    A DoS guard: the LLM spawn primitives (``spawn_agent`` create a child,
    ``create_topology`` wire an org) must not let an agent mint an unbounded
    spawn tree. These caps are **operator-set in reyn.yaml** (the restart-only OUT
    layer) — an LLM has no runtime path to raise its own limit (a self-raisable
    limit is no limit). Enforced at the LLM spawn SEAMS (host adapter): an operator
    creating agents via the CLI is unbounded (authority), consistent with the C1
    subtree forge-guard scope.

    Defense-by-default: non-zero defaults protect out of the box (there is no
    backward-compat spawn-tree to break — the primitives are new in #2103). Raise
    them in reyn.yaml when an org legitimately needs a deeper / wider tree.

    Fields:
        max_depth:
            Maximum spawn-LINEAGE chain depth (operator-top = 0; a child spawned
            under it = 1; …). A spawn that would make the new child's depth exceed
            this is rejected. ``0`` = unlimited.
        max_children:
            Maximum FAN-OUT. Governs BOTH (a) the number of direct spawn-children a
            single parent may have (``spawn_agent``) AND (b) the member count of a
            ``create_topology``d topology (org size). A spawn/wire that would exceed
            it is rejected. ``0`` = unlimited.
        max_pipeline_fan_out_depth:
            Pipeline S5 guard (b): the maximum NESTING depth of ``for_each``
            fan-out scopes (a top-level for_each = depth 1; a for_each inside
            another for_each's ``do``/``collect`` = depth 2; …). A for_each that
            would exceed this fails the step rather than spawning. Distinct from
            ``max_depth`` (the spawn-LINEAGE chain): a pipeline agent-step reaches
            ``spawn_ephemeral_session`` with no lineage, so ``max_depth`` does not
            cover fan-out — this is the fan-out-nesting bound. ``0`` = unlimited.
        max_pipeline_spawns:
            Pipeline S5 guard (c): the maximum number of ephemeral sessions ONE
            pipeline run may spawn across ALL its ``agent`` steps (top-level or
            fanned out via ``for_each``). The ONLY spawn-COUNT enforcement for
            pipeline agent-steps (they carry no spawn lineage, so ``max_children``
            does not cover them). A per-run monotonic counter; a spawn past the cap
            fails the step. ``0`` = unlimited.
    """

    max_depth: int = 10
    max_children: int = 20
    # Pipeline S5 fan-out spawn bounds (#2187 for_each). Conservative finite
    # defaults; 0 = unlimited (operator opt-out).
    max_pipeline_fan_out_depth: int = 5
    max_pipeline_spawns: int = 100


@dataclass
class SafetyConfig:
    """`safety:` — unified, user-facing namespace for stop conditions.

    Reyn stops a run for one of three reasons: a loop was detected, a
    timeout fired, or the budget was exceeded. The first two are grouped
    under ``safety.loop`` / ``safety.timeout``; budget caps stay under
    ``cost:`` because they are financial knobs (per-agent / daily /
    monthly token + USD limits) rather than runaway-detection knobs.

    See ``docs/reference/config/budget.md`` for
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
    spawn: SpawnConfig = field(default_factory=SpawnConfig)  # #2103 C3: spawn-tree bounds


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
    ``display`` — surface reasoning to the UI (TUI + web, collapsible).
      Opt-out to hide it. Independent of ``continuity``.
    ``recent_turns`` — how many recent turns' reasoning to replay under
      ``continuity``. ``<= 0`` (e.g. 0 / -1) = unbounded (keep all). Bounding
      matters on gemini (no provider auto-filter → reasoning is billed in full).
    """
    continuity: bool = True
    display: bool = True
    recent_turns: int = 3


# #3273 (#4223 removed ``inline``/``auto`` — owner instruction, 2026-08-11):
# the interactive chat renderer/driver selection. The Textual conversation-
# pane app has ONE driver mode (full-screen alt-screen); ``plain`` forces the
# ConsoleChatRenderer path even on a TTY.
#
# - ``alt-screen`` (DEFAULT): full-screen Textual (alt-screen driver).
#   Auto-saves/restores terminal scrollback on enter/exit.
# - ``plain``: force the plain ConsoleChatRenderer (no Textual), equivalent to
#   ``--cui`` (#3292: the renderer selection in ``chat.py`` forces this too,
#   not only the input-driver choice ``client_driver.resolve_render_mode``
#   makes — genuine equivalence, not a hybrid).
#
# #4223 removed the legacy bounded ``inline`` driver (upstream Textual bugs
# #3285/#3286 — reyn's own live-TTY integration reproduced #3286 but did NOT
# reproduce #3285 across 4+ resizes) and ``auto`` (behaviourally IDENTICAL to
# ``alt-screen`` given the TTY guard — a name-only third option, no distinct
# behaviour to preserve). An operator with a stale ``render_mode: inline`` or
# ``render_mode: auto`` in their config is not broken: :func:`_build_render_mode`
# below already warns-and-falls-back to ``alt-screen`` on any unrecognized
# value, the same graceful path an ordinary typo already took.
CHAT_RENDER_MODES = ("alt-screen", "plain")


@dataclass
class GutterConfig:
    """`chat.gutters:` — the TTY conversation pane's two gutter columns (#3352).

    The Textual conversation pane draws a LEFT gutter (the state-coloured
    marker, #3273 Phase 2) and a RIGHT gutter (per-entry elapsed / the turn's
    prompt+completion token split, #3283 Phase ④). Each costs a fixed number
    of columns on EVERY row, taken off the conversation body's width.

    These two flags are the STARTING state of each gutter when the pane
    mounts — the user can flip either one at runtime from the keyboard
    (``ctrl+g`` / ``ctrl+t``, see the app's ``BINDINGS``). A runtime toggle is
    SESSION-SCOPED by decision: it never writes back here, so a keypress can
    never silently rewrite the operator's ``reyn.yaml``. Set these to persist
    a preference across runs.

    Granularity follows the upstream contract exactly (two independent
    ``FlowView`` flags — ``left_gutter_visible`` / ``right_gutter_visible``);
    reyn does not invent a coarser or finer one.
    """
    left: bool = True
    right: bool = True


@dataclass
class ChatConfig:
    """`chat:` — chat-session-specific runtime knobs.

    ``gutters`` (#3352): the TTY conversation pane's per-side gutter start
    state — see :class:`GutterConfig`.

    ``render_mode`` (#3273, narrowed to 2 values by #4223): selects the
    interactive chat renderer/driver — ``alt-screen`` (default, full-screen)
    or ``plain`` (force ``ConsoleChatRenderer``, genuine ``--cui``
    equivalence, #3292). A non-TTY session always falls back to ``plain``
    regardless of this value (the interactive Textual driver needs a real
    terminal). See the ``CHAT_RENDER_MODES`` comment above for why the
    former ``inline``/``auto`` values were removed.

    ``neutralize_body`` (#3318): opt-in ESC/OSC-control-sequence stripping on
    the agent-reply / tool-result BODY text (owner ruling B — default OFF,
    "UX/predictability over security, security is opt-in"). This is a
    DISPLAY-boundary concern (what the terminal renders), not a stop
    condition, so it lives here rather than under ``safety:`` (the
    loop/timeout namespace). The label-side neutralization (#3302 —
    LLM-derived choice labels, intervention prompts) is unconditional and
    unaffected by this flag; this flag only widens that same terminal
    neutralizer to the conversation body text, where a raw ESC/OSC sequence
    from tool output or an untrusted model reply could otherwise reach the
    terminal.

    ``image_url_schemes`` (#3846, owner ruling C): opt-in narrowing of which
    URL schemes ``present``'s ``image`` component will fetch — default empty
    (unrestricted: both ``http`` and ``https`` are fetched, the owner's
    stated default: "even without the bytes, the record of what was
    presented is enough"). A non-empty list restricts to exactly those
    schemes (e.g. ``["https"]`` to reject plain ``http``); any scheme outside
    ``{"http", "https"}`` is always rejected regardless of this setting (an
    httpx client cannot fetch anything else). Same rationale as
    ``neutralize_body`` for living under ``chat:`` rather than ``safety:`` —
    this is a display-boundary narrowing, not a stop condition.
    """
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    reasoning: ReasoningConfig = field(default_factory=ReasoningConfig)
    render_mode: Literal["alt-screen", "plain"] = "alt-screen"
    gutters: GutterConfig = field(default_factory=GutterConfig)
    neutralize_body: bool = False
    image_url_schemes: "list[str]" = field(default_factory=list)


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


def _build_render_mode(raw: object) -> str:
    """#3273: parse ``chat.render_mode``. Unknown / non-str → default (warn)."""
    default = ChatConfig().render_mode
    if raw is None:
        return default
    mode = str(raw)
    if mode not in CHAT_RENDER_MODES:
        import logging
        logging.getLogger(__name__).warning(
            "chat.render_mode=%r is not one of %s; using %r",
            mode, CHAT_RENDER_MODES, default,
        )
        return default
    return mode


def _build_gutter_config(raw: object) -> GutterConfig:
    """#3352: parse ``chat.gutters`` (left / right start visibility)."""
    defaults = GutterConfig()
    if not isinstance(raw, dict):
        return defaults
    return GutterConfig(
        left=bool(raw.get("left", defaults.left)),
        right=bool(raw.get("right", defaults.right)),
    )


def _build_chat_config(raw: object) -> ChatConfig:
    if not isinstance(raw, dict):
        return ChatConfig()
    # #1652: reasoning parses independently of compaction (a chat: block with
    # only `reasoning:` and no `compaction:` must still honour it).
    reasoning = _build_reasoning_config(raw.get("reasoning"))
    # #3273: render_mode parses independently of compaction too.
    render_mode = _build_render_mode(raw.get("render_mode"))
    # #3352: so do the gutter start-visibility flags.
    gutters = _build_gutter_config(raw.get("gutters"))
    # #3318: so does the opt-in body-neutralize flag (default False/compat).
    neutralize_body = bool(raw.get("neutralize_body", ChatConfig().neutralize_body))
    # #3846: so does the opt-in image-src scheme allowlist (default []/unrestricted).
    raw_schemes = raw.get("image_url_schemes")
    image_url_schemes = (
        [str(s) for s in raw_schemes] if isinstance(raw_schemes, list) else []
    )
    compaction_raw = raw.get("compaction") or {}
    if not isinstance(compaction_raw, dict):
        return ChatConfig(  # type: ignore[arg-type]
            reasoning=reasoning, render_mode=render_mode, gutters=gutters,
            neutralize_body=neutralize_body, image_url_schemes=image_url_schemes,
        )
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
    return ChatConfig(
        compaction=compaction, reasoning=reasoning, render_mode=render_mode,  # type: ignore[arg-type]
        gutters=gutters, neutralize_body=neutralize_body,
        image_url_schemes=image_url_schemes,
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
        max_hook_driven_turns=int(loop_raw.get(
            "max_hook_driven_turns", loop_defaults.max_hook_driven_turns,
        )),
    )
    timeout = TimeoutConfig(
        llm_call_seconds=float(timeout_raw.get(
            "llm_call_seconds", timeout_defaults.llm_call_seconds,
        )),
        llm_max_retries=int(timeout_raw.get(
            "llm_max_retries", timeout_defaults.llm_max_retries,
        )),
        chain_seconds=float(timeout_raw.get(
            "chain_seconds", timeout_defaults.chain_seconds,
        )),
        mcp_probe_seconds=float(timeout_raw.get(
            "mcp_probe_seconds", timeout_defaults.mcp_probe_seconds,
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
        # Passed through as written so ThreatScanConfig.__post_init__ rejects a typo
        # (mirrors delegation.capability_default). A mis-typed security setting must
        # not silently resolve to a level the operator did not ask for — in either
        # direction: falling back to `off` would silently drop requested hardening.
        capability_narrowing=str(threat_scan_raw.get(
            "capability_narrowing", ts_defaults.capability_narrowing,
        )),
    )
    spawn_raw = raw.get("spawn") or {}
    if not isinstance(spawn_raw, dict):
        spawn_raw = {}
    spawn_defaults = SpawnConfig()
    spawn = SpawnConfig(
        max_depth=int(spawn_raw.get("max_depth", spawn_defaults.max_depth)),
        max_children=int(spawn_raw.get("max_children", spawn_defaults.max_children)),
        max_pipeline_fan_out_depth=int(spawn_raw.get(
            "max_pipeline_fan_out_depth", spawn_defaults.max_pipeline_fan_out_depth,
        )),
        max_pipeline_spawns=int(spawn_raw.get(
            "max_pipeline_spawns", spawn_defaults.max_pipeline_spawns,
        )),
    )
    return SafetyConfig(
        loop=loop, timeout=timeout, on_limit=on_limit, threat_scan=threat_scan,
        spawn=spawn,
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
    block_on_high_cost = raw.get("block_on_high_cost", defaults.block_on_high_cost)
    return CostWarnConfig(
        enabled=bool(enabled),
        model_threshold_per_1m_input_usd=threshold,
        block_on_high_cost=bool(block_on_high_cost),
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
    # FP-0005 (#1877): ``ask_on_exceed`` was subsumed into the unified
    # ``safety.on_limit`` 3-mode policy (clean-break, no shim). Warn an
    # operator who still sets the removed key so they migrate to
    # ``safety.on_limit.mode`` — safety config, so a silent drop is worse.
    if "ask_on_exceed" in raw:
        import warnings
        warnings.warn(
            "cost.*.ask_on_exceed is deprecated and ignored — removed in #1877. "
            "The cap exceed flow is now driven by "
            "safety.on_limit.mode (interactive / auto_extend / unattended). "
            "Remove this key; set safety.on_limit.mode instead.",
            DeprecationWarning, stacklevel=2,
        )
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
