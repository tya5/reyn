"""Canonical tool-result shape — the #2425 案B spec fix (guessing-free offload).

The spec hole (owner): tool results are heterogeneous per tool, so the offload layer GUESSED which
field is the LLM body (per-op ``_offload_payload_field`` marker + ``decide_payload_field``'s
sole-oversized rule) and broke whenever a result had a second large field (owner's chat-MCP
whole-envelope: a large ``structuredContent`` alongside ``content``).

案B normalizes every tool result at the boundary into ONE canonical shape the offloader never has to
interpret:

- ``text``        — the single canonical LLM-readable body (the ONLY thing the offloader truncates).
- ``attachments`` — typed non-text kept OUT of the offload decision so a large one never competes with
                    ``text``: ``{"kind": "media"|"structured", ...}``. A large ``structured`` is
                    separately referenced (not dropped, not mixed into ``text``, retrievable by ref).
- ``source_ref``  — the re-fetch origin for an on-disk body (``{"path": …, "offset": …}``); ``None``
                    for a transient body (MCP/web/exec) — which must therefore be offload-stored (the
                    tool cannot be meaningfully re-run for more).
- ``meta``        — small structured signal the LLM reads inline as YAML frontmatter (``returncode``,
                    ``truncated``, …). High-signal-only: transport identifiers (``kind``, duplicate
                    ``status``, ``server``, ``tool`` echo) are dropped — they never change what the
                    LLM does next. ``isError`` is retained as the sole error-path driver.

FP-0056 PR-F1 — coverage enforcement by construction (this module's endgame). The pre-F1 design had
two structural defects the 2026-07-09 dogfood incident exposed (a ``reyn_source__read`` doc read
offloaded as a whole-dict ``structured`` blob instead of the readable body):

1. **Free-floating ``_MAPPERS`` dict, hand-synced with the op/tool registries.** Nothing forced
   "registered a producer" to imply "declared its LLM-visible shape", so ``file`` / ``reyn_src`` /
   admin ops silently took the fallback. **Fix:** the canonical declaration is now *born at the
   registration seam* — an op kind declares it through ``op_runtime.register(kind, handler,
   canonical=…)``; a router ``ToolDefinition`` declares it through its required ``canonical`` field.
   ``_MAPPERS`` is gone; declarations live in :data:`_DECLARATIONS`, populated from both seams.
2. **Dispatch sniffed ``result["kind"]`` — data a producer may not even set (``reyn_src`` had none).**
   **Fix:** :func:`to_canonical` dispatches on the *invoked identity* (``source=`` — the tool/op the
   chokepoint called), NOT the result dict. ``result["kind"]`` stops being load-bearing for
   canonicalization; it stays ordinary result data. This fixes the kind-less-handler class outright.

A registry-derived CI gate (``tests/test_fp0056_canonical_coverage_gate.py``) walks every registered
op kind + every ToolDefinition and asserts each carries a declaration — catching design-level
omissions a hand-written table misses (it WOULD have caught the ``file`` gap).

A declaration is a **mapper** (``result -> CanonicalToolResult``), the explicit named opt-in
:data:`STRUCTURED_PASSTHROUGH` (the whole dict legitimately IS the LLM view — admin/install ops, owner
decision #1), or :data:`CANONICAL_TODO` (declared but a real mapper is not yet written — a provisional
whole-dict fallback, ratcheted so it can't become a permanent escape hatch; burn-down in issue #2681).
A genuinely unregistered ``source`` (dynamic/edge, or ``None``) keeps the lossless whole-dict fallback;
PR-F2 adds the ``canonical_fallback_used`` visibility event (degrade-with-audit) on the TODO + unknown
paths.

P7: the source → canonical mapping is OS-level (no domain-specific vocabulary). The deref / paging /
offload store machinery is reused unchanged — 案B removes only the field-guessing.
"""
from __future__ import annotations

from typing import Any, Callable, TypedDict


class CanonicalToolResult(TypedDict, total=False):
    """The single shape all tool results are normalized to before offload (see module docstring)."""

    text: str
    attachments: list[dict]
    source_ref: "dict | None"
    meta: dict


# A canonical mapper: an invoked producer's raw result dict → the canonical shape.
CanonicalMapper = Callable[[dict], CanonicalToolResult]


