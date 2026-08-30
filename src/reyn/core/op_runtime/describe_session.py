"""describe_session op — read-only session introspection (#5012-A).

Assembles the 3 fields the issue enumerates (architect ruling, `gh issue view
5012`, 2026-08-21 — a closed population, never grown ad hoc; see
``DescribeSessionIROp``'s own docstring):

1. write scope — DECLARED, never resolved/effective — from
   :func:`reyn.runtime.session_write_scope.describe_write_scope` against
   ``ctx.sandbox_config``;
2. own position — repo path / git branch+HEAD / venv / toolchain capability
   — from :func:`reyn.runtime.session_position.describe_session_position`
   against ``ctx.workspace.base_dir`` (the same repo root every other op
   resolves file paths against, see ``context.resolve_path_for_gate``).
   (Issue #5012's own field ② originally also named a remaining-hook-
   driven-turns budget, reported via ``ctx.hook_driven_turns_budget`` — #5561
   (owner ruling) retired that loop valve entirely, and this sub-key with
   it; see ``LoopConfig``'s own docstring, config/chat.py, for the
   retirement rationale.)
3. auth status — reyn-managed OAuth providers only — from
   :func:`reyn.runtime.session_auth_status.describe_auth_status` against
   ``ctx.auth_config``.

No side effect, no permission gate (mirrors ``list_actions``/
``search_actions`` — pure read of already-authorized OS state, nothing the
model didn't already have some other way to learn).
"""
from __future__ import annotations

from reyn.core.offload.canonical import describe_session_to_canonical
from reyn.runtime.session_auth_status import describe_auth_status
from reyn.runtime.session_position import describe_session_position
from reyn.runtime.session_write_scope import describe_write_scope
from reyn.schemas.models import DescribeSessionIROp

from . import register
from .context import OpContext


async def handle(
    op: DescribeSessionIROp,
    ctx: OpContext,
) -> dict:
    """Assemble the 3-field session-position report. Never raises — each
    sub-fact's own function already degrades honestly (None / declared=False
    / not-authenticated) rather than failing the whole op over one
    unavailable sub-fact, same discipline ``session_position`` documents for
    its own git-subprocess calls."""
    position = dict(describe_session_position(ctx.workspace.base_dir))
    return {
        "kind": "describe_session",
        "status": "ok",
        "write_scope": describe_write_scope(ctx.sandbox_config),
        "position": position,
        "auth_status": describe_auth_status(ctx.auth_config),
    }


register("describe_session", handle, canonical=describe_session_to_canonical)
