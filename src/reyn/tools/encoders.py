"""Encoders — the per-transport half of the tool-use presentation seam.

An ``Exposure`` (``reyn.tools.exposure``) says *what* a scheme shows the LLM.
An **encoder** says *how* one ``Transport`` writes that down: ``tool_calls``
renders the descriptors into the OpenAI ``tools=`` payload and builds the
positional tool-use system-prompt slot-map; ``content_fence`` advertises no
``tools=`` at all and renders the descriptors as a Python code-API the model
calls by identifier.

``encode_tools`` therefore returns a ``ToolsChannel`` (``reyn.tools.scheme``),
not a list: the two transports disagree about whether the channel EXISTS, which
is a different question from how many tools are in it, and an empty list could
only answer the second (#3421).

**One encoder per transport, not one per cell.** A per-cell composer would
rebuild ``_VALID_SCHEME_TRANSPORT_PAIRS`` implicitly, one decision point per
cell — the thing the pair table exists to make explicit.

**Capability is declared, not inferred from silence.** Each encoder names the
descriptor kinds it can encode, and refuses anything else with a legible error
instead of dropping it. "No exception was raised" is not evidence that a cell is
encodable; agreement with the declaration is. The ``content_fence`` encoder
therefore cannot encode a ``provider_native`` descriptor at all: a provider's
own meta-tool has no rendering as a Python function signature, and quietly
omitting it would remove a capability the operator configured.

The ``tool_calls`` slot-map and the ``content_fence`` identifier map both live
here because both change when the transport changes. The identifier map has a
second reason: it must be the **same** mapping ``CodeActScheme.execute`` injects
as sandbox stub names, so the identifier the model reads in the code-API always
matches a stub. ``build_actions_map`` is the single function both call.
"""
from __future__ import annotations

import keyword
import re
from typing import Any

from reyn.prompt.codeact import CODEACT_STATIC_HEADER
from reyn.tools.exposure import (
    DESCRIPTOR_KIND_FUNCTION,
    DESCRIPTOR_KIND_PROVIDER_NATIVE,
    Exposure,
    FunctionDescriptor,
)
from reyn.tools.scheme import AdvertisedTools, NoToolsChannel
from reyn.tools.transport import Transport


class UnencodableExposure(ValueError):
    """An exposure carries something the target transport did not declare it can
    encode. Raised rather than silently narrowing the exposed set — a capability
    that disappears without a word is the failure mode the declaration exists to
    prevent."""


# ── identifier mapping (shared with CodeActScheme.execute) ───────────────────


def sanitize_identifier(name: str) -> str:
    """An action name → a valid, CALLABLE Python identifier for the code-API.

    Most names (``read_file``, ``web_search``) already are; MCP names with
    hyphens or dots (``web-search.query``) are not — non-identifier chars
    become ``_``, a leading digit is prefixed, and a Python keyword is suffixed.
    The REAL action name is preserved in the actions map and is what the parent
    gate receives — the identifier is only the LLM-facing Python name.

    A name that collides with a BANNED builtin is suffixed for the same reason
    a keyword is, and it is not hypothetical: #3429 renamed the sandboxed-exec
    action from ``exec__run`` to ``exec``, and safe mode rejects the AST Name
    ``exec`` outright (dynamic code — it bypasses the import allowlist). Left
    unsuffixed, the code-API would render ``def exec(argv)`` and every snippet
    calling it would be refused before dispatch, i.e. the tool would be
    advertised and uncallable from CodeAct. Only the BANNED set needs this:
    shadowing an ordinary builtin inside the harness namespace is harmless,
    while shadowing a banned one is a hard refusal."""
    from reyn.core.kernel._python_allowlist import BANNED_BUILTINS

    s = re.sub(r"\W", "_", name)
    if not s or s[0].isdigit():
        s = "_" + s
    if keyword.iskeyword(s) or s in BANNED_BUILTINS:
        s = s + "_"
    return s


