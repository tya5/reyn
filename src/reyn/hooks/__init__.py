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
"""
from reyn.hooks.loader import load_hooks
from reyn.hooks.matcher import matches as matcher_matches
from reyn.hooks.registry import HookRegistry
from reyn.hooks.render import ResolvedPush, render_pipeline_input, render_push
from reyn.hooks.schema import HookConfigError, HookDef, PipelineLaunchBlock, PushBlock
from reyn.hooks.shell_runner import run_shell_hook

__all__ = [
    "HookDef",
    "PushBlock",
    "PipelineLaunchBlock",
    "HookRegistry",
    "HookConfigError",
    "load_hooks",
    "ResolvedPush",
    "render_push",
    "render_pipeline_input",
    "run_shell_hook",
    "matcher_matches",
]
