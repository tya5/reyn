"""#2186: home-addressable task references.

A task lives in its assignee/executing session's ledger (per-session isolation). A
task reference is **home-addressable**: it encodes the home (assignee) session sid, so
any holder resolves which session-ledger the task lives in WITHOUT a lookup — the basis
for cross-session op routing AND the cross-ledger-edge detection that gates the R1
durability barrier (``home_sid(depends_on) != home_sid(task_id)`` is a pure
parse-and-compare).

The kind (task vs session vs external) is **self-identifying** from the reference form —
there is no stored ``requester_kind`` (removed in #2186): a task reference carries the
``task:`` marker; a bare session routing-key (``<transport>:<native>`` / ``main`` / a
spawned uuid) or an external ref does not.

Form: ``task:<pct(home_sid)>:<uuid>`` — the home sid is percent-encoded so it is a single
colon-free segment, making the three colon-parts unambiguous even when the sid is itself
a ``<transport>:<native_id>`` routing-key (its colons become ``%3A``). ``task`` is a
reserved marker, never a routing-key transport.
"""
from __future__ import annotations

import uuid
from urllib.parse import quote, unquote

_TASK_PREFIX = "task:"


def make_task_ref(home_sid: str) -> str:
    """A fresh home-addressable task reference rooted at ``home_sid`` (the assignee /
    executing session whose ledger the task lives in)."""
    return f"{_TASK_PREFIX}{quote(home_sid, safe='')}:{uuid.uuid4().hex}"


def is_task_ref(ref: "str | None") -> bool:
    """True iff ``ref`` is a task reference (vs a bare session routing-key / external
    ref). Self-identifying — replaces the removed ``requester_kind`` discriminator."""
    if not ref or not ref.startswith(_TASK_PREFIX):
        return False
    return len(ref.split(":")) == 3


def home_sid_of(ref: "str | None") -> "str | None":
    """The home (assignee) session sid that a task reference resolves to, or ``None`` if
    ``ref`` is not a task reference (a session / external ref has no task-ledger home)."""
    if not is_task_ref(ref):
        return None
    _marker, pct_sid, _uuid = ref.split(":")
    return unquote(pct_sid)
