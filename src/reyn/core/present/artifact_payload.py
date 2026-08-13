"""#4482 PR-2: present payload construction for an "artifact" — an
LLM-produced file the terminal can't render natively (html/office/pdf/
images), which a user opens with the OS's own default app.

**① server/client payload shape** (architect's final corrected form, #4482
issue thread)::

    payload := {
      media_type  : <IANA, OPTIONAL, best-effort> (None -> "application/octet-stream")
      name        : <str>  required for a source-backed artifact, absent for inline
      description : <str, optional>
      body        : Inline | Reference
    }
    Inline    := {"inline": <str>}            # text-serialized only, never base64
    Reference := {"ref": <str>, "size": <int>}

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

    body: "dict | None" = None
    if size <= probe_max_bytes:
        probe = resolved.read_bytes()[:probe_max_bytes]
        try:
            inline_text = probe.decode("utf-8")
        except UnicodeDecodeError:
            body = None
        else:
            body = {"inline": inline_text}

    if body is None:
        ref = mint_ref(project_root, agent_name, resolved)
        body = {"ref": ref, "size": size}

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
