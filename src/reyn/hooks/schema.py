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
# recognised ``on:`` value here, with zero edits to this module. The 8
# points today are unchanged: turn_start/turn_end, session_start/
# session_end (lifecycle), mcp_resource_updated
# (#2608 H1), file_changed (#2608 H4), cron_fired/webhook_received (#2608 H5).
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
    """

    message: str
    wake: Union[bool, str] = True
    push_when: str = "true"
    session: str | None = None


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
        (unchanged for every hook that predates H2).
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
