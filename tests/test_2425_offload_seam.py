"""Tier 2: #2425 案B step1b — the offload seam builds a body where text is the sole payload.

``build_offload_body`` turns a canonical result into ``(body, media_blocks)``: media → the vision
follow-up; large structured → its OWN offload ref (preserved, non-competing); text → the body payload
the caller caps with ``payload_field="text"``. This is why the whole-envelope collapse cannot occur:
a large structured is a separate ref, never a second oversized field in the text-offload decision.
"""
from __future__ import annotations

from reyn.core.offload.canonical import to_canonical
from reyn.core.offload.seam import build_offload_body


def _fake_save(value, **_kw) -> dict:
    """Records what was stored; returns a path-ref block like MediaStore.save_tool_result."""
    _fake_save.stored.append(value)
    return {"path": f".reyn/tool-results/{len(_fake_save.stored):04d}.txt", "content_hash": "h"}


_fake_save.stored = []


def test_large_structured_is_offloaded_to_own_ref_not_competing_with_text():
    """Tier 2: CORE — a canonical result with text AND a LARGE structured attachment: the structured
    is offloaded to its own ref (preview + _offload_ref), NOT inlined and NOT merged into the text
    payload. So ``text`` stays the sole offload payload — the whole-envelope root is gone by shape."""
    _fake_save.stored = []
    canonical = to_canonical({
        "kind": "mcp", "status": "ok", "server": "s", "tool": "t",
        "content": "the body text",
        "structured": {"rows": ["x" * 3000]},  # large → its own ref
    })
    body, media = build_offload_body(canonical, save_fn=_fake_save)

    assert body["text"] == "the body text", "text is the body payload (offloaded via payload_field=text)"
    atts = body.get("attachments", [])
    assert any(a.get("kind") == "structured" and a.get("_offload_ref") for a in atts), \
        "the large structured is offloaded to its OWN ref (preserved, not dropped)"
    assert _fake_save.stored, "the large structured body was stored (retrievable)"
    # it is NOT inlined as raw data (would bloat the body) and NOT part of text
    assert not any(a.get("data") for a in atts), "large structured is a ref, not inline data"
    assert media == [], "no media in this result"


def test_small_structured_stays_inline():
    """Tier 2: a small structured attachment is kept inline (no offload) — cheap, no ref churn."""
    _fake_save.stored = []
    canonical = to_canonical({"kind": "mcp", "status": "ok", "server": "s", "tool": "t",
                              "content": "hi", "structured": {"n": 1}})
    body, _media = build_offload_body(canonical, save_fn=_fake_save)
    atts = body.get("attachments", [])
    assert any(a.get("data") == {"n": 1} for a in atts), "small structured stays inline"
    assert _fake_save.stored == [], "no offload for a small structured"


def test_media_blocks_returned_for_followup_not_in_body():
    """Tier 2: media attachments are returned as raw blocks for the caller's vision follow-up and are
    NOT left in the body (preserves the existing image-forwarding byte path)."""
    _fake_save.stored = []
    canonical = to_canonical({"kind": "mcp", "status": "ok", "server": "s", "tool": "t",
                              "content": "hi", "media_blocks": [{"type": "image", "data": "b64"}]})
    body, media = build_offload_body(canonical, save_fn=_fake_save)
    assert media == [{"type": "image", "data": "b64"}], "media returned for the vision follow-up"
    assert "attachments" not in body or all(a.get("kind") != "media" for a in body.get("attachments", [])), \
        "media is not left in the tool-message body"


def test_meta_is_inline_and_text_present():
    """Tier 2: small meta (status/server/tool) is inline in the body; text is present as the payload."""
    _fake_save.stored = []
    canonical = to_canonical({"kind": "mcp", "status": "ok", "server": "srv", "tool": "t", "content": "body"})
    body, _media = build_offload_body(canonical, save_fn=_fake_save)
    assert body["text"] == "body" and body.get("status") == "ok" and body.get("server") == "srv"
