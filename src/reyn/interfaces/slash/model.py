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

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash


def _model_class_completer(source: "object", arg_partial: str = "") -> list[str]:
    """Operator-configured model classes for ``/model <class>`` completion.

    ``source`` is a ``CompletionSourceSnapshot | None`` (#5044) — a plain
    value, never a live ``Session``. :attr:`~reyn.interfaces.repl.
    read_model.CompletionSourceSnapshot.known_model_classes` is already
    the EXISTING public accessor's result
    (:meth:`~reyn.runtime.session.Session.known_model_classes` →
    ``ModelResolver.known_classes()``) — the same list this command's own
    no-arg branch prints under ``available:`` and the drawer's Model pane
    enumerates — so the completion menu can never offer a class the command
    would then reject. No new source of truth.

    ``arg_partial`` is accepted per the ``CompleterFn`` contract and unused:
    ``/model`` takes a single argument, and prefix-filtering is the caller's
    job (it filters by the last typed word for every command uniformly).
    Returns ``[]`` for a source that cannot answer — a remote client holds
    none, and a broken completer must never break the composer.
    """
    known_model_classes = getattr(source, "known_model_classes", None)
    return list(known_model_classes) if known_model_classes is not None else []


@slash(
    "model",
    summary="Show or override the model class for this session",
    usage="/model [<class>]",
    completer=_model_class_completer,
)
async def model_cmd(ctx: "SlashContext", args: str) -> None:
    """/model [<class>] — show current model or set a per-session override."""
    resolver = ctx.session._resolver
    requested = args.strip()

    if not requested:
        agent_default = ctx.session._agent.model
        current = ctx.session.model
        override = ctx.session._model_override
        lines = [f"model: {current}"]
        if override is not None:
            lines.append(f"  override: {override} (this session — clears on restart)")
            lines.append(f"  agent default: {agent_default}")
        else:
            lines.append("  (agent default, no override set)")
        lines.append(f"available: {', '.join(resolver.known_classes())}")
        await reply(ctx, "\n".join(lines))
        return

    if not resolver.is_known_class(requested):
        await reply_error(
            ctx,
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
    if not await maybe_block_high_cost_model(ctx.session, requested, action="model_override"):
        await reply(
            ctx,
            f"model switch to {requested} cancelled (high-cost model not confirmed).",
        )
        return

    ctx.session._model_override = requested
    # #1752 / #3785: the per-turn budget consumers (history buffer / context
    # budget advisor) read the live resolved model via their model_fn, but
    # turn_budget AND compaction both bake their model in at construction, so
    # rebuild both for the new model — ONE private-Session entry point (not
    # one per engine; see the method's own docstring for why they are folded
    # together rather than exposed as two separate accessors).
    ctx.session._rebuild_derived_model_engines_for_model()

    # #1830 / FP-0052: emit model_cost_warn event if the chosen model exceeds
    # the configured cost threshold (pre-selection awareness). De-duped per
    # session: same model warned at most once.
    maybe_emit_model_cost_warn(ctx.session, requested, action="model_override")

    await reply(ctx, f"model → {requested} (this session — clears on restart)")
