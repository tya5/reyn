"""Tier 2: /attachment slash command (#5509 — owner: "スラッシュコマンド
attachment みたいなので任意ファイル指定できるようにしたい").

Mirrors ``test_user_image_input.py``'s own established pattern for the
same shared mechanism (``session._pending_user_attachments``, read via
the SAME public ``pending_user_images`` property that file's own
``_FakeSession`` exposes — never the private field directly; the #364
media-size gate; the #383 PR-C path-ref block shape) — this is the SAME
queue and drain contract /image already established, opened to any file
extension via stdlib ``mimetypes`` (never a reyn-specific table — see
``core/present/artifact_payload.py``'s own established invariant, which
``attachment.py``'s own module docstring cites).

We pin:
  - Slash command happy path: arbitrary extension → queue grows, block
    "type" is DERIVED from the resolved mime (#5526's own fix, exercised
    live here rather than only at the pure-function level).
  - An unresolvable extension degrades to the generic mime, never a
    refusal (unlike /image, which refuses unknown extensions outright).
  - Missing/non-file path errors, same shape as /image's own.
  - Media gate (#364) integration: oversize file + on_oversize=deny.
  - /image and /attachment share the SAME queue (interleaving works).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from reyn.config import MultimodalConfig
from reyn.interfaces.slash import REGISTRY
from reyn.security.permissions.permissions import PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention
from tests._support.slash import slash_ctx


class _FakeBus:
    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(text="", choice_id=self._answer)


@dataclass
class _FakeSession:
    """Minimal Session-shaped stand-in — same shape
    ``test_user_image_input.py``'s own ``_FakeSession`` uses (the two
    commands share the exact same session attributes, including the
    public ``pending_user_images`` read accessor)."""
    _multimodal_config: MultimodalConfig | None = None
    _perm: PermissionResolver | None = None
    _intervention_bus: _FakeBus | None = None
    _pending_user_attachments: list[dict] = field(default_factory=list)
    captured_outbox: list = field(default_factory=list)

    @property
    def pending_user_images(self) -> list[dict]:
        """Mirror of Session.pending_user_images for the fake stub."""
        return self._pending_user_attachments


def _ctx(session):
    return slash_ctx(session, recorder=session.captured_outbox)


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=True,
    )


def _run(coro):
    return asyncio.run(coro)


def _get_attachment_handler():
    cmd = REGISTRY.get("attachment")
    assert cmd is not None, "/attachment slash command should be registered"
    return cmd.handler


# ── happy path ─────────────────────────────────────────────────────────


def test_attachment_cmd_queues_a_pdf(tmp_path, monkeypatch):
    """Tier 2: /attachment report.pdf → block appended to queue with the
    CORRECT (mime-derived, #5526) type, not a hardcoded "image"."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake pdf bytes")

    session = _FakeSession(
        _multimodal_config=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        _perm=_resolver(tmp_path),
        _intervention_bus=_FakeBus("yes"),
    )
    handler = _get_attachment_handler()
    _run(handler(_ctx(session), "report.pdf"))

    assert session.pending_user_images, "expected file queued"
    block = session.pending_user_images[0]
    assert block["type"] == "document"  # derived from application/pdf
    assert block["mime_type"] == "application/pdf"
    assert "report.pdf" in block["path"]
    assert block["content_hash"].startswith("sha256:")
    assert any("report.pdf" in m.text for m in session.captured_outbox)


def test_attachment_cmd_accepts_an_image_too(tmp_path, monkeypatch):
    """Tier 2: /attachment is a strict superset of /image — an image
    extension still works, classified as "image" like /image's own."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    session = _FakeSession()
    handler = _get_attachment_handler()
    _run(handler(_ctx(session), "shot.png"))

    block = session.pending_user_images[0]
    assert block["type"] == "image"
    assert block["mime_type"] == "image/png"


def test_attachment_cmd_unknown_extension_uses_the_generic_mime(tmp_path, monkeypatch):
    """Tier 2: unlike /image (which refuses an unmapped extension
    outright), /attachment accepts ANY extension — an unresolvable one
    degrades to the RFC 2046 generic type + the "file" catch-all block
    type, never a refusal."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data.xyz123notreal").write_bytes(b"opaque bytes")

    session = _FakeSession()
    handler = _get_attachment_handler()
    _run(handler(_ctx(session), "data.xyz123notreal"))

    assert session.pending_user_images, "expected file queued despite unknown extension"
    block = session.pending_user_images[0]
    assert block["mime_type"] == "application/octet-stream"
    assert block["type"] == "file"


