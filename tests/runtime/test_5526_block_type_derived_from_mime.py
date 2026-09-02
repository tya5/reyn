"""Tier 2: #5526 — closing the block_type/mime independence risk found
reviewing #5525, resolved as part of #5509's own producer-side PR (the
issue's own reopen condition: "producer が non-image を生産した日").

Before this fix, ``build_wire_media_part`` (called from
``_materialise_media_part``) and the returned ref's own ``"type"`` field
(from ``_as_path_ref``) both trusted the block's OWN declared ``type``
field, independent of its ``mime_type``. A block whose two fields
disagreed (``type="document"`` + ``mime="image/png"``) would pass the
vision capability check (mime says image) yet build a document-shaped
wire part (type says document) — a real, structurally-possible mismatch
no single-field gate could catch.

Resolution chosen (this PR): (b) — derive ``type`` from ``mime`` via
:func:`classify_media_block_type`, the ONE place that mapping is made,
called by both real wire/ref-building sites instead of trusting a stored
``type`` field. This makes the mismatch UNCONSTRUCTIBLE at the point
that matters: even a block carrying a stale/wrong ``type`` self-corrects
the moment it reaches materialisation or ref-building.
"""
from __future__ import annotations

from reyn.runtime.router_loop import (
    MediaMaterialiseFailure,
    _as_path_ref,
    _materialise_media_part,
    classify_media_block_type,
)

# ---------------------------------------------------------------------------
# classify_media_block_type — the registry itself
# ---------------------------------------------------------------------------


def test_image_mime_classifies_as_image() -> None:
    """Tier 1: pin the "image/" prefix's own block-type mapping."""
    assert classify_media_block_type("image/png") == "image"


def test_video_mime_classifies_as_video_url() -> None:
    """Tier 1: pin the "video/" prefix's own block-type mapping."""
    assert classify_media_block_type("video/mp4") == "video_url"


def test_audio_mime_classifies_as_audio() -> None:
    """Tier 1: pin the "audio/" prefix's own block-type mapping."""
    assert classify_media_block_type("audio/mpeg") == "audio"


def test_pdf_mime_classifies_as_document() -> None:
    """Tier 1: pin the exact "application/pdf" mapping."""
    assert classify_media_block_type("application/pdf") == "document"


def test_an_unrecognised_mime_classifies_as_the_generic_file_catch_all() -> None:
    """Tier 2: deny-side/accept-side in one — every mime resolves to
    SOME type (never None/raise), and an unmapped one is the deliberate
    "file" catch-all, not an error."""
    assert classify_media_block_type("application/x-unknown-format") == "file"


# ---------------------------------------------------------------------------
# The #5526 mismatch itself — strip-falsified (verified in-session against
# the pre-fix code shape, restored): a block declaring one type but
# carrying a DIFFERENT mime must resolve consistently with its mime at
# every real consumption site, not with its own (possibly wrong) type.
# ---------------------------------------------------------------------------


def test_a_mismatched_declared_type_self_corrects_at_materialise_time() -> None:
    """Tier 2: the exact #5526 shape — type says document, mime says
    image. The wire part built must match the MIME (image_url), not the
    declared type (which would have produced a document-shaped part
    around image bytes — a real provider-facing defect)."""
    mismatched = {"type": "document", "mime_type": "image/png", "data": "AAAA"}
    part = _materialise_media_part(mismatched, None, model=None)
    assert part == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def test_a_correctly_matched_document_block_still_degrades_correctly() -> None:
    """Tier 2: accept-side sibling — a genuinely-consistent document
    block is unaffected (still NO_TOKEN_BOUND, unchanged PR2 behavior)."""
    consistent = {"type": "document", "mime_type": "application/pdf", "data": "AAAA"}
    assert (
        _materialise_media_part(consistent, None, model="gpt-4o")
        is MediaMaterialiseFailure.NO_TOKEN_BOUND
    )


def test_a_mismatched_declared_type_self_corrects_in_as_path_ref_too() -> None:
    """Tier 2: the SAME #5526 shape, the OTHER real consumption site —
    the ref's own "type" field must also reflect mime, not the block's
    stale declared type, or the ref-fallback wording
    (``_overflow_ref_text``) would mislabel what was actually skipped."""
    mismatched = {"type": "document", "mime_type": "image/png", "path": "/tmp/x.png"}
    ref = _as_path_ref(mismatched, None, tool_name="t", seq=1)
    assert ref == {"path": "/tmp/x.png", "mime_type": "image/png", "type": "image"}


def test_a_block_with_no_mime_at_all_falls_back_to_its_declared_type() -> None:
    """Tier 2: the one case with no mime signal to derive FROM — a
    legacy/incomplete record. Falls back to the block's own declared
    type (or "image", every real producer's default) purely to pick a
    generic mime; this is the documented last-resort path, not the
    normal one (every real producer today always sets mime_type)."""
    no_mime = {"type": "document", "path": "/tmp/x.bin"}
    ref = _as_path_ref(no_mime, None, tool_name="t", seq=1)
    assert ref == {
        "path": "/tmp/x.bin",
        "mime_type": "application/octet-stream",
        "type": "document",
    }