def build_actions_map(action_names: "list[str]") -> "dict[str, str]":
    """``{python_identifier: action_name}`` for the direct-function code-API.

    DETERMINISTIC (sorted) with collision-disambiguation (``_2`` / ``_3`` …) so
    that the code-API render and ``CodeActScheme.execute`` (which builds the
    harness stubs) compute the **identical** map over the same full dispatchable
    name set — the model's identifier call always matches a stub, and the stub
    marshals the real action name to the parent gate.

    The input is DEDUPLICATED first. Disambiguation exists for two DIFFERENT
    names that sanitize to one identifier; a name that simply appears twice in
    the dispatchable population is one action, and suffixing it would render the
    same operation as two functions. The composition that produces such a repeat
    (``base_tools`` + the flat catalog, which name the same tools) only became
    possible when #3429 gave every operation one name — before it, the two rows
    carried two different names and no repeat could occur here."""
    out: "dict[str, str]" = {}
    used: set[str] = set()
    for qn in sorted(set(action_names)):
        base = sanitize_identifier(qn)
        ident, n = base, 2
        while ident in used:
            ident, n = f"{base}_{n}", n + 1
        used.add(ident)
        out[ident] = qn
    return out


def render_code_api(entries: "list[dict]", ident_by_qn: "dict[str, str]") -> str:
    """Render flat ``{name, description, parameters}`` entries as a CodeAct
    *code-API* — DIRECT function signatures the model calls by name
    (``read_file(path=...)``), NOT a ``tool('name', ...)`` string-proxy. The
    function name is a selected Python identifier (``ident_by_qn[qualified]``),
    so the action name can never be a hallucinated produced string. Pure
    presentation — the model reads these signatures and writes the calls; the OS
    injects gated stubs of the same names into the sandbox namespace (each
    marshals to the parent gate).

    This is the SOLE tool-use instruction the model sees (``Presentation.
    tool_use_sp`` — the OS drops the universal invoke_action / list_actions vocab
    for this region), so it carries the whole CodeAct contract: act = a single
    fenced python block; prose = the terminal final answer (the loop-unify
    contract — prose ends the turn)."""
    lines = list(CODEACT_STATIC_HEADER)
    for entry in entries:
        name = entry.get("name", "")
        ident = ident_by_qn.get(name, sanitize_identifier(name))
        params = entry.get("parameters") or {}
        arg_names = list((params.get("properties") or {}).keys())
        sig = ", ".join(arg_names)
        desc_raw = (entry.get("description") or "").strip()
        desc = desc_raw.splitlines()[0] if desc_raw else ""
        # A direct function signature — the model calls `ident(args)`. No quoted
        # `tool('<x>')` token anywhere in the SP (#1638: that bare token caused
        # ~100% empty choices on gemini-2.5-flash-lite; the direct-call form
        # removes it entirely — the model writes an identifier call, not a
        # quoted string).
        line = f"- `def {ident}({sig})`"
        lines.append(f"{line} — {desc}" if desc else line)
    return "\n".join(lines)


# ── the encoders ─────────────────────────────────────────────────────────────


def _assert_encodable(encoder: Any, exposure: Exposure) -> None:
    """Fail-closed capability check, run on every encode.

    Vacuity is not a pass: an exposure carrying no descriptor at all still has
    to satisfy the slot-override arm below, and a descriptor kind absent from
    the declaration raises here rather than being filtered out upstream."""
    for descriptor in exposure.descriptors:
        if descriptor.kind not in encoder.encodable_descriptor_kinds:
            raise UnencodableExposure(
                f"{encoder.transport.value} cannot encode a "
                f"{descriptor.kind!r} descriptor; this transport declares "
                f"{sorted(encoder.encodable_descriptor_kinds)}. Refusing rather "
                "than dropping it — the entry would vanish from the LLM's "
                "surface with no error."
            )
    if exposure.sp_slot_overrides and not encoder.encodes_sp_slot_overrides:
        raise UnencodableExposure(
            f"{encoder.transport.value} has no positional prompt slots, so the "
            f"scheme-owned override(s) {sorted(exposure.sp_slot_overrides)} "
            "have nowhere to go. Refusing rather than dropping them."
        )


