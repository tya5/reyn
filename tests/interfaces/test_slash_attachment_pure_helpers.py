"""Tier 2: /attachment slash — ``_mime_for_path`` pure helper contract.

Mirrors ``test_slash_image_pure_helpers.py``'s own established shape for
``/image``'s sibling helper. ``_file_size_human`` is byte-identical logic
to ``/image``'s own copy — not re-pinned here (same coverage already
exists for that exact function body in the sibling file); this file
covers what actually DIFFERS: ``/attachment`` never returns ``None`` (it
accepts every extension, unlike ``/image``'s closed set).
"""
from __future__ import annotations

from pathlib import Path

from reyn.interfaces.slash.attachment import _GENERIC_MIME, _mime_for_path


def test_mime_pdf() -> None:
    """Tier 2: .pdf → 'application/pdf' — stdlib mimetypes, not a reyn table."""
    assert _mime_for_path(Path("report.pdf")) == "application/pdf"


def test_mime_text() -> None:
    """Tier 2: .txt → a text/* mime — accepted, unlike /image's own table."""
    mime = _mime_for_path(Path("notes.txt"))
    assert mime.startswith("text/")


def test_mime_image_still_resolves_via_stdlib() -> None:
    """Tier 2: an image extension still resolves correctly through
    stdlib mimetypes — /attachment is a strict superset of /image."""
    assert _mime_for_path(Path("shot.png")) == "image/png"


def test_mime_unresolvable_extension_falls_back_to_generic() -> None:
    """Tier 2: the #5509 design point — unlike /image (refuses an
    unmapped extension outright), /attachment NEVER refuses on mime
    resolution alone; an extension stdlib cannot map degrades to the
    RFC 2046 generic type instead of None."""
    assert _mime_for_path(Path("data.totally-made-up-ext")) == _GENERIC_MIME


def test_mime_no_extension_falls_back_to_generic() -> None:
    """Tier 2: a path with no extension at all (e.g. `Makefile`) still
    resolves (to the generic type), never None — the accept-side sibling
    of /image's own "no extension → None → refused" behavior."""
    assert _mime_for_path(Path("Makefile")) == _GENERIC_MIME
