#!/usr/bin/env python3
"""#5190 — a hook `on:` kind ``hooks.yaml`` can DECLARE must have at least
one real code path that reaches it WITHOUT requiring an LLM turn.

## The bug class this closes (#5167)

Before #5167, ``mcp_resource_updated`` was a fully declarable ``on:``
value (present in ``reyn.hooks.schema_registry.BUILTIN_HOOK_SCHEMAS``) with
exactly ONE construction site for the subscription that would ever fire
it — the LLM-facing ``subscribe_mcp_resource`` tool. A ``hooks.yaml`` entry
declaring this hook was schema-VALID and silently, permanently inert for
any agent whose model never happened to call that tool. Nothing caught
this: the schema advertised the kind, a real ``build_hook_payload``/
``HookDispatcher.dispatch`` call site existed in the source tree (so an
"emit-site census" alone would have called it reachable), and the only
thing actually missing was a NON-LLM way to reach that call site at all.

Architect's ruling (issuecomment-5384661356, on #5190): this is a
STRUCTURAL defect, not a case-by-case one, and there is no legitimate
"intentionally declare-only, no reachability yet" state — a schema is an
ADVERTISEMENT (owner ruling on #5167, "honor できない宣言は load で大きな
声で落とす" — a declaration reyn cannot honor must fail loud, never be
silently accepted), so a kind that is declarable but reaches nothing IS
the defect this gate exists to catch, not a legitimate degrade to leave
alone.

## Why "an emit call site exists" is NOT the check

The #5167 trap: ``build_hook_payload("mcp_resource_updated", ...)`` DID
exist in the source tree the whole time (inside the ``subscribe_mcp_
resource`` tool's own implementation) — a naive AST census of "does a
``build_hook_payload(kind, ...)`` call site exist for this kind" would
have read #5167 as compliant. The actual defect was that the ONLY path to
that call site required an LLM to decide to call a specific tool first.
So this gate does not (and structurally cannot) derive "reachable without
an LLM turn" from an AST scan — that requires the same kind of human
judgment ``reyn.core.events.event_schema.DYNAMIC_KIND_EMIT_SITES``
already exercises for audit-event blind spots (a classified, reviewed,
hand-maintained registry, not a scanner). ``REACHABLE_WITHOUT_LLM_TURN``
below is that registry for hook kinds: one citation per declarable kind,
to the real non-LLM code path that dispatches it, verified against the
source tree at the time each entry was written (see the entry's own
comment) — not verified BY this script at runtime, the same trust
boundary ``DYNAMIC_KIND_EMIT_SITES`` already accepts.

"Reachable without an LLM turn" (architect's own definition, mirroring
#5167 acceptance④): a code path that fires WITHOUT the model ever needing
to decide to invoke a tool first — session lifecycle, the turn-loop
itself (regardless of whether a given turn happens to call the LLM),
config-driven ingress (fs watcher / cron / webhook), or an auto-wiring
mechanism like #5167's own fix. A path gated behind runtime CONFIGURATION
(e.g. only fires when an MCP server is actually configured) still counts
— the code path exists unconditionally; whether it fires on a given
deployment is an operational fact, not a reachability one. Only "requires
an LLM to choose a tool call" disqualifies a citation.

## The only 2 remedies (architect ruling — no allowlist, ever)

When ``BUILTIN_HOOK_SCHEMAS`` gains a kind with no matching entry here,
this gate goes RED. There are exactly two ways to fix that:

  (a) build the reachability path — do what #5167 did — and add the
      citation here in the SAME PR, or
  (b) the kind isn't ready to be advertised yet — remove it from
      ``BUILTIN_HOOK_SCHEMAS`` until (a) is done.

An allowlist / "expected empty" exemption list is explicitly rejected
(architect, issuecomment-5384661356): that would make "advertised but
permanently unreachable" a PERMANENT, sanctioned state — exactly what
this gate exists to make impossible, not merely detectable. There is no
third option this gate's failure message may ever suggest.

## Baseline: the diff is currently empty, and must always be

Unlike the approval-ledger / TUI-widget boundary gates (whose baseline
population is a COUNT of forbidden things, zero), this gate's invariant
is set-theoretic: declared ⊆ reachable-without-LLM. Landing #5190 with
today's 9 declarable kinds and today's 9 registered citations makes the
diff empty NOW — every future kind added to the schema must arrive with
its own citation in the SAME PR, or this gate fails.
"""
from __future__ import annotations

import sys

from reyn.hooks.schema_registry import BUILTIN_HOOK_SCHEMAS

# ---------------------------------------------------------------------------
# The hand-maintained reachability registry — one entry per declarable kind
# (mirrors reyn.core.events.event_schema.DYNAMIC_KIND_EMIT_SITES's own
# "classified, reviewed, not scanner-derived" discipline). Each value is a
# one-line citation to the real non-LLM-turn code path, verified against
# the source tree as of #5190.
# ---------------------------------------------------------------------------

