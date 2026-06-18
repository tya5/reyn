"""reyn.runtime — the agent runtime and operational runtime concerns.

The packages and modules governing how a running OS instance executes turns and
is operated/constrained — distinct from the execution core (``reyn.core``),
persistence (``reyn.data``), surfaces (``reyn.interfaces``), and dev tooling
(``reyn.dev``):

- the **agent runtime**: the turn loop (``router_loop``), ``Session`` / agent
  lifecycle (``session``, ``registry``, ``agent``), multi-agent routing +
  transport (``routing``, ``transport``, ``outbox``, ``message_bus``, the
  ``*_routing`` modules), and planning (``planner``). ``Session`` /
  ``ChatMessage`` are re-exported here.
- ``budget``  — cost / token budget tracking
- ``limits``  — runtime limit handling (iteration / resource caps)
- ``cron``    — scheduling primitives

Note: ``reyn.runtime.cron`` is the scheduling PACKAGE; the ``reyn.tools.cron``
module (the cron tool handler, which persists to ``.reyn/cron.yaml``) is a
separate, unmoved module.

``Session`` / ``ChatMessage`` are re-exported LAZILY (PEP 562 ``__getattr__``):
importing ``reyn.runtime`` (e.g. as the parent package of
``reyn.runtime.budget``, which ``reyn.config`` imports at load) must NOT eagerly
load ``session`` — that pulls in ``reyn.config`` and closes a
``reyn.runtime`` ↔ ``reyn.config`` import cycle. The lazy re-export defers the
``session`` load until first attribute access (mirrors ``reyn/__init__``).
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import ChatMessage, Session

_LAZY_ATTRS = {"Session": ".session", "ChatMessage": ".session"}

__all__ = ["ChatMessage", "Session"]


def __getattr__(name: str):  # PEP 562 — lazy re-export (cycle-safe)
    target = _LAZY_ATTRS.get(name)
    if target is not None:
        import importlib
        return getattr(importlib.import_module(target, __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
