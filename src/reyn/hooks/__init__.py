"""reyn.hooks — agent lifecycle hook config: schema, loader, registry, renderer.

Slice A (#1800): schema + loader + registry.
Slice B (#1800): Jinja2 push-directive renderer.

Exposes:
    HookDef        — typed model for a single hook definition.
    PushBlock      — typed model for the inbox-push sub-schema.
    HookRegistry   — ordered store; query via hooks_for(point).
    load_hooks     — parse the ``hooks:`` list from a raw config dict
                     and return a ready ``HookRegistry``.
    HookConfigError — raised for config validation failures.
    ResolvedPush   — fully-rendered push directive (slice B).
    render_push    — render a ``PushBlock`` against a context dict (slice B).
"""
from reyn.hooks.loader import load_hooks
from reyn.hooks.registry import HookRegistry
from reyn.hooks.render import ResolvedPush, render_push
from reyn.hooks.schema import HookConfigError, HookDef, PushBlock

__all__ = [
    "HookDef",
    "PushBlock",
    "HookRegistry",
    "HookConfigError",
    "load_hooks",
    "ResolvedPush",
    "render_push",
]