class _StructuredPassthrough:
    """The sentinel type of :data:`STRUCTURED_PASSTHROUGH` (single instance)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "STRUCTURED_PASSTHROUGH"


# STRUCTURED_PASSTHROUGH — the explicit, greppable, reviewable opt-in a producer declares when its
# whole result dict legitimately IS the right LLM view (no single text body to surface). Behaves
# identically to the lossless whole-dict fallback, but is a *declared* choice, not a silent one — the
# framework's whole point (the file/reyn_src incident was a silent fallback, not a reviewed decision).
STRUCTURED_PASSTHROUGH = _StructuredPassthrough()


class _CanonicalTodo:
    """The sentinel type of :data:`CANONICAL_TODO` (single instance)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "CANONICAL_TODO"


# CANONICAL_TODO — a producer's canonical shape IS declared (gate-satisfying — NOT a silent gap), but
# a real mapper is not yet written; it takes a provisional whole-dict fallback. DISTINCT from
# ``STRUCTURED_PASSTHROUGH`` (whose whole-dict output is the reviewed, legitimate LLM view for the
# admin/install ops of owner decision #1): a ``CANONICAL_TODO`` producer may well have a text body a
# future mapper should surface — the marker records the debt so a reader (and PR-F2's
# ``canonical_fallback_used`` event) can tell "reviewed-legitimate" from "todo". Ratcheted: the gate
# grandfathers ONLY the existing producers relabeled at F1 migration; a NEW producer may not adopt it
# (real mapper or STRUCTURED_PASSTHROUGH only). Greppable; burn-down tracked in issue #2681.
CANONICAL_TODO = _CanonicalTodo()


