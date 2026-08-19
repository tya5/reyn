"""Tier 2: #4884 — _ErrorWatchingTransport's own class docstring claims total,
explicit delegation ("Delegation is total and explicit: every method
forwards, so a handler that reaches for any other part of the seam reaches
the real one") — this test is what makes that claim true rather than
aspirational.

#4884: 4 ClientTransport methods (attach_failed / request_attach /
request_session_switch / request_artifact_list) were silently missing from
_ErrorWatchingTransport's own class body. A caller going through the
wrapper (every slash handler does, via execute_slash_command) fell through
to ClientTransport's own base-class default instead of the wrapped
transport's real value — not a crash, a silently WRONG answer. `/agent
new`'s `request_attach` always read False through this wrapper, even when
the real attach genuinely succeeded (reproduced directly against a fake
inner transport whose real request_attach returns True).

Reyn-reviewer + lead-coder's initial pass named 2 of the 4 (grep-checking
only the names already handed to them, #4880/§24's own "independent
measurement is not independence when the question was handed to you") — a
full AST/reflection diff against every ClientTransport member found the
other 2. This test is that diff, kept live so a future method added to
ClientTransport that this wrapper forgets to delegate is caught
immediately, not discovered by a silent-failure bug report.
"""
from __future__ import annotations

from reyn.interfaces.slash.dispatch import _ErrorWatchingTransport
from reyn.interfaces.transport.client_transport import ClientTransport


def _own_public_methods(cls: type) -> "set[str]":
    """Names of public callables defined directly on ``cls``'s own class
    body (``vars(cls)``, NOT ``dir(cls)`` — the latter includes inherited
    members, which is exactly the silent-fallback shape under test)."""
    return {
        name for name, member in vars(cls).items()
        if not name.startswith("_") and callable(member)
    }


def test_error_watching_transport_overrides_every_client_transport_method() -> None:
    """Tier 2: every PUBLIC ClientTransport method must be defined directly
    on _ErrorWatchingTransport's own class body, not silently inherited
    from ClientTransport's own (deliberate, for OTHER narrow-purpose test
    stubs — see request_attach's own docstring) default. A member missing
    here means a caller reaching through this specific wrapper gets
    ClientTransport's fallback value instead of the wrapped transport's
    real one."""
    base_public = _own_public_methods(ClientTransport)
    own_public = _own_public_methods(_ErrorWatchingTransport)
    missing = base_public - own_public
    assert not missing, (
        f"_ErrorWatchingTransport's own class body does not define "
        f"{sorted(missing)} — it silently falls through to "
        f"ClientTransport's own base default for these, contradicting "
        f"its own docstring's total-delegation claim."
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
