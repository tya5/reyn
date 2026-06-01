"""Tier 2: OS invariant — #272 media-cap core + media-count cap (dead-end-free
media axis).

Closes the media-axis conversation dead-end structurally (text/count axes are
closed by #1161 + #1157):
  - Per-turn media bound: a tool turn's media follow-up is bounded to the budget
    left after its (already chat-capped) text; images materialise while they fit.
  - media-COUNT cap (this PR): the WHOLE follow-up — materialised images +
    individual overflow refs + the tail preview — stays ≤ budget_tokens. So
    neither the image bytes (Gap A: inline-shape images previously bypassed the
    bound) NOR the ref count (Gap B: one text ref per overflow image, unbounded)
    can grow the follow-up without bound. The over-budget tail collapses into ONE
    lossless offloaded-manifest preview (or, with no store, a least-lossy bounded
    note — a conscious environment-bound, never a silent drop).
  - load-contract: reading a media (image) ref back via read_tool_result returns
    a small structured error (never the raw binary, never another ref), so a
    read-back can neither overflow the prompt nor loop ref→ref.

Together: media cannot structurally overflow the prompt, so the result turn is
always single-turn compactable (the chat retry_loop's shrink can fold it).

No collaborator mocks: pure _build_media_followup_message + real read_tool_result
handler with a real MediaStore.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from reyn.chat.router_loop import (
    _MEDIA_IMAGE_TOKEN_COST,
    _build_media_followup_message,
)
from reyn.services.compaction.engine import _IMAGE_FIXED_TOKEN_COST, estimate_tokens
from reyn.tools.read_tool_result import _handle, _is_image_ref
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.workspace.media_store import MediaStore, MediaStoreConfig


def _store(tmp_path: Path) -> MediaStore:
    return MediaStore(MediaStoreConfig(), project_root=tmp_path)


def _path_blocks(store: MediaStore, n: int) -> list[dict]:
    """n real path-ref image blocks (bytes written to the store on disk)."""
    return [
        store.save_image(b"PNGBYTES" * 8, mime_type="image/png", tool="t", seq=i)
        for i in range(n)
    ]


def _inline_blocks(n: int) -> list[dict]:
    """n inline-base64 image blocks (the pre-PR-C / no-store shape)."""
    data = base64.b64encode(b"PNGBYTES" * 8).decode("ascii")
    return [{"type": "image", "data": data, "mimeType": "image/png"} for _ in range(n)]


def _imgs(fu: dict) -> list[dict]:
    return [p for p in fu["content"] if p.get("type") == "image_url"]


def _texts(fu: dict) -> list[dict]:
    # text parts other than the intro line
    return [
        p for p in fu["content"]
        if p.get("type") == "text" and not p["text"].startswith("Tool `")
    ]


def _followup_tokens(fu: dict) -> int:
    """Measure the follow-up the SAME way the compaction engine measures a turn:
    each image part = _IMAGE_FIXED_TOKEN_COST, text parts = estimate_tokens.
    This is the real cost the per-turn budget is defined against (foldability)."""
    total = 0
    for p in fu["content"]:
        if p.get("type") == "image_url":
            total += _IMAGE_FIXED_TOKEN_COST
        elif p.get("type") == "text":
            total += estimate_tokens(p["text"], "gpt-4", use_chars4=True)
    return total


# ── Per-turn media bound (materialise within budget) ─────────────────────────


def test_media_followup_materialises_only_within_budget(tmp_path: Path) -> None:
    """Tier 2: materialised image cost stays within budget; the rest are
    represented (individual refs and/or a tail preview), none dropped (the new
    bounded contract that replaces #1171's unbounded per-image refs)."""
    store = _store(tmp_path)
    blocks = _path_blocks(store, 8)
    budget = 5 * _MEDIA_IMAGE_TOKEN_COST  # room to materialise ~4 then refs
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=store, budget_tokens=budget,
    )
    imgs = _imgs(fu)
    assert imgs, "images that fit the budget materialise"
    assert len(imgs) * _MEDIA_IMAGE_TOKEN_COST <= budget, (
        "materialised image cost stays within the per-turn budget"
    )
    # The whole follow-up — images + refs + any tail preview — stays ≤ budget.
    assert _followup_tokens(fu) <= budget


def test_media_followup_unbounded_when_no_budget(tmp_path: Path) -> None:
    """Tier 2: budget_tokens=None preserves the pre-#272 unbounded behaviour."""
    store = _store(tmp_path)
    blocks = _path_blocks(store, 3)
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=store, budget_tokens=None,
    )
    assert len(_imgs(fu)) == len(blocks), "all blocks materialise when unbounded"
    assert not _texts(fu), "unbounded → nothing deferred to a ref/preview"


def test_overflow_image_is_lossless_individual_ref(tmp_path: Path) -> None:
    """Tier 2: a just-over-budget image (within ref budget) becomes an individual
    ref naming its on-disk path — lossless, the image is not lost."""
    store = _store(tmp_path)
    blocks = _path_blocks(store, 2)
    # Room for exactly one materialised image + at least one ref, no tail.
    budget = _MEDIA_IMAGE_TOKEN_COST + 400
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=store, budget_tokens=budget,
    )
    refs = [p for p in _texts(fu) if "not loaded" in p["text"]]
    assert refs, "the over-budget image is preserved as an individual ref"
    assert blocks[1]["path"] in refs[0]["text"], (
        "overflow ref carries the on-disk path so the image is recoverable"
    )
    assert _followup_tokens(fu) <= budget


# ── media-COUNT cap: Gap A (inline) + Gap B (unbounded ref count) ─────────────


