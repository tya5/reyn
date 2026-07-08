"""Tier 1: FP-0056 PR-H — canonical mappers for the file family, reyn_src dev-reads, compact, judge_output.

The dogfood incident (2026-07-09): a doc read via ``reyn_source__read`` was offloaded as a whole-dict
``structured`` attachment (a 600-char JSON-dict preview) instead of the readable text body, because
``_MAPPERS`` had no mapper for ``kind:"file"`` (unmapped) or the kind-less ``reyn_src_*`` results — both
took the whole-dict fallback. These tests pin the fix: the readable body is the ``text`` stream, never a
whole-dict structured blob. The incident regression test is RED against pre-hotfix code (with the mappers
removed, ``to_canonical`` falls back to a whole-dict structured attachment and ``text`` is empty).

Mirrors the tool-result arc (#2425) mapper-contract style: real result dicts (the shapes the producers
actually emit), no mocks, presence/absence + substring assertions (no Tier-4 formatting pins).
"""
from __future__ import annotations

import pytest

from reyn.core.offload.canonical import to_canonical
from reyn.core.offload.seam import build_offload_body
from reyn.tools.reyn_src import _handle_read
from reyn.tools.types import ToolContext


def _fake_save(value, **_kw) -> dict:
    """Records what was stored; returns a path-ref block like the real offload store (a plain stand-in
    store, not a mock of a collaborator — mirrors ``test_2425_offload_seam._fake_save``)."""
    _fake_save.stored.append(value)
    return {"path": f".reyn/tool-results/{len(_fake_save.stored):04d}.txt", "content_hash": "h"}


_fake_save.stored = []

# A document large enough (> the seam's STRUCTURED_INLINE_MAX_CHARS = 2000) that a whole-dict structured
# fallback WOULD be size-gated to its own ref — the exact shape the incident produced.
_BIG_DOC = "# Present layer\n\n" + ("The present layer renders tool results. " * 200)


# ─────────────────────────────────────────────────────────────────────────────
# Incident regression — RED against pre-hotfix code
# ─────────────────────────────────────────────────────────────────────────────


def test_incident_file_read_offloads_clean_text_not_whole_dict_blob():
    """Tier 1: INCIDENT — a large doc read via ``file`` read normalizes so the readable body is the
    ``text`` stream and NO whole-dict structured attachment is produced. RED pre-hotfix: with no ``file``
    mapper the whole result dict fell to the structured fallback (``text=""``, structured offloaded)."""
    result = {
        "kind": "file", "op": "read", "path": "docs/reference/runtime/present.ja.md",
        "status": "ok", "content": _BIG_DOC, "_self_bounded": True,
    }
    canonical = to_canonical(result)
    assert canonical["text"] == _BIG_DOC, "the doc body is the text payload (not offloaded as a dict)"
    assert not any(a.get("kind") == "structured" for a in canonical["attachments"]), \
        "no whole-dict structured attachment — the incident's blob is gone"

    _fake_save.stored = []
    frontmatter, text, _media = build_offload_body(canonical, save_fn=_fake_save)
    assert text == _BIG_DOC, "the readable body is the text stream the LLM reads"
    assert "structured" not in frontmatter and "structured_ref" not in frontmatter, \
        "no structured ref/preview — the 600-char JSON-dict preview the agent saw is gone"


def test_incident_reyn_src_read_offloads_clean_text_not_whole_dict_blob():
    """Tier 1: INCIDENT (the exact dogfood path) — a large doc read via ``reyn_src_read`` (tagged
    ``kind:"reyn_src"`` at the tool seam) normalizes to a clean ``text`` body, no whole-dict blob.
    RED pre-hotfix: the kind-less result took the fallback and offloaded the whole dict."""
    result = {"kind": "reyn_src", "path": "docs/reference/runtime/present.ja.md", "content": _BIG_DOC}
    canonical = to_canonical(result)
    assert canonical["text"] == _BIG_DOC
    assert not any(a.get("kind") == "structured" for a in canonical["attachments"])

    _fake_save.stored = []
    frontmatter, text, _media = build_offload_body(canonical, save_fn=_fake_save)
    assert text == _BIG_DOC
    assert "structured" not in frontmatter and "structured_ref" not in frontmatter


