"""SkillOutboundMessage — a skill's outbound message, transport-neutral (#1794).

A skill emits status / progress / result messages during a spawn. This is the
layer-neutral record it produces: ``kind`` + ``text`` + ``meta`` only, with no
transport coupling. The runtime boundary (the ``put_outbox`` adapter wired on
``Session``) converts it to the runtime's ``OutboxMessage`` (which carries the
transport ``reply_to``) when enqueuing — so ``reyn.skill`` stays free of the
runtime/transport ``OutboxMessage`` + ``TransportRef`` VOs (the #1794 layer
direction). A skill never sets ``reply_to``; the adapter supplies ``None``,
which is behavior-identical to the prior direct ``OutboxMessage`` constructs.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillOutboundMessage:
    """A transport-neutral outbound message produced inside a skill spawn."""

    kind: str
    text: str
    meta: dict = field(default_factory=dict)
