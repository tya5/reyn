"""Tier 1: #2425 案B — the offload seam builds frontmatter+text where text is the sole text-payload.

``build_offload_body`` turns a canonical result into ``(frontmatter, text, media_blocks)``: media → the
vision follow-up; a large structured → its OWN offload ref (``structured_ref`` + preview, non-competing);
text → the body the caller caps. A large structured is a separate ref, never a second oversized field in
the text-offload decision (the whole-envelope root, gone by shape). ``render_tool_result`` assembles the
LLM-visible frontmatter+text string.
"""
from __future__ import annotations

import yaml

from reyn.core.offload.canonical import to_canonical
from reyn.core.offload.seam import build_offload_body, render_tool_result


def _fake_save(value, **_kw) -> dict:
    """Records what was stored; returns a path-ref block like MediaStore.save_tool_result."""
    _fake_save.stored.append(value)
    return {"path": f".reyn/tool-results/{len(_fake_save.stored):04d}.txt", "content_hash": "h"}


_fake_save.stored = []


def test_large_structured_is_offloaded_to_own_ref_not_competing_with_text():
    """Tier 1: CORE — a canonical result with text AND a LARGE structured attachment: the structured
    is offloaded to its own ref (``structured_ref`` + preview), NOT inlined and NOT merged into text.
    So ``text`` stays the sole text-offload payload — the whole-envelope root is gone by shape."""
    _fake_save.stored = []
    canonical = to_canonical({
        "kind": "mcp", "status": "ok", "server": "s", "tool": "t",
        "content": "the body text",
        "structured": {"rows": ["x" * 3000]},  # large → its own ref
    })
    frontmatter, text, media = build_offload_body(canonical, save_fn=_fake_save)

    assert text == "the body text", "text is the body payload the caller caps"
    assert frontmatter.get("structured") == "offloaded", "the large structured is offloaded"
    assert frontmatter.get("structured_ref"), "a read-back ref is carried in the frontmatter"
    assert frontmatter.get("structured_preview"), "a short preview replaces the data"
    assert _fake_save.stored, "the large structured body was stored (retrievable)"
    assert media == [], "no media in this result"


def test_small_structured_stays_inline():
    """Tier 1: a small structured attachment is kept inline in the frontmatter (no offload)."""
    _fake_save.stored = []
    canonical = to_canonical({"kind": "mcp", "status": "ok", "server": "s", "tool": "t",
                              "content": "hi", "structured": {"n": 1}})
    frontmatter, _text, _media = build_offload_body(canonical, save_fn=_fake_save)
    assert frontmatter.get("structured") == {"n": 1}, "small structured stays inline"
    assert _fake_save.stored == [], "no offload for a small structured"


def test_structured_stays_inline_without_a_store():
    """Tier 1: format ⊥ store — with no ``save_fn`` (media_store absent) a large structured cannot be
    offloaded, so it is kept INLINE (never dropped); the frontmatter format still applies."""
    canonical = to_canonical({"kind": "mcp", "status": "ok", "server": "s", "tool": "t",
                              "content": "hi", "structured": {"rows": ["x" * 3000]}})
    frontmatter, _text, _media = build_offload_body(canonical, save_fn=None)
    assert frontmatter.get("structured") == {"rows": ["x" * 3000]}, "kept inline with no store"


def test_media_blocks_returned_for_followup_not_in_body():
    """Tier 1: media attachments are returned as raw blocks for the caller's vision follow-up and are
    NOT left in the frontmatter/text (preserves the existing image-forwarding byte path)."""
    _fake_save.stored = []
    canonical = to_canonical({"kind": "mcp", "status": "ok", "server": "s", "tool": "t",
                              "content": "hi", "media_blocks": [{"type": "image", "data": "b64"}]})
    frontmatter, _text, media = build_offload_body(canonical, save_fn=_fake_save)
    assert media == [{"type": "image", "data": "b64"}], "media returned for the vision follow-up"
    assert "structured" not in frontmatter, "media is not left in the tool-message body"


def test_render_plain_text_when_no_frontmatter():
    """Tier 1: no structured/signal-meta → the LLM-visible content is the plain text (no wrapper)."""
    frontmatter, text, _media = build_offload_body(
        to_canonical({"kind": "mcp", "status": "ok", "content": "just text", "media_blocks": []}),
        save_fn=_fake_save,
    )
    assert render_tool_result(frontmatter, text) == "just text", "plain text, no JSON, no wrapper"


def test_render_frontmatter_then_text_is_parseable_yaml():
    """Tier 1: structured/signal-meta present → a ``---``-delimited YAML frontmatter block precedes the
    text body, and the block parses back to the structured data (no exact-whitespace pin)."""
    frontmatter, text, _media = build_offload_body(
        to_canonical({"kind": "sandboxed_exec", "status": "error", "returncode": 3,
                      "stdout": "the output", "stderr": ""}),
        save_fn=_fake_save,
    )
    rendered = render_tool_result(frontmatter, text)
    assert rendered.startswith("---\n"), "frontmatter block leads"
    head, _, body = rendered[4:].partition("\n---\n")
    assert yaml.safe_load(head).get("returncode") == 3, "signal meta round-trips through the YAML"
    assert body == "the output", "the text body follows the frontmatter"


def test_render_edge_guard_text_starting_with_triple_dash():
    """Tier 1: edge guard — with no frontmatter, a text body that itself starts with ``---`` is
    prefixed with a blank line so it cannot be misparsed as a frontmatter block."""
    out = render_tool_result({}, "---\nnot frontmatter\n---")
    assert out.startswith("\n---"), "a leading blank line prevents frontmatter misparse"
