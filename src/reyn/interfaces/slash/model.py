"""``/model`` — runtime model-class override for the current session.

Switch the model class used by this session without restarting:

  /model              → show current model + agent default + available classes
  /model <class>      → override this session's model to <class> (sticky for
                        the session lifetime; cleared on restart)

Valid ``<class>`` values are the operator-configured model classes from
``reyn.yaml`` (e.g. ``light``, ``standard``, ``strong``, plus any user-defined
entries). Unknown class names are rejected with the full list — the resolver's
class gate ensures proxy config stays the single source of truth.

Byte-identical when unused: a session that never runs ``/model`` uses the
agent-identity default (``Agent.model``) unchanged.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session


def _model_class_completer(session: "Session", arg_partial: str = "") -> list[str]:
    """Operator-configured model classes for ``/model <class>`` completion.

    Wires the EXISTING public accessor
    (:meth:`~reyn.runtime.session.Session.known_model_classes` →
    ``ModelResolver.known_classes()``) — the same list this command's own
    no-arg branch prints under ``available:`` and the drawer's Model pane
    enumerates — so the completion menu can never offer a class the command
    would then reject. No new source of truth.

    ``arg_partial`` is accepted per the ``CompleterFn`` contract and unused:
    ``/model`` takes a single argument, and prefix-filtering is the caller's
    job (it filters by the last typed word for every command uniformly).
    Returns ``[]`` for a session that cannot answer — a remote client holds
    none, and a broken completer must never break the composer.
    """
    getter = getattr(session, "known_model_classes", None)
    if getter is None:
        return []
    try:
        return list(getter())
    except Exception:  # noqa: BLE001 — a completer must never break the composer
        return []


@slash(
    "model",
    summary="Show or override the model class for this session",
    usage="/model [<class>]",
    completer=_model_class_completer,
)
async def model_cmd(session: "Session", args: str) -> None:
    """/model [<class>] — show current model or set a per-session override."""
    resolver = session._resolver
    requested = args.strip()

    if not requested:
        agent_default = session._agent.model
        current = session.model
        override = session._model_override
        lines = [f"model: {current}"]
        if override is not None:
            lines.append(f"  override: {override} (this session — clears on restart)")
            lines.append(f"  agent default: {agent_default}")
        else:
            lines.append("  (agent default, no override set)")
        lines.append(f"available: {', '.join(resolver.known_classes())}")
        await reply(session, "\n".join(lines))
        return

    if not resolver.is_known_class(requested):
        await reply_error(
            session,
            f"unknown model class {requested!r}; "
            f"available: {', '.join(resolver.known_classes())}",
        )
        return

    # #1867 / FP-0052 S4: optional blocking confirm BEFORE the switch is applied.
    # When ``cost_warn.block_on_high_cost`` is on and the target is high-cost, the
    # switch is held for an interactive confirm via the unified safety framework;
    # a decline (or a non-interactive session — fail-closed) leaves the current
    # model unchanged. No-op (returns True) under the default warn-only config.
    from reyn.runtime.model_cost_warn import (
        maybe_block_high_cost_model,
        maybe_emit_model_cost_warn,
    )
    if not await maybe_block_high_cost_model(session, requested, action="model_override"):
        await reply(
            session,
            f"model switch to {requested} cancelled (high-cost model not confirmed).",
        )
        return

    session._model_override = requested
    # #1752: the per-turn budget consumers (history buffer / context budget
    # advisor) read the live resolved model via their model_fn, but the
    # turn_budget engine bakes derived headroom at construction, so rebuild it
    # for the new model's context window.
    session._rebuild_turn_budget_engine_for_model()

    # #1830 / FP-0052: emit model_cost_warn event if the chosen model exceeds
    # the configured cost threshold (pre-selection awareness). De-duped per
    # session: same model warned at most once.
    maybe_emit_model_cost_warn(session, requested, action="model_override")

    await reply(session, f"model → {requested} (this session — clears on restart)")
