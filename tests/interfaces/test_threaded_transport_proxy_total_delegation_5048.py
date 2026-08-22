"""Tier 2: #5048 — ThreadedTransportProxy's own total-delegation claim,
made real the same way #4884 already did for _ErrorWatchingTransport.

lead-coder's own measurement (#5050③ review, the same night this PR
lands): of the production ClientTransport wrappers, exactly ONE
(_ErrorWatchingTransport) had a live gate against this exact shape — a
future method added to ClientTransport that a wrapper forgets to
delegate, silently falling through to ClientTransport's own base
default instead of the wrapped transport's real value. ThreadedTransport
Proxy had none, and the SAME night's ``state_ready()`` addition opened
exactly that hole (fixed in the #5050③ review round) — caught only
because lead-coder happened to enumerate all 18 methods by hand before
that PR, not by a gate.

Why THIS class, now (issuecomment-5377858174): before #5048,
ThreadedTransportProxy had ZERO production call sites (its own class
docstring, pre-#5048: "NO production call site") — a delegation gap here
had no real consequence. #5048 wires it in as TextualChatApp's/run_repl's
DEFAULT local transport, so a silent delegation gap now means every local
session reads a base-default (wrong) answer through the thread boundary
instead of the worker-owned transport's real one. Per lead-coder's
explicit scoping: this gate covers ONLY ThreadedTransportProxy — NOT a
general delegation-completeness sweep across every ClientTransport
wrapper (SessionBoundTransport is a deliberate SEND-side-only design,
13/18 by intent, not an oversight; generalizing the #4884 shape to every
wrapper is #5043's own scope, not this issue's)."""
from __future__ import annotations

from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.threaded import ThreadedTransportProxy


def _own_public_methods(cls: type) -> "set[str]":
    """Names of public callables defined directly on ``cls``'s own class
    body (``vars(cls)``, NOT ``dir(cls)`` — the latter includes inherited
    members, which is exactly the silent-fallback shape under test)."""
    return {
        name for name, member in vars(cls).items()
        if not name.startswith("_") and callable(member)
    }


def test_threaded_transport_proxy_overrides_every_client_transport_method() -> None:
    """Tier 2: every PUBLIC ClientTransport method must be defined directly
    on ThreadedTransportProxy's own class body, not silently inherited
    from ClientTransport's own (deliberate, for OTHER narrow-purpose test
    stubs) default. A member missing here means a caller crossing this
    thread boundary gets ClientTransport's fallback value instead of the
    worker-owned inner transport's real one — invisible until #5048's own
    production cutover, at which point it is every local session's
    answer, not a hypothetical one."""
    base_public = _own_public_methods(ClientTransport)
    own_public = _own_public_methods(ThreadedTransportProxy)
    missing = base_public - own_public
    assert not missing, (
        f"ThreadedTransportProxy's own class body does not define "
        f"{sorted(missing)} — it silently falls through to "
        f"ClientTransport's own base default for these, and (post-#5048) "
        f"that default becomes every local session's answer, not the "
        f"worker-owned inner transport's real one."
    )


def test_positive_control_the_walk_actually_sees_client_transport_methods() -> None:
    """Tier 2: positive control (per this repo's own testing policy —
    a broken enumeration passes the ceiling above trivially) — confirms
    ``_own_public_methods`` genuinely finds real members, not an empty
    set that would make the assertion above vacuous."""
    base_public = _own_public_methods(ClientTransport)
    assert {"request_attach", "request_session_switch", "shutdown"} <= base_public, (
        f"the member-enumeration is broken — it produced {base_public!r}, "
        "which does not contain methods ClientTransport certainly has."
    )