REACHABLE_WITHOUT_LLM_TURN: "dict[str, str]" = {
    "builtin:lifecycle:session_start": (
        "session.py:6872, Session.run() — unconditional session-boot "
        "lifecycle dispatch, no LLM call anywhere on this path"
    ),
    "builtin:lifecycle:session_end": (
        "session.py:6956, Session.run()'s own teardown — fires on every "
        "session shutdown regardless of any LLM activity"
    ),
    "builtin:lifecycle:turn_start": (
        "session.py:6553 — fires from the turn-loop's own trigger "
        "handling BEFORE any model call; a turn can originate from user "
        "input, a hook self-continuation push, cron, or a webhook, none "
        "of which require the LLM to have run first"
    ),
    "builtin:lifecycle:turn_end": (
        "session.py:9264 — wraps every turn unconditionally, regardless "
        "of whether that particular turn happened to invoke the LLM"
    ),
    "builtin:external:mcp_resource_updated": (
        "session.py:8648, Session._auto_subscribe_mcp_resource_hooks() "
        "(#5167) — called unconditionally from Session.run() at "
        "session.py:6880 for every declared hook whose matcher names a "
        "concrete (server, uri); the fix THIS gate's own bug class "
        "names — no LLM tool call needed"
    ),
    "builtin:external:file_changed": (
        "hooks/ingress.py:237, FsIngressAdapter.to_event — purely "
        "config-driven (fs_watch.paths in reyn.yaml) via the watchdog "
        "thread; the code path is unconditional, whether it FIRES on a "
        "given deployment depends on config, not on an LLM turn"
    ),
    "builtin:external:cron_fired": (
        "hooks/ingress.py:280, CronIngressAdapter.to_event — purely "
        "config-driven cron: schedule, resolved out-of-process via "
        "reyn.runtime.cron.routing; no LLM call on this path"
    ),
    "builtin:external:webhook_received": (
        "hooks/ingress.py:319, WebhookIngressAdapter.to_event — fires "
        "from inbound HTTP via reyn.runtime.webhook_routing; no LLM "
        "call on this path"
    ),
    "builtin:task:task_settled": (
        "runtime/services/pipeline_executor_driver.py:474, "
        "PipelineExecutorDriver._finish() — settles when a Pipeline run "
        "launched via the config-driven `pipeline_launch` hook action "
        "(attachable to any non-LLM hook point, e.g. cron_fired) "
        "reaches a terminal disposition. Deliberately NOT "
        "inter_agent_messaging.py:655's own producer, which settles "
        "only via run_prompt(collect=\"async\") — an LLM-tool-invoked "
        "path that would be exactly the #5167 trap if used as this "
        "kind's sole witness"
    ),
}


def find_undeclared_reachability(schemas=None, registry=None) -> "list[str]":
    """The declarable kinds (*schemas*, defaults to the real
    ``BUILTIN_HOOK_SCHEMAS``) with no entry in *registry* (defaults to
    the real :data:`REACHABLE_WITHOUT_LLM_TURN`) — the failure set,
    sorted for a stable diagnostic order. Both parameters are injectable
    (resolved by name lookup below, not bound defaults) so a test can
    monkeypatch either module-level constant and still reach this
    function through :func:`main`."""
    if schemas is None:
        schemas = BUILTIN_HOOK_SCHEMAS
    if registry is None:
        registry = REACHABLE_WITHOUT_LLM_TURN
    return sorted(set(schemas) - set(registry))


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-registry set diff, no target to name
    offenders = find_undeclared_reachability()

    if not offenders:
        print(
            f"OK: all {len(BUILTIN_HOOK_SCHEMAS)} declarable hook kinds "
            "have a registered non-LLM-turn reachability witness."
        )
        return 0

    print("hooks-declared-reachable gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} hook kind(s) are declarable in hooks.yaml "
        "(present in reyn.hooks.schema_registry.BUILTIN_HOOK_SCHEMAS) "
        "but have NO registered non-LLM-turn reachability witness in "
        "scripts/check_hooks_declared_reachable.py's own "
        "REACHABLE_WITHOUT_LLM_TURN (#5190):",
        file=sys.stderr,
    )
    for kind in offenders:
        print(f"  {kind}", file=sys.stderr)
    print(
        "\nA hook config can declare an `on:` value for one of these "
        "kinds and it will silently never fire for any agent whose "
        "model never happens to call a specific tool — the exact #5167 "
        "defect class. There are exactly 2 ways to fix this, no other:\n"
        "\n"
        "  (a) build a real code path that reaches this kind WITHOUT "
        "requiring an LLM turn (session lifecycle / turn loop / "
        "config-driven ingress / an auto-wiring mechanism, see #5167's "
        "own fix), then add a citation to REACHABLE_WITHOUT_LLM_TURN "
        "in the SAME PR; or\n"
        "  (b) remove the kind from BUILTIN_HOOK_SCHEMAS until (a) is "
        "done — an advertised-but-unreachable kind must not ship.\n"
        "\n"
        "An allowlist / exemption is not a third option (architect "
        "ruling, #5190) — that would make 'advertised but permanently "
        "unreachable' a sanctioned state instead of a caught defect.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
