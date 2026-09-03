"""Sub-loop LLM-call memo-key computation (ADR-0025).

The stable args-hash that keys ``RouterLoop`` sub-loop memoization — used as
the chat-router fallback when the host provides no ``compute_memo_key``
(``router_loop.py``'s own ``getattr(self.host, "compute_memo_key", None)``
seam). A pure function over the inputs that drive deterministic LLM output;
no I/O, no persistence.

#5703 (doc-sync): this module docstring used to also name the phase-axis
consumer of that seam (``LLMCallRecorder.make_phase_memo_provider`` +
``PhaseRouterLoopHost.compute_memo_key``, both introduced by #1092). Neither
exists in this repo any more — the whole phase engine, including both, was
deleted by #2434/#2438's clean-break arc (same removal #5701 traced for the
sibling ``record_force_close`` seam). The chat-router fallback role this
module plays is unaffected either way — no host in this repo implements
``compute_memo_key`` today, so ``RouterLoop``'s own memo key is always this
module's output.
"""
from __future__ import annotations

import hashlib
import json


def compute_sub_loop_args_hash(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    tool_choice: str | dict | None,
    sampling: dict | None = None,
) -> str:
    """Stable hash for sub-loop ``call_llm_tools`` invocations.

    Hashes over the inputs that drive deterministic output. Mirrors
    ``dispatcher._compute_llm_args_hash`` shape (SHA-256 truncated to
    16 hex) but uses RouterLoop-shaped inputs (``messages`` list of
    role/content/tool_calls/tool_call_id dicts) rather than
    ContextFrame.

    No volatile-field stripping is applied: chat-router messages don't
    typically embed datetime fields. If the caller injects a volatile
    string into ``messages``, the resume-side memo will miss; the
    fresh-call path records the new hash and proceeds correctly
    (= same drift handling as R-D2).
    """
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools or [],
        "tool_choice": tool_choice,
        "sampling": sampling or {},
    }
    try:
        canonical = json.dumps(
            payload, sort_keys=True, default=str, ensure_ascii=False,
        )
    except Exception:  # noqa: BLE001 — fallback for unhashable values
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
