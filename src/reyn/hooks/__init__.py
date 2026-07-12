"""reyn.hooks — agent lifecycle hook config: schema, loader, registry, renderer, runner.

Slice A (#1800): schema + loader + registry.
Slice B (#1800): Jinja2 push-directive renderer.
Slice C (#1800): shell-hook runner (side-effect only; output ignored).

Slice H3 (#2608): ``pipeline_launch`` action — launch a registered Pipeline
from a hook, with an input rendered from the event payload.

Slice H2 (#2608): ``matcher`` interpretation — a hook filters WHICH events
of an external-event hook-point it fires on (field->pattern dict evaluated
against ``template_vars``; see ``reyn.hooks.matcher``).

Exposes:
    HookDef        — typed model for a single hook definition.
    PushBlock      — typed model for the inbox-push sub-schema.
    PipelineLaunchBlock — typed model for the pipeline-launch sub-schema (H3).
    HookRegistry   — ordered store; query via hooks_for(point).
    load_hooks     — parse the ``hooks:`` list from a raw config dict
                     and return a ready ``HookRegistry``.
    HookConfigError — raised for config validation failures.
    ResolvedPush   — fully-rendered push directive (slice B).
    render_push    — render a ``PushBlock`` against a context dict (slice B).
    render_pipeline_input — render a ``PipelineLaunchBlock.input_template``
                     against a context dict (H3).
    run_shell_hook — execute a shell HookDef command (slice C).
    matcher_matches — evaluate a ``HookDef.matcher`` against ``template_vars`` (H2).

Hook-Event Redesign Phase 1 (proposal 0059) additionally exposes:
    HookEvent      — the typed hook-event wrapper (``reyn.hooks.event``).
    build_hook_payload — construct + schema-validate a builtin hook-event
                     payload (``reyn.hooks.schema_registry``); the single
                     producer every builtin dispatch call site funnels
                     through.

Hook-Event Redesign Phase 3 (proposal 0059 §10 Q-reyn-4) additionally exposes:
    EventPattern   — the typed kind/source/payload match grammar
                     generalizing ``HookDef.matcher`` (``reyn.hooks.
                     event_pattern``).
    event_pattern_matches — evaluate an ``EventPattern`` against a
                     ``HookEvent`` (byte-identical to the legacy matcher for
                     every payload-only pattern).
    event_pattern_from_legacy_matcher — wrap a pre-Phase-3 matcher dict as a
                     payload-only ``EventPattern``.
    validate_event_pattern_against_schema — OPT-IN static validation: flag an
                     ``EventPattern`` payload field not in a kind's builtin
                     schema (typo-resistance).
"""
from reyn.hooks.event import HookEvent
from reyn.hooks.event_pattern import EventPattern
from reyn.hooks.event_pattern import from_legacy_matcher as event_pattern_from_legacy_matcher
from reyn.hooks.event_pattern import matches as event_pattern_matches
from reyn.hooks.event_pattern import (
    validate_against_schema as validate_event_pattern_against_schema,
)
from reyn.hooks.loader import load_hooks
from reyn.hooks.matcher import matches as matcher_matches
from reyn.hooks.registry import HookRegistry
from reyn.hooks.render import ResolvedPush, render_pipeline_input, render_push
from reyn.hooks.schema import HookConfigError, HookDef, PipelineLaunchBlock, PushBlock
from reyn.hooks.schema_registry import build_hook_payload
from reyn.hooks.shell_runner import run_shell_hook

__all__ = [
    "EventPattern",
    "HookDef",
    "HookEvent",
    "PushBlock",
    "PipelineLaunchBlock",
    "HookRegistry",
    "HookConfigError",
    "build_hook_payload",
    "event_pattern_from_legacy_matcher",
    "event_pattern_matches",
    "load_hooks",
    "ResolvedPush",
    "render_push",
    "render_pipeline_input",
    "run_shell_hook",
    "matcher_matches",
    "validate_event_pattern_against_schema",
]
