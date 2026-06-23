"""reyn.hooks.loader — parse and validate the ``hooks:`` config block (#1800 slice A).

Entry point: ``load_hooks(raw)`` — accepts the raw value of the ``hooks:``
key from a reyn.yaml dict and returns a ``HookRegistry``.

Validation is *structural only* (field presence, types, hook-point membership,
push/shell mutual-exclusion).  Template *semantics* are not validated here —
rendering is a later slice.

Validation errors raise ``HookConfigError`` with a decision-enabling message
that names the entry index and the failing field so the operator can fix the
config immediately.
"""
from __future__ import annotations

import logging

from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import (
    ALLOWED_HOOK_POINTS,
    HookConfigError,
    HookDef,
    PushBlock,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_push_block(raw: object, entry_index: int) -> PushBlock:
    """Validate and convert the ``push:`` sub-dict to a ``PushBlock``.

    All Jinja2 template strings are stored raw; no rendering here.
    """
    if not isinstance(raw, dict):
        raise HookConfigError(
            f"hooks[{entry_index}].push must be a mapping, "
            f"got {type(raw).__name__!r}."
        )

    # Required: message
    message = raw.get("message")
    if message is None:
        raise HookConfigError(
            f"hooks[{entry_index}].push.message is required."
        )
    if not isinstance(message, str):
        raise HookConfigError(
            f"hooks[{entry_index}].push.message must be a string, "
            f"got {type(message).__name__!r}."
        )
    if not message.strip():
        raise HookConfigError(
            f"hooks[{entry_index}].push.message must not be empty."
        )

    # Optional: wake (bool or Jinja2 template string → bool, default True)
    raw_wake = raw.get("wake", True)
    if not isinstance(raw_wake, (bool, str)):
        raise HookConfigError(
            f"hooks[{entry_index}].push.wake must be a bool or template string, "
            f"got {type(raw_wake).__name__!r}."
        )
    wake: bool | str = raw_wake

    # Optional: push_when (Jinja2 template string → bool, default "true")
    raw_push_when = raw.get("push_when", "true")
    if not isinstance(raw_push_when, (bool, str)):
        raise HookConfigError(
            f"hooks[{entry_index}].push.push_when must be a bool or template string, "
            f"got {type(raw_push_when).__name__!r}."
        )
    # Normalise a plain bool to its string form so the type is uniform.
    if isinstance(raw_push_when, bool):
        push_when: str = "true" if raw_push_when else "false"
    else:
        push_when = raw_push_when

    # Optional: session (template string or None)
    raw_session = raw.get("session", None)
    if raw_session is not None and not isinstance(raw_session, str):
        raise HookConfigError(
            f"hooks[{entry_index}].push.session must be a string or null, "
            f"got {type(raw_session).__name__!r}."
        )
    session: str | None = raw_session if raw_session else None

    return PushBlock(
        message=message,
        wake=wake,
        push_when=push_when,
        session=session,
    )


def _parse_entry(raw: object, entry_index: int) -> HookDef:
    """Validate one raw hooks list entry and return a ``HookDef``."""
    if not isinstance(raw, dict):
        raise HookConfigError(
            f"hooks[{entry_index}] must be a mapping, "
            f"got {type(raw).__name__!r}."
        )

    # ── on (required) ──────────────────────────────────────────────────────
    # YAML 1.1 (PyYAML default) parses the bare keyword ``on`` as boolean
    # ``True``; quote-free ``on: skill_end`` in reyn.yaml becomes
    # ``{True: 'skill_end', ...}``.  Try the string key first (= quoted
    # ``"on"``), then fall back to the boolean key ``True`` so that both
    # ``on: skill_end`` and ``"on": skill_end`` work.
    on_raw = raw.get("on", raw.get(True))
    if on_raw is None:
        raise HookConfigError(
            f"hooks[{entry_index}].on is required."
        )
    if not isinstance(on_raw, str):
        raise HookConfigError(
            f"hooks[{entry_index}].on must be a string, "
            f"got {type(on_raw).__name__!r}."
        )
    on_key = on_raw.strip().lower()
    if on_key not in ALLOWED_HOOK_POINTS:
        sorted_points = ", ".join(sorted(ALLOWED_HOOK_POINTS))
        raise HookConfigError(
            f"hooks[{entry_index}].on={on_raw!r} is not a recognised hook-point. "
            f"Allowed: {sorted_points}."
        )

    # ── push / shell mutual-exclusion ──────────────────────────────────────
    has_push = "push" in raw
    has_shell = "shell" in raw

    if has_push and has_shell:
        raise HookConfigError(
            f"hooks[{entry_index}]: 'push' and 'shell' are mutually exclusive; "
            f"specify exactly one."
        )
    if not has_push and not has_shell:
        raise HookConfigError(
            f"hooks[{entry_index}]: exactly one of 'push' or 'shell' is required."
        )

    # ── push block ─────────────────────────────────────────────────────────
    push_block: PushBlock | None = None
    if has_push:
        push_block = _parse_push_block(raw["push"], entry_index)

    # ── shell ──────────────────────────────────────────────────────────────
    shell: str | None = None
    if has_shell:
        shell_raw = raw["shell"]
        if not isinstance(shell_raw, str):
            raise HookConfigError(
                f"hooks[{entry_index}].shell must be a string, "
                f"got {type(shell_raw).__name__!r}."
            )
        if not shell_raw.strip():
            raise HookConfigError(
                f"hooks[{entry_index}].shell must not be empty."
            )
        shell = shell_raw

    # ── matcher (optional, reserved) ───────────────────────────────────────
    matcher_raw = raw.get("matcher", None)
    if matcher_raw is not None and not isinstance(matcher_raw, str):
        raise HookConfigError(
            f"hooks[{entry_index}].matcher must be a string or null, "
            f"got {type(matcher_raw).__name__!r}."
        )
    matcher: str | None = matcher_raw if matcher_raw else None

    # ── name (optional, #1800 slice 6) — the [hook:name] attribution label; ──
    # absent / blank → None (the dispatcher defaults it to the hook-point).
    name_raw = raw.get("name", None)
    if name_raw is not None and not isinstance(name_raw, str):
        raise HookConfigError(
            f"hooks[{entry_index}].name must be a string or null, "
            f"got {type(name_raw).__name__!r}."
        )
    name: str | None = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else None

    return HookDef(
        on=on_key,
        name=name,
        push=push_block,
        shell=shell,
        matcher=matcher,
    )


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_hooks(raw: object) -> HookRegistry:
    """Parse and validate the ``hooks:`` value from a reyn.yaml dict.

    Parameters
    ----------
    raw:
        The value of the ``hooks:`` key from the config dict.  May be
        ``None`` (absent), an empty list, or a list of hook dicts.
        Any other type is logged as a warning and treated as empty.

    Returns
    -------
    HookRegistry
        A ready registry containing all validated ``HookDef`` objects in
        registration (list) order.

    Raises
    ------
    HookConfigError
        On structural validation failure.  The message names the offending
        entry index and the failing constraint so the operator can fix it.
    """
    if raw is None:
        return HookRegistry([])

    if not isinstance(raw, list):
        _log.warning(
            "config key 'hooks' must be a list; got %s — ignoring it.",
            type(raw).__name__,
        )
        return HookRegistry([])

    defs: list[HookDef] = []
    for idx, entry in enumerate(raw):
        defs.append(_parse_entry(entry, idx))

    return HookRegistry(defs)
