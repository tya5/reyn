"""OutboxMessage — structured payload for ChatSession's display stream.

Replaces the previous (kind, text) tuple. Provenance fields (run_id,
skill_name, intervention_id, …) live in `meta: dict` rather than as fixed
attributes, so future additions (e.g. `agent_id` for multi-agent sessions)
don't require dataclass schema changes. This mirrors the `ChatMessage.meta`
convention already used for history entries.

Outbox is the **presentation stream**, distinct from history (durable log).
- agent / skill_done → also persisted to history.jsonl by ChatSession
- status / error / trace / intervention → display-only, never in history
- __end__ → control signal for _output_loop shutdown
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OutboxMessage:
    """One item published by ChatSession to its outbox queue.

    `kind` selects the renderer's formatting branch. `meta` carries
    optional provenance:

    Common keys:
      run_id           full chat-side run id (e.g. "20260501T...Z_skill_foo_abcd")
      run_id_short     trailing 4 chars of run_id, used in display prefix
      skill_name       human-friendly skill name for [skill#abcd] prefix
      intervention_id  for kind="intervention", which UI is being announced

    Future keys (multi-agent):
      agent_id         which agent emitted this message
    """
    kind: str
    text: str
    meta: dict = field(default_factory=dict)


__all__ = ["OutboxMessage"]
