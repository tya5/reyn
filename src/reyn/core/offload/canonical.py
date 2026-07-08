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

Every op kind that once declared ``_offload_payload_field`` now has a mapper here (案B endgame, not a
partial migration): MCP + web fetch/search + sandboxed exec + recall/index_query + run_pipeline(_async).
An unregistered kind falls back to a whole-dict ``structured`` attachment (never lost, and
``ctx.<name>.structured.<field>`` still gives programmatic access). The chat offload path no longer
guesses a payload field: the six per-op ``_offload_payload_field`` markers and the chat use of
``decide_payload_field`` / the sole-oversized condition are gone — ``text`` is the payload by
construction. (``decide_payload_field`` / ``_oversized_fields`` themselves remain only for the now-dead
control_ir/phase offloader, pending its separate removal.)

P7: the op-kind → canonical mapping is OS-level (no domain-specific vocabulary). The deref / paging / offload
store machinery is reused unchanged — 案B removes only the field-guessing.

FP-0056 PR-H (hotfix): the original 案B mapper table scoped migration to the ops that once declared
``_offload_payload_field`` (+ mcp/pipeline) — so the ``file`` family (``kind:"file"``, unmapped), the
``reyn_src_*`` dev-reads (kind-less), and ``compact`` / ``judge_output`` all took the whole-dict fallback.
In dogfood a doc read via ``reyn_source__read`` was therefore offloaded as a 600-char JSON-dict preview
instead of the readable text body. This module now registers a ``file`` mapper (op-dispatched: read →
``content`` as ``text``, grep/glob → rendered lines, mutations → a status line), a ``reyn_src`` mapper
(the tool-handler seam tags its kind-less result ``kind:"reyn_src"`` so it routes here, not to the
fallback), and ``compact`` / ``judge_output`` mappers. The registry-enforcement framework, identity-keyed
dispatch, and the ``canonical_fallback_used`` event are follow-ups (PR-F1/PR-F2), not this hotfix.
"""
from __future__ import annotations

from typing import Any, TypedDict


class CanonicalToolResult(TypedDict, total=False):
    """The single shape all tool results are normalized to before offload (see module docstring)."""

    text: str
    attachments: list[dict]
    source_ref: "dict | None"
    meta: dict


def _is_error(result: dict) -> bool:
    """A result is an error when it explicitly flags one (MCP ``isError``) or its op-level
    ``status`` is ``error`` (the sole error-path driver kept after meta-tightening)."""
    return bool(result.get("isError")) or result.get("status") == "error"


def _mcp_to_canonical(result: dict) -> CanonicalToolResult:
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


def _mcp_read_resource_to_canonical(result: dict) -> CanonicalToolResult:
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


def _mcp_get_prompt_to_canonical(result: dict) -> CanonicalToolResult:
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


def _web_fetch_to_canonical(result: dict) -> CanonicalToolResult:
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


def _web_search_to_canonical(result: dict) -> CanonicalToolResult:
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


def _sandboxed_exec_to_canonical(result: dict) -> CanonicalToolResult:
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


def _chunks_to_canonical(result: dict) -> CanonicalToolResult:
    """recall / index_query result → canonical. The retrieved ``chunks`` list → a ``structured``
    attachment (frontmatter YAML, or its own ref when large). There is no text body. Transport
    (``mode``) is dropped."""
    chunks = result.get("chunks")
    attachments = [{"kind": "structured", "data": chunks}] if chunks is not None else []
    return CanonicalToolResult(text="", attachments=attachments, source_ref=None, meta={})


def _run_pipeline_to_canonical(result: dict) -> CanonicalToolResult:
    """Sync run_pipeline result → canonical. The final ``output`` is the whole thing the calling LLM
    wants: a str output → ``text``; a non-str output → a ``structured`` attachment. ``run_id`` and
    ``named_stores`` are correlation/transport plumbing the caller never acts on → dropped (owner
    ruling)."""
    output = result.get("output")
    if isinstance(output, str):
        return CanonicalToolResult(text=output, attachments=[], source_ref=None, meta={})
    attachments = [{"kind": "structured", "data": output}] if output is not None else []
    return CanonicalToolResult(text="", attachments=attachments, source_ref=None, meta={})


def _run_pipeline_async_to_canonical(result: dict) -> CanonicalToolResult:
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


def _file_to_canonical(result: dict) -> CanonicalToolResult:
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


def _reyn_src_to_canonical(result: dict) -> CanonicalToolResult:
    """``reyn_src_*`` handler result (read/list/glob/grep) → canonical. These handlers return a
    kind-less ``{path, content}`` / ``{entries}`` / ``{matches}`` dict tagged with ``kind:"reyn_src"``
    at the tool-handler seam so this mapper (not the whole-dict fallback) shapes them — the dogfood
    incident root: a doc read via ``reyn_source__read`` was offloaded as a whole-dict ``structured``
    blob instead of the readable body.

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


