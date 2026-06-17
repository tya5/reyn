"""Tier 2: file__read binary image content (issue #365).

Imports the media-size gate landed in #364 and applies it to local
image files read via ``FileIROp(op="read", path="...png")``.

What we pin:
  - Image extensions (.png/.jpg/.jpeg/.gif/.webp/.svg) trigger the
    binary path → result carries media_blocks instead of garbage text.
  - Non-image extensions take the existing text path unchanged.
  - Media-size gate behaviour mirrors #364:
      under-limit → media_blocks loaded
      over-limit + deny → status=denied, no payload
      over-limit + ask + user-no → status=denied
  - File-not-found still surfaces cleanly with suggestions (= pre-#365
    behaviour preserved for binary paths too).
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from reyn.config import MultimodalConfig
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _FakeBus:
    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(text="", choice_id=self._answer)


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=True,
    )


def _ctx(tmp_path: Path, *, multimodal: MultimodalConfig | None, bus_answer: str = "yes") -> OpContext:
    events = EventLog()
    resolver = _resolver(tmp_path)
    workspace = Workspace(events=events, permission_resolver=resolver)
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        skill_name="test",
        intervention_bus=_FakeBus(bus_answer),  # type: ignore[arg-type]
        multimodal_config=multimodal,
    )


def _run(coro):
    return asyncio.run(coro)


# ── image extensions → binary path ─────────────────────────────────────


def test_read_png_returns_media_block(tmp_path, monkeypatch):
    """Tier 2: .png file → result carries media_blocks with base64 data
    and mimeType image/png; content is empty.
    """
    monkeypatch.chdir(tmp_path)
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300
    (tmp_path / "shot.png").write_bytes(raw)

    ctx = _ctx(tmp_path, multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"))
    op = FileIROp(kind="file", op="read", path="shot.png")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["content"] == ""
    assert result["media_blocks"]
    block = result["media_blocks"][0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]) == raw


@pytest.mark.parametrize(
    "filename, expected_mime",
    [
        ("img.jpg", "image/jpeg"),
        ("img.jpeg", "image/jpeg"),
        ("anim.gif", "image/gif"),
        ("pic.webp", "image/webp"),
        ("icon.svg", "image/svg+xml"),
        ("UPPER.PNG", "image/png"),  # case-insensitive extension match
    ],
)
def test_read_recognised_image_extensions(tmp_path, monkeypatch, filename, expected_mime):
    """Tier 2: each known image extension routes through the binary path
    with the correct mimeType. Case-insensitive on the extension.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / filename).write_bytes(b"binary-payload")

    ctx = _ctx(tmp_path, multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"))
    op = FileIROp(kind="file", op="read", path=filename)
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["media_blocks"][0]["mimeType"] == expected_mime


def test_read_text_file_unchanged_when_extension_not_image(tmp_path, monkeypatch):
    """Tier 2: .md / .txt / .py extension → existing text path; no
    media_blocks key. Backward compat with all callers that read code /
    markdown.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.md").write_text("# title\n\nhello world\n")

    ctx = _ctx(tmp_path, multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"))
    op = FileIROp(kind="file", op="read", path="notes.md")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert "hello world" in result["content"]
    assert "media_blocks" not in result


def test_read_unknown_extension_takes_text_path(tmp_path, monkeypatch):
    """Tier 2: extension not in _IMAGE_EXTENSIONS (= .pdf / .bin / no
    extension) takes the text path (= errors='replace' fallback). Pre-
    #365 behaviour preserved for those file types — separate issue if
    they need binary handling.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "noext").write_text("plain text")

    ctx = _ctx(tmp_path, multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"))
    op = FileIROp(kind="file", op="read", path="noext")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["content"] == "plain text"
    assert "media_blocks" not in result


# ── media-size gate (reuses #364 infra) ────────────────────────────────


def test_oversize_image_with_deny_returns_status_denied(tmp_path, monkeypatch):
    """Tier 2: image > cap + on_oversize=deny → status=denied, no media
    payload, file-read permission gate already passed (= shape matches
    web__fetch denied path from #364).
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "huge.png").write_bytes(b"x" * 10_000_000)

    ctx = _ctx(
        tmp_path,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
    )
    op = FileIROp(kind="file", op="read", path="huge.png")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "denied"
    assert result["size_bytes"] == 10_000_000
    assert "media_blocks" not in result or not result.get("media_blocks")


def test_oversize_image_with_ask_no_returns_status_denied(tmp_path, monkeypatch):
    """Tier 2: image > cap + on_oversize=ask + user-no → status=denied."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "huge.jpg").write_bytes(b"x" * 10_000_000)

    ctx = _ctx(
        tmp_path,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        bus_answer="no",
    )
    op = FileIROp(kind="file", op="read", path="huge.jpg")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "denied"


def test_oversize_image_with_allow_loads_anyway(tmp_path, monkeypatch):
    """Tier 2: image > cap + on_oversize=allow → loads without prompt."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "huge.png").write_bytes(b"x" * 10_000_000)

    ctx = _ctx(
        tmp_path,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="allow"),
        bus_answer="never_called",
    )
    op = FileIROp(kind="file", op="read", path="huge.png")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["media_blocks"]


def test_no_multimodal_config_skips_gate(tmp_path, monkeypatch):
    """Tier 2: when multimodal_config is None (= direct-OpContext tests
    or callers that never built a ReynConfig), the gate is bypassed
    entirely — backward compat for the legacy code path.
    """
    monkeypatch.chdir(tmp_path)
    raw = b"any-size"
    (tmp_path / "shot.png").write_bytes(raw)

    ctx = _ctx(tmp_path, multimodal=None)
    op = FileIROp(kind="file", op="read", path="shot.png")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["media_blocks"]


# ── error paths ────────────────────────────────────────────────────────


def test_read_missing_image_returns_not_found(tmp_path, monkeypatch):
    """Tier 2: image extension + non-existent path → status=not_found
    with suggestions list (= same shape as text not_found, NOT denied).
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "other.png").write_bytes(b"sibling")

    ctx = _ctx(tmp_path, multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"))
    op = FileIROp(kind="file", op="read", path="missing.png")
    result = _run(handle(op, ctx, "control_ir"))

    assert result["status"] == "not_found"
    assert "suggestions" in result