@pytest.mark.asyncio
async def test_incident_end_to_end_real_reyn_src_read_handler_tags_kind_and_maps_clean():
    """Tier 1: INTEGRATION — the REAL ``reyn_src_read`` handler reads a real repo file, its result is
    tagged ``kind:"reyn_src"`` at the tool seam, and ``to_canonical`` yields the file body as ``text``
    (not a structured blob). Proves the tag→mapper wiring end-to-end, not just the mapper in isolation."""
    ctx = ToolContext(events=None, permission_resolver=None, workspace=None, caller_kind="router")
    result = await _handle_read({"path": "pyproject.toml"}, ctx)
    assert result["kind"] == "reyn_src", "the tool seam tags the kind so the mapper (not fallback) runs"

    canonical = to_canonical(result)
    assert canonical["text"] == result["content"], "the file body is the text payload"
    assert 'name = "reyn"' in canonical["text"], "the real file content is present as readable text"
    assert not any(a.get("kind") == "structured" for a in canonical["attachments"])


# ─────────────────────────────────────────────────────────────────────────────
# file mapper — per-op contract
# ─────────────────────────────────────────────────────────────────────────────


def test_file_read_content_is_text_path_op_status_are_signal_meta():
    """Tier 1: file read → ``content`` is ``text``; ``path``/``op``/``status`` are signal meta (which
    file, what happened), never the body."""
    c = to_canonical({"kind": "file", "op": "read", "path": "a/b.md", "status": "ok",
                      "content": "hello world", "_self_bounded": True})
    assert c["text"] == "hello world"
    assert c["meta"].get("path") == "a/b.md"
    assert c["meta"].get("op") == "read" and c["meta"].get("status") == "ok"
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


def test_file_read_not_found_surfaces_error_text_and_iserror():
    """Tier 1: a not_found read surfaces the error message as ``text`` with ``meta.isError`` — the sole
    error-path driver (so the seam renders ``Error: ...`` and the LLM retries a different path)."""
    c = to_canonical({"kind": "file", "op": "read", "path": "missing.md",
                      "status": "not_found", "error": "file not found: missing.md", "content": ""})
    assert c["meta"].get("isError") is True
    assert "file not found" in c["text"]


def test_file_read_image_media_blocks_become_media_attachments():
    """Tier 1: an image read (content empty, media_blocks present) surfaces the blocks as MEDIA
    attachments (matching the MCP mapper), never a structured blob."""
    c = to_canonical({"kind": "file", "op": "read", "path": "x.png", "status": "ok",
                      "content": "", "media_blocks": [{"type": "image", "data": "..."}]})
    assert [a["kind"] for a in c["attachments"]] == ["media"]


def test_file_grep_content_mode_renders_match_lines_as_text():
    """Tier 1: grep content mode → ``path:line: text`` lines as ``text`` (readable, not a dict blob)."""
    c = to_canonical({"kind": "file", "op": "grep", "status": "ok", "output_mode": "content",
                      "pattern": "foo", "matches": [
                          {"path": "a.py", "line_number": 3, "content": "foo = 1"},
                          {"path": "b.py", "line_number": 9, "content": "foo()"}],
                      "count": 2})
    assert "a.py:3: foo = 1" in c["text"] and "b.py:9: foo()" in c["text"]
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


def test_file_glob_matches_render_as_path_lines():
    """Tier 1: glob → the matched paths as newline-joined ``text``."""
    c = to_canonical({"kind": "file", "op": "glob", "pattern": "*.py", "status": "ok",
                      "matches": ["a.py", "b.py"], "count": 2})
    assert c["text"] == "a.py\nb.py"


def test_file_write_is_short_status_text():
    """Tier 1: write → a short human-readable status ``text`` (bytes + path), not a JSON envelope."""
    c = to_canonical({"kind": "file", "op": "write", "path": "out.txt", "status": "ok",
                      "bytes_written": 42})
    assert "42" in c["text"] and "out.txt" in c["text"]
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


