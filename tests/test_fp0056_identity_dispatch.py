"""Tier 1: canonicalization dispatches on INVOKED IDENTITY (``source=``), not ``result["kind"]``.

The 2026-07-09 dogfood incident root: ``reyn_src_read`` returns a kind-LESS ``{path, content}`` dict,
so the old ``result["kind"]`` dispatch fell through to the whole-dict structured fallback and hid the
document body. FP-0056 PR-F1 resolves the mapper from what the chokepoint invoked — so a kind-less
result canonicalizes correctly, and a MISLEADING kind on the result is irrelevant.
"""
from __future__ import annotations

from reyn.core.offload.canonical import (
    CANONICAL_TODO,
    STRUCTURED_PASSTHROUGH,
    canonical_declaration,
    to_canonical,
)


def test_kindless_reyn_src_result_canonicalizes_via_source() -> None:
    """Tier 1: a reyn_src-shaped result with NO ``kind`` field surfaces its ``content`` as ``text``
    when dispatched by ``source`` — the incident class, fixed."""
    result = {"path": "docs/x.md", "content": "# Title\n\nbody text"}
    assert "kind" not in result
    canonical = to_canonical(result, source="reyn_src_read")
    assert canonical["text"] == "# Title\n\nbody text"
    assert not any(a.get("kind") == "structured" for a in canonical["attachments"])


def test_source_wins_over_a_misleading_result_kind() -> None:
    """Tier 1: ``result["kind"]`` is no longer load-bearing — a result carrying a WRONG ``kind`` still
    canonicalizes by the invoked ``source`` (file read → clean text), not by the misleading kind."""
    result = {"kind": "web_search", "op": "read", "path": "a.md", "status": "ok", "content": "FILE BODY"}
    canonical = to_canonical(result, source="read_file")
    assert canonical["text"] == "FILE BODY", "resolved via source=read_file, not kind=web_search"


def test_unknown_source_falls_back_lossless_and_visible() -> None:
    """Tier 1: a genuinely unregistered ``source`` (or ``None``) keeps the lossless whole-dict
    fallback — nothing is lost, the whole dict is a structured attachment (PR-F2 adds the audit
    event on this path)."""
    result = {"kind": "totally_unknown", "field": [1, 2, 3]}
    canonical = to_canonical(result, source="totally_unknown_producer")
    assert canonical["text"] == ""
    assert canonical["attachments"] == [{"kind": "structured", "data": result}]
    # source=None is the same lossless fallback (not a crash).
    assert to_canonical(result, source=None)["attachments"][0]["data"] == result


def test_structured_passthrough_admin_result_passes_through_whole_dict() -> None:
    """Tier 1: an admin/install op declared STRUCTURED_PASSTHROUGH surfaces its whole dict as a
    ``structured`` attachment (the reviewed opt-in behaves like the lossless fallback, but by an
    explicit declaration — not a silent fall-through)."""
    result = {"kind": "mcp_install", "status": "ok", "server": "acme", "installed": True}
    canonical = to_canonical(result, source="mcp_install")
    assert canonical["text"] == ""
    assert canonical["attachments"] == [{"kind": "structured", "data": result}]
    # And the declaration really is the passthrough sentinel, not a bespoke mapper.
    assert canonical_declaration("mcp_install") is STRUCTURED_PASSTHROUGH


def test_canonical_todo_producer_gets_the_whole_dict_fallback() -> None:
    """Tier 1: a ``CANONICAL_TODO`` producer (declared, pending a real mapper) takes the SAME lossless
    whole-dict fallback as an unknown source — behavior-preserving vs the pre-framework silent
    fallback, but now an explicit, ratcheted, tracked declaration (issue #2681)."""
    result = {"servers": [{"name": "acme"}], "status": "ok"}
    canonical = to_canonical(result, source="list_mcp_servers")
    assert canonical["text"] == ""
    assert canonical["attachments"] == [{"kind": "structured", "data": result}]
    assert canonical_declaration("list_mcp_servers") is CANONICAL_TODO


def test_triaged_text_shaped_producers_surface_their_text_not_a_blob() -> None:
    """Tier 1: the two producers triaged as text-shaped in F1 got REAL mappers (not CANONICAL_TODO) —
    ``read_memory_body`` surfaces the memory body as ``text`` (the file-class / G12 attractor), and
    ``ask_user`` surfaces the user's answer — instead of hiding them in a whole-dict blob."""
    mem = to_canonical(
        {"content": "Yasuda", "layer": "user", "slug": "identity"}, source="read_memory_body",
    )
    assert mem["text"] == "Yasuda" and not mem["attachments"]

    ans = to_canonical(
        {"kind": "ask_user", "question": "name?", "answer": "Ada", "status": "ok"}, source="ask_user",
    )
    assert ans["text"] == "Ada" and not ans["attachments"]

    # Neither is a passthrough/todo marker — they resolve to real callable mappers.
    assert callable(canonical_declaration("read_memory_body"))
    assert callable(canonical_declaration("ask_user"))
