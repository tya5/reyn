"""Tier 2: OS invariant — #272 media-cap core (per-turn media bound + load-contract).

Closes the media-axis conversation dead-end structurally (the text/count axes are
closed by #1161 + #1157):
  - Part-1 per-turn media bound: a tool turn's media follow-up is bounded to the
    budget left after its (already chat-capped) text; overflow path-ref images
    stay a small LOSSLESS ref (image remains on disk), NEVER materialised beyond
    budget → result turn stays ≤ the per-turn cap.
  - Part-2 load-contract: reading a media (image) ref back via read_tool_result
    returns a small structured error (never the raw binary, never another ref),
    so a read-back can neither overflow the prompt nor loop ref→ref.

Together: media cannot structurally overflow the prompt (ref-default + bounded
read-back). give-up = valid continue (bytes preserved on disk, conversation
continues) — dead-end-free without requiring every image be usable.

No collaborator mocks: pure _build_media_followup_message + real read_tool_result
handler with a real MediaStore-backed OpContext.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.chat.router_loop import _MEDIA_IMAGE_TOKEN_COST, _build_media_followup_message
from reyn.tools.read_tool_result import _handle, _is_image_ref
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.workspace.media_store import MediaStore, MediaStoreConfig


class _StubStore:
    """Minimal media_store: read_image returns fixed bytes for any path."""

    def read_image(self, path: str):  # noqa: ARG002
        return b"PNGBYTES", True


def _img_blocks(n: int) -> list[dict]:
    return [
        {"type": "image", "path": f".reyn/tool-results/img{i}.png", "mime_type": "image/png"}
        for i in range(n)
    ]


# ── Part-1: per-turn media bound ─────────────────────────────────────────────


def _imgs(fu: dict) -> list[dict]:
    return [p for p in fu["content"] if p.get("type") == "image_url"]


def _overflow_refs(fu: dict) -> list[dict]:
    return [p for p in fu["content"] if p.get("type") == "text" and "not loaded" in p.get("text", "")]


def test_media_followup_materialises_only_within_budget() -> None:
    """Tier 2: materialised image cost stays within budget; overflow → refs, none dropped."""
    blocks = _img_blocks(3)
    budget = _MEDIA_IMAGE_TOKEN_COST + 200  # room for one image only
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=_StubStore(), budget_tokens=budget,
    )
    imgs, refs = _imgs(fu), _overflow_refs(fu)
    assert imgs, "an image that fits the budget materialises"
    assert len(imgs) * _MEDIA_IMAGE_TOKEN_COST <= budget, (
        "materialised image cost must stay within the per-turn budget"
    )
    assert refs, "images over budget become small refs (not materialised)"
    assert len(imgs) + len(refs) == len(blocks), "every block represented — none silently dropped"


def test_media_followup_unbounded_when_no_budget() -> None:
    """Tier 2: budget_tokens=None preserves the pre-#272 unbounded behaviour (no overflow refs)."""
    blocks = _img_blocks(3)
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=blocks, media_store=_StubStore(), budget_tokens=None,
    )
    assert not _overflow_refs(fu), "unbounded → nothing deferred to a ref"
    assert len(_imgs(fu)) == len(blocks), "all blocks materialise when unbounded"


def test_overflow_ref_is_lossless_pointer() -> None:
    """Tier 2: an over-budget image becomes a ref naming its on-disk path (lossless)."""
    fu = _build_media_followup_message(
        tool_name="t", media_blocks=_img_blocks(1), media_store=_StubStore(),
        budget_tokens=0,  # nothing fits → the image becomes a ref
    )
    assert not _imgs(fu), "budget 0 → no image materialises"
    refs = _overflow_refs(fu)
    assert refs, "the over-budget image is preserved as a ref"
    assert ".reyn/tool-results/img0.png" in refs[0]["text"], (
        "overflow ref must carry the on-disk path so the image is not lost"
    )


# ── Part-2: load-contract (image ref read-back → small error, never ref/binary) ──


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
    # never ref→ref: the result carries no path-ref / offload pointer to re-load.
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
