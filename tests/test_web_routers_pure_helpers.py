"""Tier 2: pure helpers in interfaces/web/routers/*.

  ``resources._mime_for(artifact)``           — extension → Content-Type string
  ``resources._browser_headers(artifact, *)`` — hardened response header dict
  ``runs._run_id_from_file(path)``            — path stem → run_id string
  ``budget._cap_detail(cap_cfg)``             — config object → BudgetCapDetail
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from reyn.interfaces.web.routers.budget import BudgetCapDetail, _cap_detail
from reyn.interfaces.web.routers.resources import _browser_headers, _mime_for
from reyn.interfaces.web.routers.runs import _run_id_from_file

# ---------------------------------------------------------------------------
# _mime_for
# ---------------------------------------------------------------------------


def test_mime_for_png() -> None:
    """Tier 2: .png maps to image/png."""
    assert _mime_for("photo.png") == "image/png"


def test_mime_for_jpg() -> None:
    """Tier 2: .jpg maps to image/jpeg."""
    assert _mime_for("photo.jpg") == "image/jpeg"


def test_mime_for_json() -> None:
    """Tier 2: .json maps to application/json."""
    assert _mime_for("data.json") == "application/json"


def test_mime_for_markdown() -> None:
    """Tier 2: .md maps to text/markdown with charset."""
    assert _mime_for("readme.md") == "text/markdown; charset=utf-8"


def test_mime_for_unknown_extension_returns_octet_stream() -> None:
    """Tier 2: unrecognised extension returns application/octet-stream."""
    assert _mime_for("archive.xyz") == "application/octet-stream"


def test_mime_for_uppercase_extension_normalised() -> None:
    """Tier 2: extension lookup is case-insensitive."""
    assert _mime_for("IMAGE.PNG") == "image/png"


def test_mime_for_no_extension_returns_octet_stream() -> None:
    """Tier 2: artifact with no extension returns application/octet-stream."""
    assert _mime_for("no_ext") == "application/octet-stream"


# ---------------------------------------------------------------------------
# _browser_headers
# ---------------------------------------------------------------------------


def test_browser_headers_inline_content_disposition() -> None:
    """Tier 2: download=False produces inline disposition."""
    headers = _browser_headers("result.png", download=False)
    assert headers["content-disposition"] == 'inline; filename="result.png"'


def test_browser_headers_attachment_content_disposition() -> None:
    """Tier 2: download=True produces attachment disposition."""
    headers = _browser_headers("result.png", download=True)
    assert headers["content-disposition"] == 'attachment; filename="result.png"'


def test_browser_headers_cors_always_present() -> None:
    """Tier 2: CORS header is always permissive regardless of download flag."""
    headers = _browser_headers("file.txt", download=False)
    assert headers["access-control-allow-origin"] == "*"


def test_browser_headers_cors_present_on_download() -> None:
    """Tier 2: CORS header present even when download=True."""
    headers = _browser_headers("file.txt", download=True)
    assert headers["access-control-allow-origin"] == "*"


# ---------------------------------------------------------------------------
# _run_id_from_file
# ---------------------------------------------------------------------------


def test_run_id_from_file_standard_format() -> None:
    """Tier 2: well-formed timestamp_slug stem returns the matched group."""
    path = Path("/events/direct/skill_runs/2026/20260601T120000Z_my-skill.jsonl")
    assert _run_id_from_file(path) == "20260601T120000Z_my-skill"


def test_run_id_from_file_non_matching_returns_stem() -> None:
    """Tier 2: stem not matching the timestamp pattern falls back to stem."""
    path = Path("/events/direct/skill_runs/2026/plainname.jsonl")
    assert _run_id_from_file(path) == "plainname"


def test_run_id_from_file_longer_slug_extracted() -> None:
    """Tier 2: longer run_id slug is extracted correctly."""
    path = Path("/foo/20261231T235959Z_complex-skill-name.jsonl")
    assert _run_id_from_file(path) == "20261231T235959Z_complex-skill-name"


# ---------------------------------------------------------------------------
# _cap_detail
# ---------------------------------------------------------------------------


def test_cap_detail_extracts_hard_limit_and_warn_ratio() -> None:
    """Tier 2: _cap_detail maps hard_limit and warn_ratio from config object."""
    cfg = SimpleNamespace(hard_limit=1000.0, warn_ratio=0.8)
    result = _cap_detail(cfg)
    assert isinstance(result, BudgetCapDetail)
    assert result.hard_limit == 1000.0
    assert result.warn_ratio == 0.8


def test_cap_detail_none_hard_limit_allowed() -> None:
    """Tier 2: hard_limit=None (no cap) passes through as None."""
    cfg = SimpleNamespace(hard_limit=None, warn_ratio=0.9)
    result = _cap_detail(cfg)
    assert result.hard_limit is None
    assert result.warn_ratio == 0.9