# ─────────────────────────────────────────────────────────────────────────────
# reyn_src mapper — per-shape contract
# ─────────────────────────────────────────────────────────────────────────────


def test_reyn_src_read_content_is_text_path_is_meta():
    """Tier 1: reyn_src read → ``content`` is the ``text`` body; ``path`` is signal meta."""
    c = to_canonical({"kind": "reyn_src", "path": "README.md", "content": "# Reyn"})
    assert c["text"] == "# Reyn"
    assert c["meta"].get("path") == "README.md"


def test_reyn_src_error_surfaces_iserror():
    """Tier 1: a reyn_src error (e.g. path outside repo) surfaces the message as ``text`` + isError."""
    c = to_canonical({"kind": "reyn_src", "error": "reyn_src: path '..' resolves outside repo"})
    assert c["meta"].get("isError") is True
    assert "outside" in c["text"]


def test_reyn_src_list_entries_render_as_text_lines():
    """Tier 1: reyn_src list → ``type: name`` lines as ``text`` (a browsable listing, not a dict)."""
    c = to_canonical({"kind": "reyn_src", "path": "docs", "entries": [
        {"name": "concepts", "type": "dir"}, {"name": "README.md", "type": "file"}]})
    assert "dir: concepts" in c["text"] and "file: README.md" in c["text"]


def test_reyn_src_grep_matches_render_as_text_lines():
    """Tier 1: reyn_src grep → ``path:line: snippet`` lines as ``text``."""
    c = to_canonical({"kind": "reyn_src", "pattern": "def", "count": 1, "truncated": False,
                      "matches": [{"path": "x.py", "line": 5, "snippet": "def foo():"}]})
    assert "x.py:5: def foo():" in c["text"]
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


def test_reyn_src_glob_matches_render_as_path_lines():
    """Tier 1: reyn_src glob → the matched path strings as newline-joined ``text``."""
    c = to_canonical({"kind": "reyn_src", "pattern": "**/*.md", "count": 2,
                      "matches": ["docs/a.md", "docs/b.md"]})
    assert c["text"] == "docs/a.md\ndocs/b.md"


# ─────────────────────────────────────────────────────────────────────────────
# compact / judge_output mappers
# ─────────────────────────────────────────────────────────────────────────────


def test_compact_ok_summarizes_metrics_as_text():
    """Tier 1: compact ok → a short ``text`` summary of the freed-token / free-window metrics (no blob)."""
    c = to_canonical({"kind": "compact", "status": "ok", "freed_tokens": 1200,
                      "free_window_after": 90000, "summarized_turns": 8})
    assert "freed_tokens=1200" in c["text"] and "free_window_after=90000" in c["text"]
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


def test_compact_error_surfaces_iserror():
    """Tier 1: compact error → the message as ``text`` with ``meta.isError``."""
    c = to_canonical({"kind": "compact", "status": "error", "error_kind": "compaction_unavailable",
                      "error": "no compaction context is wired here"})
    assert c["meta"].get("isError") is True
    assert "no compaction context" in c["text"]


def test_judge_output_reason_is_text_score_is_signal_meta():
    """Tier 1: judge_output → ``reason`` is the ``text``; score/passed/threshold/on_fail are signal
    meta (they drive the caller's next move — a failed judgment triggers on_fail)."""
    c = to_canonical({"kind": "judge_output", "score": 0.4, "passed": False, "reason": "too terse",
                      "threshold": 0.7, "on_fail": "retry"})
    assert c["text"] == "too terse"
    assert c["meta"].get("passed") is False and c["meta"].get("score") == 0.4
    assert c["meta"].get("threshold") == 0.7 and c["meta"].get("on_fail") == "retry"


def test_judge_output_error_surfaces_iserror():
    """Tier 1: judge_output error (target resolution failed) → the message as ``text`` + isError."""
    c = to_canonical({"kind": "judge_output", "status": "error",
                      "error": "target resolution failed: 'summary'"})
    assert c["meta"].get("isError") is True
    assert "target resolution failed" in c["text"]
