"""#2663 — carry content_type/mimeType from an offload producer to present's stage-3 default
viewer, as a RENDERER-only sidecar that never reaches the LLM-visible frontmatter.

The chain (real components throughout, no collaborator mocks):

  canonical mapper (``content_type`` sidecar on ``CanonicalToolResult``, #2663)
    -> ``build_offload_body`` (returns it as a 4th tuple element, NEVER folds it into
       ``frontmatter`` — that would leak a renderer/transport signal into the LLM's
       ``role: tool`` body)
    -> ``cap_tool_result_content`` (forwards it to the store's ``mime_type``, so the
       offloaded ref's ON-DISK EXTENSION carries it — reusing the #385 image-store
       mechanism verbatim, no new sidecar field/file)
    -> ``resolve_present_source`` (recovers it back from the ref's extension via
       ``media_store.mime_type_for_ext``)
    -> ``default_viewer_blueprint`` (stage 3: a declared markdown/code type defaults to
       a rich ``markdown``/``code`` component instead of diff-sniff -> shape).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import to_canonical
from reyn.core.offload.seam import build_offload_body
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.present import handle
from reyn.core.present.source import resolve_present_source
from reyn.data.workspace.media_store import MediaStore, mime_type_for_ext
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.services.tool_result_cap import cap_tool_result_content
from reyn.schemas.models import PresentIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

_MODEL = "gpt-4o"


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)


def _ctx(tmp_path: Path) -> tuple[OpContext, EventLog]:
    events = EventLog()
    resolver = _resolver(tmp_path)
    ws = Workspace(events=events, permission_resolver=resolver)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="present_2663_test",
    )
    return ctx, events


def _run(coro):
    return asyncio.run(coro)


class _RecordingRenderer:
    """Real (non-mock) PresentationRenderer recording what reached the surface."""

    surface_name = "inline-cui"

    def __init__(self) -> None:
        self.rendered: list = []

    def render(self, resolved) -> None:
        self.rendered.append(resolved)


# ── Tier 1: canonical mapper declares content_type (renderer sidecar only) ───


def test_web_fetch_raw_body_declares_content_type():
    """Tier 1: a RAW (non-extracted) web_fetch body carries the HTTP content_type as the
    canonical's renderer-only sidecar."""
    c = to_canonical({
        "kind": "web_fetch", "url": "http://x", "status": "ok",
        "content": "# Title\n\nbody", "content_type": "text/markdown", "extractor": "none",
    }, source="web_fetch")
    assert c.get("content_type") == "text/markdown"


def test_web_fetch_extracted_html_does_not_declare_content_type():
    """Tier 1: an HTML page run through the trafilatura/stdlib extractor becomes plain
    readable text (no longer HTML) — the original ``text/html`` header must NOT be
    echoed as the extracted text's content_type (it would be a stale/misleading
    declaration for a different body)."""
    c = to_canonical({
        "kind": "web_fetch", "url": "http://x", "status": "ok",
        "content": "Title\n\nbody", "content_type": "text/html", "extractor": "trafilatura",
    }, source="web_fetch")
    assert c.get("content_type") is None


def test_web_fetch_content_type_absent_when_source_has_none():
    """Tier 1: falsify direction — no content_type on the raw result -> None (not KeyError,
    not an empty-string false-positive)."""
    c = to_canonical({
        "kind": "web_fetch", "url": "http://x", "status": "ok",
        "content": "text", "extractor": "none",
    }, source="web_fetch")
    assert c.get("content_type") is None


# ── Tier 1: build_offload_body never leaks content_type into the LLM-visible frontmatter ──


def test_build_offload_body_returns_content_type_but_never_in_frontmatter():
    """Tier 1: CORE — content_type is returned as its own tuple element, and is bound-out
    of frontmatter/meta entirely — the whole point of #2663's sidecar design (it must
    never reach the LLM's ``role: tool`` YAML frontmatter)."""
    canonical = to_canonical({
        "kind": "web_fetch", "url": "http://x", "status": "ok",
        "content": "raw body", "content_type": "text/markdown", "extractor": "none",
        "truncated": True, "next_start": 10,
    }, source="web_fetch")
    frontmatter, text, _media, content_type = build_offload_body(canonical, save_fn=None)

    assert content_type == "text/markdown"
    assert text == "raw body"
    # Strip-falsify anchor: meta (truncated/next_start) DOES reach frontmatter (existing
    # signal channel) while content_type (renderer-only) explicitly does not.
    assert frontmatter.get("truncated") is True
    assert "content_type" not in frontmatter
    assert "content_type" not in str(frontmatter), "content_type must not leak anywhere in the YAML frontmatter dict"


def test_build_offload_body_content_type_none_when_mapper_declares_none():
    """Tier 1: falsify direction — a mapper/result with no content_type -> the 4th tuple
    element is None (identity), not a missing-key crash."""
    canonical = to_canonical({
        "kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "hi",
    }, source="mcp")
    _fm, _text, _media, content_type = build_offload_body(canonical, save_fn=None)
    assert content_type is None


