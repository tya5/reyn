"""Tier 1: Contract — #3288 ③a capability-is-driver gate.

The streaming decision (``reyn.llm.llm._streaming_capable``) must be driven
by a litellm inline capability query, NEVER a hardcoded provider/model-name
check (owner design principle — no "Gemini doesn't stream" string literal).

Two independent proofs:

1. Structural: ``_streaming_capable``'s source contains no provider-name
   string literal (gemini/openai/anthropic/vertex/...) — a hardcoded branch
   would show up as a literal compared against ``model``.
2. Behavioral strip: neuter the underlying litellm capability query (a real
   function replacement — ``monkeypatch.setattr`` with a real callable, the
   allowed idiom, not a MagicMock) to a fixed value and show the branch
   selection FOLLOWS it — proving the decision genuinely reads the query
   result instead of e.g. always returning True/False regardless.
"""
from __future__ import annotations

import inspect
import re

import litellm
import litellm.utils as litellm_utils

from reyn.llm.llm import _streaming_capable

_PROVIDER_NAME_HINTS = (
    "gemini", "vertex", "openai", "anthropic", "claude", "bedrock", "azure",
)


def test_no_hardcoded_provider_name_in_capability_check() -> None:
    """Tier 1: strip — ``_streaming_capable``'s source names no provider."""
    src = inspect.getsource(_streaming_capable)
    # Strip the docstring (prose may legitimately discuss "Gemini" as
    # historical context) — only the executable body must be hardcode-free.
    body = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    lowered = body.lower()
    offenders = [name for name in _PROVIDER_NAME_HINTS if name in lowered]
    assert not offenders, (
        f"_streaming_capable's executable body names provider(s) {offenders} — "
        "the streaming decision must come from a litellm capability query, "
        "never a hardcoded provider/model-name check."
    )


def test_capability_query_drives_the_decision(monkeypatch) -> None:
    """Tier 1: behavioral strip — neuter the capability query to a fixed
    value and observe the branch selection change with it (RED would be:
    the decision stays the same regardless of what the query reports, i.e.
    the "query" is decorative and something else — a hardcode — actually
    decides)."""
    # gpt-4o-mini genuinely supports native streaming per litellm's own data
    # — confirm the baseline is True before neutering.
    assert _streaming_capable("gpt-4o-mini", has_tools=False) is True

    def _always_false(model: str, custom_llm_provider=None) -> bool:  # noqa: ANN001
        return False

    monkeypatch.setattr(litellm_utils, "supports_native_streaming", _always_false)
    assert _streaming_capable("gpt-4o-mini", has_tools=False) is False, (
        "neutering the capability query to False must flip the decision to "
        "False — if it stays True, something other than the query is driving "
        "the branch (a hardcode)."
    )


def test_capability_query_gates_tools_axis_too(monkeypatch) -> None:
    """Tier 1: the tools-present axis (supports_function_calling) is ALSO
    query-driven, not skipped/hardcoded. Neuter it to False and confirm a
    tools-bearing call is denied even though plain streaming is allowed."""
    assert _streaming_capable("gpt-4o-mini", has_tools=True) is True

    def _no_function_calling(model: str, custom_llm_provider=None) -> bool:  # noqa: ANN001
        return False

    monkeypatch.setattr(litellm, "supports_function_calling", _no_function_calling)
    assert _streaming_capable("gpt-4o-mini", has_tools=True) is False
    # Plain-text (no tools) is unaffected — the function-calling axis is only
    # consulted when tools are actually attached.
    assert _streaming_capable("gpt-4o-mini", has_tools=False) is True


def test_unknown_model_is_conservative_fallback() -> None:
    """Tier 1: an unmapped/unknown model → False (whole-collect fallback),
    never an optimistic guess."""
    assert _streaming_capable("totally-unknown-model-xyz-3288", has_tools=False) is False
    assert _streaming_capable("totally-unknown-model-xyz-3288", has_tools=True) is False


def test_reasoning_only_endpoint_denied_streaming() -> None:
    """Tier 1: a real, non-hardcoded litellm capability fact — o1-pro's
    model-info map entry says supports_native_streaming=False (litellm's own
    data, not reyn's) — must deny streaming."""
    assert _streaming_capable("o1-pro", has_tools=False) is False
