"""Tier 2: #2425 案B — canonical tool-result normalization removes the offload field-guess.

The spec hole was that offload GUESSED the payload field (per-op marker + sole-oversized), breaking on
a 2nd large field (owner's chat-MCP whole-envelope: a large ``structuredContent`` alongside
``content``). ``to_canonical`` maps each result to ``{text, attachments, source_ref, meta}`` so ``text``
is the payload by construction and non-text (structured/media) is an ``attachment`` kept OUT of the
offload decision — so the whole-envelope case cannot structurally occur.
"""
from __future__ import annotations

import json

from reyn.core.offload.canonical import to_canonical


def test_mcp_content_becomes_text_structured_and_media_become_attachments():
    """Tier 2: CORE — an MCP result with a large ``content`` AND a large ``structured`` (the owner
    whole-envelope root) normalizes so ``text`` = the content body and BOTH structured + media are
    typed attachments (out of the offload decision) — no field to guess, no whole-dict fallback."""
    mcp = {
        "kind": "mcp", "status": "ok", "server": "s", "tool": "t",
        "content": "the body text", "structured": {"rows": [1, 2, 3]},
        "media_blocks": [{"type": "image", "data": "..."}],
    }
    c = to_canonical(mcp)

    assert c["text"] == "the body text", "content → the single offload payload (text)"
    kinds = [a["kind"] for a in c["attachments"]]
    assert "structured" in kinds and "media" in kinds, "structured + media are typed attachments"
    assert any(a.get("data") == {"rows": [1, 2, 3]} for a in c["attachments"]), "structured preserved (not dropped)"
    assert c["source_ref"] is None, "MCP is transient → no on-disk origin → its body must be stored"
    assert c["meta"].get("status") == "ok" and c["meta"].get("server") == "s", "small status → meta (inline)"


def test_mcp_without_structured_has_no_structured_attachment():
    """Tier 2: the common case — an MCP result with only text has ``text`` set and no structured
    attachment (clean end-state, no shim)."""
    c = to_canonical({"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "hi",
                      "media_blocks": []})
    assert c["text"] == "hi"
    assert not any(a["kind"] == "structured" for a in c["attachments"])


def test_unregistered_kind_falls_back_losslessly():
    """Tier 2: an op not yet migrated to a canonical mapper round-trips through a whole-dict fallback
    (``text`` = the JSON) — migration is incremental and lossless, never a data-loss."""
    result = {"kind": "web_fetch", "status": "ok", "content": "page", "results": [1, 2]}
    c = to_canonical(result)
    assert json.loads(c["text"]) == result, "the whole dict is preserved as text until the op migrates"
    assert c["attachments"] == [] and c["source_ref"] is None