def test_total_followup_bounded_for_huge_image_count(tmp_path: Path) -> None:
    """Tier 2: Gap B (retry-loop-safe pin) — a tool result with MANY images keeps the
    whole follow-up ≤ budget; the over-budget tail collapses into ONE preview, not
    one ref per image, which is what keeps the result turn bounded for retry loops."""
    store = _store(tmp_path)
    blocks = _path_blocks(store, 500)
    budget = 6 * _MEDIA_IMAGE_TOKEN_COST
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=store, budget_tokens=budget,
    )
    assert _followup_tokens(fu) <= budget, "whole follow-up must stay within budget"
    # The 500 over-budget images collapse into a single tail preview (behavioural
    # invariant: bounded follow-up), not one ref per image.
    tail = [p for p in _texts(fu) if "more image(s) exceed" in p["text"]]
    assert tail, "a tail preview stands in for the over-budget images"


def test_tail_preview_manifest_is_lossless(tmp_path: Path) -> None:
    """Tier 2: Gap B (lossless) — with a store, the tail preview points to an
    offloaded manifest listing every over-budget image's on-disk path, so the
    images are recoverable (read_tool_result-able): bounded AND lossless."""
    store = _store(tmp_path)
    blocks = _path_blocks(store, 40)
    budget = 4 * _MEDIA_IMAGE_TOKEN_COST
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=store, budget_tokens=budget,
    )
    tail = [p for p in _texts(fu) if "manifest" in p["text"]]
    assert tail, "tail preview names a lossless manifest"
    # Extract the manifest path from the preview text and read it back.
    import re
    m = re.search(r"stored at (\S+);", tail[0]["text"])
    assert m, tail[0]["text"]
    manifest_text, found = store.read_tool_result(m.group(1))
    assert found, "manifest is readable"
    manifest = json.loads(manifest_text)
    # Over-budget images are split between individual refs (named inline) and the
    # offloaded manifest; their UNION must cover every over-budget image (none
    # lost) — that is the losslessness invariant.
    inline_ref_paths = {
        mm.group(1)
        for p in _texts(fu)
        if (mm := re.search(r"Stored at (\S+) \(", p["text"]))
    }
    manifest_paths = {img["path"] for img in manifest["images"]}
    recoverable = inline_ref_paths | manifest_paths
    materialised = len(_imgs(fu))
    assert recoverable.issuperset({b["path"] for b in blocks[materialised:]}), (
        "every over-budget image is recoverable via an individual ref or the manifest"
    )


def test_inline_images_are_budget_accounted(tmp_path: Path) -> None:
    """Tier 2: Gap A — inline-base64 images are accounted against the budget too
    (previously materialised unconditionally, bypassing the bound)."""
    store = _store(tmp_path)
    blocks = _inline_blocks(50)
    budget = 3 * _MEDIA_IMAGE_TOKEN_COST
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=store, budget_tokens=budget,
    )
    assert _followup_tokens(fu) <= budget, "inline images must not bypass the budget"
    assert len(_imgs(fu)) <= budget // _MEDIA_IMAGE_TOKEN_COST


def test_no_store_tail_degrades_to_bounded_note(tmp_path: Path) -> None:
    """Tier 2: no-store sub-case — without a store, an over-budget inline tail
    degrades to a bounded count note (conscious environment-bound), never a
    silent drop, and the follow-up still stays bounded."""
    blocks = _inline_blocks(100)
    budget = 3 * _MEDIA_IMAGE_TOKEN_COST
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=None, budget_tokens=budget,
    )
    assert _followup_tokens(fu) <= budget
    notes = [p for p in _texts(fu) if "not shown" in p["text"]]
    assert notes, "the dropped tail is surfaced as a bounded note, not silently lost"
    assert "more image(s)" in notes[0]["text"]


# ── load-contract (image ref read-back → small error, never ref/binary) ──────


def _ctx(media_store):
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    class _Events:
        subscribers: list = []

        def emit(self, *a, **k):
            pass

    ev = _Events()

    def _factory():
        return OpContext(
            workspace=None, events=ev, permission_decl=PermissionDecl(),
            permission_resolver=None, skill_name="", subscribers=[],
            media_store=media_store,
        )

    return ToolContext(
        events=ev, permission_resolver=None, workspace=None, caller_kind="router",
        router_state=RouterCallerState(op_context_factory=_factory), phase_state=None,
    )


def test_image_ref_read_back_returns_small_error_never_binary(tmp_path: Path) -> None:
    """Tier 2: read_tool_result on an image ref → small structured error, not binary, not a ref."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    result = asyncio.run(_handle({"path": ".reyn/tool-results/pic.png"}, _ctx(store)))
    assert result["status"] == "error"
    assert result["error_kind"] == "media_not_text_loadable"
    assert "media_size_tokens" in result, "the LLM needs the context cost of the image"
    assert "_offload_ref" not in result and "tool_result_ref" not in str(result)


def test_text_ref_read_back_is_not_guarded(tmp_path: Path) -> None:
    """Tier 2: a TEXT tool-result ref still reads normally (the guard is image-only)."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    block = store.save_tool_result("hello body\n", mime_type="text/plain", chain_id="c", tool="x", seq=1)
    result = asyncio.run(_handle({"path": block["path"]}, _ctx(store)))
    assert result["status"] == "ok"
    assert result["content"] == "hello body\n"


def test_is_image_ref_extensions() -> None:
    """Tier 2: _is_image_ref recognises image extensions across path / url / uri tails."""
    for ident in (
        ".reyn/tool-results/a.png", "x.JPG", "y.jpeg", "z.gif", "w.webp",
        "https://h/agents/me/tool-results/p.png?v=1",
        "reyn-tool-result://me/q.bmp",
    ):
        assert _is_image_ref(ident), ident
    for ident in (".reyn/tool-results/a.txt", "notes.json", "data.bin", "readme"):
        assert not _is_image_ref(ident), ident
