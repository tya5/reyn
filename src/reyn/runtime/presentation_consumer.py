"""reyn.runtime.presentation_consumer — the orphan-impossible present-sink seam (#2708 P1).

A `present` op writes its resolved render model to a `PresentationRenderer` sink
(`core/present/renderer.py`). Two sink shapes exist: a **self-delivering** one that
draws directly to a surface (e.g. `StdoutPresentationRenderer` for `reyn pipe run`),
and an **outbox-backed** one (`OutboxPresentationRenderer`, `runtime/session_buses.py`)
that only puts a `"presentation"` message on the Session's outbox — it reaches the user
ONLY if some per-surface drain loop consumes and renders that message. The historical
bug class (#2688 / #2707) was an *orphan* outbox sink: a surface wired the outbox
producer but had no consumer draining it, so a `present` op returned `ok:True` while the
user saw nothing.

This module makes that orphan **impossible by construction**:

- `PresentationConsumer` is the ONLY public way to obtain a sink for a Session — its
  `.sink(session)` returns a `PresentationRenderer`. `build_scoped_chat_session` takes a
  `PresentationConsumer` as a REQUIRED, no-default kwarg (#1402 completeness-by-
  construction), so a surface that declares no consumer cannot construct a Session at all
  (compile/type error, not a silent null). The consumer — not a bare renderer — is the
  required input because the outbox-backed renderer needs the Session, which does not yet
  exist when `build_scoped_chat_session` is called; `.sink(session)` defers its
  construction to Session init.
- `OutboxPresentationConsumer.sink()` is the SOLE construction site for
  `OutboxPresentationRenderer` (enforced by the AST guard in
  `tests/test_present_sink_ast_guard_2708.py`, the #1190/#2683 single-writer model), so a
  bare/orphan outbox sink cannot be instantiated anywhere in `src/reyn`.
- A `NullPresentationSink` (documented no-op) is available ONLY through
  `NullPresentationConsumer`, whose surface name must be a member of the reviewed
  `_NA_PRESENTATION_SURFACES` frozenset — a machine surface with no human presentation
  drain (web / mcp / dogfood). A new human surface cannot silently NA-dodge (the ratchet
  test `tests/test_present_sink_na_ratchet_2708.py` pins the frozenset by equality, the
  FP-0056 admin-6 model).

P1 is present-sink-specific and byte-identical: it moves WHERE the sink is provided (from
the uniform `session.py` default to each frontend's explicit consumer) without changing
any render behavior, except the forced chainlit fix (its incomplete outbox drain dropped
the render model — now repaired, since chainlit is a human surface that cannot NA-dodge).
The canonical user-reaching kind enum + full kind-complete typed consumer contract is P3.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from reyn.core.present.binding import ResolvedPresentation
    from reyn.core.present.renderer import PresentationRenderer
    from reyn.runtime.session import Session

# The reviewed set of surfaces that legitimately have NO human presentation drain, so a
# `present` op reaching them is a documented no-op (machine/headless surface). Membership
# is a REVIEWED ratchet (FP-0056 admin-6 equality model): a new human surface cannot add
# itself here to dodge providing a real sink. Each member states WHY it is NA:
#   - "web":     the A2A/web transport has no human web-UI present drain; its external
#                outbox interceptor routes only kind="agent" (external_routing.py:333),
#                so a "presentation" message reaches no surface.
#   - "mcp":     the stdio-MCP server is a history-harvest surface — no live present drain.
#   - "dogfood": the headless dogfood eval harness has no outbox/present drain at all.
_NA_PRESENTATION_SURFACES = frozenset({"web", "mcp", "dogfood"})


@runtime_checkable
class PresentationConsumer(Protocol):
    """A surface's declared ability to consume a `present` op's render model. `.sink(session)`
    yields the `PresentationRenderer` the Session wires into its per-turn OpContext — the ONLY
    supported way to obtain a sink. A surface with no `PresentationConsumer` cannot build a
    Session (the required-kwarg forcing), so an orphan sink is structurally impossible."""

    def sink(self, session: "Session") -> "PresentationRenderer": ...


class OutboxPresentationConsumer:
    """Consumer for surfaces whose Session outbox is drained by a live loop that renders the
    `"presentation"` message — the inline/plain CUI (`interfaces/repl/renderer.py`), chainlit
    (`chainlit_app/adapter.py::outbox_to_chainlit`), and the registry base session whose outbox
    is either drained by the REPL, overridden by `reyn pipe run` (self-delivering stdout), or
    bridged to a parent (#2707 driver forward).

    `.sink()` is the SOLE `OutboxPresentationRenderer` construction site in `src/reyn` (AST
    guard-enforced) — that single seam is what makes a bare orphan outbox sink impossible."""

    def sink(self, session: "Session") -> "PresentationRenderer":
        from reyn.runtime.session_buses import OutboxPresentationRenderer

        return OutboxPresentationRenderer(session)


class SpawnBridgePresentationConsumer:
    """A spawned/driver session's presentation consumer that DELEGATES to the PARENT's
    consumer bound to the PARENT session — the child's `present` output reaches the
    parent's surface *by construction* (#2708 P3.1).

    A chat-invoked pipeline runs in a spawned driver-session; a `present` step in it
    would otherwise render through the driver's OWN outbox sink, isolated from the parent
    chat (the #2688 orphan class). This consumer, wired ONLY on the attached driver-spawn
    path (`session_api._spawn_pipeline_driver_session`), makes the driver's present sink
    resolve to the PARENT session's sink instead: `sink(child)` ignores the child and
    returns `parent_consumer.sink(parent_session)`, so the render lands on the parent's
    outbox exactly as a chat-native present would.

    It STRUCTURALLY REPLACES the #2707 interim per-message outbox forward
    (`session_api.py::run_pipeline_attached`): with the sink inherited, the parent receives
    the present by construction (single delivery), not by a post-hoc copy of the driver's
    drained outbox. It constructs NO `OutboxPresentationRenderer` itself (it delegates), so
    the #2708 AST guard (`tests/test_present_sink_ast_guard_2708.py`) — which pins
    `OutboxPresentationConsumer.sink` as the SOLE renderer construction site — is
    unaffected."""

    def __init__(
        self, parent_consumer: "PresentationConsumer", parent_session: "Session"
    ) -> None:
        self._parent_consumer = parent_consumer
        self._parent_session = parent_session

    def sink(self, session: "Session") -> "PresentationRenderer":
        # The child `session` is intentionally ignored: bind to the PARENT so render()
        # writes to the parent's outbox (or, if the parent is itself a driver, recursively
        # up; or a NullPresentationSink if the parent is an NA surface — inherited for
        # free by delegating to the parent's own consumer).
        return self._parent_consumer.sink(self._parent_session)


class NullPresentationSink:
    """`PresentationRenderer` (`core/present/renderer.py`) for a surface with no human
    presentation drain: `render` is a documented no-op. Obtainable ONLY through
    `NullPresentationConsumer` (whose surface is a reviewed `_NA_PRESENTATION_SURFACES`
    member), so a human surface cannot silently reach this NA behavior."""

    surface_name = "none"

    def render(self, resolved: "ResolvedPresentation") -> None:
        # Documented no-op: this surface (web JSON / mcp harvest / dogfood eval) has no
        # human presentation drain, so the resolved render model is intentionally dropped.
        # The `present` op's ack + audit event still fire upstream (PR-A null-surface
        # semantics); only the visible draw is a no-op.
        return None


class NullPresentationConsumer:
    """Consumer for a reviewed NA surface (web / mcp / dogfood). Its `surface` MUST be a
    member of `_NA_PRESENTATION_SURFACES` — constructing one for any other (e.g. a human)
    surface raises, so a human surface cannot NA-dodge by passing a Null sink."""

    def __init__(self, surface: str) -> None:
        if surface not in _NA_PRESENTATION_SURFACES:
            raise ValueError(
                f"NullPresentationConsumer refused for surface {surface!r}: not a reviewed "
                f"NA surface. A human/visible surface must provide a real presentation "
                f"consumer; only {sorted(_NA_PRESENTATION_SURFACES)} may use a Null sink "
                f"(add here ONLY via review — see _NA_PRESENTATION_SURFACES rationale)."
            )
        self._surface = surface

    @property
    def surface(self) -> str:
        """The reviewed NA surface name this consumer was constructed for."""
        return self._surface

    def sink(self, session: "Session") -> "PresentationRenderer":
        return NullPresentationSink()


__all__ = [
    "NullPresentationConsumer",
    "NullPresentationSink",
    "OutboxPresentationConsumer",
    "PresentationConsumer",
    "SpawnBridgePresentationConsumer",
    "_NA_PRESENTATION_SURFACES",
]
