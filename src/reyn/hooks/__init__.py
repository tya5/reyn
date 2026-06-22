"""reyn.hooks — agent lifecycle hook config: schema, loader, registry (#1800 slice A).

Exposes:
    HookDef        — typed model for a single hook definition.
    PushBlock      — typed model for the inbox-push sub-schema.
    HookRegistry   — ordered store; query via hooks_for(point).
    load_hooks     — parse the ``hooks:`` list from a raw config dict
                     and return a ready ``HookRegistry``.
    HookConfigError — raised for config validation failures.
"""
from reyn.hooks.loader import load_hooks
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookConfigError, HookDef, PushBlock

__all__ = [
    "HookDef",
    "PushBlock",
    "HookRegistry",
    "HookConfigError",
    "load_hooks",
]
