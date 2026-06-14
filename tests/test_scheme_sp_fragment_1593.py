"""Tier 2: #1593 — the scheme-owned ``Presentation.sp_fragment`` SP channel.

A tool-use scheme shapes the system prompt through two deliberately separate
channels (see ``Presentation``):

  - ``sp_params`` — named gates ``build_system_prompt`` already understands
    (``universal_wrappers_enabled`` / ``search_actions_enabled`` …). The
    named-gate schemes (universal-category, enumerate-all) express their whole
    SP through these, leaving ``sp_fragment`` empty.
  - ``sp_fragment`` — free-form, scheme-owned tool-use SP text the OS appends
    **verbatim** without interpreting it (P7: the OS has no notion of
    "code-API" / "search-SP"). CodeAct / retrieval are the first consumers.

This file pins the OS-side contract of that second channel:

  1. Empty fragment (the default) ⇒ the SP is byte-identical to the call with
     no fragment argument at all — the named-gate path is untouched.
  2. A non-empty fragment appears verbatim in the rendered SP.
  3. The fragment is appended (OS-agnostic): the OS does not transform, prefix,
     or wrap it — what the scheme wrote is what lands.
  4. The fragment sits before the volatile context-size signal (so the
     scheme's tool-use SP stays in the cached prefix, not after the tail).

No mocks. Tests call ``build_system_prompt`` with real arguments.
"""
from __future__ import annotations

from reyn.chat.router_system_prompt import build_system_prompt


def _sp(*, scheme_sp_fragment: str = "", context_size_signal: str | None = None) -> str:
    return build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        scheme_sp_fragment=scheme_sp_fragment,
        context_size_signal=context_size_signal,
    )


# ── 1. Empty fragment = byte-identical named-gate path ───────────────────────


def test_empty_fragment_leaves_named_gate_sp_unchanged() -> None:
    """Tier 2: #1593 — default ``sp_fragment=""`` renders an SP equal to the call
    that passes no fragment argument; the named-gate schemes (universal-category
    / enumerate-all) are therefore unchanged by this seam. This is a permanent
    contract (the empty channel is invisible), not a refactor-equivalence check."""
    without_arg = build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
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
