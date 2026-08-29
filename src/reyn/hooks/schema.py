"""reyn.hooks.schema — typed models for hook definitions (#1800 slice A).

Defines ``HookDef`` (a single hook entry from the ``hooks:`` config block)
and ``PushBlock`` (the inline inbox-push sub-schema).  Template strings are
stored **raw** — rendering is a later slice.

Hook-point identifiers are normalised lowercase; the allowed set is the
starter set agreed in #1800 (skill_start/skill_end removed — never dispatched;
task_start/task_end removed with the internal task system, #2839 Phase 2):

    turn_start   turn_end
    session_start  session_end

#2608 H1 adds the first EXTERNAL-event hook-point, ``mcp_resource_updated``
(fired by a server-pushed MCP ``resources/updated`` notification on a
subscribed resource — see ``reyn.mcp.message_handler.on_resource_updated``
and ``reyn.mcp.connection_service.MCPConnectionService``'s bounded
sync->async bridge). Unlike the six lifecycle points above (fired from the
session/turn/task run-loop on the agent's own task), this point is fired
from the MCP receive-loop task via a bounded queue drained on the session's
event loop. ``HookDef.matcher`` stayed reserved/uninterpreted for this
point in H1 (scoping was via which resources the user subscribed to).

#2608 H2 interprets ``matcher``: a ``dict[str, str]`` of field -> pattern,
evaluated against the event's ``template_vars`` BEFORE the hook's action runs.
For ``mcp_resource_updated`` the two matchable fields are ``server`` (exact
match) and ``uri`` (glob via ``fnmatch``) — e.g. ``{"server": "github", "uri":
"file:///repo/**"}``. Absent/empty matcher -> fires always (unchanged for
every pre-H2 hook, lifecycle or external-event).

Hook-Event Redesign Phase 3 (proposal 0059 §10 Q-reyn-4): ``HookDispatcher.
dispatch`` no longer calls ``reyn.hooks.matcher.matches`` on ``hook.matcher``
directly — every ``matcher`` is wrapped into a payload-only ``EventPattern``
(``reyn.hooks.event_pattern.from_legacy_matcher``) and evaluated through
``reyn.hooks.event_pattern.matches`` (whose payload predicate still delegates
to the unchanged ``reyn.hooks.matcher.matches``, so every existing
``hooks.yaml`` entry's match semantics are byte-identical — this field's own
dict-of-field->pattern shape and validation, below, are unaffected). Phase 3
also makes an out-of-schema matcher field FAIL-LOUD at ``load_hooks`` time
(``HookConfigError``, typo-resistance against the Phase-1 Schema Registry)
rather than silently never matching at dispatch time.

#2608 H3 adds the 4th action, ``pipeline_launch`` — a hook can launch a
REGISTERED Pipeline (``reyn.core.pipeline.registry.PipelineRegistry.get``)
with an ``input`` built from the event payload (``PipelineLaunchBlock.
input_template``, Jinja2-rendered over the hook's ``template_vars`` — see
``reyn.hooks.render.render_pipeline_input``). Works from ANY hook-point
(the six lifecycle points and ``mcp_resource_updated``) since it dispatches
through the same ``HookDispatcher._dispatch_one`` scheme-branch as the other
three actions. Launch is ASYNC/detached (``reyn.runtime.session_api.
start_pipeline_run`` — the same call the ``run_pipeline_async`` tool verb
makes): the hook fires-and-continues, the pipeline runs in its own
recoverable driver-session, and the result arrives later on the hook's own
session inbox as a ``pipeline_result`` message.

#2608 H5 (LAST slice of the external-event->hooks arc) adds the final two
external-event points, ``cron_fired`` and ``webhook_received`` — completing
the source set alongside H1's ``mcp_resource_updated`` and H4's
``file_changed``. Unlike H1/H4 (a source running INSIDE the target session's
own process — an MCP receive-loop task / a watchdog thread bridged onto the
session's event loop), cron and webhook ingress run OUTSIDE any Session:
``reyn.runtime.cron.routing.resolve_cron_session`` /
``reyn.runtime.webhook_routing.resolve_webhook_session`` get-or-spawn the
target Session from the ``AgentRegistry`` at fire/request time. H5 therefore
reaches the resolved session's dispatcher through a new public accessor,
``Session.dispatch_external_event(point, template_vars)`` (see
``reyn.runtime.session``), called via ``reyn.hooks.external_fire.
fire_and_forget`` — a background ``asyncio.create_task`` wrapper so a slow
hook action (e.g. ``exec``) can never stall the cron job's own inbox
delivery or the webhook's HTTP response (see
``reyn.runtime.cron.routing.dispatch_cron_fired`` /
``reyn.runtime.webhook_routing.dispatch_webhook_received``). ``cron_fired``
carries ``{point, job_name, to}`` (all operator-config metadata, never
secret); ``webhook_received`` carries ONLY ``{point, transport, sender}`` —
deliberately NOT the raw inbound body/text, which may carry tokens/PII the
operator never intended a hook action to see. Matchable fields: ``job_name``
(cron, exact) / ``transport`` + ``sender`` (webhook, exact) — none of the
three are glob fields (only ``uri``/``path`` are, per ``hooks/matcher.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from reyn.hooks.schema_registry import BARE_TO_KIND

# ---------------------------------------------------------------------------
# Allowed hook-points (starter set — #1800 CONVERGED DESIGN)
# ---------------------------------------------------------------------------

# Hook-Event Redesign Phase 1 (proposal 0059 §2/§4): DERIVED from
# ``reyn.hooks.schema_registry.BARE_TO_KIND`` (the Schema Registry's builtin
# kind table) rather than hand-maintained here — a future builtin point (the
# proposal's "future point" list: pre/post_tool_use, pipeline_start/end) is
# added there (schema + one dispatch call site) and automatically becomes a
# recognised ``on:`` value here, with zero edits to this module. See
# ``schema_registry.py``'s own ``_LIFECYCLE_POINTS``/``_EXTERNAL_POINTS``/
# ``_TASK_POINTS`` for the current membership — deliberately not re-listed
# here, so this comment can't itself go stale the way it just did (it named
# a fixed 8-point enumeration that silently excluded ``task_settled``, #3978
# P3, once that landed).
# The registry's namespaced kind (e.g. ``builtin:lifecycle:turn_end``) is
# ALSO accepted in ``on:`` — a permanent alias of the bare form below,
# normalized by ``reyn.hooks.loader`` — but this frozenset (the internal
# bare-form key HookDef/HookRegistry/HookDispatcher use) is unchanged.
ALLOWED_HOOK_POINTS: frozenset[str] = frozenset(BARE_TO_KIND)


# ---------------------------------------------------------------------------
# Validation error
# ---------------------------------------------------------------------------


class HookConfigError(ValueError):
    """Raised when a ``hooks:`` entry fails structural validation.

    The message is decision-enabling: it names the offending entry index,
    the failing field, and a remediation hint so the operator can fix the
    config without reading source.
    """


# ---------------------------------------------------------------------------
# PushBlock — inbox-push sub-schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushBlock:
    """Inbox-push directive for a hook definition.

    Stores Jinja2 templates as **raw strings** (rendering is slice B).

    Fields
    ------
    message:
        Jinja2 template string that renders to the message content to push
        into the session inbox.  Required.
    wake:
        Controls whether the pushed message triggers a new turn (``True``)
        or rides along with the next scheduled turn (``False``).  May be a
        plain bool or a Jinja2 template string that renders to a bool.
        Default: ``True`` (the push-and-wake / self-continuation path,
        matching the dominant use-case E from the design).
    push_when:
        Optional Jinja2 template string that renders to a bool.  When
        ``False`` the push is skipped entirely (conditional push). Default
        ``"true"`` (always push).
    session:
        Optional Jinja2 template string or static session identifier.
        When absent the runtime will default to the current session.
    include:
        Names of hook-event payload fields to append VERBATIM after the
        rendered ``message`` — fenced, attributed, and NEVER passed through
        Jinja2 (proposal 0067 P2: "fenced and attributed, appended after
        the message, never interpolated into it"). This is the door for a
        field ``CONTEXT_UNSAFE_FIELDS`` excludes from ``message``
        interpolation to still reach the pushed text — content-carrying
        without letting the field's raw value drive template control flow
        (a Jinja2 conditional/loop keyed on operator-uncontrolled content).
        Default ``()`` (nothing appended, byte-identical to pre-P2
        behaviour for every existing config).
    """

    message: str
    wake: Union[bool, str] = True
    push_when: str = "true"
    session: str | None = None
    include: "tuple[str, ...]" = ()


# ---------------------------------------------------------------------------
# PipelineLaunchBlock — launch-a-registered-pipeline sub-schema (#2608 H3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineLaunchBlock:
    """Launch-a-registered-pipeline directive for a hook definition (#2608 H3).

    Fields
    ------
    name:
        The pipeline's registered name — resolved via
        ``PipelineRegistry.get(name)`` at dispatch time.  Required.
    input_template:
        Optional input for the launched pipeline, Jinja2-rendered against the
        hook's ``template_vars`` (see ``reyn.hooks.render.render_pipeline_input``
        for the exact rendering contract):

        - a ``dict``: every STRING leaf (recursively, through nested dicts/
          lists) is rendered as a Jinja2 template; the dict's structure and
          non-string leaves pass through unchanged.
        - a ``str``: rendered as ONE Jinja2 template whose output is parsed as
          a JSON object (mirrors the ``exec_capture`` stdout-is-JSON contract).
        - ``None`` (default): the pipeline launches with ``input=None``.
    """

    name: str
    input_template: "dict | str | None" = None


# ---------------------------------------------------------------------------
# HookDef — the top-level hook entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookDef:
    """A single lifecycle hook definition.

    Exactly one of ``template_push`` / ``exec`` / ``exec_capture`` /
    ``pipeline_launch`` must be set (validated by the loader, not by the
    dataclass itself — the dataclass is a plain data container). The
    consistent ``<source>_<action>`` keywords (#2069 converged design;
    #2608 H3 adds ``pipeline_launch``; #3226 Phase 4 renames the two shell
    actions ``shell_exec``/``shell_push`` → ``exec``/``exec_capture`` — a
    naming-honesty fix, NOT a security one: ``reyn.hooks.shell_runner`` never
    ran ``/bin/sh -c <string>`` — it always ``shlex.split`` a command into
    argv and executed with ``shell=False``. The ``shell_`` prefix was a
    misnomer; #3226 Phase 4 also collapses the payload to **argv-list-only**
    (a ``list[str]``, stored as ``tuple[str, ...]`` since ``HookDef`` is
    frozen) — the pre-Phase-4 shell-command STRING shape is gone, a clean
    break, not a compat alias. See ``docs/concepts/runtime/hooks.md`` §
    "exec / exec_capture" for the operator-facing migration note).

    Fields
    ------
    on:
        Hook-point name — one of ``ALLOWED_HOOK_POINTS``.
    name:
        Optional operator label for the hook (#1800 slice 6). Surfaced as the
        ``[hook:<name>]`` attribution prefix on a push. **Absent → the dispatcher
        defaults it to the hook-point** (``on``), preserving slice-5b behavior.
    template_push:
        Declarative inbox-push block from config Jinja2 templates (C/E). The
        push directive is computed from the template against event/context.
        Mutually exclusive with the other actions.
    exec:
        Argv (``tuple[str, ...]``) run as a pure side-effect — **output
        IGNORED**. Executed directly (``shell=False``, no shell
        interpretation) via the same sandbox backend ``sandboxed_exec`` uses.
        Mutually exclusive with the other actions. (Renamed from
        ``shell_exec`` in #3226 Phase 4 — naming honesty only, the execution
        mechanism is unchanged.)
    exec_capture:
        Argv (``tuple[str, ...]``) whose **stdout is a JSON push-directive**
        (``{push_when, wake, message, session?}``, #2069) → pushed via the same
        C/E dispatch path as ``template_push``. Mutually exclusive with the
        other actions. (Renamed from ``shell_push`` in #3226 Phase 4 — naming
        honesty only, the execution mechanism is unchanged.)
    pipeline_launch:
        Launch a registered Pipeline (#2608 H3) with an input built from the
        event payload — see ``PipelineLaunchBlock``. Async/detached (the
        launched pipeline runs in its own driver-session; the result arrives
        later on this session's inbox as a ``pipeline_result`` message).
        Mutually exclusive with the other actions.
    matcher:
        Optional ``dict[str, str]`` filter (#2608 H2) — a hook fires only when
        every named field matches the event's ``template_vars``: exact string
        equality for every field except ``uri`` (glob via ``fnmatch``). See
        ``reyn.hooks.matcher.matches`` for the match semantics and
        ``reyn.hooks.dispatcher.HookDispatcher.dispatch`` for where it's
        applied (before the hook's action runs). Absent/empty -> always fires
        (unchanged for every hook that predates H2). ``cron_fired``'s
        ``action`` field (#5209, ``"message"``/``"hook"``) is also exact-match
        here, same as ``job_name`` — not a glob field either.
    subprocess:
        OPERATOR-declared per-hook sandbox knob (#2827): may this hook's exec
        argv spawn children? Only meaningful for ``exec`` / ``exec_capture``
        (the loader rejects it on the other schemes rather than silently
        ignoring a security field — the #2976 eager-rejection model).

        ``None`` = omitted = keep the floor (``False``, today's behaviour); an
        explicit ``true``/``false`` is the operator's expressed will. This is
        the #2964 principle applied per-hook: *the default is a floor the
        operator ADDS to; only an explicit write is the operator's will* —
        hence ``bool | None``, not a bare ``bool`` that cannot tell "omitted"
        from "deliberately false".

        Deliberately NOT defaulted to ``True`` (contrast ``subprocess: true``'s
        default on an MCP stdio server, #2820 part C): a stdio MCP server
        *forks to exist* (``npx``/``uvx`` → the tool), so ``False`` there
        hardened nothing and only hid the knob behind an opaque failure. A hook
        shell's fork need instead depends on the operator's own command — a
        ``git``/``npm``/pipeline hook forks; a pure-python one may not — so the
        judgment is the operator's per hook, not a blanket flip (#2827).
    network:
        OPERATOR-declared per-hook sandbox knob (#3005): may this hook's exec
        argv reach the network? Same shape, scheme-restriction and
        ``bool | None`` semantics as ``subprocess`` above — ``None`` = omitted =
        the ``False`` floor.

        Exists because the agent-level ``reyn.yaml sandbox.policy`` does NOT
        reach a hook exec (it is resolved only on the op path), so before this
        knob an operator had **no** way to grant a hook network at all — their
        global ``network: true`` was silently dropped. The direction of that
        drop was fail-safe (the hook got *less* than asked), which is why it was
        a legibility defect and not a security hole; the fix is to make the axis
        reachable at the site that owns it *and* to stop dropping the global
        silently (see ``reyn.hooks.sandbox_scope``).
    write_paths:
        OPERATOR-declared per-hook sandbox knob (#3005): filesystem paths this
        hook's exec argv may write (``~`` expanded by the backend, write
        implies read). ``None`` = omitted = the floor, which grants **no** write
        paths; an explicit list — including ``[]`` — is the operator's expressed
        will. Optional (``... | None``) rather than a bare sequence for the same
        #2964 reason ``subprocess`` is ``bool | None``: an empty list cannot
        otherwise be told from an omission. Stored as a ``tuple`` because
        ``HookDef`` is frozen — the loader converts the YAML list.

        A write grant does not defeat the sensitive-read deny-list — the deny
        wins over an overlapping grant (#2978), exactly as on the op path.

        Together with ``subprocess`` and ``network`` this completes the per-site
        sandbox triad an operator already has on a stdio MCP server, so the same
        three axes are expressible at every per-site sandbox surface.
    origin:
        #5213: which config LAYER declared this hook — one of
        ``HOOK_ORIGIN_ORDER`` (``"startup"``/``"runtime"``/``"per-agent"``/
        ``"per-session"``), threaded through by
        :func:`~reyn.hooks.loader.load_hooks`'s ``origin=`` parameter, or
        ``"unknown"`` (the default) for a ``HookDef`` constructed directly
        without threading one through (test fixtures, most existing
        callers).

        Exists because the ``hooks:`` COMBINE loop
        (``Session._build_hook_registry``) used to concatenate every
        layer's raw dicts into one flat list before parsing — provenance
        was discarded at concatenation, so nothing downstream could ask
        "which layer declared this hook?". That question matters for
        exactly one predicate: ``disabled:`` (a SEPARATE, layer-agnostic
        axis, persisted at the per-session state file) used to disable a
        hook by NAME ALONE, with no origin check — so any agent that can
        write its own per-session state (every agent) could silently
        neutralise a project-layer hook the operator declared specifically
        because that layer is the one the agent CANNOT write (#5213's own
        motivating example: #5041's supervision hook). See
        :func:`hook_origin_is_at_least_as_specific_as` for the rule this
        field makes askable.

        ``"unknown"`` is deliberately NOT in ``HOOK_ORIGIN_ORDER`` and is
        treated as the MOST specific origin (freely disableable) —
        fail-OPEN for this one default, matching every pre-#5213
        test/direct-construction call site's behavior exactly (nothing
        regresses); the protection this field exists to provide only
        applies to a ``HookDef`` that actually went through the real
        session composition path, which always sets a real origin.
    fold:
        #5516 (owner ruling §1/§1b): OPERATOR opt-OUT for this ONE hook
        entry from launch-folding — ``None``/omitted/``True`` = the
        floor = FOLD (N queued events become ONE launch carrying an
        ``N``-item array); an explicit ``False`` is the operator's
        expressed will to opt OUT (each queued event gets its OWN
        launch, still array-wrapped as a single-item ``[payload]`` —
        the #5516 clean-break, payload is always an array, is
        unconditional and this flag never touches it). Only meaningful
        on ``exec``/``exec_capture``/``template_push`` — the three
        schemes whose receiver CAN take N items in one call (stdin JSON
        array / concatenated text); rejected on ``pipeline_launch``
        (whose receiver takes ONE ``input: dict`` and can never fold at
        all, unconditionally — see ``HookDispatcher._dispatch_one_batch``'s
        own docstring), same eager-rejection posture as ``subprocess``/
        ``network`` being rejected on a non-exec scheme (a
        silently-ignored operator flag reads as an applied restriction
        that was never applied).

        Default is "fold" because folding loses nothing (every event's
        data still arrives, just batched) and adds no latency (the
        countdown launches on the FIRST item, never after a time
        window). The one real reason to opt out: wanting MORE wake
        opportunities — a hook design that wants to "think" once per
        event rather than once per batch.

        🔴 Causality an opted-out hook's operator MUST know (owner §1b,
        stated here because #5516's issue thread is not somewhere a
        future reader will look): an opted-out hook consumes
        ``max_hook_driven_turns`` valve units ONE PER EVENT (see
        ``dispatcher.py``'s own ``:270-272`` comment on that valve) —
        N queued events opting out means N valve units spent where a
        folded hook on the same burst would spend ONE. An operator who
        does not know this stops at a DIFFERENT place (the valve cap)
        than the one they were adjusting.

        ⚪ ``skipped_session_wide`` still applies regardless of this
        flag's value — folding vs. not-folding only decides what
        happens to events that MADE IT into the queue; an event lost to
        bridge queue overflow before that point is lost either way.
    """

    on: str
    name: str | None = field(default=None)
    template_push: PushBlock | None = field(default=None)
    exec: "tuple[str, ...] | None" = field(default=None)
    exec_capture: "tuple[str, ...] | None" = field(default=None)
    pipeline_launch: PipelineLaunchBlock | None = field(default=None)
    matcher: "dict[str, str] | None" = field(default=None)
    subprocess: bool | None = field(default=None)
    network: bool | None = field(default=None)
    write_paths: "tuple[str, ...] | None" = field(default=None)
    origin: str = field(default="unknown")
    fold: bool | None = field(default=None)


#: #5213: the 4 config layers ``hooks:`` composes across, in ORDER FROM LEAST
#: TO MOST specific (trust runs the same direction, most-to-least) —
#: ``Session._build_hook_registry``'s own combine loop's real layer order:
#: ``startup`` (reyn.yaml, the operator's, trusted) → ``runtime``
#: (``.reyn/config/hooks.yaml``) → ``per-agent`` (``.reyn/agents/<name>/hooks.yaml``)
#: → ``per-session`` (session-defined, read from the SAME per-session state
#: file ``disabled:`` itself is persisted to). Deliberately NOT re-derived
#: from that loop's own string literals (a second hand-typed copy would be
#: the exact "same fact in 2 places" risk this session's own #5202/#5206
#: work flagged repeatedly) — this IS the one declared vocabulary; the
#: combine loop's own labels must match these 4 strings exactly, or a hook
#: silently gets treated as more-specific-than-it-is by
#: :func:`hook_origin_is_at_least_as_specific_as` (fail-open, per that
#: function's own "unrecognized origin" branch).
HOOK_ORIGIN_ORDER = ("startup", "runtime", "per-agent", "per-session")


def hook_origin_is_at_least_as_specific_as(origin: str, layer: str) -> bool:
    """#5213 (architect ruling): the rule that closes the ``disabled:``
    layer-bypass hole — "a layer's ``disabled:`` may only disable hooks
    whose origin is that layer or below [more specific]". Returns whether
    *origin* is *layer* itself or a MORE SPECIFIC layer (later in
    :data:`HOOK_ORIGIN_ORDER`).

    Concretely, ``Session``'s own ``is_hook_disabled`` predicate calls this
    with ``layer="per-agent"``, NOT ``"per-session"`` and NOT ``"runtime"``
    — the real boundary (verified: ``_canonical_protected_write_paths()``
    and ``_RECOVERY_CORE_WRITE_PREFIXES``) is the WRITE ZONE, not the file
    ``disabled:`` happens to be persisted to. ``.reyn/agents/<name>/hooks.yaml``
    (per-agent) and the per-session state file are BOTH agent-writable —
    disabling a hook at either origin grants no power an agent didn't
    already have (it could edit that same file directly to remove the hook
    entirely). ``runtime`` (the IN-set's ``hooks:`` key, physically
    ``.reyn/config/hooks.yaml``) is under ``_RECOVERY_CORE_WRITE_PREFIXES``
    (``.reyn/config/``, ``.reyn/state/``) — NOT in an agent's default write
    zone, a raw ``file.write`` there is denied — and ``startup``
    (reyn.yaml/reyn.local.yaml, read once at boot, never re-read from a
    writable path) is restart-only. Both genuinely differ from per-agent/
    per-session, so ``"per-agent"`` is the correct threshold — everything
    less specific than per-agent (``startup``, ``runtime``) stays
    protected. An earlier version of this threshold used ``"runtime"``,
    treating ``.reyn/config/hooks.yaml`` as agent-writable on the strength
    of a stale pre-#2073-file-split filename (``.reyn/hooks.yaml``) — caught
    in #5218 review before merge; #5220 swept the remaining bare mentions
    of that stale filename (this one deliberately kept, as the historical
    record of what was caught and why). Expressed as a general order comparison
    (not a hardcoded membership test) so the check keeps working for free
    if the write-zone boundary ever moves again, or a SECOND
    ``disabled:``-shaped axis is added at a different layer.

    Fail-OPEN (returns ``True``, i.e. "disableable") for either argument
    NOT in :data:`HOOK_ORIGIN_ORDER` — covers ``HookDef.origin``'s own
    ``"unknown"`` default (every test/direct-construction call site that
    predates #5213, preserved byte-identical) and protects against a typo'd
    *layer* argument silently becoming "nothing is ever disableable" instead
    of a loud failure — this function's OWN contract is permissive-by-
    default, matching ``disabled:`` being a legitimate feature this issue
    narrows the SCOPE of, not a security boundary it tightens to
    fail-closed (see #5213's own issue thread: "disabled: 自体は正当な機能
    なので消さず射程を切る")."""
    if origin not in HOOK_ORIGIN_ORDER or layer not in HOOK_ORIGIN_ORDER:
        return True
    return HOOK_ORIGIN_ORDER.index(origin) >= HOOK_ORIGIN_ORDER.index(layer)