def test_multiple_attachment_calls_stack(tmp_path, monkeypatch):
    """Tier 2: same stacking contract as /image — two calls before the
    next user turn queue both, in order."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.pdf").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")

    session = _FakeSession()
    handler = _get_attachment_handler()
    _run(handler(_ctx(session), "a.pdf"))
    _run(handler(_ctx(session), "b.txt"))

    paths = [b["path"] for b in session.pending_user_images]
    assert any("a.pdf" in p for p in paths)
    assert any("b.txt" in p for p in paths)


def test_image_and_attachment_share_the_same_queue(tmp_path, monkeypatch):
    """Tier 2: #5509's own explicit design point — "同じ queue に別 mime
    の block を積むだけです". /image and /attachment interleave into ONE
    queue, drained together by the same next-turn contract."""
    from reyn.interfaces.slash.image import image_cmd

    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")

    session = _FakeSession()
    _run(image_cmd(_ctx(session), "shot.png"))
    attachment_handler = _get_attachment_handler()
    _run(attachment_handler(_ctx(session), "report.pdf"))

    queued = session.pending_user_images
    types = {b["type"] for b in queued}
    assert types == {"image", "document"}


# ── error paths (mirror /image's own) ───────────────────────────────────


def test_attachment_cmd_empty_path_errors(tmp_path):
    """Tier 2: /attachment with empty path → queue stays empty; outbox
    shows a usage error."""
    session = _FakeSession()
    handler = _get_attachment_handler()
    _run(handler(_ctx(session), ""))

    assert session.pending_user_images == []
    assert any(m.kind == "error" and "usage" in m.text for m in session.captured_outbox)


def test_attachment_cmd_missing_file_errors(tmp_path, monkeypatch):
    """Tier 2: /attachment with a non-existent file → queue stays empty;
    outbox shows a not-found error."""
    monkeypatch.chdir(tmp_path)
    session = _FakeSession()
    handler = _get_attachment_handler()
    _run(handler(_ctx(session), "no-such-file.pdf"))

    assert session.pending_user_images == []
    assert any(m.kind == "error" and "not found" in m.text for m in session.captured_outbox)


def test_attachment_cmd_directory_path_errors(tmp_path, monkeypatch):
    """Tier 2: /attachment given a directory path → queue stays empty;
    outbox shows a not-a-file error."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "adir").mkdir()
    session = _FakeSession()
    handler = _get_attachment_handler()
    _run(handler(_ctx(session), "adir"))

    assert session.pending_user_images == []
    assert any(m.kind == "error" and "not a file" in m.text for m in session.captured_outbox)


# ── media gate integration (= #364 reuse, same as /image) ───────────────


def test_attachment_cmd_oversize_with_deny_keeps_queue_empty(tmp_path, monkeypatch):
    """Tier 2: oversize file + on_oversize=deny → gate denies, queue
    stays empty, error in outbox."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "huge.pdf").write_bytes(b"x" * 10_000_000)
    session = _FakeSession(
        _multimodal_config=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
        _perm=_resolver(tmp_path),
        _intervention_bus=_FakeBus("never_called"),
    )
    handler = _get_attachment_handler()
    _run(handler(_ctx(session), "huge.pdf"))

    assert session.pending_user_images == []
    assert any(m.kind == "error" for m in session.captured_outbox)


def test_attachment_cmd_no_multimodal_config_skips_gate(tmp_path, monkeypatch):
    """Tier 2: when the session lacks a multimodal_config (= direct test
    construction), the gate is skipped — the file still queues."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.pdf").write_bytes(b"x" * 10_000_000)
    session = _FakeSession()  # no multimodal_config, no perm

    handler = _get_attachment_handler()
    _run(handler(_ctx(session), "any.pdf"))

    assert session.pending_user_images, "expected file queued (gate skipped)"
