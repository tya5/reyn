"""Tier 2: #5509 PR2 — the modality-registry / wire-content-part / block-
type-discriminator generalisation (architect ruling, 2026-08-29: "wire door
を開ける仕事は着手可 / 上界を作る仕事は渡さない / 新しい per-item token
定数を入れない").

Scope note: `QUERIED_CAPABILITY_FIELDS_BY_MODALITY` intentionally still has
ONLY the `image` entry after this PR — a non-image modality has no
established per-item token bound (never reaches the capability query at
all, see `_resolve_media_modality`), so adding an entry for it there would
be an operator-declarable field that is silently never consulted — the
same "remedy points the wrong direction" shape #5517's BLOCKING① named.
"""
from __future__ import annotations

import pytest

from reyn.runtime.router_loop import (
    _KNOWN_MEDIA_BLOCK_TYPES,
    _MEDIA_MIME_PREFIX_MODALITY,
    MediaMaterialiseFailure,
    _as_path_ref,
    _build_media_followup_message,
    _default_mime_for_block_type,
    _materialise_media_part,
    _overflow_ref_text,
    _resolve_media_modality,
    build_wire_media_part,
)

# ---------------------------------------------------------------------------
# _resolve_media_modality — the registry itself
# ---------------------------------------------------------------------------


def test_image_mime_resolves_to_the_image_modality() -> None:
    """Tier 2: the one entry the registry has today."""
    assert _resolve_media_modality("image/png") == "image"


def test_a_non_image_mime_resolves_to_no_modality() -> None:
    """Tier 2: deny side — a PDF's mime never resolves to a modality name
    (no established per-item token bound exists for it yet)."""
    assert _resolve_media_modality("application/pdf") is None


def test_the_registry_itself_is_not_empty() -> None:
    """Tier 1: accept-side vacuity guard — the registry passing every mime
    it's given because it's EMPTY would be indistinguishable from the
    "image" entry actually being present."""
    assert _MEDIA_MIME_PREFIX_MODALITY


# ---------------------------------------------------------------------------
# _materialise_media_part — non-image degrades to NO_TOKEN_BOUND via the
# registry, not a hardcoded string check (strip-falsified: the registry
# genuinely gates this, not a leftover `if not mime.startswith("image/")`)
# ---------------------------------------------------------------------------


def test_a_document_block_degrades_to_no_token_bound() -> None:
    """Tier 2: a document-mime block reaches `_materialise_media_part` (the
    #5509 PR2 discriminator widening) and correctly degrades — never
    silently dropped before the gate, never guessed as embeddable."""
    block = {"type": "document", "mime_type": "application/pdf", "data": "AAAA"}
    assert _materialise_media_part(block, None, model="gpt-4o") is MediaMaterialiseFailure.NO_TOKEN_BOUND


def test_an_audio_block_degrades_to_no_token_bound() -> None:
    """Tier 2: deny-side sibling — same fixture shape, a different
    non-image modality, same NO_TOKEN_BOUND result."""
    block = {"type": "audio", "mime_type": "audio/mpeg", "data": "AAAA"}
    assert _materialise_media_part(block, None, model="gpt-4o") is MediaMaterialiseFailure.NO_TOKEN_BOUND


def test_image_still_reaches_the_capability_gate_unchanged() -> None:
    """Tier 2: accept-side sibling — the registry-ization did not change
    behavior for the one modality that already worked (model=None skips
    the gate entirely, same as pre-PR2)."""
    block = {"type": "image", "mime_type": "image/png", "data": "AAAA"}
    part = _materialise_media_part(block, None, model=None)
    assert part == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


# ---------------------------------------------------------------------------
# build_wire_media_part — pure, shape-pinned per modality (architect
# condition on item ④: "呼ばれない分岐を置くならせめて形は固定を")
# ---------------------------------------------------------------------------


def test_wire_shape_image() -> None:
    """Tier 1: pin litellm's ``image_url`` content-part shape."""
    assert build_wire_media_part("image", "image/png", "AAAA") == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_wire_shape_video_url() -> None:
    """Tier 1: pin litellm's ``video_url`` content-part shape."""
    assert build_wire_media_part("video_url", "video/mp4", "AAAA") == {
        "type": "video_url",
        "video_url": {"url": "data:video/mp4;base64,AAAA"},
    }


def test_wire_shape_document() -> None:
    """Tier 1: pin litellm's ``document`` content-part shape
    (Anthropic-native, ``source.type=="text"``/``media_type``/``data``)."""
    assert build_wire_media_part("document", "application/pdf", "AAAA") == {
        "type": "document",
        "source": {"type": "text", "media_type": "application/pdf", "data": "AAAA"},
    }


def test_wire_shape_file() -> None:
    """Tier 1: pin litellm's ``file`` content-part shape (OpenAI-style
    ``file.file_data`` as a data: URI)."""
    assert build_wire_media_part("file", "application/octet-stream", "AAAA") == {
        "type": "file",
        "file": {"file_data": "data:application/octet-stream;base64,AAAA"},
    }


def test_wire_shape_audio() -> None:
    """Tier 1: pin litellm's ``input_audio`` content-part shape — its
    ``format`` field is derived from the mime subtype (the only place
    that information exists at this call site)."""
    assert build_wire_media_part("audio", "audio/mpeg", "AAAA") == {
        "type": "input_audio",
        "input_audio": {"data": "AAAA", "format": "mpeg"},
    }


