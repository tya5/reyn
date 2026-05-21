"""MediaStore — flat-file storage for multimodal media + tool result text
under ``.reyn/`` (issue #383 E-full Phase 3, F1-B scope).

Two storage directories with parallel file naming convention:

  .reyn/media/         — image binary (= web_fetch image / file_read image /
                         mcp image media blocks). Consumed by the chat
                         router and history builder.
  .reyn/tool-results/  — text-y tool result dumps (= web_fetch text /
                         mcp text / future preview-driven tool results
                         per #385). PR-C lands the writer; PR-D wires
                         the consumer + preview generation.

Filename convention (both dirs):

  <YYYYMMDDTHHMMSS>-<chain_short>-<tool>-<seq>.<ext>

This sorts chronologically with ``ls -la``, groups by conversation chain
when you grep for ``<chain_short>``, and is browseable as plain files —
users can ``open``, ``ls``, or delete entries to manage disk usage.

ChatMessage carries **path-refs** (= ``{"type": "image", "path": ...,
"mime_type": ..., "content_hash": ...}``) instead of inline base64. The
LLM-wire boundary (``_build_history_for_router`` / the chat router's
synthetic follow-up builder) reads the path, encodes, and embeds the
binary as a data URL ONLY when sending to the model. Storage stays
light; the LLM sees the materialised form.

Out of scope for PR-C (F1-B):
  - Preview generator (= deferred to PR-D / #385 PoC).
  - ``read_tool_result(path)`` action (= deferred to PR-D).
  - Cleanup policy (TTL / max-N / session boundary) — files accumulate
    until the user deletes them. Deferred to PR-D.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Conservative mapping from MIME type to file extension; unknown types
# fall back to ``""`` so the storage layer still writes a file (= user
# can rename / inspect with their preferred tool). Extension is purely
# for explorability — it isn't used by the lookup path.
_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/json": ".json",
    "application/xml": ".xml",
}


def _ext_for_mime(mime: str) -> str:
    """Return the file extension (with leading dot) for ``mime``.

    Strips any ``; charset=...`` suffix before lookup. Returns ``""`` for
    unknown types — caller still writes the file, just without a hint.
    """
    base = mime.split(";", 1)[0].strip().lower() if mime else ""
    return _MIME_TO_EXT.get(base, "")


def _safe_token(value: str) -> str:
    """Sanitise a value for embedding in a filename.

    Replaces path-separators, spaces, and other shell-unfriendly chars
    with ``_``. Keeps the result reasonable on common filesystems
    (Linux / macOS / Windows).
    """
    if not value:
        return ""
    out = []
    for ch in value:
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def _timestamp() -> str:
    """``YYYYMMDDTHHMMSS`` UTC timestamp used as the filename prefix."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


@dataclass
class MediaStoreConfig:
    """Storage location configuration for :class:`MediaStore`.

    Paths are interpreted relative to ``project_root`` (= the chat
    session's CWD-rooted workspace). Defaults match the user-browsable
    convention chosen in issue #383 / #385:
        ``.reyn/media``        for image binary
        ``.reyn/tool-results`` for text-y tool result dumps
    """
    media_dir: str = ".reyn/media"
    tool_results_dir: str = ".reyn/tool-results"