# ── Tier 2: cap_tool_result_content forwards content_type to the store's mime_type ──


def test_cap_tool_result_content_type_drives_stored_ref_extension(tmp_path: Path) -> None:
    """Tier 2: OS invariant — an oversized body offloaded WITH a declared content_type is
    stored under an extension matching that type (real MediaStore, no fake collaborator);
    with content_type=None the store's existing text/plain default (``.txt``) is unchanged.
    """
    store = MediaStore(project_root=tmp_path)
    big = "# Title\n\n" + ("word " * 20_000)

    out_md = cap_tool_result_content(
        big, cap_tokens=64, model=_MODEL, save_fn=store.save_tool_result,
        use_chars4=True, content_type="text/markdown",
    )
    ref_md = _ref_from(out_md)
    assert ref_md.endswith(".md"), f"declared text/markdown must drive a .md ref, got {ref_md}"

    out_plain = cap_tool_result_content(
        big, cap_tokens=64, model=_MODEL, save_fn=store.save_tool_result,
        use_chars4=True, content_type=None,
    )
    ref_plain = _ref_from(out_plain)
    assert ref_plain.endswith(".txt"), "no declared content_type -> unchanged .txt default"


def _ref_from(preview: str) -> str:
    import re
    m = re.search(r'file__read\(path="([^"]+)"\)', preview)
    assert m, f"preview must name a file__read read-back path: {preview[:200]!r}"
    return m.group(1)


# ── Tier 2: media_store.mime_type_for_ext round-trips a non-default value ───


def test_mime_type_for_ext_roundtrips_markdown_and_rejects_unknown():
    """Tier 2: round-trip a NON-default MIME type (text/markdown, not the text/plain
    default) through the write-side extension table and the read-side reverse lookup;
    an unrecognized/absent extension yields None (safe degrade, not a crash/guess)."""
    assert mime_type_for_ext("a/b/2026-tool-1.md") == "text/markdown"
    assert mime_type_for_ext("a/b/2026-tool-1.txt") == "text/plain"
    assert mime_type_for_ext("a/b/2026-tool-1.unknownext") is None
    assert mime_type_for_ext("a/b/no_extension_at_all") is None


# ── Tier 2: end-to-end wiring — present(data_ref=...) picks the declared-type default ──


def test_present_data_ref_defaults_to_markdown_when_ref_carries_markdown_type(
    tmp_path: Path, monkeypatch: "object",
) -> None:
    """Tier 2: OS invariant — WIRING PROOF. A ref stored with ``mime_type="text/markdown"``
    (the real store write-path #2663 threads content_type through) resolves through the
    real ``present`` op (mode="default", no view/blueprint) to a ``markdown`` component —
    not the generic ``text`` stage-3 default a same-shaped untyped ref would get.
    Strip-falsify: an identical body stored WITHOUT a declared type renders as ``text``,
    proving the markdown default came from the declared type, not the data's shape."""
    monkeypatch.chdir(tmp_path)
    store = MediaStore(project_root=tmp_path)
    body = "# Heading\n\nSome *emphasis* text."

    md_block = store.save_tool_result(body, mime_type="text/markdown", tool="t", seq=1)
    plain_block = store.save_tool_result(body, mime_type="text/plain", tool="t", seq=2)

    ctx, _events = _ctx(tmp_path)
    renderer = _RecordingRenderer()
    ctx.presentation_renderer = renderer

    md_op = PresentIROp(kind="present", data_ref=md_block["path"])
    ack = _run(handle(md_op, ctx))
    assert ack["status"] == "ok"
    assert renderer.rendered, "renderer must have received the resolved presentation"
    md_components = [n.get("component") for n in renderer.rendered[-1].nodes]
    assert "markdown" in md_components, f"declared text/markdown ref must default to markdown, got {md_components}"

    plain_op = PresentIROp(kind="present", data_ref=plain_block["path"])
    _run(handle(plain_op, ctx))
    plain_components = [n.get("component") for n in renderer.rendered[-1].nodes]
    assert plain_components == ["text"], (
        f"an identical body with NO declared markdown type must still default to text "
        f"(falsifies that the markdown default is shape-derived, not type-derived); got {plain_components}"
    )


def test_resolve_present_source_recovers_content_type_from_stored_ref(
    tmp_path: Path, monkeypatch: "object",
) -> None:
    """Tier 2: resolve_present_source's 3rd return value recovers the content_type a
    producer declared at store time, purely from the ref's on-disk extension — the
    read-side half of the #2663 sidecar (no separate metadata file)."""
    monkeypatch.chdir(tmp_path)
    store = MediaStore(project_root=tmp_path)
    block = store.save_tool_result("code here", mime_type="application/json", tool="t", seq=1)

    ctx, _events = _ctx(tmp_path)
    _value, _ingested, content_type = _run(resolve_present_source(block["path"], ctx))
    assert content_type == "application/json"
