"""reyn.runtime.errors — exception types raised by the agent runtime.

Runtime-level exceptions that the turn loop raises and its handlers catch to
surface a structured fallback to the user / requester. Pure exception types —
no dependency on ``Session`` (the runtime raises and catches these).
"""
from __future__ import annotations


class RouterCapExceeded(Exception):
    """Raised when a user turn (or top-level agent_request) drives more
    router invocations than the configured cap. Caught by handlers,
    which surface a structured fallback reply to the user / requester.

    FP-0004: ``hint_config_key`` is the user-facing config knob to raise
    when an operator decides the cap is too tight for their workload.
    """

    hint_config_key: str = "safety.loop.max_router_calls_per_turn"

    def __init__(self, count: int, cap: int, last_reason: str = "") -> None:
        super().__init__(
            f"Router exhausted retry budget ({count}/{cap}) for this turn. "
            f"→ Raise {RouterCapExceeded.hint_config_key} to allow more "
            f"router invocations per turn (0 = unlimited)."
        )
        self.count = count
        self.cap = cap
        self.last_reason = last_reason


class AgentStepError(Exception):
    """Raised by ``session_api.run_agent_step`` (R5: agent-step run+collect).

    Covers every way the collected output of a spawned ephemeral session's
    turn fails to become the caller's requested result: the spawn's session
    id resolved to no live ``Session`` (mis-wired registry), the collected
    text is not valid JSON under a declared ``schema``, or the parsed JSON
    fails ``core.pipeline.schema.validate`` against that schema. In the
    eventual Pipeline executor this is an ordinary step failure (→ the
    step's retry/error path), not a construction-time / programming error.
    """