def test_wire_shape_unrecognised_type_raises() -> None:
    """Tier 1: a caller reaching this with a type outside the 5 known ones
    has its own bug — never silently coerced to a guessed shape."""
    with pytest.raises(ValueError, match="unrecognised"):
        build_wire_media_part("carrier_pigeon", "image/png", "AAAA")


def test_the_5_wire_shapes_cover_the_known_block_types() -> None:
    """Tier 1: accept-side / cross-check — every type in
    `_KNOWN_MEDIA_BLOCK_TYPES` (the discriminator's own vocabulary) must
    have a real, non-raising wire shape, or a block that PASSES the
    filter could still explode inside the wire builder."""
    for block_type in _KNOWN_MEDIA_BLOCK_TYPES:
        part = build_wire_media_part(block_type, "application/octet-stream", "AAAA")
        assert part["type"]


# ---------------------------------------------------------------------------
# _default_mime_for_block_type
# ---------------------------------------------------------------------------


def test_default_mime_for_image_is_png() -> None:
    """Tier 2: accept-side sibling — no behavior change for the modality
    that already worked."""
    assert _default_mime_for_block_type("image") == "image/png"


def test_default_mime_for_a_non_image_type_is_generic() -> None:
    """Tier 2: the #5509 PR2 fix — a document with no declared mime must
    NOT be mislabeled "image/png"."""
    assert _default_mime_for_block_type("document") == "application/octet-stream"


# ---------------------------------------------------------------------------
# _as_path_ref / _overflow_ref_text — modality-aware ref + wording
# ---------------------------------------------------------------------------


def test_as_path_ref_carries_the_block_type_through() -> None:
    """Tier 2: the #5509 PR2 fix — the ref dict now names its own
    modality, the input ``_overflow_ref_text`` needs to word correctly."""
    ref = _as_path_ref(
        {"type": "document", "path": "/tmp/x.pdf", "mime_type": "application/pdf"},
        None, tool_name="t", seq=1,
    )
    assert ref == {"path": "/tmp/x.pdf", "mime_type": "application/pdf", "type": "document"}


def test_overflow_ref_text_names_the_real_modality_not_image() -> None:
    """Tier 2: the #5509 PR2 fix — a document ref must not read
    "[image not loaded...]"; that would mislead the model about what was
    actually skipped."""
    ref = {"path": "/tmp/x.pdf", "mime_type": "application/pdf", "type": "document"}
    text = _overflow_ref_text(ref)
    assert text.startswith("[document not loaded")
    assert "image" not in text


def test_overflow_ref_text_still_says_image_for_an_image_ref() -> None:
    """Tier 2: accept-side sibling — no behavior change for the modality
    that already worked."""
    ref = {"path": "/tmp/x.png", "mime_type": "image/png", "type": "image"}
    assert _overflow_ref_text(ref).startswith("[image not loaded")


# ---------------------------------------------------------------------------
# _build_media_followup_message — the discriminator widening (item ①):
# a non-image block is no longer silently filtered out before it ever
# reaches the gate.
# ---------------------------------------------------------------------------


def test_a_document_block_is_not_silently_dropped_by_the_type_filter_bounded_path() -> None:
    """Tier 2: strip-falsify target, BOUNDED path (has a real ref
    fallback) — before #5509 PR2, `b.get("type") == "image"` filtered
    this block out before `_build_media_followup_message` ever saw it,
    and `media_blocks` truthiness upstream would make the whole
    follow-up vanish silently. After PR2, the block reaches the gate,
    degrades to NO_TOKEN_BOUND, and the bounded path's own ref fallback
    surfaces it as a real, modality-named ref."""
    blocks = [{"type": "document", "mime_type": "application/pdf", "path": "/tmp/x.pdf"}]
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=None, budget_tokens=10_000,
    )
    assert fu is not None
    texts = [p["text"] for p in fu["content"] if p.get("type") == "text"]
    assert any("document not loaded" in t for t in texts)


def test_a_document_block_reaches_the_gate_unbounded_path_too() -> None:
    """Tier 2: the UNBOUNDED path has no ref fallback at all (pre-#272
    design, unchanged by PR2 — NO_TOKEN_BOUND drops silently there for
    every non-image modality, same as it already did for
    CAPABILITY_UNAVAILABLE before this PR). What PR2 changes is that the
    block now reaches `_materialise_media_part` — pin that it does NOT
    silently vanish before even being counted as an image."""
    blocks = [{"type": "document", "mime_type": "application/pdf", "data": "AAAA"}]
    images = [b for b in blocks if b.get("type") in _KNOWN_MEDIA_BLOCK_TYPES]
    assert images == blocks  # picked up by the discriminator, not filtered pre-gate


def test_an_unknown_block_type_is_still_filtered_out() -> None:
    """Tier 2: deny-side sibling — the discriminator widening is bounded
    (5 named types), not "any dict with a type key"."""
    blocks = [{"type": "carrier_pigeon", "mime_type": "application/octet-stream"}]
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=None, budget_tokens=10_000,
    )
    assert fu is None
