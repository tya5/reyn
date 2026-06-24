"""Tier 2: #2120 — session_spawn is ADVERTISED (reachable from chat), not just registered.

The #2120 defect (tui-found): session_spawn was registered + floored but NOT in
build_tools' per-tool advertising enumeration → absent from the LLM's individual-mode
tool catalog → unreachable → the S1bc primitive unusable. Root: build_tools manually
enumerates each router-only tool (per-tool blocks); the session_spawn block was missed
(the #1953/#2120 router=allow-but-unadvertised drift).

This pins reachability: session_spawn (a router-only, static-schema spawn primitive) is
advertised by build_tools in individual-tool mode (the live mode — a ~72-tool catalog).
delegate_to_agent (its unconditional router-only peer) is the paired sentinel — both
must advertise; a regression that drops session_spawn's block again → RED.

(The broader "every router=allow tool advertised-or-exempt" invariant is mode/condition-
dependent — 31 tools are universal-catalog-routed [invoke_action] or deeper-gated — so a
clean blanket guard needs a build_tools data-driven refactor; flagged for lead. The
universal-catalog channel for session_spawn [multi_agent__session_spawn] is a future-mode
follow-up, only live once universal_wrappers flips.)

Real build_tools + the real default registry; no mocks.
"""
from __future__ import annotations

from reyn.runtime.router_tools import build_tools


def _advertised(**kw) -> set:
    return {t.get("function", {}).get("name") for t in build_tools([], [], **kw)}


def test_session_spawn_is_advertised_individual_mode() -> None:
    """Tier 2: session_spawn is in the individual-mode tool catalog (the live chat mode)
    — reachable, not just registered. The #2120 regression guard."""
    advertised = _advertised()  # minimal config = unconditional router-only tools
    assert "session_spawn" in advertised, (
        "session_spawn registered + floored but NOT advertised by build_tools — the "
        "#2120 unreachable defect (add the per-tool block in router_tools.build_tools)"
    )


def test_delegate_to_agent_paired_sentinel_advertised() -> None:
    """Tier 2: the paired unconditional router-only sentinel — delegate_to_agent (the
    block session_spawn mirrors) is advertised, so the test pins the shared enumeration
    path, not a session_spawn-only fluke."""
    assert "delegate_to_agent" in _advertised()


def test_session_spawn_schema_is_advertised_complete() -> None:
    """Tier 2: the advertised session_spawn carries its spawn-time schema (the mode
    enum + request) — the LLM sees a usable tool, not a name-only stub."""
    tool = next(
        t for t in build_tools([], [])
        if t.get("function", {}).get("name") == "session_spawn"
    )
    props = tool["function"]["parameters"]["properties"]
    assert props["mode"]["enum"] == ["ephemeral", "persistent"]
    assert "request" in props


def test_session_spawn_stripped_in_wrappers_mode_like_delegate() -> None:
    """Tier 2: in exclusive-wrapper mode session_spawn is stripped from the per-tool
    surface (routed via the universal catalog, like delegate_to_agent) — it must NOT
    leak as a leftover individual tool. Pins the strip-list pairing with its sentinel:
    a regression that adds session_spawn's per-tool block but forgets the wrappers-mode
    strip → session_spawn present while delegate_to_agent is absent → RED."""
    wrapped = _advertised(universal_wrappers_enabled=True)
    assert "delegate_to_agent" not in wrapped  # the established sentinel
    assert "session_spawn" not in wrapped
