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


class StructuredOutputError(AgentStepError):
    """Base for 0062 structured-output failures — a ``schema``-bearing
    ``run_agent_step`` invocation whose provider-constrained (``response_format``)
    answer turn (``RouterLoop``) could not produce a valid result. Subclasses
    distinguish the THREE distinct failure modes the proposal requires never
    be conflated (§2.1): unsupported model (pre-check, no call wasted),
    provider-rejected schema (fail fast, no re-prompt), and generation-side
    non-conformance (bounded re-prompt, then this). A subtype of
    ``AgentStepError`` so the existing pipeline-executor / caller handling
    (``except AgentStepError``) already covers it uniformly — no separate
    catch site needed."""


class StructuredOutputUnsupportedModelError(StructuredOutputError):
    """Failure mode (a): the resolved model does not support provider-side
    structured output (``litellm.supports_response_schema`` returned False).
    Raised BEFORE the turn's first LLM call (the pre-check runs ahead of the
    whole turn, tool calls included) so no completion is ever wasted on a
    model that cannot honor ``response_format`` at all."""


class StructuredOutputSchemaRejectedError(StructuredOutputError):
    """Failure mode (b): the provider's own json_schema-subset validation
    rejected the SCHEMA itself (a 400 on the constrained-generation call,
    reached only after the mode-(a) pre-check already passed). Re-prompting
    cannot fix an incompatible schema, so this is raised on the FIRST such
    failure — never entered into the mode-(c) re-prompt loop."""


class StructuredOutputNonConformingError(StructuredOutputError):
    """Failure mode (c): the model returned syntactically-valid-schema but
    semantically non-conforming JSON (or non-JSON text) after the bounded
    re-prompt budget (feed-the-validation-error-back, N small attempts) was
    exhausted."""

