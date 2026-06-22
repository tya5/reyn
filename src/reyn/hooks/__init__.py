"""reyn.hooks — agent lifecycle hook config: schema, loader, registry, renderer, runner.

Slice A (#1800): schema + loader + registry.
Slice B (#1800): Jinja2 push-directive renderer.
Slice C (#1800): shell-hook runner (side-effect only; output ignored).

Exposes:
    HookDef        — typed model for a single hook definition.
    PushBlock      — typed model for the inbox-push sub-schema.
    HookRegistry   — ordered store; query via hooks_for(point).
    load_hooks     — parse the ``hooks:`` list from a raw config dict
                     and return a ready ``HookRegistry``.
    HookConfigError — raised for config validation failures.
    ResolvedPush   — fully-rendered push directive (slice B).
    render_push    — render a ``PushBlock`` against a context dict (slice B).
    run_shell_hook — execute a shell HookDef command (slice C).
"""
from reyn.hooks.loader import load_hooks
from reyn.hooks.registry import HookRegistry
from reyn.hooks.render import ResolvedPush, render_push
from reyn.hooks.schema import HookConfigError, HookDef, PushBlock
from reyn.hooks.shell_runner import run_shell_hook

__all__ = [
    "HookDef",
    "PushBlock",
    "HookRegistry",
    "HookConfigError",
    "load_hooks",
    "ResolvedPush",
    "render_push",
    "run_shell_hook",
]