class ToolCallsEncoder:
    """``Transport.TOOL_CALLS``: descriptors → the OpenAI ``tools=`` payload,
    facts → the positional tool-use slot-map."""

    transport = Transport.TOOL_CALLS
    encodable_descriptor_kinds = frozenset(
        {DESCRIPTOR_KIND_FUNCTION, DESCRIPTOR_KIND_PROVIDER_NATIVE}
    )
    encodes_sp_slot_overrides = True

    def encode_tools(self, exposure: Exposure) -> AdvertisedTools:
        _assert_encodable(self, exposure)
        # Always the ``AdvertisedTools`` arm, INCLUDING when the exposure carries
        # no descriptor: this transport does have a ``tools=`` channel, and an
        # exposure narrowed down to nothing means the channel is empty — a state
        # the other arm does not describe (#3421).
        return AdvertisedTools(entries=[d.as_tool_calls_entry() for d in exposure.descriptors])

    def encode_tool_use_sp(self, exposure: Exposure) -> "dict[str, str]":
        _assert_encodable(self, exposure)
        # Imported at call time: the slot builder lives under
        # ``reyn.tools.schemes``, whose package ``__init__`` imports every
        # scheme module, and a scheme module imports this one.
        from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

        slots = build_universal_tool_use_slots(**exposure.sp_facts)
        slots.update(exposure.sp_slot_overrides)
        return slots


class ContentFenceEncoder:
    """``Transport.CONTENT_FENCE``: no ``tools=`` payload at all; the descriptors
    become a Python code-API in the tool-use system prompt, and the model
    expresses its chosen action by writing a fenced snippet that calls one."""

    transport = Transport.CONTENT_FENCE
    encodable_descriptor_kinds = frozenset({DESCRIPTOR_KIND_FUNCTION})
    encodes_sp_slot_overrides = False

    def encode_tools(self, exposure: Exposure) -> NoToolsChannel:
        _assert_encodable(self, exposure)
        # Not "there are no tools" — this transport has no ``tools=`` channel.
        # The whole tool-use surface is the code-API below. #3421 moved that
        # sentence out of this comment and into the return TYPE, so a consumer
        # can act on it instead of only reading about it here.
        return NoToolsChannel()

    def encode_tool_use_sp(self, exposure: Exposure) -> str:
        _assert_encodable(self, exposure)
        # The map is built over the FULL dispatchable name set, not over the
        # exposed subset, so it is identical to the one ``CodeActScheme.execute``
        # builds for the sandbox stubs even when narrowing hides a row.
        ident_by_qn = {
            qn: ident
            for ident, qn in build_actions_map(list(exposure.dispatchable_names)).items()
        }
        flat = [
            {
                "name": d.name,
                "description": d.description,
                "parameters": d.parameters,
            }
            for d in exposure.descriptors
            if isinstance(d, FunctionDescriptor)
        ]
        return render_code_api(flat, ident_by_qn)


# The encoder capability registry. Distinct from
# ``transport._VALID_SCHEME_TRANSPORT_PAIRS``, which stays the fail-closed
# authority on which (scheme, transport) CELLS resolve: this table says what a
# transport can *encode*, that one says which cells exist. Both are declarations;
# neither infers validity from an absent exception.
_ENCODERS: "dict[Transport, Any]" = {
    Transport.TOOL_CALLS: ToolCallsEncoder(),
    Transport.CONTENT_FENCE: ContentFenceEncoder(),
}


def encoder_for_transport(transport: Transport) -> Any:
    """The encoder implementing ``transport`` — fail-closed on an unregistered
    one, mirroring ``resolve_scheme_for_transport``."""
    try:
        return _ENCODERS[transport]
    except KeyError:
        raise ValueError(
            f"no encoder registered for transport {transport!r}; registered: "
            + ", ".join(sorted(t.value for t in _ENCODERS))
        ) from None


def encodable_descriptor_kinds(transport: Transport) -> frozenset:
    """The descriptor kinds ``transport`` declares it can encode (introspection
    / conformance tests)."""
    return encoder_for_transport(transport).encodable_descriptor_kinds


__all__ = [
    "ContentFenceEncoder",
    "ToolCallsEncoder",
    "UnencodableExposure",
    "build_actions_map",
    "encodable_descriptor_kinds",
    "encoder_for_transport",
    "render_code_api",
    "sanitize_identifier",
]
