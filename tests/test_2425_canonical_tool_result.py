"""Tier 1: #2425 案B — canonical tool-result normalization for every op kind (guessing-free offload).

``to_canonical`` maps each op result to ``{text, attachments, source_ref, meta}`` so ``text`` is the
LLM body by construction and non-text (structured/media) is an ``attachment`` kept OUT of the offload
decision — the owner whole-envelope case cannot structurally occur. Every op that once declared
``_offload_payload_field`` now has a mapper (MCP + web fetch/search + exec + recall/index_query +
run_pipeline(_async)); ``meta`` is signal-only (transport echo dropped, ``isError`` kept as the
error-path driver).
"""
from __future__ import annotations

from reyn.core.offload.canonical import to_canonical


def test_mcp_content_becomes_text_structured_and_media_become_attachments():
    """Tier 1: CORE — an MCP result with a large ``content`` AND a large ``structured`` (the owner
    whole-envelope root) normalizes so ``text`` = the content body and BOTH structured + media are
    typed attachments (out of the offload decision) — no field to guess, no whole-dict fallback."""
    mcp = {
        "kind": "mcp", "status": "ok", "server": "s", "tool": "t",
        "content": "the body text", "structured": {"rows": [1, 2, 3]},
        "media_blocks": [{"type": "image", "data": "..."}],
    }
    c = to_canonical(mcp, source="mcp")

    assert c["text"] == "the body text", "content → the single offload payload (text)"
    kinds = [a["kind"] for a in c["attachments"]]
    assert "structured" in kinds and "media" in kinds, "structured + media are typed attachments"
    assert any(a.get("data") == {"rows": [1, 2, 3]} for a in c["attachments"]), "structured preserved (not dropped)"
    assert c["source_ref"] is None, "MCP is transient → no on-disk origin → its body must be stored"
    # meta is signal-only: transport echo (kind/status/server/tool) dropped; success → no isError.
    assert "server" not in c["meta"] and "kind" not in c["meta"]
    assert not c["meta"].get("isError")


def test_mcp_error_status_sets_iserror_meta():
    """Tier 1: an MCP error (``status: error``) sets ``meta.isError`` — the sole error-path driver
    kept after meta-tightening; the description stays in ``text``."""
    c = to_canonical({"kind": "mcp", "status": "error", "server": "s", "tool": "t",
                      "content": "boom: tool failed", "media_blocks": []}, source="mcp")
    assert c["meta"].get("isError") is True
    assert c["text"] == "boom: tool failed"


def test_mcp_without_structured_has_no_structured_attachment():
    """Tier 1: the common case — an MCP result with only text has ``text`` set and no structured
    attachment (clean end-state, no shim)."""
    c = to_canonical({"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "hi",
                      "media_blocks": []}, source="mcp")
    assert c["text"] == "hi"
    assert not any(a["kind"] == "structured" for a in c["attachments"])


def test_web_fetch_content_is_text_truncated_is_signal():
    """Tier 1: web_fetch → the page ``content`` is ``text``; a ``truncated`` fetch surfaces
    ``truncated`` + ``next_start`` (the pagination handle) as signal meta; transport is dropped."""
    c = to_canonical({"kind": "web_fetch", "url": "http://x", "status": "ok", "content": "PAGE",
                      "truncated": True, "next_start": 4096}, source="web_fetch")
    assert c["text"] == "PAGE"
    assert c["meta"].get("truncated") is True and c["meta"].get("next_start") == 4096
    assert "url" not in c["meta"] and "status" not in c["meta"]


def test_web_search_results_become_structured():
    """Tier 1: web_search → the ``results`` list is a structured attachment (no text body)."""
    c = to_canonical({"kind": "web_search", "query": "q", "backend": "b", "status": "ok",
                      "results": [{"url": "u", "title": "t"}]}, source="web_search")
    assert c["text"] == ""
    assert c["attachments"] == [{"kind": "structured", "data": [{"url": "u", "title": "t"}]}]
    assert "query" not in c["meta"]


def test_sandboxed_exec_stdout_is_text_nonzero_returncode_is_signal():
    """Tier 1: sandboxed_exec → stdout(+stderr) is ``text``; a NONZERO returncode is signal meta; a
    zero exit is NOT signal (nothing for the LLM to act on)."""
    ok = to_canonical({"kind": "sandboxed_exec", "status": "ok", "returncode": 0,
                       "stdout": "hello", "stderr": ""}, source="sandboxed_exec")
    assert ok["text"] == "hello" and "returncode" not in ok["meta"], "0 exit is not signal"

    fail = to_canonical({"kind": "sandboxed_exec", "status": "error", "returncode": 2,
                         "stdout": "out", "stderr": "boom"}, source="sandboxed_exec")
    assert "out" in fail["text"] and "boom" in fail["text"], "stdout + stderr both in text"
    assert fail["meta"].get("returncode") == 2, "nonzero returncode is signal"


def test_recall_and_index_query_chunks_become_structured():
    """Tier 1: semantic_search (FP-0057 Phase 2a; renamed from recall) / index_query →
    the ``chunks`` list is a structured attachment (no text)."""
    for kind in ("semantic_search", "index_query"):
        c = to_canonical({"kind": kind, "chunks": [{"id": 1}], "mode": "semantic"}, source=kind)
        assert c["text"] == ""
        assert c["attachments"] == [{"kind": "structured", "data": [{"id": 1}]}]
        assert "mode" not in c["meta"], "transport 'mode' dropped"


def test_run_pipeline_sync_str_output_is_text_drops_run_id():
    """Tier 1: sync run_pipeline → a str ``output`` is ``text``; ``run_id`` / ``named_stores`` are
    dropped from the LLM-visible side (owner ruling)."""
    c = to_canonical({"kind": "run_pipeline", "run_id": "R1", "output": "final answer",
                      "named_stores": {"x": "..."}}, source="run_pipeline")
    assert c["text"] == "final answer"
    assert c["attachments"] == []
    assert "R1" not in c["text"], "run_id dropped for the sync result"


def test_run_pipeline_sync_nonstr_output_is_structured():
    """Tier 1: sync run_pipeline → a non-str ``output`` becomes a structured attachment."""
    c = to_canonical({"kind": "run_pipeline", "run_id": "R1", "output": {"k": "v"},
                      "named_stores": None}, source="run_pipeline")
    assert c["text"] == ""
    assert c["attachments"] == [{"kind": "structured", "data": {"k": "v"}}]


def test_run_pipeline_async_keeps_run_id_in_text():
    """Tier 1: CONTRAST — async run_pipeline_async KEEPS ``run_id`` (the correlation handle for the
    later [pipeline] completion message), unlike the sync result which drops it."""
    c = to_canonical({"kind": "run_pipeline_async", "run_id": "R42"}, source="run_pipeline_async")
    assert "R42" in c["text"], "async result keeps run_id (the completion-message handle)"
    assert "[pipeline]" in c["text"]
    assert c["attachments"] == []


def test_unregistered_kind_falls_back_to_structured_not_text_blob():
    """Tier 1: an op with no registered mapper falls back to a whole-dict STRUCTURED attachment
    (never a whole-dict json-into-text blob) — lossless, renders as readable frontmatter YAML."""
    result = {"kind": "some_new_op", "status": "ok", "field_a": "x", "field_b": [1, 2]}
    c = to_canonical(result, source="some_new_op")
    assert c["text"] == "", "no text blob — the whole-dict-into-text fallback is gone"
    assert c["attachments"] == [{"kind": "structured", "data": result}], "whole dict preserved as structured"
    assert c["source_ref"] is None
