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

Lifecycle policy (#385 β core impl sub-task 5, 2026-05-22 frozen
contract Phase 1 = "(a) Persistent until user delete"):

  - **No auto-GC.** Files written by ``save_*`` remain on disk until a
    user / operator deletes them out-of-band (= ``rm``, file explorer,
    cleanup script). The MediaStore class does NOT enforce TTL,
    max-N, session-end cleanup, or any other automatic eviction.
  - **Cross-turn / cross-session re-access supported.** A path-ref
    minted in user turn 1 remains valid in user turn 2 / next chat
    session / a forwarded A2A peer's expand — the file is still there.
    (See Q1 of the frozen contract: ``agent_id = agent name`` durable
    identity, not per-turn ``chain_id``.)
  - **Disk usage grows unboundedly.** Documented operational caveat —
    operators are expected to ``ls -la .reyn/tool-results/`` and clean
    up periodically. The browsable filename convention makes manual
    audit straightforward.
  - **Phase 2 reservation.** When measurement surfaces a real disk
    pressure or stale-handle problem (= not just hypothetical), Phase 2
    adds a config-driven policy: TTL / LRU / session-end / mixed. The
    reyn.yaml ``multimodal:`` block is the natural insertion point;
    no schema reservation made today (= YAGNI). The ``MediaStoreConfig``
    dataclass is the future extension surface.

