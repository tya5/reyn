"""`resolve_present_source` — the single data_ref resolution seam for present (FP-0054).

This is the one point where the present arc meets the tool-result / offload arc. A
``data_ref`` may be a plain workspace path OR an offload ref (e.g. a
``structured_ref``). present resolves it by loading the **full value** through the
same ``file.read`` authority + workspace access the file ops use — so an offloaded
structured payload is re-hydrated from its ref rather than read from the
LLM-visible preview, and inline-vs-offloaded is transparent to the renderer.

**Read-authority equivalence (hard invariant).** The gate here is exactly
``require_file_read`` against the resolved path: present can never read more than
the agent's ``file.read`` can (present denied ⇔ file.read denied). A denied read
raises ``PermissionError`` — propagated so the op-dispatch layer returns
``status="denied"`` (the same channel a denied file op uses).

``ingested`` is **OS-computed** from the events log (was there a prior
``read_file`` on this ref this session?), never LLM-self-reported.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from reyn.data.workspace.text_codec import decode_text_or_none

if TYPE_CHECKING:
    from reyn.core.op_runtime.context import OpContext


class PresentSourceNotFound(FileNotFoundError):
    """The ``data_ref`` does not resolve to a readable file (after the read gate
    passed) — distinct from a permission denial."""


async def resolve_present_source(data_ref: str, ctx: "OpContext") -> tuple[Any, str]:
    """Resolve ``data_ref`` to its full value under ``file.read`` authority.

    Returns ``(value, ingested)`` where ``value`` is the re-hydrated structured
    object (when the ref's bytes parse as JSON) or the decoded text, and
    ``ingested`` ∈ ``{none, partial, full}`` (OS-computed).

    Raises ``PermissionError`` when the read is not authorized (identical gate to
    ``file.read``); ``PresentSourceNotFound`` when the path is missing.
    """
    from reyn.core.op_runtime.file import _resolve_for_gate

    resolved = _resolve_for_gate(ctx, data_ref)

    # Read-authority equivalence: the SAME gate file.read uses. bus=None → the
    # non-interactive deny path; a wired bus → the same JIT prompt.
    if ctx.permission_resolver is not None:
        from reyn.core.op_runtime.context import sandbox_policy_from_ctx

        await ctx.permission_resolver.require_file_read(
            ctx.permission_decl,
            resolved,
            ctx.actor,
            sandbox_policy=sandbox_policy_from_ctx(ctx),
            bus=ctx.intervention_bus,
        )

    raw_bytes, found = ctx.workspace.read_file_bytes(data_ref)
    if not found:
        raise PresentSourceNotFound(f"present data_ref not found: {data_ref}")

    text, _encoding = decode_text_or_none(raw_bytes)
    value: Any
    if text is None:
        # Non-text binary — present it as an opaque marker (image routing is the
        # renderer's concern in a later PR); the value is the byte count.
        value = {"binary": True, "byte_size": len(raw_bytes)}
    else:
        value = _rehydrate(text)

    ingested = compute_ingested(ctx, data_ref, resolved)
    return value, ingested


def _rehydrate(text: str) -> Any:
    """Re-hydrate an offloaded structured ref (JSON bytes) to its object, else
    keep the plain text. The offload store writes ``json.dumps(...)`` for a
    ``structured_ref``; a plain-text ref is returned as its string."""
    import json

    stripped = text.strip()
    if stripped and stripped[0] in "{[":
        try:
            return json.loads(stripped)
        except (ValueError, TypeError):
            return text
    return text


def compute_ingested(ctx: "OpContext", data_ref: str, resolved: str) -> str:
    """Compute ``ingested`` ∈ ``{none, partial, full}`` from the events log.

    Blindness is an audit annotation, not a permission mode: this reports whether
    a prior ``read_file`` on this ref appears earlier in the session — a full
    (untruncated) read → ``full``; only truncated reads → ``partial``; no read →
    ``none``. Never LLM-self-reported.
    """
    saw_full = False
    saw_partial = False
    for event in ctx.events.all():
        if event.type != "tool_executed":
            continue
        d = event.data
        if d.get("op") not in ("read_file", "read"):
            continue
        path = d.get("path")
        if path != data_ref and path != resolved:
            continue
        if d.get("truncated"):
            saw_partial = True
        else:
            saw_full = True
    if saw_full:
        return "full"
    if saw_partial:
        return "partial"
    return "none"