class _Undeclared:
    """The sentinel type of :data:`UNDECLARED` (single instance)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNDECLARED"


# UNDECLARED — the default ``ToolDefinition.canonical`` value. A producer left UNDECLARED has made no
# canonical choice; the coverage gate rejects it (red CI naming the tool). It exists so a missing
# declaration is a loud gate failure, not a silent fallback.
UNDECLARED = _Undeclared()


# A canonical declaration: a mapper, the reviewed passthrough opt-in, or the provisional TODO marker.
CanonicalDeclaration = "CanonicalMapper | _StructuredPassthrough | _CanonicalTodo"


# source-identity (op kind / tool name) → declaration. Populated at BOTH registration seams:
# ``op_runtime.register(kind, handler, canonical=…)`` for op kinds, and ``ToolRegistry.register`` for
# router ToolDefinitions (via their ``canonical`` field). This is the migrated ``_MAPPERS`` — no
# longer a free-floating hand-synced dict, but derived from the registrations themselves.
_DECLARATIONS: "dict[str, CanonicalMapper | _StructuredPassthrough | _CanonicalTodo]" = {}


def declare_canonical(
    source_id: str, declaration: "CanonicalMapper | _StructuredPassthrough | _CanonicalTodo"
) -> None:
    """Register ``source_id``'s canonical declaration (a mapper, ``STRUCTURED_PASSTHROUGH``, or
    ``CANONICAL_TODO``).

    Called from the two registration seams. Idempotent for an identical re-declaration (registries
    are rebuilt per ``get_default_registry()`` call); a *conflicting* re-declaration raises, since two
    different shapes for one invoked identity is a registration bug, not a legitimate override."""
    if declaration is UNDECLARED:
        raise ValueError(
            f"canonical declaration for {source_id!r} is UNDECLARED — every registered producer must "
            f"declare a mapper, STRUCTURED_PASSTHROUGH, or CANONICAL_TODO (FP-0056 PR-F1)"
        )
    existing = _DECLARATIONS.get(source_id)
    if existing is not None and existing is not declaration:
        raise ValueError(
            f"conflicting canonical declaration for {source_id!r}: {existing!r} vs {declaration!r}"
        )
    _DECLARATIONS[source_id] = declaration


def canonical_declaration(
    source_id: "str | None",
) -> "CanonicalMapper | _StructuredPassthrough | _CanonicalTodo | None":
    """Return the declared canonical mapping for ``source_id`` (op kind / tool name), or ``None`` when
    the identity was never registered (a genuine unknown → the visible fallback in
    :func:`to_canonical`)."""
    if not isinstance(source_id, str):
        return None
    return _DECLARATIONS.get(source_id)


def _is_error(result: dict) -> bool:
    """A result is an error when it explicitly flags one (MCP ``isError``) or its op-level
    ``status`` is ``error`` (the sole error-path driver kept after meta-tightening)."""
    return bool(result.get("isError")) or result.get("status") == "error"


def mcp_to_canonical(result: dict) -> CanonicalToolResult:
    """MCP result → canonical. ``content`` (joined text) → ``text``; ``structured``
    (structuredContent) + ``media_blocks`` → typed ``attachments`` (a large ``structured`` is
    separately referenced, never competing with ``text`` in the offload decision — the owner
    whole-envelope root). ``meta`` is tightened to signal-only: the transport echo
    (``kind``/``status``/``server``/``tool``) is dropped; only ``isError`` survives, as the error-path
    driver. source_ref is None (transient: an MCP call can't be meaningfully re-run for more, so its
    body is offload-stored)."""
    attachments: list[dict] = []
    structured = result.get("structured")
    if structured is not None:
        attachments.append({"kind": "structured", "data": structured})
    for block in result.get("media_blocks") or []:
        attachments.append({"kind": "media", "block": block})
    meta = {"isError": True} if _is_error(result) else {}
    return CanonicalToolResult(
        text=result.get("content", "") or "",
        attachments=attachments,
        source_ref=None,
        meta=meta,
    )


def mcp_read_resource_to_canonical(result: dict) -> CanonicalToolResult:
    """MCP resource-read result → canonical (#2597 slice ②a). ``contents`` is a list of flattened
    ``TextResourceContents``/``BlobResourceContents``: a ``text`` entry joins into the canonical
    ``text``; a ``blob`` entry (base64, arbitrary mimeType) becomes a ``structured`` attachment rather
    than a ``media`` one (the existing ``media`` path assumes vision-follow-up image blocks). A large
    blob is size-gated to its own ref so it never competes with ``text``. ``meta`` is signal-only
    (``isError`` when error)."""
    attachments: list[dict] = []
    text_parts: list[str] = []
    for item in result.get("contents") or []:
        if not isinstance(item, dict):
            continue
        if "text" in item and item["text"] is not None:
            text_parts.append(str(item["text"]))
        elif "blob" in item:
            attachments.append({"kind": "structured", "data": item})
    meta = {"isError": True} if _is_error(result) else {}
    return CanonicalToolResult(
        text="\n".join(text_parts),
        attachments=attachments,
        source_ref=None,
        meta=meta,
    )


def mcp_get_prompt_to_canonical(result: dict) -> CanonicalToolResult:
    """MCP get-prompt result → canonical (#2597 slice ②c). ``messages`` is a list of flattened
    ``PromptMessage`` dicts; each message's text content joins into the canonical ``text``; a non-text
    content block (image/audio/embedded-resource) becomes a ``structured`` attachment (kept out of the
    text-offload decision, same as its two siblings). ``meta`` is signal-only (``isError`` when
    error)."""
    attachments: list[dict] = []
    text_parts: list[str] = []
    for message in result.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, dict) and content.get("type") == "text" and content.get("text") is not None:
            text_parts.append(str(content["text"]))
        elif content is not None:
            attachments.append({"kind": "structured", "data": content})
    meta = {"isError": True} if _is_error(result) else {}
    return CanonicalToolResult(
        text="\n".join(text_parts),
        attachments=attachments,
        source_ref=None,
        meta=meta,
    )


def web_fetch_to_canonical(result: dict) -> CanonicalToolResult:
    """web_fetch result → canonical. The fetched page text (``content``, or the op's own ``preview``
    when it pre-offloaded to a ``path_ref``) → ``text``. Signal meta: ``truncated`` + ``next_start``
    (the LLM's pagination handle) when the fetch was cut. Transport (url/status/content_type/extractor)
    is dropped."""
    content = result.get("content")
    text = content if content else str(result.get("preview") or "")
    meta: dict[str, Any] = {}
    if result.get("truncated"):
        meta["truncated"] = True
        next_start = result.get("next_start")
        if next_start is not None:
            meta["next_start"] = next_start
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta=meta)


def web_search_to_canonical(result: dict) -> CanonicalToolResult:
    """web_search result → canonical. The ``results`` list → a ``structured`` attachment (rendered as
    frontmatter YAML, or offloaded to its own ref when large). An op-level failure (no ``results``,
    ``status: error``) surfaces its message as ``text``. Transport (query/backend) is dropped."""
    results = result.get("results")
    if results is not None:
        return CanonicalToolResult(
            text="", attachments=[{"kind": "structured", "data": results}], source_ref=None, meta={},
        )
    text = str(result.get("error") or "") if result.get("status") == "error" else ""
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta={})


def sandboxed_exec_to_canonical(result: dict) -> CanonicalToolResult:
    """sandboxed_exec result → canonical. ``stdout`` (+ ``stderr`` when present) → ``text``; a NONZERO
    ``returncode`` → signal meta (it changes what the LLM does next — a zero code is not signal).
    Transport (backend/truncated) is dropped."""
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if stderr:
        text = f"{stdout}\n{stderr}" if stdout else stderr
    else:
        text = stdout
    meta: dict[str, Any] = {}
    returncode = result.get("returncode")
    if returncode:  # nonzero (or truthy) only — a 0 exit is not actionable signal
        meta["returncode"] = returncode
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta=meta)


def chunks_to_canonical(result: dict) -> CanonicalToolResult:
    """recall / index_query result → canonical. The retrieved ``chunks`` list → a ``structured``
    attachment (frontmatter YAML, or its own ref when large). There is no text body. Transport
    (``mode``) is dropped."""
    chunks = result.get("chunks")
    attachments = [{"kind": "structured", "data": chunks}] if chunks is not None else []
    return CanonicalToolResult(text="", attachments=attachments, source_ref=None, meta={})


def run_pipeline_to_canonical(result: dict) -> CanonicalToolResult:
    """Sync run_pipeline result → canonical. The final ``output`` is the whole thing the calling LLM
    wants: a str output → ``text``; a non-str output → a ``structured`` attachment. ``run_id`` and
    ``named_stores`` are correlation/transport plumbing the caller never acts on → dropped (owner
    ruling)."""
    output = result.get("output")
    if isinstance(output, str):
        return CanonicalToolResult(text=output, attachments=[], source_ref=None, meta={})
    attachments = [{"kind": "structured", "data": output}] if output is not None else []
    return CanonicalToolResult(text="", attachments=attachments, source_ref=None, meta={})


def run_pipeline_async_to_canonical(result: dict) -> CanonicalToolResult:
    """Async run_pipeline result → canonical. Unlike the sync case, ``run_id`` is KEPT — it is the
    correlation handle the caller uses to match the later ``[pipeline]`` completion message."""
    run_id = result.get("run_id")
    text = (
        f"Pipeline started (run_id: {run_id}). "
        f"Result will arrive as a [pipeline] message."
    )
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta={})


def _file_signal_meta(result: dict) -> "dict[str, Any]":
    """High-signal meta for a ``file`` op result: ``op`` + ``status`` (a ``truncated`` read tells the
    LLM there is more; a ``not_found`` tells it to retry another path) + ``path`` (which file). Transport
    noise beyond these is dropped, per the module's high-signal-only rule. ``isError`` is added on the
    error path by the caller."""
    meta: dict[str, Any] = {}
    for key in ("op", "status", "path"):
        value = result.get(key)
        if value is not None:
            meta[key] = value
    return meta


def file_to_canonical(result: dict) -> CanonicalToolResult:
    """``file`` op result (read/write/glob/grep/edit/delete/mkdir/move/stat/regenerate_index) → canonical.

    Dispatch is on the result's ``op`` field (NOT ``kind``, which is the coarse ``"file"`` for the whole
    family). The LLM-readable body per op:

    - ``read`` → the file ``content`` as ``text`` (an image read surfaces its ``media_blocks`` as media
      attachments, matching the MCP mapper); ``path``/``op``/``status`` are signal meta, never the body.
    - ``grep`` / ``glob`` → the rendered match / path lines as ``text`` (``content`` mode → ``path:line:
      text``; ``files_with_matches`` / glob → the paths; ``count`` mode → a one-line total).
    - ``write`` / ``edit`` / ``delete`` / ``mkdir`` / ``move`` / ``stat`` / ``regenerate_index`` → a short
      status ``text``.

    Any op whose result is an error (``status`` error/denied/not_found, or an ``error`` field) surfaces
    the ``error`` message as ``text`` with ``meta.isError`` — the sole error-path driver."""
    op = result.get("op")
    status = result.get("status")
    meta = _file_signal_meta(result)
    if _is_error(result) or status in ("error", "denied", "not_found") or (
        "error" in result and result.get("error")
    ):
        meta["isError"] = True
        return CanonicalToolResult(
            text=str(result.get("error") or ""), attachments=[], source_ref=None, meta=meta,
        )

    if op == "read":
        attachments = [
            {"kind": "media", "block": block} for block in (result.get("media_blocks") or [])
        ]
        return CanonicalToolResult(
            text=result.get("content", "") or "", attachments=attachments, source_ref=None, meta=meta,
        )

    if op == "grep":
        return CanonicalToolResult(
            text=_render_file_grep(result), attachments=[], source_ref=None, meta=meta,
        )

    if op == "glob":
        matches = result.get("matches") or []
        text = "\n".join(str(m) for m in matches) if matches else "(no matches)"
        return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta=meta)

    # write / edit / delete / mkdir / move / stat / regenerate_index → a short status text.
    return CanonicalToolResult(
        text=_render_file_status(op, result), attachments=[], source_ref=None, meta=meta,
    )


def _render_file_grep(result: dict) -> str:
    """Render a ``file`` grep result's matches as text lines. ``content`` mode → ``path:line: text``;
    ``files_with_matches`` → one path per line; ``count`` mode → a one-line total."""
    output_mode = result.get("output_mode")
    if output_mode == "count":
        return f"{result.get('count', 0)} match(es)"
    if output_mode == "files_with_matches":
        files = result.get("files") or []
        return "\n".join(str(f) for f in files) if files else "(no matches)"
    matches = result.get("matches") or []
    if not matches:
        return "(no matches)"
    lines = [
        f"{m.get('path', '')}:{m.get('line_number', '')}: {m.get('content', '')}" for m in matches
    ]
    return "\n".join(lines)


def _render_file_status(op: "str | None", result: dict) -> str:
    """A short, human-readable status line for a mutating / metadata ``file`` op (write/edit/delete/
    mkdir/move/stat/regenerate_index). Descriptive, not JSON — the LLM acts on the outcome, not the
    envelope."""
    path = result.get("path", "")
    if op == "write":
        text = f"Wrote {result.get('bytes_written', 0)} bytes to {path}."
        note = result.get("encoding_note")
        return f"{text} {note}" if note else text
    if op == "edit":
        text = f"Edited {path}: {result.get('replacements', 0)} replacement(s)."
        preview = result.get("preview")
        return f"{text}\n{preview}" if preview else text
    if op == "delete":
        return f"Deleted {path} (deleted={result.get('deleted')})."
    if op == "mkdir":
        return f"Created directory {path} (created={result.get('created')})."
    if op == "move":
        return f"Moved {path} -> {result.get('dest_path', '')}."
    if op == "regenerate_index":
        return f"Regenerated index at {result.get('output_path', '')}: {result.get('entries', 0)} entries."
    if op == "stat":
        return f"stat {path}: {result.get('info')}"
    return f"{op}: {result.get('status', 'ok')}"


def reyn_src_to_canonical(result: dict) -> CanonicalToolResult:
    """``reyn_src_*`` handler result (read/list/glob/grep) → canonical. These handlers return a
    kind-less ``{path, content}`` / ``{entries}`` / ``{matches}`` dict — the dogfood incident root: a
    doc read via ``reyn_source__read`` was offloaded as a whole-dict ``structured`` blob instead of the
    readable body. Under PR-F1 the ``reyn_src_*`` ToolDefinitions *declare* this mapper (identity
    dispatch), so the result no longer needs a ``kind`` field to route here.

    - ``read`` (``content``) → the file body as ``text`` (``path`` is signal meta).
    - ``list`` (``entries``) → ``type: name`` lines as ``text``.
    - ``glob`` (``matches`` of paths) / ``grep`` (``matches`` of ``{path, line, snippet}``) → the
      rendered lines as ``text``.
    - an ``error`` → the message as ``text`` with ``meta.isError``."""
    if result.get("error"):
        return CanonicalToolResult(
            text=str(result["error"]), attachments=[], source_ref=None, meta={"isError": True},
        )
    if "content" in result:
        meta = {"path": result["path"]} if result.get("path") is not None else {}
        return CanonicalToolResult(
            text=result.get("content", "") or "", attachments=[], source_ref=None, meta=meta,
        )
    if "entries" in result:
        entries = result.get("entries") or []
        text = "\n".join(f"{e.get('type', '')}: {e.get('name', '')}" for e in entries) or "(empty)"
        return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta={})
    if "matches" in result:
        matches = result.get("matches") or []
        if matches and isinstance(matches[0], dict):
            # grep: {path, line, snippet}
            text = "\n".join(
                f"{m.get('path', '')}:{m.get('line', '')}: {m.get('snippet', '')}" for m in matches
            )
        else:
            # glob: a list of repo-relative path strings.
            text = "\n".join(str(m) for m in matches)
        return CanonicalToolResult(
            text=text or "(no matches)", attachments=[], source_ref=None, meta={},
        )
    # A reyn_src shape with none of the known bodies — keep the whole dict lossless as structured.
    return CanonicalToolResult(
        text="", attachments=[{"kind": "structured", "data": result}], source_ref=None, meta={},
    )


def render_template_to_canonical(result: dict) -> CanonicalToolResult:
    """``render_template`` op result → canonical (FP-0055 PR-2). The rendered string
    (``rendered``) IS the LLM-readable body → ``text`` (NOT a whole-dict ``structured``
    blob). Signal meta: ``truncated`` (+ which bound fired, ``truncate_reason``) tells the LLM the
    output was capped mid-generate; ``undefined_vars`` (lenient mode) names the
    referenced-but-unbound template variables so it can self-correct. An error
    (``status="error"`` — syntax / SSTI-blocked / strict-undefined) surfaces the
    message as ``text`` with ``meta.isError``."""
    if _is_error(result):
        return CanonicalToolResult(
            text=str(result.get("error") or ""), attachments=[], source_ref=None, meta={"isError": True},
        )
    meta: dict[str, Any] = {}
    if result.get("truncated"):
        meta["truncated"] = True
        reason = result.get("truncate_reason")
        if reason:
            meta["truncate_reason"] = reason
    undefined_vars = result.get("undefined_vars")
    if undefined_vars:
        meta["undefined_vars"] = undefined_vars
    return CanonicalToolResult(
        text=result.get("rendered", "") or "", attachments=[], source_ref=None, meta=meta,
    )


def compact_to_canonical(result: dict) -> CanonicalToolResult:
    """``compact`` op result → canonical. On success the freed-token / free-window metrics (+ the chat-
    axis compression fields when present) render as a short ``text`` summary; on error the ``error``
    message surfaces as ``text`` with ``meta.isError``. Result shape:
    ``{kind:"compact", status:"ok", freed_tokens?, free_window_after?, summarized_turns?,
    compressed_tokens?, bridge_tokens?}`` (ok) or ``{status:"error", error_kind, error}`` (error)."""
    if _is_error(result):
        return CanonicalToolResult(
            text=str(result.get("error") or ""), attachments=[], source_ref=None, meta={"isError": True},
        )
    parts: list[str] = ["Compaction complete."]
    for label, key in (
        ("freed_tokens", "freed_tokens"),
        ("free_window_after", "free_window_after"),
        ("summarized_turns", "summarized_turns"),
        ("compressed_tokens", "compressed_tokens"),
        ("bridge_tokens", "bridge_tokens"),
    ):
        value = result.get(key)
        if value is not None:
            parts.append(f"{label}={value}")
    return CanonicalToolResult(text=" ".join(parts), attachments=[], source_ref=None, meta={})


def judge_output_to_canonical(result: dict) -> CanonicalToolResult:
    """``judge_output`` op result → canonical. The scorer's ``reason`` (its LLM-readable explanation) is
    the ``text``; ``score`` / ``passed`` / ``threshold`` / ``on_fail`` are signal meta (they drive the
    caller's next move — a failed judgment triggers ``on_fail``). An error surfaces the message as
    ``text`` with ``meta.isError``. Shape: ``{kind:"judge_output", score, passed, reason, threshold,
    on_fail}`` (ok) or ``{status:"error", error}`` (error)."""
    if _is_error(result) or (result.get("status") == "error"):
        return CanonicalToolResult(
            text=str(result.get("error") or ""), attachments=[], source_ref=None, meta={"isError": True},
        )
    meta: dict[str, Any] = {}
    for key in ("score", "passed", "threshold", "on_fail"):
        value = result.get(key)
        if value is not None:
            meta[key] = value
    return CanonicalToolResult(
        text=str(result.get("reason", "") or ""), attachments=[], source_ref=None, meta=meta,
    )


def memory_body_to_canonical(result: dict) -> CanonicalToolResult:
    """``read_memory_body`` result → canonical (FP-0056 PR-F1 triage: text-shaped). The memory entry's
    body (``content``, frontmatter already stripped by the handler) IS the LLM-readable text → ``text``
    — NOT a whole-dict blob. This is the same file-class the incident exposed, and it has its own
    documented G12 empty-stop attractor (an LLM handed non-clean memory text stopped with an empty
    reply — router_loop._read_memory_body). ``layer`` / ``slug`` are signal meta (which entry). An
    error (``error`` field) surfaces the message as ``text`` with ``meta.isError``. Shape:
    ``{content, layer?, slug?}`` (ok) or ``{error, layer?, slug?}`` (error)."""
    if result.get("error"):
        return CanonicalToolResult(
            text=str(result["error"]), attachments=[], source_ref=None, meta={"isError": True},
        )
    meta: dict[str, Any] = {}
    for key in ("layer", "slug"):
        value = result.get(key)
        if value is not None:
            meta[key] = value
    return CanonicalToolResult(
        text=result.get("content", "") or "", attachments=[], source_ref=None, meta=meta,
    )


def ask_user_to_canonical(result: dict) -> CanonicalToolResult:
    """``ask_user`` op result → canonical (FP-0056 PR-F1 triage: text-shaped). The user's ``answer``
    (free text or the chosen option) IS what the LLM acts on → ``text`` — not a whole-dict blob hiding
    it behind ``kind``/``question``/``status`` transport. Shape:
    ``{kind:"ask_user", question, answer, status:"ok"}``."""
    return CanonicalToolResult(
        text=str(result.get("answer", "") or ""), attachments=[], source_ref=None, meta={},
    )


def _fallback_structured(result: dict) -> CanonicalToolResult:
    """The lossless whole-dict fallback: the entire result becomes a ``structured`` attachment
    (readable as frontmatter YAML, ``ctx.<name>.structured.<field>`` still programmatically reachable),
    ``text`` empty. Used for a declared ``STRUCTURED_PASSTHROUGH`` producer, a provisional
    ``CANONICAL_TODO`` producer, AND a genuinely unregistered ``source`` (dynamic/edge). PR-F2 adds a
    ``canonical_fallback_used`` event on the ``CANONICAL_TODO`` + unregistered paths (degrade-with-
    audit) — but NOT on ``STRUCTURED_PASSTHROUGH`` (a reviewed, legitimate whole-dict view)."""
    return CanonicalToolResult(
        text="", attachments=[{"kind": "structured", "data": result}], source_ref=None, meta={},
    )


def to_canonical(result: dict, *, source: "str | None" = None) -> CanonicalToolResult:
    """Normalize an op/tool result dict to :class:`CanonicalToolResult`, dispatching on the **invoked
    identity** ``source`` (the op kind / tool name the chokepoint called — FP-0056 PR-F1), NOT on
    ``result["kind"]`` (which a producer may not set — the ``reyn_src`` incident class).

    - ``source`` declared with a mapper → the mapper shapes the result.
    - ``source`` declared ``STRUCTURED_PASSTHROUGH`` (reviewed) or ``CANONICAL_TODO`` (provisional,
      pending a real mapper) → the whole dict is a ``structured`` attachment.
    - ``source`` ``None`` or unregistered (genuine unknown) → the same lossless whole-dict fallback
      (PR-F2 will emit ``canonical_fallback_used`` on the TODO + unknown paths). Nothing is ever lost."""
    declaration = canonical_declaration(source)
    if declaration is None or declaration is STRUCTURED_PASSTHROUGH or declaration is CANONICAL_TODO:
        return _fallback_structured(result)
    return declaration(result)


# The audit-event kind the two live ``to_canonical`` callers emit when a result took a VISIBLE
# fallback path — the observability half of FP-0056 (the static coverage gate is PR-F1; this makes
# the runtime debt + genuine-unknown fallbacks visible instead of silent). It is an audit / P6 event,
# NOT a WAL / recovery-core event.
CANONICAL_FALLBACK_EVENT = "canonical_fallback_used"


def canonical_fallback_reason(
    source: "str | None", *, structured_offloaded: bool = False
) -> "str | None":
    """Return the audit reason a :data:`CANONICAL_FALLBACK_EVENT` should carry for ``source``'s
    canonicalization, or ``None`` when nothing should fire (FP-0056 PR-F2 — the visibility half).

    A short category string is returned on each of the three fail-visible paths (owner decisions #2/#3
    — degrade-with-audit, never silently):

    - ``source`` unregistered / ``None`` (a genuine unknown the registries can't enumerate → the
      lossless whole-dict fallback) → ``"unregistered"``.
    - ``source`` declared :data:`CANONICAL_TODO` (gate-satisfying debt, no real mapper yet → the same
      whole-dict fallback) → ``"canonical_todo"``. This is the #2681 burn-down debt made runtime-visible.
    - ``source`` declared :data:`STRUCTURED_PASSTHROUGH` whose whole-dict serialization exceeded the
      structured offload gate (caller passes ``structured_offloaded=True``) → ``"passthrough_oversized"``
      (owner decision #2: an oversized passthrough blob signals passthrough was the wrong choice for
      this producer — make it visible). A SMALL (inline) passthrough is a reviewed, legitimate view →
      ``None`` (no event).

    A real mapper always returns ``None`` — a mapped producer never took a fallback. Only a reason
    CATEGORY is returned; NO result content is ever returned or logged (audit signal, not data — the
    callers emit the ``source`` id + this reason, never the result body)."""
    declaration = canonical_declaration(source)
    if declaration is None:
        return "unregistered"
    if declaration is CANONICAL_TODO:
        return "canonical_todo"
    if declaration is STRUCTURED_PASSTHROUGH and structured_offloaded:
        return "passthrough_oversized"
    return None


_CANONICAL_SOURCE_KEY = "_canonical_source"


def extract_canonical_source(result: Any) -> "tuple[str | None, Any]":
    """Split the invoked-identity tag off a (possibly envelope-wrapped) result: return
    ``(source, cleaned)`` where ``source`` is the DEEPEST ``_canonical_source`` in the envelope chain
    and ``cleaned`` is the result with every such tag removed (FP-0056 PR-F1).

    Why deepest-wins: the dispatch layer wraps a handler's return in ``{status, data: <return>}``
    (``dispatch_tool``), so a WRAPPER handler that resolved the true target (``invoke_action`` /
    pipeline tool dispatch) tags the INNER ``data`` with the resolved tool name, while the outer
    dispatch loop tags the envelope with the wrapper's own name. The inner (resolved) identity is the
    correct canonicalization source, so descending into ``data`` overrides the shallower tag. A direct
    (unwrapped) call has only the outer tag — which is then the correct identity."""
    if not isinstance(result, dict):
        return None, result
    source: "str | None" = None

    def _walk(d: dict) -> dict:
        nonlocal source
        cleaned: dict[str, Any] = {}
        for key, value in d.items():
            if key == _CANONICAL_SOURCE_KEY:
                source = value
                continue
            cleaned[key] = value
        inner = cleaned.get("data")
        if isinstance(inner, dict):
            cleaned["data"] = _walk(inner)
        return cleaned

    cleaned = _walk(result)
    return source, cleaned


def unwrap_dispatch_envelope(result: Any) -> Any:
    """Peel any ``{"status": ..., "data": {...}}`` dispatch envelope(s) off a raw tool-dispatch
    result, stopping at the first dict that already carries a ``kind`` (the shape :func:`to_canonical`
    dispatches on). A tool-registry handler's own return value can itself be an envelope (e.g.
    ``run_pipeline``), so more than one layer may need peeling — hence the loop, not a single unwrap."""
    inner = result
    while (
        isinstance(inner, dict)
        and isinstance(inner.get("data"), dict)
        and set(inner) <= {"status", "data", "error"}
        and "kind" not in inner
    ):
        inner = inner["data"]
    return inner


def canonical_to_ctx_fields(canonical: CanonicalToolResult) -> "dict[str, Any]":
    """Reduce a :class:`CanonicalToolResult` to the flat ``{"text": ..., "structured": ...}`` shape a
    pipeline step's ``ctx.<name>`` exposes (``structured`` key absent when there is no structured
    attachment) — shape-only, mirroring ``seam.py``'s attachments reduction but with NO size gating:
    pipeline ctx retains full values for downstream programmatic step processing (owner ruling)."""
    fields: dict[str, Any] = {"text": canonical.get("text", "")}
    structured_items = [
        att.get("data") for att in canonical.get("attachments", []) or [] if att.get("kind") == "structured"
    ]
    if structured_items:
        fields["structured"] = structured_items[0] if len(structured_items) == 1 else structured_items
    return fields
