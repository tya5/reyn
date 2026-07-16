"""Tier 1: FP-0056 PR-H — canonical mappers for the file family, reyn_repo dev-reads, compact.

The dogfood incident (2026-07-09): a doc read via ``reyn_repo__read`` was offloaded as a whole-dict
``structured`` attachment (a 600-char JSON-dict preview) instead of the readable text body, because
``_MAPPERS`` had no mapper for ``kind:"file"`` (unmapped) or the kind-less ``reyn_repo_*`` results — both
took the whole-dict fallback. These tests pin the fix: the readable body is the ``text`` stream, never a
whole-dict structured blob. The incident regression test is RED against pre-hotfix code (with the mappers
removed, ``to_canonical`` falls back to a whole-dict structured attachment and ``text`` is empty).

Mirrors the tool-result arc (#2425) mapper-contract style: real result dicts (the shapes the producers
actually emit), no mocks, presence/absence + substring assertions (no Tier-4 formatting pins).
"""
from __future__ import annotations

import pytest

from reyn.core.offload.canonical import to_canonical
from reyn.core.offload.seam import build_offload_body, render_tool_result
from reyn.tools.reyn_repo import _handle_read
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
    canonical = to_canonical(result, source="read_file")
    assert canonical["text"] == _BIG_DOC, "the doc body is the text payload (not offloaded as a dict)"
    assert not any(a.get("kind") == "structured" for a in canonical["attachments"]), \
        "no whole-dict structured attachment — the incident's blob is gone"

    _fake_save.stored = []
    frontmatter, text, _media = build_offload_body(canonical, save_fn=_fake_save)
    assert text == _BIG_DOC, "the readable body is the text stream the LLM reads"
    assert "structured" not in frontmatter and "structured_ref" not in frontmatter, \
        "no structured ref/preview — the 600-char JSON-dict preview the agent saw is gone"


def test_incident_reyn_repo_read_offloads_clean_text_not_whole_dict_blob():
    """Tier 1: INCIDENT (the exact dogfood path) — a large doc read via ``reyn_repo_read`` (tagged
    ``kind:"reyn_repo"`` at the tool seam) normalizes to a clean ``text`` body, no whole-dict blob.
    RED pre-hotfix: the kind-less result took the fallback and offloaded the whole dict."""
    result = {"kind": "reyn_repo", "path": "docs/reference/runtime/present.ja.md", "content": _BIG_DOC}
    canonical = to_canonical(result, source="reyn_repo_read")
    assert canonical["text"] == _BIG_DOC
    assert not any(a.get("kind") == "structured" for a in canonical["attachments"])

    _fake_save.stored = []
    frontmatter, text, _media = build_offload_body(canonical, save_fn=_fake_save)
    assert text == _BIG_DOC
    assert "structured" not in frontmatter and "structured_ref" not in frontmatter


@pytest.mark.asyncio
async def test_incident_end_to_end_real_reyn_repo_read_handler_tags_kind_and_maps_clean():
    """Tier 1: INTEGRATION — the REAL ``reyn_repo_read`` handler reads a real repo file, its result is
    tagged ``kind:"reyn_repo"`` at the tool seam, and ``to_canonical`` yields the file body as ``text``
    (not a structured blob). Proves the tag→mapper wiring end-to-end, not just the mapper in isolation."""
    # "README.md" (not "pyproject.toml" — 0061 §3.3 narrowed the reyn_repo
    # reachable set to {README.md, CHANGELOG.md, docs, src}; pyproject.toml
    # is deliberately excluded in both dev and wheel mode now).
    ctx = ToolContext(events=None, permission_resolver=None, workspace=None, caller_kind="router")
    result = await _handle_read({"path": "README.md"}, ctx)
    assert result["kind"] == "reyn_repo", "the tool seam tags the kind so the mapper (not fallback) runs"

    canonical = to_canonical(result, source="reyn_repo_read")
    assert canonical["text"] == result["content"], "the file body is the text payload"
    assert "Reyn" in canonical["text"], "the real file content is present as readable text"
    assert not any(a.get("kind") == "structured" for a in canonical["attachments"])


# ─────────────────────────────────────────────────────────────────────────────
# file mapper — per-op contract
# ─────────────────────────────────────────────────────────────────────────────


