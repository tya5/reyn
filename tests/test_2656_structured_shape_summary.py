"""Tier 1: #2656 — offloaded ``structured`` attachments carry a bounded ``structured_shape`` summary
(key names / value types / array length) in the frontmatter, additive alongside the existing
``structured_ref`` / ``structured_preview`` (0053 offload seam, `seam.py`'s `build_offload_body`).

A head-N-chars ``structured_preview`` is a poor shape summary for a large array-of-objects (only the
first element, possibly truncated, is visible). ``structured_shape`` is deterministically derived (no
extra LLM call) and BOUNDED in depth/breadth/array-sample so summarizing a huge/deep payload is never
itself an unbounded-cost operation — these tests exercise the real offload seam (no mocks/patches),
per CLAUDE.md's ban on faking a cheaply-constructible collaborator.
"""
from __future__ import annotations

from reyn.core.offload.canonical import to_canonical
from reyn.core.offload.seam import (
    _SHAPE_MAX_ARRAY_SAMPLE,
    _SHAPE_MAX_DEPTH,
    _SHAPE_MAX_KEYS,
    build_offload_body,
    summarize_structured_shape,
)


def _fake_save(value, **_kw) -> dict:
    """Records what was stored; returns a path-ref block like MediaStore.save_tool_result."""
    _fake_save.stored.append(value)
    return {"path": f".reyn/tool-results/{len(_fake_save.stored):04d}.txt", "content_hash": "h"}


_fake_save.stored = []


def test_offloaded_structured_carries_a_shape_summary_alongside_the_preview():
    """Tier 1: CORE — a large array-of-objects, offloaded, gets BOTH the existing char-slice preview
    AND the new ``structured_shape`` — additive, neither field replaces the other."""
    _fake_save.stored = []
    rows = [{"id": i, "name": f"row-{i}", "active": i % 2 == 0} for i in range(50)]
    canonical = to_canonical(
        {"kind": "mcp", "status": "ok", "server": "s", "tool": "t",
         "content": "the body text", "structured": {"rows": rows}},
        source="mcp",
    )
    frontmatter, _text, _media, _ct = build_offload_body(canonical, save_fn=_fake_save)

    assert frontmatter.get("structured") == "offloaded"
    assert frontmatter.get("structured_preview"), "existing preview field is untouched"
    shape = frontmatter.get("structured_shape")
    assert shape is not None, "shape summary is present for an offloaded structured attachment"
    assert shape["rows"]["length"] == 50, "array length is exact, not sampled-approximate"
    # The element shape is the union of keys across the sampled rows — every top-level key of a
    # single row is visible from the frontmatter alone, without a read_file round-trip.
    assert set(shape["rows"]["element"].keys()) == {"id", "name", "active"}
    assert shape["rows"]["element"]["id"] == "number"
    assert shape["rows"]["element"]["name"] == "string"
    assert shape["rows"]["element"]["active"] == "boolean"


def test_small_inline_structured_has_no_shape_summary():
    """Tier 1: a small (non-offloaded) structured attachment stays fully inline — the shape summary is
    only added for the offloaded path, since the raw data itself already answers "what is the shape"."""
    _fake_save.stored = []
    canonical = to_canonical(
        {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "hi",
         "structured": {"n": 1}},
        source="mcp",
    )
    frontmatter, _text, _media, _ct = build_offload_body(canonical, save_fn=_fake_save)
    assert frontmatter.get("structured") == {"n": 1}
    assert "structured_shape" not in frontmatter


def test_shape_summary_depth_is_bounded_and_records_the_bound():
    """Tier 1: BOUNDEDNESS — a deeply nested structure (well past ``_SHAPE_MAX_DEPTH``) stops
    descending and records a ``<max_depth:N>`` marker instead of silently truncating without a trace."""
    nested: dict = {"leaf": "bottom"}
    for _ in range(_SHAPE_MAX_DEPTH + 5):
        nested = {"child": nested}
    shape = summarize_structured_shape(nested)

    def _depth(d) -> int:
        n = 0
        while isinstance(d, dict) and "child" in d:
            d = d["child"]
            n += 1
        return n

    assert _depth(shape) == _SHAPE_MAX_DEPTH, "descent stops exactly at the bound"
    # Walk to the stopping point and confirm the marker records that the bound was hit.
    cur = shape
    for _ in range(_SHAPE_MAX_DEPTH):
        cur = cur["child"]
    assert cur == f"<max_depth:{_SHAPE_MAX_DEPTH}>", "hitting the bound is recorded IN the summary"


def test_shape_summary_key_breadth_is_bounded_and_records_the_bound():
    """Tier 1: BOUNDEDNESS — an object with far more keys than ``_SHAPE_MAX_KEYS`` is summarized with
    only the first N keys shown plus a ``<truncated>`` marker recording how many more exist."""
    huge = {f"key_{i}": i for i in range(_SHAPE_MAX_KEYS + 37)}
    shape = summarize_structured_shape(huge)
    assert "<truncated>" in shape, "the bound-hit is recorded in the summary itself"
    assert shape["<truncated>"] == "37 more keys"
    shown = [k for k in shape if k != "<truncated>"]
    assert len(shown) == _SHAPE_MAX_KEYS, "exactly the bounded number of keys is shown"


def test_shape_summary_array_sampling_is_bounded_and_records_the_bound():
    """Tier 1: BOUNDEDNESS — a huge array's element shape is derived from only the first
    ``_SHAPE_MAX_ARRAY_SAMPLE`` elements (never a full scan), and the ``<sampled>`` marker records
    that the length exceeds what was actually inspected. Elements past the sample window are free to
    differ in shape without inflating the summarizer's work."""
    n = 200_000
    array = [{"a": 1} for _ in range(_SHAPE_MAX_ARRAY_SAMPLE)] + [
        {"totally": "different", "shape": True} for _ in range(n - _SHAPE_MAX_ARRAY_SAMPLE)
    ]
    shape = summarize_structured_shape(array)
    assert shape["length"] == n, "the true length is always cheap to report (len(), not a scan)"
    assert shape["<sampled>"] == f"first {_SHAPE_MAX_ARRAY_SAMPLE} of {n}"
    # Only the sampled (uniform) elements' keys appear — proves the summarizer never looked past
    # the sample window, which is what keeps summarizing a 200k-element array cheap.
    assert set(shape["element"].keys()) == {"a"}


def test_shape_summary_is_total_over_json_scalars():
    """Tier 1: the summarizer is total over JSON-serializable scalars — every scalar kind that can
    appear as a structured attachment's leaf value resolves to a bare type name, never raises."""
    assert summarize_structured_shape(None) == "null"
    assert summarize_structured_shape(True) == "boolean"
    assert summarize_structured_shape(3.14) == "number"
    assert summarize_structured_shape("x") == "string"
