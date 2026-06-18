"""Tier 2: #1593/#1627 — the scheme-owned ``Presentation.sp_fragment`` / slot-map SP channel.

#1627 Stage 4: ``build_system_prompt`` is now a pure slot-injector. The legacy
``scheme_sp_fragment`` backward-compat param is still accepted and appended
verbatim. The canonical path is ``tool_use_sp["slot_post_catalog"]`` (used by
retrieval's ``_search_sp``); ``scheme_sp_fragment`` stays for callers that haven't
migrated. This file pins the OS-side contract for the fragment channel:

  1. Empty fragment + empty slot-map ⇒ no fragment in the SP.
  2. A non-empty fragment appears verbatim in the rendered SP.
  3. The fragment is appended (OS-agnostic): the OS does not transform, prefix,
     or wrap it — what the scheme wrote is what lands.
  4. The fragment sits before the volatile context-size signal (so the
     scheme's tool-use SP stays in the cached prefix, not after the tail).

No mocks. Tests call ``build_system_prompt`` with real arguments.
"""
from __future__ import annotations

from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots


def _base_slots() -> "dict[str, str]":
    """Minimal universal slot-map (wrappers on, search off) — mirrors the
    prior universal_wrappers_enabled=True / search_actions_enabled=False call."""
    return build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        non_interactive=False,
    )


def _sp(*, scheme_sp_fragment: str = "", context_size_signal: str | None = None) -> str:
    return build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        tool_use_sp=_base_slots(),
        scheme_sp_fragment=scheme_sp_fragment,
        context_size_signal=context_size_signal,
    )


# ── 1. Empty fragment = byte-identical to same call with no fragment arg ────────


def test_empty_fragment_leaves_named_gate_sp_unchanged() -> None:
    """Tier 2: #1593/#1627 — ``scheme_sp_fragment=""`` renders an SP equal to the call
    that passes no fragment argument; the empty channel is invisible, permanent contract."""
    without_arg = build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        tool_use_sp=_base_slots(),
    )
    with_empty = _sp(scheme_sp_fragment="")
    assert with_empty == without_arg, (
        "empty sp_fragment must leave the SP byte-identical to the named-gate path"
    )


# ── 2. Non-empty fragment appears verbatim ───────────────────────────────────


def test_non_empty_fragment_appears_in_sp() -> None:
    """Tier 2: #1593 — a scheme's free-form tool-use SP lands verbatim in the SP."""
    fragment = "## Code API\nCall tools by writing Python: `result = file__read(path=...)`."
    sp = _sp(scheme_sp_fragment=fragment)
    assert fragment in sp, "the scheme sp_fragment must appear verbatim in the SP"


def test_fragment_is_not_transformed_by_the_os() -> None:
    """Tier 2: #1593 — the OS appends the fragment verbatim (P7): it does not
    interpret, re-wrap, or strip the scheme's text. A fragment that mentions a
    scheme-specific concept the OS has no name for survives untouched."""
    fragment = "SEARCH-PARADIGM-SENTINEL: emit search_actions(query=...) then call a match."
    sp = _sp(scheme_sp_fragment=fragment)
    assert "SEARCH-PARADIGM-SENTINEL: emit search_actions(query=...) then call a match." in sp


# ── 3. Placement: before the volatile context-size signal ────────────────────


def test_fragment_precedes_context_size_signal() -> None:
    """Tier 2: #1593 — the fragment is rendered before the (LAST-placed)
    context-size signal, so the scheme's tool-use SP sits with the cached
    prefix rather than after the most-volatile tail section."""
    fragment = "FRAGMENT-SENTINEL-XYZ"
    signal = "CONTEXT-SIGNAL-SENTINEL"
    sp = _sp(scheme_sp_fragment=fragment, context_size_signal=signal)
    assert fragment in sp and signal in sp
    assert sp.index(fragment) < sp.index(signal), (
        "the scheme fragment must precede the context-size signal in the SP"
    )
