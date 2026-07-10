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

FP-0056 v2 closes the three canonical silent-loss modes structurally: piece #1 the shared error seam
(M1, ``error_to_canonical`` before every mapper), piece #2 the runtime ``canonical_degraded`` invariant
(M2, a success-mapper returning empty), and piece #3 (this) the inner-dispatch FAIL-VISIBLE seam (M3):
a mapper whose inner discriminator is missing/unknown raises :class:`CanonicalDiscriminatorMiss` instead
of emitting status-only garbage (the #2695 ``"None: ok"`` — non-empty so M2's empty-check misses it, not
an error so M1's seam misses it); :func:`to_canonical` catches it and rides the EXISTING whole-dict
fallback + ``canonical_fallback_used`` (reason ``"discriminator_miss"``).

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


class CanonicalDiscriminatorMiss(Exception):
    """Raised by an inner-dispatch mapper when its inner discriminator (the sub-field it switches on —
    ``file``'s ``op``, ``reyn_src``'s ``content``/``entries``/``matches`` body key) is MISSING or an
    UNKNOWN value, so the mapper cannot render a success view (FP-0056 v2 piece #3, mode M3).

    The mapper raises this instead of falling through to a status-only catch-all that interpolates the
    (often ``None``) discriminator into text — the ``"None: ok"`` garbage the #2695 file glob/list bug
    emitted (non-empty, so piece #2's ``canonical_degraded`` empty-check does NOT catch it; not an
    error, so piece #1's shared error seam does NOT catch it either). :func:`to_canonical` catches this
    and takes the SAME lossless whole-dict fallback a genuine unknown source would (:func:`_fallback_structured`),
    marking it so the caller emits the EXISTING ``canonical_fallback_used`` audit-event (reason
    ``"discriminator_miss"``) — the discriminator-miss renders the full dict (recoverable) + emits the
    audit signal, instead of silent garbage. No new mechanism: M3 rides the existing whole-dict-fallback
    visibility (the path #2695 should have taken)."""


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
    ``status`` is ``error`` (the sole error-path driver kept after meta-tightening).

    Retained after FP-0056 v2 piece #1 for the TWO producers whose ``status:"error"`` carries its
    payload in a non-``error`` field (so the shared error seam intentionally does NOT intercept them,
    and they keep their own ``meta.isError`` handling): ``mcp`` (message in ``content``) and
    ``sandboxed_exec`` (a nonzero exit — output in ``stdout``/``stderr``, ``returncode`` as signal).
    Also consulted by :func:`canonical_degraded_reason` (piece #2)."""
    return bool(result.get("isError")) or result.get("status") == "error"


# FP-0056 v2 piece #1 — the shared error seam (the M1-class linchpin).
#
# The dedicated error-MESSAGE fields, in extractor priority order. This IS the true union of the
# per-mapper error branches removed in piece #1 (``file``/``reyn_src``/``render_template``/``compact``/
# ``present``/``judge_output``/``memory_body``/``web_search`` — and the recall/task_ops ``{ok:False,
# error_message}`` that had NO branch, the #2698 gap). Every one of those producers surfaces its error
# through one of these fields, so keying the predicate on them (not on the ambiguous ``status``/``ok``
# values) makes removing the branches regression-free by construction.
_ERROR_MESSAGE_FIELDS = ("error_message", "error", "error_kind")


def is_error_result(result: object) -> bool:
    """The union error predicate the shared error seam runs BEFORE a mapper's success-only logic
    (FP-0056 v2 piece #1). True for the FIXED SET of known error shapes:

    - ``{isError}`` (MCP-style explicit flag);
    - a truthy dedicated error-message field — ``{error}`` / ``{error_message}`` / ``{error_kind}`` —
      which subsumes the recall/task_ops ``{ok:False, error_message}`` + ``{error_kind}`` shapes, the
      ``file`` ``denied``/``not_found`` (which carry ``error``), and every other per-mapper branch.

    **Tightening A (the misclassification safety).** The seam runs on ALL mapper-path results, so the
    predicate must NOT over-match a SUCCESS payload that happens to carry a data-meaning ``ok``/``status``
    key. Two concrete hazards this predicate is designed around:

    - a health-check-style ``{ok: False, service: "db"}`` (``ok:False`` MEANS "DB is down" — success
      data) → NOT matched (no error-message field; a bare ``ok is False`` is never a trigger);
    - ``sandboxed_exec``'s ``{status: "error", returncode: 2, stdout, stderr}`` (a nonzero exit is a
      SUCCESSFUL execution whose output is ``stdout``/``stderr``) → NOT matched (no error-message
      field). This is a REAL in-repo instance of the ``status`` data-meaning hazard: routing it through
      :func:`error_to_canonical` would drop ``stdout``/``stderr`` on the router error path (which renders
      ``text`` only, dropping attachments, when ``meta.isError``). So a bare ``status`` value is NOT a
      standalone trigger — every real error producer pairs its status with an error-message field, and
      the two producers that don't (``mcp`` content / ``sandboxed_exec`` stdout) keep their in-mapper
      ``meta.isError`` handling (:func:`_is_error`) precisely because their payload is NOT a message.

    Even were a misclassification to slip through, :func:`error_to_canonical` is LOSSLESS (whole dict →
    structured attachment), so it only ever MIS-LABELS, never loses data."""
    if not isinstance(result, dict):
        return False
    if result.get("isError"):
        return True
    return any(result.get(field) for field in _ERROR_MESSAGE_FIELDS)


def _extract_error_message(result: dict) -> str:
    """The union message extractor: read the first present error-message field in priority order,
    ALWAYS returning a non-empty string (so an error never renders to empty ``text`` — the M1 fix).
    A shape with an error signal but no readable message (e.g. a bare ``{error_kind}``) still yields a
    non-empty line; the full dict is preserved in the structured attachment by :func:`error_to_canonical`."""
    for field in ("error_message", "error"):
        value = result.get(field)
        if value:
            return str(value)
    error_kind = result.get("error_kind")
    if error_kind:
        return f"error: {error_kind}"
    return "error"


def error_to_canonical(result: dict) -> CanonicalToolResult:
    """Render ANY :func:`is_error_result`-classified result to a LOSSLESS canonical error view
    (FP-0056 v2 piece #1). Two guarantees:

    - a NON-EMPTY ``text`` (the extracted union message) — eliminating the M1 silent-loss class where a
      mapper with no error branch rendered an error to empty text (recall #2698 et al.);
    - the WHOLE result dict as a ``structured`` attachment, plus ``meta.isError``. This losslessness is
      what makes the fixed-set union predicate safe to run before every mapper (tightening A): even a
      hypothetical misclassification of a success payload only MIS-LABELS it (``isError`` on a success)
      — it NEVER loses data (the full payload survives in the attachment; the existing offload gate
      handles it if large)."""
    return CanonicalToolResult(
        text=_extract_error_message(result),
        attachments=[{"kind": "structured", "data": result}],
        source_ref=None,
        meta={"isError": True},
    )


def _explicit_empty(text: str, marker: str) -> str:
    """Render a legit-empty SUCCESS body (an empty file read, a no-output command, an empty
    template render, …) as an EXPLICIT marker instead of a blank string.

    Two reasons (FP-0056 v2 piece #2): (1) a blank ``text`` with no attachments on a non-error
    result would spuriously fire the runtime ``canonical_degraded`` invariant, which exists to catch
    a *success-mapper losing content it had* (M2) — a genuinely-empty success is not that loss;
    (2) an explicit "(empty file)" / "(no output)" is better LLM UX than a blank tool result the
    model has to guess about. Only mappers whose success path can legitimately produce no output
    wrap their body here; a mapper that regresses to empty when it should always produce something
    (or an unknown future mapper) still fires the invariant."""
    return text if text.strip() else marker


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
    text = result.get("content", "") or ""
    # A successful MCP call with no content AND no attachments renders an explicit empty (else the
    # canonical_degraded invariant fires on a legit no-output tool). An error keeps its (possibly
    # terse) message untouched; an attachment-carrying result already has visible content.
    if not attachments and not _is_error(result):
        text = _explicit_empty(text, "(no content)")
    return CanonicalToolResult(
        text=text,
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
    text = "\n".join(text_parts)
    if not attachments and not _is_error(result):
        text = _explicit_empty(text, "(no content)")
    return CanonicalToolResult(
        text=text,
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
    text = "\n".join(text_parts)
    if not attachments and not _is_error(result):
        text = _explicit_empty(text, "(no content)")
    return CanonicalToolResult(
        text=text,
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
    text = _explicit_empty(text, "(no content)")
    meta: dict[str, Any] = {}
    if result.get("truncated"):
        meta["truncated"] = True
        next_start = result.get("next_start")
        if next_start is not None:
            meta["next_start"] = next_start
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta=meta)


def web_search_to_canonical(result: dict) -> CanonicalToolResult:
    """web_search result → canonical (SUCCESS shape only — FP-0056 v2 piece #1 routes an error
    ``{status:"error", error}`` through the shared ``error_to_canonical`` seam before this mapper runs).
    The ``results`` list → a ``structured`` attachment (rendered as frontmatter YAML, or offloaded to
    its own ref when large). Transport (query/backend) is dropped."""
    results = result.get("results")
    if results is not None:
        return CanonicalToolResult(
            text="", attachments=[{"kind": "structured", "data": results}], source_ref=None, meta={},
        )
    return CanonicalToolResult(text="", attachments=[], source_ref=None, meta={})


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
    # A command that produced no stdout/stderr (e.g. mkdir/touch/mv) renders an explicit empty so the
    # canonical_degraded invariant does not fire on a legit no-output exec (returncode carries signal).
    text = _explicit_empty(text, "(no output)")
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
        return CanonicalToolResult(
            text=_explicit_empty(output, "(no output)"), attachments=[], source_ref=None, meta={},
        )
    if output is None:
        # A pipeline that completed with no final output: explicit empty text (no structured to carry
        # it), so a legit no-output run does not fire the canonical_degraded invariant.
        return CanonicalToolResult(text="(no output)", attachments=[], source_ref=None, meta={})
    return CanonicalToolResult(
        text="", attachments=[{"kind": "structured", "data": output}], source_ref=None, meta={},
    )


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

    SUCCESS shape only — FP-0056 v2 piece #1 routes any error (``status`` error/denied/not_found, which
    carry an ``error`` field) through the shared ``error_to_canonical`` seam before this mapper runs; the
    whole result dict (incl. ``op``/``status``/``path``) is preserved in that error view's lossless
    structured attachment.

    ``op`` is the inner discriminator: when it is MISSING or an UNKNOWN value (the #2695 glob/list
    adapters that normalized ``op`` away), this raises :class:`CanonicalDiscriminatorMiss` — FAIL-VISIBLE
    (mode M3) — so :func:`to_canonical` takes the lossless whole-dict fallback + fires
    ``canonical_fallback_used``, INSTEAD of the old status-only ``f"{op}: {status}"`` = ``"None: ok"``
    garbage."""
    op = result.get("op")
    meta = _file_signal_meta(result)
    if op == "read":
        attachments = [
            {"kind": "media", "block": block} for block in (result.get("media_blocks") or [])
        ]
        text = result.get("content", "") or ""
        # An empty file read (no media blocks carrying an image body) renders an explicit empty so the
        # canonical_degraded invariant does not fire on a legit read of a genuinely empty file.
        if not attachments:
            text = _explicit_empty(text, "(empty file)")
        return CanonicalToolResult(
            text=text, attachments=attachments, source_ref=None, meta=meta,
        )

    if op == "grep":
        return CanonicalToolResult(
            text=_render_file_grep(result), attachments=[], source_ref=None, meta=meta,
        )

    if op == "glob":
        matches = result.get("matches") or []
        text = "\n".join(str(m) for m in matches) if matches else "(no matches)"
        return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta=meta)

    # write / edit / delete / mkdir / move / stat / regenerate_index → a short status text. A missing/
    # unknown ``op`` is a discriminator-miss → fail-visible (M3), NEVER the old ``"None: ok"`` garbage.
    if op in _FILE_STATUS_OPS:
        return CanonicalToolResult(
            text=_render_file_status(op, result), attachments=[], source_ref=None, meta=meta,
        )
    raise CanonicalDiscriminatorMiss(f"file_to_canonical: missing/unknown op {op!r}")


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


# The mutating / metadata ``file`` ops whose success view is a short status line (read/grep/glob are
# handled earlier in ``file_to_canonical`` by their own body). Any ``op`` outside read/grep/glob AND
# this set is a discriminator-miss → :class:`CanonicalDiscriminatorMiss` (M3), never status garbage.
_FILE_STATUS_OPS = frozenset(
    {"write", "edit", "delete", "mkdir", "move", "stat", "regenerate_index"}
)


def _render_file_status(op: "str | None", result: dict) -> str:
    """A short, human-readable status line for a mutating / metadata ``file`` op (write/edit/delete/
    mkdir/move/stat/regenerate_index). Descriptive, not JSON — the LLM acts on the outcome, not the
    envelope. Only ever called with an ``op`` in :data:`_FILE_STATUS_OPS` (``file_to_canonical`` raises
    :class:`CanonicalDiscriminatorMiss` for a missing/unknown ``op`` before reaching here), so there is
    no status-only ``f"{op}: {status}"`` catch-all — that was the #2695 ``"None: ok"`` garbage."""
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
    # Unreachable for a valid op (caller guards with ``_FILE_STATUS_OPS``); defensive fail-visible
    # rather than the removed ``f"{op}: {status}"`` = ``"None: ok"`` garbage catch-all.
    raise CanonicalDiscriminatorMiss(f"_render_file_status: unhandled op {op!r}")


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

    SUCCESS shape only — FP-0056 v2 piece #1 routes an ``{error}`` result through the shared
    ``error_to_canonical`` seam before this mapper runs. The body key (``content``/``entries``/
    ``matches``) is the inner discriminator: a result carrying NONE of them raises
    :class:`CanonicalDiscriminatorMiss` (mode M3) so :func:`to_canonical` takes the lossless whole-dict
    fallback + fires ``canonical_fallback_used`` — the SAME recoverable output as the old inline
    whole-dict return, now AUDITED (fail-visible) rather than silently unmapped."""
    if "content" in result:
        meta = {"path": result["path"]} if result.get("path") is not None else {}
        text = _explicit_empty(result.get("content", "") or "", "(empty file)")
        return CanonicalToolResult(
            text=text, attachments=[], source_ref=None, meta=meta,
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
    # A reyn_src shape with none of the known bodies (content/entries/matches) — discriminator-miss.
    # Fail-visible (M3): raise so ``to_canonical`` takes the lossless whole-dict fallback AND fires
    # ``canonical_fallback_used``, instead of an inline whole-dict return that was recoverable but
    # SILENT (unaudited).
    raise CanonicalDiscriminatorMiss("reyn_src_to_canonical: no content/entries/matches body key")


def render_template_to_canonical(result: dict) -> CanonicalToolResult:
    """``render_template`` op result → canonical (FP-0055 PR-2). The rendered string
    (``rendered``) IS the LLM-readable body → ``text`` (NOT a whole-dict ``structured``
    blob). Signal meta: ``truncated`` (+ which bound fired, ``truncate_reason``) tells the LLM the
    output was capped mid-generate; ``undefined_vars`` (lenient mode) names the
    referenced-but-unbound template variables so it can self-correct.

    SUCCESS shape only — FP-0056 v2 piece #1 routes an error (``status="error"``/``not_found`` — syntax /
    SSTI-blocked / strict-undefined, which carry an ``error`` field) through the shared
    ``error_to_canonical`` seam before this mapper runs."""
    meta: dict[str, Any] = {}
    if result.get("truncated"):
        meta["truncated"] = True
        reason = result.get("truncate_reason")
        if reason:
            meta["truncate_reason"] = reason
    undefined_vars = result.get("undefined_vars")
    if undefined_vars:
        meta["undefined_vars"] = undefined_vars
    text = _explicit_empty(result.get("rendered", "") or "", "(empty render)")
    return CanonicalToolResult(
        text=text, attachments=[], source_ref=None, meta=meta,
    )


def compact_to_canonical(result: dict) -> CanonicalToolResult:
    """``compact`` op result → canonical. On success the freed-token / free-window metrics (+ the chat-
    axis compression fields when present) render as a short ``text`` summary; on error the ``error``
    message surfaces as ``text`` with ``meta.isError``. Result shape:
    ``{kind:"compact", status:"ok", freed_tokens?, free_window_after?, summarized_turns?,
    compressed_tokens?, bridge_tokens?}`` (ok) or ``{status:"error", error_kind, error}`` (error).

    SUCCESS shape only — FP-0056 v2 piece #1 routes the error shape through the shared
    ``error_to_canonical`` seam before this mapper runs."""
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


def present_to_canonical(result: dict) -> CanonicalToolResult:
    """``present`` op/tool result → canonical (FP-0054 / FP-0056). ``present`` is fire-and-continue: it
    routes the bulk data to the user surface itself and returns a compact ACK. That ack is an
    AGENT-facing signal (did the presentation reach the user? did the view bind? which fallback fired?),
    NOT bulk content — so it renders as a short ``text`` line, not a whole-dict ``structured`` blob (the
    incident class). Success shape: ``{kind:"present", status:"ok", ok:True, mode, bindings_resolved,
    bindings_dropped, rows, all_bindings_missed, note?}``.

    SUCCESS shape only — FP-0056 v2 piece #1 routes any non-``ok`` status (``error`` — malformed inline
    blueprint / XOR violation; ``not_found`` — missing ``data_ref``; ``denied`` — read-authority; each
    carries an ``error`` field) through the shared ``error_to_canonical`` seam before this mapper runs,
    so the LLM still self-corrects from a non-empty error message."""
    parts: list[str] = ["Presented to the user."]
    mode = result.get("mode")
    if mode is not None:
        parts.append(f"mode={mode}")
    for key in ("rows", "bindings_resolved"):
        value = result.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    dropped = result.get("bindings_dropped")
    if dropped:
        parts.append(f"bindings_dropped={len(dropped)}")
    if result.get("all_bindings_missed"):
        parts.append("all_bindings_missed=True")
    text = " ".join(parts)
    note = result.get("note")
    if note:
        text = f"{text}\n{note}"
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta={})


def judge_output_to_canonical(result: dict) -> CanonicalToolResult:
    """``judge_output`` op result → canonical. The scorer's ``reason`` (its LLM-readable explanation) is
    the ``text``; ``score`` / ``passed`` / ``threshold`` / ``on_fail`` are signal meta (they drive the
    caller's next move — a failed judgment triggers ``on_fail``). An error surfaces the message as
    ``text`` with ``meta.isError``. Shape: ``{kind:"judge_output", score, passed, reason, threshold,
    on_fail}`` (ok) or ``{status:"error", error}`` (error).

    SUCCESS shape only — FP-0056 v2 piece #1 routes the ``{status:"error", error}`` shape through the
    shared ``error_to_canonical`` seam before this mapper runs."""
    meta: dict[str, Any] = {}
    for key in ("score", "passed", "threshold", "on_fail"):
        value = result.get(key)
        if value is not None:
            meta[key] = value
    text = _explicit_empty(str(result.get("reason", "") or ""), "(no reason given)")
    return CanonicalToolResult(
        text=text, attachments=[], source_ref=None, meta=meta,
    )


def memory_body_to_canonical(result: dict) -> CanonicalToolResult:
    """``read_memory_body`` result → canonical (FP-0056 PR-F1 triage: text-shaped). The memory entry's
    body (``content``, frontmatter already stripped by the handler) IS the LLM-readable text → ``text``
    — NOT a whole-dict blob. This is the same file-class the incident exposed, and it has its own
    documented G12 empty-stop attractor (an LLM handed non-clean memory text stopped with an empty
    reply — router_loop._read_memory_body). ``layer`` / ``slug`` are signal meta (which entry). An
    error (``error`` field) surfaces the message as ``text`` with ``meta.isError``. Shape:
    ``{content, layer?, slug?}`` (ok) or ``{error, layer?, slug?}`` (error).

    SUCCESS shape only — FP-0056 v2 piece #1 routes the ``{error, layer?, slug?}`` shape through the
    shared ``error_to_canonical`` seam before this mapper runs."""
    meta: dict[str, Any] = {}
    for key in ("layer", "slug"):
        value = result.get(key)
        if value is not None:
            meta[key] = value
    text = _explicit_empty(result.get("content", "") or "", "(empty)")
    return CanonicalToolResult(
        text=text, attachments=[], source_ref=None, meta=meta,
    )


def ask_user_to_canonical(result: dict) -> CanonicalToolResult:
    """``ask_user`` op result → canonical (FP-0056 PR-F1 triage: text-shaped). The user's ``answer``
    (free text or the chosen option) IS what the LLM acts on → ``text`` — not a whole-dict blob hiding
    it behind ``kind``/``question``/``status`` transport. Shapes:
    ``{kind:"ask_user", question, answer, status:"ok"}`` (answered) or
    ``{kind:"ask_user", question, answer:"", status:"refused", reason}`` (a #2708 P3-item3 refusal).

    The ``refused`` shape is the THIRD in-mapper hybrid boundary (with ``mcp`` content / ``sandboxed_exec``
    stdout — FP-0056 v2 piece #1): a DELIBERATE, reason'd refusal is a typed NON-error outcome, NOT a tool
    error and NOT an empty answer. It carries no error-message field, so the shared error seam correctly
    does not intercept it (it is not an error). But it MUST be handled here BEFORE the answer/explicit-empty
    logic — otherwise ``_explicit_empty`` sees the empty ``answer`` and renders ``(no answer)``, silently
    DROPPING the ``reason`` and re-introducing the very empty-answer the P3-item3 refusal design removed
    (the LLM could then not tell a refusal from a blank answer). The reason is surfaced as ``text``; NO
    ``meta.isError`` is set — framing a deliberate refusal as an error would contradict its
    typed-non-error design."""
    if result.get("status") == "refused":
        reason = str(result.get("reason", "") or "")
        text = f"(no answer — refused: {reason})" if reason else "(no answer — refused)"
        return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta={})
    text = _explicit_empty(str(result.get("answer", "") or ""), "(no answer)")
    return CanonicalToolResult(
        text=text, attachments=[], source_ref=None, meta={},
    )


# A private, NON-rendered marker key stamped on the whole-dict fallback canonical when it was taken
# because an inner-dispatch mapper raised :class:`CanonicalDiscriminatorMiss` (FP-0056 v2 piece #3, M3).
# It is a signal channel for :func:`canonical_fallback_reason` only — the renderer (``build_offload_body``
# reads ``attachments``/``meta``) and the ctx reducer (``canonical_to_ctx_fields`` reads ``text``/
# ``attachments``) never read it, so it never reaches the LLM body (unlike ``meta``, which renders as
# frontmatter YAML).
_DISCRIMINATOR_MISS_MARKER = "_discriminator_miss"


def _fallback_structured(result: dict, *, discriminator_miss: bool = False) -> CanonicalToolResult:
    """The lossless whole-dict fallback: the entire result becomes a ``structured`` attachment
    (readable as frontmatter YAML, ``ctx.<name>.structured.<field>`` still programmatically reachable),
    ``text`` empty. Used for a declared ``STRUCTURED_PASSTHROUGH`` producer, a provisional
    ``CANONICAL_TODO`` producer, a genuinely unregistered ``source`` (dynamic/edge), AND a mapped
    producer whose inner discriminator missed (``discriminator_miss=True`` — FP-0056 v2 piece #3, M3).
    PR-F2 emits ``canonical_fallback_used`` on the ``CANONICAL_TODO`` + unregistered paths, and piece #3
    on the discriminator-miss path (degrade-with-audit) — but NOT on ``STRUCTURED_PASSTHROUGH`` (a
    reviewed, legitimate whole-dict view)."""
    canonical = CanonicalToolResult(
        text="", attachments=[{"kind": "structured", "data": result}], source_ref=None, meta={},
    )
    if discriminator_miss:
        canonical[_DISCRIMINATOR_MISS_MARKER] = True  # type: ignore[typeddict-unknown-key]
    return canonical


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
    # FP-0056 v2 piece #1 — the shared error seam, scope-limited to the MAPPER + CANONICAL_TODO paths
    # (tightening A #3). A known error shape routes to the single lossless ``error_to_canonical`` BEFORE
    # the mapper's (now success-only) logic OR the TODO whole-dict fallback — structurally eliminating
    # the M1 class (a mapper with no error branch rendering an error to empty text). STRUCTURED_PASSTHROUGH
    # (a reviewed "the whole dict IS the view") and unregistered/``None`` (a genuine unknown) are
    # deliberately OUT of scope: they are already lossless whole-dict, M1 loss only occurs where a mapper
    # would interpret the result, and keeping them on ``_fallback_structured`` preserves their
    # ``canonical_fallback_used`` (PR-F2) visibility semantics.
    if declaration is STRUCTURED_PASSTHROUGH or declaration is None:
        return _fallback_structured(result)
    if is_error_result(result):
        return error_to_canonical(result)
    if declaration is CANONICAL_TODO:
        return _fallback_structured(result)
    # FP-0056 v2 piece #3 — the M3 fail-visible seam. A mapper whose inner discriminator is missing/
    # unknown raises ``CanonicalDiscriminatorMiss`` rather than emitting status-only garbage (#2695
    # ``"None: ok"``). Route it to the SAME lossless whole-dict fallback a genuine unknown takes, marked
    # so the caller emits ``canonical_fallback_used`` (reason ``"discriminator_miss"``) — full dict
    # recoverable + audit signal, never silent garbage.
    try:
        return declaration(result)
    except CanonicalDiscriminatorMiss:
        return _fallback_structured(result, discriminator_miss=True)


# The audit-event kind the two live ``to_canonical`` callers emit when a result took a VISIBLE
# fallback path — the observability half of FP-0056 (the static coverage gate is PR-F1; this makes
# the runtime debt + genuine-unknown fallbacks visible instead of silent). It is an audit / P6 event,
# NOT a WAL / recovery-core event.
CANONICAL_FALLBACK_EVENT = "canonical_fallback_used"


def canonical_fallback_reason(
    source: "str | None",
    *,
    structured_offloaded: bool = False,
    canonical: "CanonicalToolResult | None" = None,
) -> "str | None":
    """Return the audit reason a :data:`CANONICAL_FALLBACK_EVENT` should carry for ``source``'s
    canonicalization, or ``None`` when nothing should fire (FP-0056 PR-F2 — the visibility half).

    A short category string is returned on each of the four fail-visible paths (owner decisions #2/#3
    — degrade-with-audit, never silently):

    - ``canonical`` carries the discriminator-miss marker — a MAPPED producer whose inner discriminator
      was missing/unknown, so :func:`to_canonical` took the lossless whole-dict fallback instead of the
      mapper's status-only garbage (FP-0056 v2 piece #3, mode M3) → ``"discriminator_miss"``. Checked
      FIRST because it is the ONE fallback a real-mapper ``source`` can take; without it the declaration
      lookup below would (wrongly) report ``None`` for a mapped producer that DID fall back. The #2695
      ``"None: ok"`` class made runtime-visible.
    - ``source`` unregistered / ``None`` (a genuine unknown the registries can't enumerate → the
      lossless whole-dict fallback) → ``"unregistered"``.
    - ``source`` declared :data:`CANONICAL_TODO` (gate-satisfying debt, no real mapper yet → the same
      whole-dict fallback) → ``"canonical_todo"``. This is the #2681 burn-down debt made runtime-visible.
    - ``source`` declared :data:`STRUCTURED_PASSTHROUGH` whose whole-dict serialization exceeded the
      structured offload gate (caller passes ``structured_offloaded=True``) → ``"passthrough_oversized"``
      (owner decision #2: an oversized passthrough blob signals passthrough was the wrong choice for
      this producer — make it visible). A SMALL (inline) passthrough is a reviewed, legitimate view →
      ``None`` (no event).

    A real mapper that mapped cleanly always returns ``None`` — a mapped producer that did not fall back
    never took a fallback. Only a reason CATEGORY is returned; NO result content is ever returned or
    logged (audit signal, not data — the callers emit the ``source`` id + this reason, never the result
    body)."""
    if canonical is not None and canonical.get(_DISCRIMINATOR_MISS_MARKER):
        return "discriminator_miss"
    declaration = canonical_declaration(source)
    if declaration is None:
        return "unregistered"
    if declaration is CANONICAL_TODO:
        return "canonical_todo"
    if declaration is STRUCTURED_PASSTHROUGH and structured_offloaded:
        return "passthrough_oversized"
    return None


# The audit-event kind the two live ``to_canonical`` callers emit when a NON-error result canonicalized
# to a completely empty view — no text AND no attachments — i.e. a success-mapper silently lost the
# content it should have surfaced (FP-0056 v2 piece #2, mode M2), or an unknown future mapper bug did.
# Distinct from ``canonical_fallback_used`` (which fires on the *declared/unknown whole-dict fallback*
# paths, never on a mapped producer): this one fires on a MAPPED producer that emitted nothing. It is
# an audit / P6 event, NOT a WAL / recovery-core event (no truncate-falsify obligation).
CANONICAL_DEGRADED_EVENT = "canonical_degraded"

# The single reason category ``canonical_degraded_reason`` returns when it fires. A category string
# (not result content) so the callers emit ``source`` id + this reason, never the result body.
_CANONICAL_DEGRADED_REASON = "empty_canonical"


def canonical_degraded_reason(
    result: dict, canonical: CanonicalToolResult
) -> "str | None":
    """Return the audit reason a :data:`CANONICAL_DEGRADED_EVENT` should carry, or ``None`` when the
    result canonicalized to a visible view (FP-0056 v2 piece #2 — the runtime M2 safety net).

    Fires (non-``None``) iff ALL hold:

    - the result is **not error-classified** — neither :func:`_is_error` (``isError`` / ``status ==
      "error"``) nor the canonical's ``meta.isError`` (the broader per-mapper error checks — ``file``'s
      ``denied``/``not_found``, an ``error`` field, …) flags it. An error result may legitimately carry
      a terse message; it is not a silent SUCCESS loss (piece #1's shared error seam will further
      guarantee non-empty error text);
    - the canonical ``text`` is empty after ``.strip()``;
    - the canonical ``attachments`` list is empty.

    A ``data: []`` (or any) structured attachment is an EXPLICIT empty the LLM sees → does NOT fire
    (the rule is purely text-empty AND attachments-empty; there is deliberately NO "trivial attachment"
    check). A legit-empty success (empty file, no-output command, …) is rendered to an explicit marker
    by its mapper (:func:`_explicit_empty`), so it too renders non-empty text and does NOT fire — only a
    mapper that *lost* content it had, or a not-yet-fixed future mapper, produces the empty+empty shape.

    The helper is PURE (a sibling of :func:`canonical_fallback_reason`): the event fires caller-side at
    the two ``to_canonical`` call sites, never here."""
    if not isinstance(result, dict):
        return None
    if _is_error(result) or (canonical.get("meta") or {}).get("isError"):
        return None
    if (canonical.get("text", "") or "").strip():
        return None
    if canonical.get("attachments"):
        return None
    return _CANONICAL_DEGRADED_REASON


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
