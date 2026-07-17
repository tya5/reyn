"""reyn.hooks.sandbox_scope — which agent-level sandbox fields reach a hook
shell, and through which per-hook key (#3005).

Why this module exists
----------------------
A hook shell's sandbox is scoped **per hook**, not by the agent-level
``reyn.yaml sandbox.policy``. That policy is resolved by
``resolve_sandbox_policy`` and reaches only the op path
(``runtime/router_op_context.py``); the hook-shell path never calls it. The
choice is structural rather than accidental — a hook is a small, declarative
reaction to a lifecycle event, so "no network, no fork, no writes" is the right
*floor* for it even in a run whose ops are deliberately unsandboxed. A hook that
needs more says so at its own site.

The defect that motivated this module was not the scoping — it was the
**silence**. An operator who wrote ``sandbox.policy: {network: true}`` got a
hook shell with ``network=False`` and no signal of any kind: not an error, not a
warning, not an audit-event. Their expressed will was neither applied nor
refused; it was dropped. A security field that is silently ignored reads as an
applied restriction that was never applied (#2976/#3003), and here it read as an
applied *grant* that was never granted.

So this module owns the vocabulary of the boundary:

* :data:`HOOK_SANDBOX_SCOPE` — the agent-level policy field ↔ per-hook key
  mapping. Each pair says "this field does not cross the boundary globally, but
  the operator can express it here instead."
* :func:`unapplied_policy_fields` — a pure function (no I/O, no events, the
  ``deny_narrowed_write_grants`` shape) naming the fields an operator wrote
  globally that this hook did not re-declare, i.e. exactly the set that would
  otherwise be dropped in silence.
* :func:`unapplied_policy_message` — the decision-enabling sentence for one such
  field: what was written, what actually applied, and the two concrete moves
  that resolve it (grant it here, or pin the floor here and silence this).

The mapping is deliberately the same triad an operator already has on a stdio
MCP server (``network`` / ``subprocess`` / ``write_paths``): the per-site,
operator-owned sandbox surface. Fields outside it (``read_deny_paths``,
``read_paths``, ``env_passthrough``, ``timeout_seconds``) are not part of that
surface on either side of the boundary, so they are not reported here —
``read_deny_paths`` in particular is *supplied* to hook shells by
``SandboxPolicy``'s own default factory, so it is not a hole (measured in
#3003, restated in #3005).

Declaring the per-hook key — with **either** value — is what makes the
operator's will explicit at the site that consumes it, which is why a declared
key removes the field from the report even when it *contradicts* the global
value. That is the point: contradiction is a decision, silence is not.
"""
from __future__ import annotations

from typing import Mapping

# Agent-level ``sandbox.policy`` field → the per-hook key that reaches a hook
# shell's sandbox for the same axis. The three axes an operator owns per-site
# (the same triad a stdio MCP server exposes). A field absent from this map has
# no per-hook equivalent and is not part of the per-site sandbox surface.
HOOK_SANDBOX_SCOPE: tuple[tuple[str, str], ...] = (
    ("network", "network"),
    ("allow_subprocess", "subprocess"),
    ("write_paths", "write_paths"),
)


def unapplied_policy_fields(
    config_policy: "dict | None",
    declared: "Mapping[str, object | None]",
) -> list[tuple[str, str]]:
    """Return ``(policy_field, hook_key)`` pairs the operator wrote in the
    agent-level ``sandbox.policy`` that do NOT reach this hook shell.

    Pure function of its two arguments — no I/O, no events — so the caller
    (``run_shell_hook``) owns the reporting and this stays trivially testable.
    Mirrors :func:`~reyn.security.sandbox.policy.deny_narrowed_write_grants`,
    which plays the same role for the op path's deny-vs-grant narrowing.

    Parameters
    ----------
    config_policy:
        The operator's raw ``reyn.yaml sandbox.policy`` mapping (``SandboxConfig
        .policy``), or ``None`` when they declared none. Key **presence** is the
        test for "the operator wrote this" — the same dict-key-presence
        semantics ``resolve_sandbox_policy`` uses to tell an explicit
        ``write_paths: []`` from an omitted one (#2964).
    declared:
        The per-hook keys this hook declared, keyed by hook-key name (the right
        column of :data:`HOOK_SANDBOX_SCOPE`); ``None`` = the operator omitted
        it = the hook keeps the floor. A key missing from the mapping is treated
        as omitted.

    Returns
    -------
    list[tuple[str, str]]
        One pair per axis where the operator expressed a global will and left
        the hook site silent, in :data:`HOOK_SANDBOX_SCOPE` order. Empty when
        the operator declared no policy, or re-declared every affected axis on
        the hook (either way — an explicit per-hook value is a decision, so
        there is nothing silent left to report).
    """
    if not config_policy:
        return []
    return [
        (policy_field, hook_key)
        for policy_field, hook_key in HOOK_SANDBOX_SCOPE
        if policy_field in config_policy and declared.get(hook_key) is None
    ]


def unapplied_policy_message(
    *,
    hook_label: str,
    policy_field: str,
    hook_key: str,
    configured: object,
    effective: object,
) -> str:
    """Build the decision-enabling sentence for one unapplied policy field.

    Decision-enabling means it names the **next move**, not just the fault: an
    operator who reads it should be able to act without reading the source. So
    it states the cause (what they wrote), the observed effect (what the hook
    actually ran with), and the two concrete resolutions — grant the axis at the
    hook site, or pin the floor at the hook site (which is also how the message
    is silenced, because an explicit value is a decision).
    """
    return (
        f"reyn.yaml sandbox.policy declares {policy_field}={configured!r}, but the "
        f"agent-level sandbox policy does NOT apply to shell hooks — a hook shell's "
        f"sandbox is scoped per-hook. Hook {hook_label!r} ran with "
        f"{policy_field}={effective!r} (the hook floor). To grant it here, set "
        f"`{hook_key}:` on this hook to the value you want; to keep the floor and "
        f"silence this, set `{hook_key}:` on this hook to the floor value "
        f"explicitly. Either way the hook site — not sandbox.policy — is where a "
        f"hook shell's {policy_field} is decided."
    )
