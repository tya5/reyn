"""Tier 2: #2336 — an offloaded op result stores the CLEAN dominant payload, not a JSON-of-JSON.

The offloaded file used to hold ``json.dumps(whole_result_dict)`` — reading the ref gave a JSON
envelope with the content collapsed to escaped ``\\n`` (owner concern 2). Fix (producer-declares-
payload, mirrors #2296 ``_self_bounded`` — P7-safe): an op result declares ``_offload_payload_field``;
when that field is the SOLE oversized field the file stores it CLEAN (a str raw with real newlines,
a list/dict as a clean array/object). Multi-large-field → whole-dict fallback (zero data-loss: a
non-dominant large field's full content is never dropped to preview-only).

``read_offloaded`` is unchanged (reads text + verifies hash → clean content). The inline envelope +
per-field preview are unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES as CAP
from reyn.core.context_builder import offload_control_ir_result
from reyn.services.offload.store import read_offloaded

_BIG = "x" * (CAP + 4000)  # a single field well over the 8 KB per-field threshold


def _offload(result: dict, tmp_path: Path) -> dict:
    return offload_control_ir_result(result, 0, tmp_path)  # default cap = the 8 KB floor


def _raw(inline: dict) -> str:
    return Path(inline["_offload_ref"]).read_text(encoding="utf-8")


def test_marker_str_payload_stored_raw_not_json_envelope(tmp_path):
    """Tier 2: a marker + oversized str → the offload file is RAW content, not a ``{"kind":...}``
    whole-dict envelope. RED on main (whole-dict), GREEN after."""
    result = {"kind": "web_fetch", "url": "http://x", "status": "ok",
              "content": _BIG, "_offload_payload_field": "content"}
    inline = _offload(result, tmp_path)
    raw = _raw(inline)
    assert not raw.startswith("{"), "the file must be raw content, not a JSON dict envelope"
    assert raw == _BIG, "the file is exactly the clean payload"
    assert inline["_offload_ref_format"] == "raw_field"
    assert inline["_offload_payload_field"] == "content"


def test_read_back_returns_clean_content(tmp_path):
    """Tier 2: read_offloaded returns the clean content (hash-verified), with no dict envelope."""
    result = {"kind": "web_fetch", "url": "http://x", "status": "ok",
              "content": _BIG, "_offload_payload_field": "content"}
    inline = _offload(result, tmp_path)
    content, _ = read_offloaded(
        inline["_offload_ref"], base_dir=tmp_path, content_hash=inline["_offload_content_hash"],
    )
    assert content == _BIG
    assert '"kind"' not in content, "clean payload, no whole-dict envelope"


def test_list_payload_stored_as_clean_json_array(tmp_path):
    """Tier 2: a list payload → the file is a clean JSON array (not a whole-dict envelope)."""
    results = [{"title": f"t{i}", "body": "b" * 400} for i in range(40)]
    result = {"kind": "web_search", "query": "q", "status": "ok",
              "results": results, "_offload_payload_field": "results"}
    inline = _offload(result, tmp_path)
    raw = _raw(inline)
    parsed = json.loads(raw)
    assert parsed == results, "the file is the clean results array (round-trips), not a whole-dict"
    assert not raw.lstrip().startswith("{"), "no whole-dict envelope wrapping the array"


def test_no_marker_falls_back_to_whole_dict(tmp_path):
    """Tier 2: a result with NO marker keeps the existing whole-dict json fallback (unchanged)."""
    result = {"kind": "custom", "status": "ok", "blob": _BIG}  # no _offload_payload_field
    inline = _offload(result, tmp_path)
    raw = _raw(inline)
    assert raw.startswith("{") and '"kind"' in raw, "whole-dict fallback preserved"
    assert "_offload_ref_format" not in inline


def test_inline_keeps_envelope_and_preview_not_full_payload(tmp_path):
    """Tier 2: the inline still carries the envelope fields + a bounded content PREVIEW (not the
    full payload)."""
    result = {"kind": "web_fetch", "url": "http://x", "status": "ok",
              "content": _BIG, "_offload_payload_field": "content"}
    inline = _offload(result, tmp_path)
    assert inline["kind"] == "web_fetch" and inline["status"] == "ok" and inline["url"] == "http://x"
    assert len(json.dumps(inline)) <= CAP, "inline is bounded (a preview, not the full payload)"
    assert inline.get("content", "") != _BIG, "the full payload is not inlined"


def test_multiline_str_payload_has_real_newlines(tmp_path):
    """Tier 2: owner concern 2 — a multi-line str payload → the file has REAL newlines, not escaped
    ``\\n`` inside a JSON string."""
    content = "".join(f"line {i}\n" for i in range(2000))  # ~ many real newlines, > cap
    result = {"kind": "web_fetch", "url": "http://x", "status": "ok",
              "content": content, "_offload_payload_field": "content"}
    raw = _raw(_offload(result, tmp_path))
    assert raw.count("\n") == content.count("\n"), "real newlines preserved (not collapsed)"
    assert "\\n" not in raw, "no JSON-escaped newlines (the JSON-of-JSON symptom)"


def test_multi_large_field_falls_back_to_whole_dict_no_data_loss(tmp_path):
    """Tier 2: when 2+ fields are oversized, fall back to whole-dict so the NON-dominant large
    field's full content is preserved (zero data-loss), even with a marker."""
    result = {"kind": "mcp", "status": "ok", "content": "C" * (CAP + 2000),
              "raw": {"big": "R" * (CAP + 2000)}, "_offload_payload_field": "content"}
    inline = _offload(result, tmp_path)
    raw = _raw(inline)
    assert raw.startswith("{") and '"kind"' in raw, "multi-large → whole-dict envelope"
    assert "C" * (CAP + 2000) in raw and "R" * (CAP + 2000) in raw, "BOTH large fields preserved full"
    assert "_offload_ref_format" not in inline, "not the raw-field format (fell back)"