def _render_template_to_canonical(result: dict) -> CanonicalToolResult:
    """``render_template`` op result → canonical (FP-0055 PR-2). The rendered string
    (``rendered``) IS the LLM-readable body → ``text`` (NOT a whole-dict ``structured``
    blob — a render_template result without its own mapper would fall to the FP-0056
    whole-dict fallback and hide the rendered text behind a JSON envelope). Signal
    meta: ``truncated`` (+ which bound fired, ``truncate_reason``) tells the LLM the
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


def _compact_to_canonical(result: dict) -> CanonicalToolResult:
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


def _judge_output_to_canonical(result: dict) -> CanonicalToolResult:
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


# op-kind → canonical mapper. Every op that once declared ``_offload_payload_field`` is registered
# here; an unregistered kind falls back to a whole-dict ``structured`` attachment (see ``to_canonical``).
_MAPPERS: dict[str, Any] = {
    "mcp": _mcp_to_canonical,
    "mcp_read_resource": _mcp_read_resource_to_canonical,
    "mcp_get_prompt": _mcp_get_prompt_to_canonical,
    "web_fetch": _web_fetch_to_canonical,
    "web_search": _web_search_to_canonical,
    "sandboxed_exec": _sandboxed_exec_to_canonical,
    "recall": _chunks_to_canonical,
    "index_query": _chunks_to_canonical,
    "run_pipeline": _run_pipeline_to_canonical,
    "run_pipeline_async": _run_pipeline_async_to_canonical,
    # FP-0055 PR-2: render_template producer — the rendered string → text (never the
    # whole-dict fallback for its OWN result; truncated/undefined_vars as signal meta).
    "render_template": _render_template_to_canonical,
    # FP-0056 PR-H hotfix: the file family + reyn_src dev-reads + compact/judge_output — the coverage
    # gap that offloaded a doc read as a whole-dict ``structured`` blob (dogfood 2026-07-09).
    "file": _file_to_canonical,
    "reyn_src": _reyn_src_to_canonical,
    "compact": _compact_to_canonical,
    "judge_output": _judge_output_to_canonical,
}


def to_canonical(result: dict) -> CanonicalToolResult:
    """Normalize an op result dict to :class:`CanonicalToolResult`. Dispatches on ``result["kind"]``;
    an unregistered kind falls back to a whole-dict ``structured`` attachment (``text`` empty) so
    nothing is ever lost — the whole dict renders as readable frontmatter YAML and
    ``ctx.<name>.structured.<field>`` still gives programmatic access."""
    kind = result.get("kind")
    mapper = _MAPPERS.get(kind) if isinstance(kind, str) else None
    if mapper is not None:
        return mapper(result)
    # Fallback: not-yet-migrated kind → the whole dict is a structured attachment (lossless, readable
    # as frontmatter YAML), never a whole-dict JSON-into-text blob.
    return CanonicalToolResult(
        text="",
        attachments=[{"kind": "structured", "data": result}],
        source_ref=None,
        meta={},
    )


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