class MediaStore:
    """Path-ref'd file storage for multimodal media + tool result text.

    Each ``save_*`` call writes a file under the appropriate directory
    and returns a **path-ref block** suitable for placement in a
    ``ChatMessage.content`` list (= part of the OpenAI/Anthropic wire
    shape mirror; see issue #383). The corresponding ``read_*`` methods
    do the inverse lookup with workspace-boundary validation.
    """

    def __init__(
        self,
        config: MediaStoreConfig | None = None,
        *,
        project_root: Path,
    ) -> None:
        self._config = config or MediaStoreConfig()
        self._project_root = project_root.resolve()
        self._media_dir = (
            self._project_root / self._config.media_dir
        ).resolve()
        self._tool_results_dir = (
            self._project_root / self._config.tool_results_dir
        ).resolve()

    # ── Image storage (= .reyn/media/) ────────────────────────────────

    def save_image(
        self,
        data: bytes,
        *,
        mime_type: str,
        chain_id: str = "",
        tool: str = "tool",
        seq: int = 1,
    ) -> dict:
        """Write ``data`` to a new file under ``media_dir`` and return a
        path-ref block (= ``{"type": "image", "path": ..., "mime_type":
        ..., "content_hash": ...}``).

        ``chain_id`` (= short prefix), ``tool``, and ``seq`` are encoded
        into the filename for explorability. ``content_hash`` is the
        SHA-256 of ``data`` (= verifies the path-ref hasn't drifted
        from the original content; used by the history builder when
        materialising back to a data URL).
        """
        self._media_dir.mkdir(parents=True, exist_ok=True)
        chain_short = _safe_token(chain_id)[:6] if chain_id else ""
        tool_token = _safe_token(tool) or "tool"
        ext = _ext_for_mime(mime_type)
        filename = f"{_timestamp()}-{chain_short}-{tool_token}-{seq}{ext}"
        path = self._media_dir / filename
        path.write_bytes(data)
        return {
            "type": "image",
            "path": str(path.relative_to(self._project_root)),
            "mime_type": mime_type,
            "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        }

    def read_image(self, path_str: str) -> tuple[bytes, bool]:
        """Read image binary by project-relative path.

        Validates the resolved path lives inside ``media_dir`` (=
        defends against path-traversal injection from migrated /
        adversarial ChatMessage content). Returns ``(data, found)``;
        ``found=False`` if the file does not exist OR was deleted by
        the user since the path-ref was minted.
        """
        full = (self._project_root / path_str).resolve()
        try:
            full.relative_to(self._media_dir)
        except ValueError as exc:
            raise PermissionError(
                f"path {path_str!r} is outside media_dir "
                f"{self._media_dir} — refusing to read"
            ) from exc
        if not full.exists():
            return b"", False
        return full.read_bytes(), True

    # ── Tool result storage (= .reyn/tool-results/) ───────────────────

    def save_tool_result(
        self,
        content: str,
        *,
        mime_type: str = "text/plain",
        chain_id: str = "",
        tool: str = "tool",
        seq: int = 1,
    ) -> dict:
        """Write a tool result text dump to ``tool_results_dir`` and
        return a path-ref block (= ``{"type": "tool_result_ref", "path":
        ..., "mime_type": ..., "content_hash": ...}``).

        PR-C lands this writer alongside ``save_image`` so the
        abstraction is uniform across multimodal axes. The CONSUMER
        side (= web_fetch / file_read text-path rework to actually
        emit path-refs + preview) is deferred to PR-D per #385.
        """
        self._tool_results_dir.mkdir(parents=True, exist_ok=True)
        chain_short = _safe_token(chain_id)[:6] if chain_id else ""
        tool_token = _safe_token(tool) or "tool"
        ext = _ext_for_mime(mime_type)
        filename = f"{_timestamp()}-{chain_short}-{tool_token}-{seq}{ext}"
        path = self._tool_results_dir / filename
        path.write_text(content, encoding="utf-8")
        return {
            "type": "tool_result_ref",
            "path": str(path.relative_to(self._project_root)),
            "mime_type": mime_type,
            "content_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        }

    def read_tool_result(self, path_str: str) -> tuple[str, bool]:
        """Read tool result text by project-relative path.

        Validates the resolved path lives inside ``tool_results_dir``.
        Returns ``(text, found)``.
        """
        full = (self._project_root / path_str).resolve()
        try:
            full.relative_to(self._tool_results_dir)
        except ValueError as exc:
            raise PermissionError(
                f"path {path_str!r} is outside tool_results_dir "
                f"{self._tool_results_dir} — refusing to read"
            ) from exc
        if not full.exists():
            return "", False
        return full.read_text(encoding="utf-8"), True

    # ── Introspection ─────────────────────────────────────────────────

    @property
    def media_dir(self) -> Path:
        """Absolute path of the image storage directory."""
        return self._media_dir

    @property
    def tool_results_dir(self) -> Path:
        """Absolute path of the tool result text storage directory."""
        return self._tool_results_dir
