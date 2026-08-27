"""Tier 2: #2120 — spawn_session is ADVERTISED (reachable from chat), not just registered.

The #2120 defect (tui-found): spawn_session was registered + floored but NOT in
build_tools' per-tool advertising enumeration → absent from the LLM's individual-mode
tool catalog → unreachable → the S1bc primitive unusable. Root: build_tools manually
enumerates each router-only tool (per-tool blocks); the spawn_session block was missed
(the #1953/#2120 router=allow-but-unadvertised drift).

This pins reachability: spawn_session (a router-only, static-schema spawn primitive) is
advertised by build_tools in individual-tool mode (the live mode — a ~72-tool catalog).
send_to_session (its unconditional router-only peer, proposal 0067 P5 #4101 — the
original sentinel here, delegate_to_agent, retired in P6 #3978) is the paired sentinel —
both must advertise; a regression that drops spawn_session's block again → RED.

(The broader "every router=allow tool advertised-or-exempt" invariant is mode/condition-
dependent — 31 tools are universal-catalog-routed [invoke_action] or deeper-gated — so a
clean blanket guard needs a build_tools data-driven refactor; flagged for lead. The
universal-catalog channel for spawn_session [multi_agent__session_spawn] is a future-mode
follow-up, only live once universal_wrappers flips.)

Real build_tools + the real default registry; no mocks.
"""
from __future__ import annotations

from reyn.runtime.router_tools import build_tools


def _advertised(**kw) -> set:
    return {t.get("function", {}).get("name") for t in build_tools(**kw)}


def test_session_spawn_is_advertised_individual_mode() -> None:
    """Tier 2: spawn_session is in the individual-mode tool catalog (the live chat mode)
    — reachable, not just registered. The #2120 regression guard."""
    advertised = _advertised()  # minimal config = unconditional router-only tools
    assert "spawn_session" in advertised, (
        "spawn_session registered + floored but NOT advertised by build_tools — the "
        "#2120 unreachable defect (add the per-tool block in router_tools.build_tools)"
    )


def test_send_to_session_paired_sentinel_advertised() -> None:
    """Tier 2: the paired unconditional router-only sentinel — send_to_session (the
    block spawn_session mirrors) is advertised, so the test pins the shared enumeration
    path, not a spawn_session-only fluke."""
    assert "send_to_session" in _advertised()


def test_session_spawn_schema_is_advertised_complete() -> None:
    """Tier 2: the advertised spawn_session carries its spawn-time schema (the mode
    enum + request) — the LLM sees a usable tool, not a name-only stub."""
    tool = next(
        t for t in build_tools()
        if t.get("function", {}).get("name") == "spawn_session"
    )
    props = tool["function"]["parameters"]["properties"]
    assert props["mode"]["enum"] == ["ephemeral", "persistent"]
    assert "request" in props


def test_session_spawn_stripped_in_wrappers_mode() -> None:
    """Tier 2: in exclusive-wrapper mode spawn_session is stripped from the
    per-tool surface (routed via the universal catalog) — it must NOT leak
    as a leftover individual tool. delegate_to_agent (the former paired
    strip-list sentinel here) retired in proposal 0067 P6 (#3978); no
    currently-registered router-only tool shares spawn_session's specific
    stripped-in-wrappers-mode behavior (send_to_session/run_prompt/
    spawn_agent/create_topology all stay advertised in this mode — verified
    directly), so there is no substitute paired sentinel to assert here."""
    wrapped = _advertised(universal_wrappers_enabled=True)
    assert "spawn_session" not in wrapped
