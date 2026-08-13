"""#4482 PR-2: present payload construction for an "artifact" — an
LLM-produced file the terminal can't render natively (html/office/pdf/
images), which a user opens with the OS's own default app.

PR-2b wires this module in: ``catalog.py``'s ``CATALOG`` gates the
``"artifact"`` component's own structural rules (exactly one of
source/content; media_type required alongside content, forbidden
alongside source), ``binding.py``'s ``resolve_bindings`` resolves an
``artifact`` node's raw slot values (bind-or-literal, same as ``image``'s
own ``src``) WITHOUT touching disk (stays pure — see that function's own
"artifact" branch comment for why), and :func:`apply_artifact_resolution`
below is the separate post-processing pass — run by
``op_runtime/present.py``'s ``handle()``, the layer that actually has
``project_root``/``agent_name`` — that turns those raw values into the
final OS-derived payload this module's other two functions build.

**① server/client payload shape** (architect's final corrected form, #4482
issue thread)::

    payload := {
      media_type  : <IANA, OPTIONAL, best-effort> (None -> "application/octet-stream")
      name        : <str>  required for a source-backed artifact, absent for inline
      description : <str, optional>
      body        : Reference | (Reference & Inline) | Inline
    }
    Reference := {"ref": <str>, "size": <int>}
    Inline    := {"inline": <str>}            # text-serialized only, never base64

    #4574 design C (owner GO, 2026-08-13): for a SOURCE-backed artifact, ``body``
    ALWAYS carries a ``ref`` — never gated on the inline-probe outcome — with the
    small-and-decodable-text ``inline`` PREVIEW added alongside it when the probe
    succeeds, never in its place. Before #4574, a small decodable source file got
    ``Inline`` ONLY: no ``ref`` meant no text-client renderer could show it (the
    #4574 REPL/TUI ``artifact`` render-branch gap) AND nothing was openable via
    the OS (the Art tab's row had ``ref=None``) — a real file the agent handed
    over that the operator could neither see nor open. Bare ``Inline`` (no
    ``ref``) is now reachable ONLY through :func:`build_inline_artifact_payload`
    — agent-DECLARED content with no backing file to mint a ref FOR.

**② agent-facing slots** — an agent says EITHER:
  - ``source`` + ``description`` — the OS derives ``media_type``/``name``/
    ``ref``/which body shape from the real file (:func:`build_source_artifact_payload`).
  - ``media_type`` + ``content`` + ``description`` directly — there is no
    real file for the OS to derive anything from, so the agent NAMES
    ``media_type`` itself (:func:`build_inline_artifact_payload`). "Don't
    let the agent write ``media_type``" is the SOURCE case's rule, whose
    reason ("the OS can derive it from the real file") doesn't apply when
    there is no real file.

**Invariants** (architect's ruling, unchanged from PR-1, #4482):

1. **Never put a raw filesystem path on the wire.** The payload carries a
   ``ref`` only (Reference body) or fully-materialized text (Inline body)
   — never a path string. Local-host resolution is the transport layer's
   own optimization, not this function's concern.
2. **No reyn-specific extension table.** ``mimetypes.guess_type()``'s
   result, unmodified. Two NARROWER tables already exist —
   ``op_runtime/file.py``'s ``_IMAGE_EXTENSIONS`` and ``slash/image.py``'s
   own copy of it — but both answer a different, narrower question
   (whether ``read_file``'s BINARY read path should fire) and neither is
   reused here: this module derives a general ``media_type`` for a
   present payload, which stdlib ``mimetypes`` already answers directly,
   without adding a THIRD hand-maintained table (#4431's own class).
3. **Inline vs Reference for a source-backed artifact is decided by
   OBSERVATION, never by a media_type table lookup.** ``media_type`` is
   unreliable for this call: architect's own measurement found stdlib
   ``mimetypes`` resolves ``.pptx``/``.docx``/``.xlsx`` to ``None`` on a
   minimal host (no ``/etc/mime.types``) — exactly the formats motivating
   this feature. The decision is: stat the file (never read it for this
   step) — above the probe limit, Reference, no further read needed;
   otherwise read ONLY that many bytes and attempt a UTF-8 decode —
   success is Inline, failure is Reference. The amount ever read is
   therefore structurally bounded regardless of the real file's size.
4. **No inline byte cap for agent-DECLARED inline content** (the
   :func:`build_inline_artifact_payload` path). The model's own output-
   token ceiling already bounds what an agent can write inline — a
   second, reyn-side byte cap would just repeat that same bound under a
   different error message, to the same caller (the model itself) that
   already gets told when it overruns the first one.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from reyn.data.workspace.artifact_ref import mint_ref
from reyn.data.workspace.ref_path_normalize import normalize_ref_path

# The size boundary between "read a bounded probe and try to inline it"
# and "too big, go straight to Reference" for a SOURCE-backed artifact
# (invariant 3 above). Reuses MultimodalConfig.max_bytes's own value
# (config/media.py) — the existing, already owner-approved media-size
# bound — rather than inventing a new number for a closely related
# question ("how much binary content is reasonable to inline/decode").
# If real usage later shows this needs its own, different value, that is
# a config knob to ADD then, backed by evidence — not a number to invent
# now without any (CLAUDE.md: no unjustified constant without a knob).
INLINE_PROBE_MAX_BYTES = 5_000_000


def _derive_media_type(name: str) -> "str | None":
    """``mimetypes.guess_type()``'s result, unmodified — invariant 2. No
    reyn-specific table, no fallback table, no override list."""
    guessed, _encoding = mimetypes.guess_type(name)
    return guessed


def build_source_artifact_payload(
    project_root: Path,
    agent_name: str,
    source: "str | Path",
    *,
    description: "str | None" = None,
    probe_max_bytes: int = INLINE_PROBE_MAX_BYTES,
) -> dict:
    """Build a payload for a ``source``-backed artifact (agent gives a
    path; the OS derives everything else — invariant/slot ②).

    Raises ``FileNotFoundError`` if *source* does not resolve to a real
    file — the caller decides how to surface that (e.g. as a present-node
    render error), this function's own job stops at "can I even see it".
    """
    resolved = normalize_ref_path(source, project_root)
    if not resolved.is_file():
        raise FileNotFoundError(f"artifact source {source!r} is not a file")

    name = resolved.name
    media_type = _derive_media_type(name)
    size = resolved.stat().st_size

    # #4574 design C (owner GO): a source-backed artifact ALWAYS mints a ref
    # — never gated on the probe outcome — with the small-and-decodable-text
    # inline preview added ALONGSIDE it, not instead of it. Before this, a
    # small decodable file got `{"inline": ...}` and NOTHING ELSE: no `ref`
    # meant `_artifact_row_from_node` (artifact_list.py) built an
    # `is_inline=True` row with `ref=None`, so the Art tab's Enter had
    # nothing to open (#4574's own reported symptom — "gave a real file,
    # couldn't open it"). The probe's outcome now only decides whether an
    # inline PREVIEW is attached, never whether the file is openable.
    ref = mint_ref(project_root, agent_name, resolved)
    body: dict = {"ref": ref, "size": size}
    if size <= probe_max_bytes:
        probe = resolved.read_bytes()[:probe_max_bytes]
        try:
            inline_text = probe.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            body["inline"] = inline_text

    payload: dict = {"media_type": media_type, "name": name, "body": body}
    if description is not None:
        payload["description"] = description
    return payload


def build_inline_artifact_payload(
    media_type: str,
    content: str,
    *,
    description: "str | None" = None,
) -> dict:
    """Build a payload for agent-DECLARED inline content (no real file
    exists — slot ②'s other form). The agent names ``media_type`` itself
    (invariant/slot ②'s own carve-out) since there is no file for the OS
    to derive it from. No ``name`` slot — there is no real-file identity
    to name, and nothing is ever opened for this form, so the "user sees
    what they're about to open" requirement this whole feature exists for
    (#4482's own single stated requirement) doesn't apply to it. No byte
    cap (invariant 4)."""
    payload: dict = {"media_type": media_type, "body": {"inline": content}}
    if description is not None:
        payload["description"] = description
    return payload


def apply_artifact_resolution(
    nodes: "list[dict]", project_root: Path, agent_name: str,
) -> "list[dict]":
    """#4482 PR-2b: the post-processing pass that turns a
    ``resolve_bindings``-produced ``"artifact"`` node's raw
    source/content/media_type/description STRINGS into the final
    OS-derived payload (:func:`build_source_artifact_payload` /
    :func:`build_inline_artifact_payload`).

    Deliberately NOT part of ``resolve_bindings`` itself — see that
    function's own "artifact" branch comment for why (lead-coder's
    ruling: ``replay.py``'s re-render must stay disk-state-independent,
    so this pass only runs in a layer that legitimately HAS
    ``project_root``/``agent_name`` — ``op_runtime/present.py``'s
    ``handle()``, not the pure binding-resolution layer).

    Every non-``"artifact"`` node passes through completely unchanged.
    An ``"artifact"`` node missing what it needs (both ``source`` AND
    ``content`` soft-missed at binding time, or ``content`` resolved but
    ``media_type`` didn't) is left as a bare ``{"component": "artifact"}``
    — present's own soft-skip philosophy, never a hard failure from a
    binding miss. A ``source`` file that has vanished since the agent
    referenced it (:func:`build_source_artifact_payload` raising
    ``FileNotFoundError``) becomes an explicit ``error`` marker rather
    than propagating an exception through op execution — the same shape
    :func:`reyn.data.workspace.artifact_ref.resolve_ref` already
    established for a GC'd/deleted target (missing is unresolvable, not
    a crash).
    """
    out: "list[dict]" = []
    for node in nodes:
        if node.get("component") != "artifact":
            out.append(node)
            continue
        description = node.get("description")
        if "source" in node:
            try:
                payload = build_source_artifact_payload(
                    project_root, agent_name, node["source"], description=description,
                )
            except FileNotFoundError:
                out.append({"component": "artifact", "error": "source_not_found"})
                continue
        elif "content" in node and "media_type" in node:
            payload = build_inline_artifact_payload(
                node["media_type"], node["content"], description=description,
            )
        else:
            out.append({"component": "artifact"})
            continue
        payload["component"] = "artifact"
        out.append(payload)
    return out
