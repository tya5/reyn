"""Plugin source ``kind`` vocabulary + name-collision precedence (ADR 0064 §3.8/§3.10).

The ADR's typed ``plugin_install`` op (P2+) takes a discriminated ``source``
of ``kind in {"builtin", "local", "git"}`` — reyn's own shipped plugin, a
local directory the LLM authored/tested, or a remote git URL, in that
increasing order of RCE trust risk (§3.10: "builtin ≤ local ≪ git/remote").

P1 defines the shared ``kind`` vocabulary and the **name-collision
precedence** this ordering also settles: when two plugin sources declare the
same ``PluginManifest.name``, ``builtin`` wins over ``local`` wins over
``git`` — the lower-trust-risk source never silently shadows a
higher-trust-risk one. This is a schema/resolution-layer definition only;
the P2 install op is what actually calls it during a real name collision.
"""
from __future__ import annotations

from typing import Literal

PluginSourceKind = Literal["builtin", "local", "git"]

# Index = precedence rank, lower wins a name collision. Mirrors the RCE
# trust-risk ordering in ADR 0064 §3.10 ("builtin <= local << git/remote"):
# the shipped-with-reyn plugin is the most trusted, so it always wins a
# same-name collision over anything the operator installed themselves.
PLUGIN_SOURCE_PRECEDENCE: tuple[PluginSourceKind, ...] = ("builtin", "local", "git")

_RANK: dict[PluginSourceKind, int] = {
    kind: rank for rank, kind in enumerate(PLUGIN_SOURCE_PRECEDENCE)
}


def resolve_name_collision(candidates: "list[PluginSourceKind]") -> PluginSourceKind:
    """Given the ``kind``s of every plugin source declaring the same name,
    return the one that wins (ADR 0064 §3.8: "builtin priority" on collision).

    Raises ``ValueError`` on an empty list — a collision needs >= 1 candidate,
    and the caller should not invoke this for a name with a single source.
    """
    if not candidates:
        raise ValueError("resolve_name_collision requires at least one candidate kind")
    return min(candidates, key=lambda kind: _RANK[kind])
