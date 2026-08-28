"""Offload store — axis-agnostic infrastructure for value-to-file offloading.

"Offload" = a value is too large to inline → write the full content to a
workspace file, return a short preview (produced by an injected strategy) +
a path reference + a content hash so the reader can verify integrity later.

This module is **axis-agnostic / P7-clean**: it contains no skill names,
phase names, artifact-type literals, or any other OS-internal domain strings.
Preview generation is strategy-injected because what constitutes a useful
preview differs per use-site (LLM cognition context, output format, etc.).

Content hash format: ``"sha256:<hex>"`` — matches MediaStore (media_store.py)
so Phase 2 can unify the two implementations without a format migration.

★ Three-party preview-bound contract
=====================================
When ``preview_strategy=None`` (= the chat/MediaStore axis), the service
stores the content and computes the hash, but does **NOT** apply any preview
bound.  Responsibility for bounding the inline preview lies with the CALLER:

- **phase axis** (``preview_strategy`` is a real Callable):
  The injected strategy holds the hard size-bound (e.g. the #1100 bound).
  The service delegates entirely to the strategy — it neither caps nor expands
  the result.

- **chat axis** (``preview_strategy=None``, used by ``MediaStore``):
  ``OffloadResult.preview`` is ``None``.  The calling code (e.g.
  ``web.py`` ``_generate_web_fetch_preview``, first 10 lines) is responsible
  for constructing and bounding the preview before returning it to the LLM.
  The service does NOT bound anything on this axis.

- **service**:  Holds **no** preview bound when ``preview_strategy=None``.
  A future axis must NOT assume "the service bounds for me" — it must either
  supply a strategy or bound externally.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class OffloadResult:
    """Returned by :func:`offload_value` after writing the full content to disk.

    Attributes:
        preview:      The inline preview produced by the injected strategy, or
                      ``None`` when ``preview_strategy=None`` was passed (= chat
                      axis; the caller builds the preview externally).
                      Shape is strategy-dependent — callers may attach it to
                      the inline slot as-is.
        path_ref:     Absolute path string pointing at the file containing the
                      full serialised content.
        content_hash: ``"sha256:<hex>"`` digest of the full serialised bytes.
                      Verifiable via :func:`read_offloaded` with the same hash.
    """

    preview: Any
    path_ref: str
    content_hash: str


def offload_value(
    value: Any,
    *,
    store_dir: Path,
    preview_strategy: Callable[[Any, str], Any] | None = None,
    filename: str | None = None,
    payload_field: str | None = None,
    submit_nowait: "Callable[[Callable[[], Any]], None] | None" = None,
) -> OffloadResult:
    """Write *value* to *store_dir* and return preview + path_ref + content_hash.

    Serialisation: dicts are serialised via ``json.dumps(ensure_ascii=False)``;
    strings are written directly (UTF-8). Other types are serialised via
    ``json.dumps`` as well.

    Args:
        value:            The value to offload (dict or str; other types via json).
        store_dir:        Directory to write the full content into. Created if
                          absent (parents included).
        preview_strategy: Callable ``(value, path_ref) -> preview``, or ``None``.
                          When provided, invoked after the file is written so
                          ``path_ref`` is available for embedding in the preview
                          (e.g. truncation markers); the result lands in
                          ``OffloadResult.preview``.
                          When ``None`` (= chat axis / MediaStore), the service
                          performs NO preview generation; ``OffloadResult.preview``
                          is ``None`` and the CALLER is responsible for bounding
                          the inline preview externally.  See the module-level
                          ★ three-party preview-bound contract for details.
        filename:         Optional explicit filename. When omitted a unique name
                          is derived from a UUID fragment.
        payload_field:    #2336 (producer-declares-payload). When set AND *value* is
                          a dict carrying that field, the file stores THAT field's
                          value CLEAN — a str payload is written raw (real newlines,
                          not a JSON-escaped ``\\n`` inside a dict envelope; fixes the
                          "JSON-of-JSON" read-back), a non-str payload is
                          ``json.dumps``'d (a clean array/object, not the whole dict).
                          The caller passes this only when the payload field is the
                          sole oversized field (single-dominant); a multi-large-field
                          result falls back to the whole-dict path (caller passes
                          ``None``) so no non-dominant field's full content is lost.
                          Absent / field missing → the whole-value serialisation.
                          The preview (and thus the inline envelope) is always built
                          over the WHOLE *value*, unchanged.
        submit_nowait:    #5364 §1.4 (owner: "UIを止めさせたくない" — a synchronous
                          write on the event loop, e.g. onto slow/network storage,
                          stalls the whole loop, same class of problem #1765 fixed
                          for the WAL). When provided (a ``DurabilityWorker.
                          submit_nowait``-shaped callable), the actual disk write
                          is handed to it FIRE-AND-FORGET instead of running
                          inline — everything else (hash, preview, the returned
                          path_ref) is still computed synchronously, since
                          ``serialized`` is already fully in memory before the
                          write is enqueued; only the write's OWN latency moves
                          off the loop. ``None`` (default) keeps the prior
                          synchronous behaviour byte-identical — every caller but
                          MediaStore's own chat axis.

    Returns:
        :class:`OffloadResult` with preview (or ``None``), path_ref, content_hash.
    """
    store_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        uid = uuid.uuid4().hex[:8]
        filename = f"{uid}.json"

    dest = store_dir / filename
    if payload_field is not None and isinstance(value, dict) and payload_field in value:
        payload = value[payload_field]
        serialized = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    else:
        serialized = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    if submit_nowait is not None:
        async def _write_job(_dest: Path = dest, _serialized: str = serialized) -> None:
            await asyncio.to_thread(_dest.write_text, _serialized, encoding="utf-8")
        submit_nowait(_write_job)
    else:
        dest.write_text(serialized, encoding="utf-8")

    path_ref = str(dest)
    content_hash = "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()
    preview = preview_strategy(value, path_ref) if preview_strategy is not None else None

    return OffloadResult(preview=preview, path_ref=path_ref, content_hash=content_hash)


def read_offloaded(
    path_str: str,
    *,
    base_dir: Path,
    content_hash: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> tuple[str, bool]:
    """Read back offloaded content with path-boundary validation.

    Args:
        path_str:     Absolute path string to the offloaded file (the
                      ``path_ref`` from :class:`OffloadResult`).
        base_dir:     Validation boundary. The resolved path MUST be under
                      ``base_dir``; raises :class:`PermissionError` otherwise.
                      Mirrors ``MediaStore.read_tool_result``'s boundary check.
        content_hash: When provided, verifies the SHA-256 of the **full**
                      (pre-slice) content. Raises :class:`ValueError` on
                      mismatch. Format: ``"sha256:<hex>"``.
        offset:       0-indexed line slice start. ``None`` = from beginning.
        limit:        Maximum number of lines to return. ``None`` = all lines.

    Returns:
        ``(content, found)`` where ``found=False`` when the file does not exist.

    Raises:
        PermissionError: When ``path_str`` resolves outside ``base_dir``.
        ValueError:      When ``content_hash`` is provided and does not match.
    """
    full = Path(path_str).resolve()
    base_resolved = base_dir.resolve()
    try:
        full.relative_to(base_resolved)
    except ValueError as exc:
        raise PermissionError(
            f"path {path_str!r} is outside base_dir "
            f"{base_resolved} — refusing to read"
        ) from exc

    if not full.exists():
        return "", False

    raw = full.read_text(encoding="utf-8")

    # Integrity check on full content (before any slice)
    if content_hash is not None:
        actual = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        if actual != content_hash:
            raise ValueError(
                f"content_hash mismatch for {path_str!r}: "
                f"expected {content_hash!r}, got {actual!r}"
            )

    # Line-based slice
    if offset is not None or limit is not None:
        lines = raw.splitlines(keepends=True)
        start = offset if offset is not None else 0
        end = (start + limit) if limit is not None else None
        sliced = lines[start:end]
        return "".join(sliced), True

    return raw, True