def test_file_read_content_is_text_path_op_status_are_signal_meta():
    """Tier 1: file read → ``content`` is ``text``; ``path``/``op``/``status`` are signal meta (which
    file, what happened), never the body."""
    c = to_canonical({"kind": "file", "op": "read", "path": "a/b.md", "status": "ok",
                      "content": "hello world", "_self_bounded": True}, source="file")
    assert c["text"] == "hello world"
    assert c["meta"].get("path") == "a/b.md"
    assert c["meta"].get("op") == "read" and c["meta"].get("status") == "ok"
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


def test_file_read_not_found_surfaces_error_text_and_iserror():
    """Tier 1: a not_found read surfaces the error message as ``text`` with ``meta.isError`` — the sole
    error-path driver (so the seam renders ``Error: ...`` and the LLM retries a different path)."""
    c = to_canonical({"kind": "file", "op": "read", "path": "missing.md",
                      "status": "not_found", "error": "file not found: missing.md", "content": ""}, source="file")
    assert c["meta"].get("isError") is True
    assert "file not found" in c["text"]


def test_file_read_image_media_blocks_become_media_attachments():
    """Tier 1: an image read (content empty, media_blocks present) surfaces the blocks as MEDIA
    attachments (matching the MCP mapper), never a structured blob."""
    c = to_canonical({"kind": "file", "op": "read", "path": "x.png", "status": "ok",
                      "content": "", "media_blocks": [{"type": "image", "data": "..."}]}, source="file")
    assert [a["kind"] for a in c["attachments"]] == ["media"]


def test_file_grep_content_mode_renders_match_lines_as_text():
    """Tier 1: grep content mode → ``path:line: text`` lines as ``text`` (readable, not a dict blob)."""
    c = to_canonical({"kind": "file", "op": "grep", "status": "ok", "output_mode": "content",
                      "pattern": "foo", "matches": [
                          {"path": "a.py", "line_number": 3, "content": "foo = 1"},
                          {"path": "b.py", "line_number": 9, "content": "foo()"}],
                      "count": 2}, source="file")
    assert "a.py:3: foo = 1" in c["text"] and "b.py:9: foo()" in c["text"]
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


def test_file_glob_matches_render_as_structured_with_count_summary():
    """Tier 1: glob -> a short count summary as ``text`` + the matched paths as a ``structured``
    attachment (#2955/#2972 -- a genuinely-structured record-list result, same shape as
    ``web_search``, changed FROM the prior newline-joined-into-``text`` shape so a pipeline
    ``for_each`` can fan out over ``ctx.<name>.structured`` without a shell round-trip)."""
    c = to_canonical({"kind": "file", "op": "glob", "pattern": "*.py", "status": "ok",
                      "matches": ["a.py", "b.py"], "count": 2}, source="file")
    assert c["text"] == "2 files"
    assert c["attachments"] == [{"kind": "structured", "data": ["a.py", "b.py"]}]


def test_file_glob_large_match_list_offloads_smaller_than_old_text_shape():
    """Tier 1: real LLM-visible char count for a large glob result — the ``structured`` shape
    (#2955/#2972) is NOT a token-cost regression versus the prior newline-joined-``text`` shape;
    it is a large reduction once the match count crosses the seam's inline-size gate, because a
    ``structured`` attachment offloads to its own ref when large while ``text`` never does (it IS
    the body). Measures ``render_tool_result(*build_offload_body(...))`` -- the actual assembled
    ``role: tool`` content string an LLM reads -- end-to-end through the real seam, not a
    hand-computed estimate. This is the concrete evidence for the #2955 PR body's token-cost claim.
    """
    def _rendered_char_count(canonical: dict) -> int:
        _fake_save.stored = []
        frontmatter, text, _media = build_offload_body(canonical, save_fn=_fake_save)
        return len(render_tool_result(frontmatter, text))

    def _old_shape(n: int) -> int:
        paths = [f"src/pkg/module_{i:04d}.py" for i in range(n)]
        return _rendered_char_count(
            {"text": "\n".join(paths), "attachments": [], "source_ref": None, "meta": {}}
        )

    def _new_shape(n: int) -> int:
        paths = [f"src/pkg/module_{i:04d}.py" for i in range(n)]
        canonical = to_canonical(
            {"kind": "file", "op": "glob", "status": "ok", "matches": paths}, source="file",
        )
        return _rendered_char_count(canonical)

    # Small (60 files, below the structured-inline gate): both shapes stay inline; the new shape
    # costs a modest amount MORE here (frontmatter YAML overhead), never a large regression.
    old_60, new_60 = _old_shape(60), _new_shape(60)
    assert new_60 < old_60 * 1.5, (
        f"small-N should not regress by more than ~50%: old={old_60} new={new_60}"
    )

    # Large (1000 files, above the structured-inline gate): the OLD text-only shape hot-paths the
    # full path list every time (text IS the body, never offloaded); the NEW structured shape
    # offloads to a ref once it crosses the size gate. This is the actual production shape a
    # folder-wide RAG ingest glob would hit -- the OLD shape is the real token bomb, not this PR.
    old_1000, new_1000 = _old_shape(1000), _new_shape(1000)
    assert new_1000 < old_1000 * 0.2, (
        f"large-N structured offload must cut LLM-visible chars by >80%: "
        f"old={old_1000} new={new_1000}"
    )


