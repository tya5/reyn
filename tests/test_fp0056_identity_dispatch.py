"""Tier 1: canonicalization dispatches on INVOKED IDENTITY (``source=``), not ``result["kind"]``.

The 2026-07-09 dogfood incident root: ``reyn_src_read`` returns a kind-LESS ``{path, content}`` dict,
so the old ``result["kind"]`` dispatch fell through to the whole-dict structured fallback and hid the
document body. FP-0056 PR-F1 resolves the mapper from what the chokepoint invoked — so a kind-less
result canonicalizes correctly, and a MISLEADING kind on the result is irrelevant.
"""
from __future__ import annotations

from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH, to_canonical


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
    from reyn.core.offload.canonical import canonical_declaration

    assert canonical_declaration("mcp_install") is STRUCTURED_PASSTHROUGH