Out of scope (= future work):
  - Phase 2 cleanup policy (= TTL / max-N / session boundary). Trigger
    is measurement evidence, not hypothesis.
  - Cross-host RPC dispatcher for ``resource_uri`` (= #385 β core
    impl sub-task 3, pending scheme arbitration).
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

    Phase 1 (= "(a) Persistent until user delete", #385 β sub-task 5):
    no cleanup-policy fields here today — the storage is intentionally
    unbounded, audit is via on-disk inspection. Phase 2 (= TTL / max-N /
    session-end / mixed) extends this dataclass when measurement
    surfaces a real disk-pressure or stale-handle problem. The
    extension surface is documented to keep the future addition out of
    the public-API surprise zone, but no fields are reserved today
    (= YAGNI; field shape is a Phase 2 design decision).
    """
    media_dir: str = ".reyn/media"
    tool_results_dir: str = ".reyn/tool-results"


# #385 β core impl sub-task 1: cross-host capable resource URI scheme.
# A path-ref minted with ``agent_name`` set carries this scheme so a
# downstream consumer (= another agent / a different host) can dispatch
# back to the producing agent's MediaStore. Sub-task 3 will wire the
# cross-host RPC; this sub-task lands the schema + same-host resolution.
_RESOURCE_URI_SCHEME = "reyn-tool-result://"


def parse_resource_uri(uri: str) -> tuple[str, str] | None:
    """Parse a ``reyn-tool-result://<agent>/<artifact>`` URI.

    Returns ``(agent, artifact)`` on a successful parse, or ``None`` when
    the input doesn't match the expected scheme / shape. The artifact
    portion may itself contain ``/`` (= not consumed by the split) so
    nested-path artifacts remain addressable; ``agent`` is always the
    single segment between the scheme and the first ``/``.
    """
    if not isinstance(uri, str) or not uri.startswith(_RESOURCE_URI_SCHEME):
        return None
    rest = uri[len(_RESOURCE_URI_SCHEME):]
    if "/" not in rest:
        return None
    agent, artifact = rest.split("/", 1)
    if not agent or not artifact:
        return None
    return agent, artifact


class MediaStore:
    """Path-ref'd file storage for multimodal media + tool result text.

    Each ``save_*`` call writes a file under the appropriate directory
    and returns a **path-ref block** suitable for placement in a
    ``ChatMessage.content`` list (= part of the OpenAI/Anthropic wire
    shape mirror; see issue #383). The corresponding ``read_*`` methods
    do the inverse lookup with workspace-boundary validation.

    Path-ref shape (#385 β core impl sub-task 1, 2026-05-22 frozen
    contract): when ``agent_name`` is supplied at construction, save_*
    returns the extended shape that carries cross-host routing fields::

        {
          "type": "tool_result_ref" | "image",
          "path": "<project-relative>",       # same-host fast-path
          "resource_uri": "reyn-tool-result://<agent_name>/<filename>",
          "source_agent": "<agent_name>",     # durable identity for dispatch
          "source_chain_id": "<chain_id>",    # audit annotation only (optional)
          "mime_type": "...",
          "content_hash": "sha256:...",
        }

    When ``agent_name`` is omitted (= legacy call sites, test stubs),
    save_* returns the pre-β shape (= no resource_uri / source_agent /
    source_chain_id, just ``path``). Consumers must treat the cross-host
    fields as optional: when present, the dispatcher CAN route across
    hosts; when absent, only the same-host ``path`` is available.

    Cross-host RPC routing (sub-task 3 of the β core impl) is NOT
    implemented in this sub-task — ``read_tool_result_by_uri`` raises
    ``ValueError`` when the URI's source_agent doesn't match this
    store's identity, with a clear "cross-host not yet supported"
    message so the read_tool_result handler can surface a stub error.
    """

    def __init__(
        self,
        config: MediaStoreConfig | None = None,
        *,
        project_root: Path,
        agent_name: str | None = None,
    ) -> None:
        self._config = config or MediaStoreConfig()
        self._project_root = project_root.resolve()
        self._media_dir = (
            self._project_root / self._config.media_dir
        ).resolve()
        self._tool_results_dir = (
            self._project_root / self._config.tool_results_dir
        ).resolve()
        self._agent_name = agent_name or None

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
        block: dict = {
            "type": "image",
            "path": str(path.relative_to(self._project_root)),
            "mime_type": mime_type,
            "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        }
        self._attach_cross_host_fields(block, filename=filename, chain_id=chain_id)
        return block

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
        block: dict = {
            "type": "tool_result_ref",
            "path": str(path.relative_to(self._project_root)),
            "mime_type": mime_type,
            "content_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        }
        self._attach_cross_host_fields(block, filename=filename, chain_id=chain_id)
        return block

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

    # ── Cross-host routing (#385 β core impl sub-task 1) ──────────────

    def _attach_cross_host_fields(
        self, block: dict, *, filename: str, chain_id: str,
    ) -> None:
        """Augment a path-ref block with resource_uri / source_agent /
        source_chain_id when this store has an ``agent_name`` identity.

        No-op when ``agent_name`` is unset — leaves the block in the
        pre-β shape so legacy call sites and test stubs keep working
        with their original expectations. The added fields are purely
        additive; the ``path`` fast-path stays usable for same-host
        consumers regardless.
        """
        if not self._agent_name:
            return
        block["resource_uri"] = f"{_RESOURCE_URI_SCHEME}{self._agent_name}/{filename}"
        block["source_agent"] = self._agent_name
        if chain_id:
            block["source_chain_id"] = chain_id

    def read_tool_result_by_uri(self, uri: str) -> tuple[str, bool]:
        """Resolve a ``reyn-tool-result://...`` URI and read the body.

        Same-host case (= the URI's source_agent matches this store's
        ``agent_name``): the artifact portion is interpreted as a filename
        inside ``tool_results_dir`` and read like ``read_tool_result``.

        Cross-host case (= source_agent differs): raises ``ValueError``
        with a "cross-host not yet supported" message. The actual RPC
        routing lands in sub-task 3 of the #385 β core impl; this
        sub-task's contract is the schema + same-host resolution + a
        clear stub error for the dispatcher to surface.

        Malformed URI: raises ``ValueError`` with the offending input.
        Missing file: returns ``("", False)`` matching
        ``read_tool_result``'s past-EOF / deleted-file convention.
        """
        parsed = parse_resource_uri(uri)
        if parsed is None:
            raise ValueError(
                f"invalid resource_uri (expected "
                f"{_RESOURCE_URI_SCHEME}<agent>/<artifact>): {uri!r}"
            )
        agent, artifact = parsed
        if not self._agent_name:
            raise ValueError(
                "this MediaStore has no agent_name configured; "
                "cannot resolve cross-host resource URIs"
            )
        if agent != self._agent_name:
            raise ValueError(
                f"cross-host resource_uri (source_agent={agent!r}) is not "
                "yet supported in this build (= sub-task 3 of #385 β core "
                f"impl). This store's identity is {self._agent_name!r}."
            )
        # Same-host: the artifact is a filename inside tool_results_dir.
        # Delegate to read_tool_result with the project-relative path so
        # workspace-boundary validation runs through the existing path.
        try:
            rel_path = str(
                (self._tool_results_dir / artifact).relative_to(
                    self._project_root,
                ),
            )
        except ValueError as exc:
            raise ValueError(
                f"artifact {artifact!r} resolves outside tool_results_dir"
            ) from exc
        return self.read_tool_result(rel_path)

    @property
    def agent_name(self) -> str | None:
        """Agent identity bound to this store (= source_agent for path-refs).

        ``None`` when the store was constructed without an identity (=
        legacy / test stubs). Consumers that need to render cross-host
        capable path-refs MUST construct the store with this set.
        """
        return self._agent_name

    # ── Introspection ─────────────────────────────────────────────────

    @property
    def media_dir(self) -> Path:
        """Absolute path of the image storage directory."""
        return self._media_dir

    @property
    def tool_results_dir(self) -> Path:
        """Absolute path of the tool result text storage directory."""
        return self._tool_results_dir
