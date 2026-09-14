"""#1652/②: cross-user-turn reasoning continuity — bundle normalize/re-attach
primitives, plus (#6179 stage ⑵) the spillability mapping that governs which
of those bundle fields the shrink flow may replace with a preview string.

The model's reasoning text (provider ``reasoning_content``, captured on
``LLMToolCallResult.reasoning``) is carried across chat user-turns NATIVELY
(#1652/②): ``RouterHistoryBuffer.attach_reasoning`` (router_history_buffer.py
:1145-1157) re-attaches the captured bundle onto the wire assistant message,
gated by continuity — NOT by a text section appended to the router system
prompt. litellm re-applies the bundle per provider; Reyn writes no
provider-specific logic. The live bound on its size is
``RouterHistoryBuffer._bound_wire_reasoning``, keyed off
``chat.reasoning.recent_turns``.

PAST FALSE CLAIM (do not restore): this docstring used to say reasoning "is
carried across chat user-turns by appending prior reasoning as a TEXT section
to the router system prompt" and that "the proxy tool-use path does NOT
require a native reasoning_content round-trip ... so no native within-loop
round-trip is needed for the gemini tier ... YAGNI on the proxy + gemini
reality". Both were true only before #1652/② landed native replay. That
PRIOR text-section-append mechanism (the router-system-prompt tail; this
module's own ``bound_reasoning``/``render_reasoning_section``,
``Session.reasoning_continuity_section``) was RETIRED by #1652/②'s native
re-attach above, then REMOVED entirely in #6182 after a #6178 census found 0
production callers of any of it — see git history for the retired shape if
ever needed. Reviving it on the belief that native replay is absent would
recreate the exact double-inject this module's bounding exists to prevent.

Anthropic/DeepSeek DIRECT-API note: those providers DO require the native
reasoning_content round-trip on the tool-use path (400 otherwise). litellm
auto-manages it when ``reasoning_content`` is present on the assistant message
(vertex/gemini transformation + anthropic factory read it) — reyn's gemini
tier now takes this same native path (#1652/②), not the proxy-specific text
section this docstring used to describe.
"""
from __future__ import annotations

#: #6179 stage ⑵ (lead-coder correction, owner "構造的設計であること確認して
#: よ"): the SINGLE source for both "which wire fields a reasoning bundle
#: may carry" AND "which of them may be safely REPLACED with a spill
#: preview string" (``RouterHistoryBuffer.spill_turn_content``,
#: ``router_loop_driver.py``'s ``_spill_batch_within_face``). A NEW field
#: cannot be added to the bundle without ALSO declaring its own
#: spillability here — there is no separate, un-annotated field-name list
#: to add it to instead (the #6162 "curated list drifts when it lives in 2
#: places" shape, closed structurally rather than by a completeness test
#: alone: `_REASONING_BUNDLE_FIELDS` below is DERIVED from this dict's own
#: keys, so the two can never diverge — a set-equality test would only
#: have CAUGHT drift after the fact; this makes the drift impossible to
#: write in the first place).
_REASONING_BUNDLE_SPILLABLE_FIELDS: "dict[str, bool]" = {
    "reasoning_content": True,
    #: plain text — a preview-string replacement reads honestly, the same
    #: shape ``content`` itself already uses.
    "thinking_blocks": False,
    #: Anthropic/DeepSeek DIRECT-API require the NATIVE round-trip on this
    #: exact field (400 otherwise — see this module's own docstring above).
    #: A preview string is not what the provider returned, and litellm has
    #: no way to round-trip a preview back into a valid `thinking_blocks`
    #: payload — replacing it would break the tool-use loop on those tiers.
    "provider_specific_fields": False,
    #: opaque, provider-defined payload — its shape is not ours to assume,
    #: so there is no safe way to replace it with a preview string either.
}

#: Wire fields a reasoning bundle may carry (the litellm cross-provider
#: standard). Re-attached verbatim to the assistant message so litellm
#: re-applies them per provider — Reyn writes NO provider-specific logic.
#: DERIVED from `_REASONING_BUNDLE_SPILLABLE_FIELDS` above (never a second,
#: independently-typed tuple) — see that mapping's own docstring for why.
_REASONING_BUNDLE_FIELDS = tuple(_REASONING_BUNDLE_SPILLABLE_FIELDS)


def as_reasoning_bundle(value: object) -> dict | None:
    """Normalize a persisted reasoning value to the bundle dict shape (#1652/②).

    Captured bundles are dicts ({reasoning_content?, thinking_blocks?, ...}).
    LEGACY persisted entries are a plain ``str`` (the old text-only #1652 shape)
    → absorbed as ``{"reasoning_content": str}``. Falsy / unrecognised → None.
    """
    if not value:
        return None
    if isinstance(value, str):
        return {"reasoning_content": value}
    if isinstance(value, dict):
        return value or None
    return None


def reasoning_text(value: object) -> str:
    """Extract the human-readable reasoning text from a bundle (or legacy str).

    Used by the display path, which only ever wants the text. Empty string when
    there is no text (e.g. a thinking_blocks-only bundle)."""
    bundle = as_reasoning_bundle(value)
    return (bundle or {}).get("reasoning_content", "") or ""


def attach_reasoning(msg: dict, value: object) -> None:
    """Attach a persisted reasoning bundle's fields onto a wire assistant dict
    (#1652/②) so litellm re-attaches the model's prior reasoning natively. No-op
    when there is no reasoning (omit-when-empty → byte-identical). Mutates ``msg``.
    """
    bundle = as_reasoning_bundle(value)
    if not bundle:
        return
    for field in _REASONING_BUNDLE_FIELDS:
        if bundle.get(field):
            msg[field] = bundle[field]
