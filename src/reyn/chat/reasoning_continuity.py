"""#1652: cross-user-turn reasoning continuity — bounding + render primitives.

The model's reasoning text (provider ``reasoning_content``, captured on
``LLMToolCallResult.reasoning``) is carried across chat user-turns by appending
prior reasoning as a TEXT section to the router system prompt — the same
provider-agnostic mechanism as the phase-side ``act_turn_reasoning`` (#1212).
Verified (live, gemini-via-proxy): text-section continuity is the reliable
efficacy guarantee (the model sees prior reasoning in-prompt), and the proxy
tool-use path does NOT require a native reasoning_content round-trip (no 400),
so no native within-loop round-trip is needed for the gemini tier.

These are the config-DEFAULT-independent primitives (the bounding knob exists
regardless of its default value; the render format is fixed). The capture →
persist → gated replay → UI wiring + the config schema land on top of these.

Anthropic/DeepSeek DIRECT-API note: those providers DO require the native
reasoning_content round-trip on the tool-use path (400 otherwise). litellm
auto-manages it when ``reasoning_content`` is present on the assistant message
(vertex/gemini transformation + anthropic factory read it). If such a tier is
ever adopted, include the prior assistant turn's reasoning on the reconstructed
assistant message in the tool loop; litellm does the rest. YAGNI on the proxy +
gemini reality (#1652).
"""
from __future__ import annotations

#: ``keep_recent`` values <= 0 mean "unbounded — keep all reasoning". The config
#: knob exposes this as the unbounded sentinel; a positive N bounds to the most
#: recent N entries. (Default value is set by the config layer, not here.)
UNBOUNDED = 0

_REASONING_CONTINUITY_HEADER = "━━━ prior_reasoning ━━━"

#: Wire fields a reasoning bundle may carry (the litellm cross-provider standard).
#: Re-attached verbatim to the assistant message so litellm re-applies them per
#: provider — Reyn writes NO provider-specific logic.
_REASONING_BUNDLE_FIELDS = (
    "reasoning_content",
    "thinking_blocks",
    "provider_specific_fields",
)


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


def bound_reasoning(items: list[str], keep_recent: int) -> list[str]:
    """Return the reasoning entries to replay, bounded to the most recent
    ``keep_recent`` (mirrors act_turn_reasoning's ``[-keep:]``, #1212).

    ``keep_recent <= 0`` (= :data:`UNBOUNDED`) keeps all entries — the
    'always-send-all' option. A positive N keeps the last N. Bounding matters on
    gemini specifically: there is no provider auto-filter (that is an
    Anthropic-native primitive), so reasoning accumulates and is billed in full
    unless we bound it.
    """
    if keep_recent <= UNBOUNDED:
        return list(items)
    return items[-keep_recent:]


def render_reasoning_section(items: list[str]) -> str:
    """Render the prior-reasoning text section appended to the router system
    prompt, or ``""`` when there is nothing to carry.

    Empty → empty string so the system prompt is byte-identical to the
    no-continuity shape (keeps LLMReplay fixtures valid — same omit-when-empty
    discipline as #1212's act_turn_reasoning section). Most recent last.
    """
    if not items:
        return ""
    body = "\n\n".join(items)
    return (
        f"\n\n{_REASONING_CONTINUITY_HEADER}\n"
        "- This is YOUR OWN reasoning from previous turns in this conversation "
        "(most recent last), carried forward so you keep a continuous line of "
        "thought. Use it to avoid re-deriving what you already worked out; it is "
        f"context, not an instruction.\n\n{body}"
    )