def test_file_write_is_short_status_text():
    """Tier 1: write → a short human-readable status ``text`` (bytes + path), not a JSON envelope."""
    c = to_canonical({"kind": "file", "op": "write", "path": "out.txt", "status": "ok",
                      "bytes_written": 42}, source="file")
    assert "42" in c["text"] and "out.txt" in c["text"]
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


# ─────────────────────────────────────────────────────────────────────────────
# reyn_repo mapper — per-shape contract
# ─────────────────────────────────────────────────────────────────────────────


def test_reyn_repo_read_content_is_text_path_is_meta():
    """Tier 1: reyn_repo read → ``content`` is the ``text`` body; ``path`` is signal meta."""
    c = to_canonical({"kind": "reyn_repo", "path": "README.md", "content": "# Reyn"}, source="reyn_repo_read")
    assert c["text"] == "# Reyn"
    assert c["meta"].get("path") == "README.md"


def test_reyn_repo_error_surfaces_iserror():
    """Tier 1: a reyn_repo error (e.g. path outside repo) surfaces the message as ``text`` + isError."""
    c = to_canonical({"kind": "reyn_repo", "error": "reyn_repo: path '..' resolves outside repo"}, source="reyn_repo_read")
    assert c["meta"].get("isError") is True
    assert "outside" in c["text"]


def test_reyn_repo_list_entries_render_as_text_lines():
    """Tier 1: reyn_repo list → ``type: name`` lines as ``text`` (a browsable listing, not a dict)."""
    c = to_canonical({"kind": "reyn_repo", "path": "docs", "entries": [
        {"name": "concepts", "type": "dir"}, {"name": "README.md", "type": "file"}]}, source="reyn_repo_list")
    assert "dir: concepts" in c["text"] and "file: README.md" in c["text"]


def test_reyn_repo_grep_matches_render_as_text_lines():
    """Tier 1: reyn_repo grep → ``path:line: snippet`` lines as ``text``."""
    c = to_canonical({"kind": "reyn_repo", "pattern": "def", "count": 1, "truncated": False,
                      "matches": [{"path": "x.py", "line": 5, "snippet": "def foo():"}]}, source="reyn_repo_grep")
    assert "x.py:5: def foo():" in c["text"]
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


def test_reyn_repo_glob_matches_render_as_path_lines():
    """Tier 1: reyn_repo glob → the matched path strings as newline-joined ``text``."""
    c = to_canonical({"kind": "reyn_repo", "pattern": "**/*.md", "count": 2,
                      "matches": ["docs/a.md", "docs/b.md"]}, source="reyn_repo_glob")
    assert c["text"] == "docs/a.md\ndocs/b.md"


# ─────────────────────────────────────────────────────────────────────────────
# compact mapper
# ─────────────────────────────────────────────────────────────────────────────


def test_compact_ok_summarizes_metrics_as_text():
    """Tier 1: compact ok → a short ``text`` summary of the freed-token / free-window metrics (no blob)."""
    c = to_canonical({"kind": "compact", "status": "ok", "freed_tokens": 1200,
                      "free_window_after": 90000, "summarized_turns": 8}, source="compact")
    assert "freed_tokens=1200" in c["text"] and "free_window_after=90000" in c["text"]
    assert not any(a.get("kind") == "structured" for a in c["attachments"])


def test_compact_error_surfaces_iserror():
    """Tier 1: compact error → the message as ``text`` with ``meta.isError``."""
    c = to_canonical({"kind": "compact", "status": "error", "error_kind": "compaction_unavailable",
                      "error": "no compaction context is wired here"}, source="compact")
    assert c["meta"].get("isError") is True
    assert "no compaction context" in c["text"]


