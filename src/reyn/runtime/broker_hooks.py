"""Broker-participation hook derivation — #5084 ③-b.

Owner's own acceptance witness (relayed via architect, #5084's issue
thread): "write two ``profile.yaml``s, no slash command, and `--connect` x2
boots two separate trees/identities/REYN.md, each wired to the broker under
its own name." ``AgentProfile.broker_identity`` (#5084 ③-a, landed #5085)
gives each agent its own name; this module is what turns THAT field into
the two hooks a hand-authored broker-participating agent needs — the exact
shape architect/lead-coder measured from the real, running
``reyn-self/reyn.yaml`` (issuecomment on #5084):

- an ``mcp_resource_updated`` push hook, ``matcher: {server: broker, uri:
  "broker://inbox/<identity>"}`` — wakes the agent when a message arrives
  addressed to it;
- a ``session_start`` ``exec`` hook running ``register_with_broker.py`` —
  registers the identity with the broker MCP server at boot.

``None`` (absent ``broker_identity``, the default for every agent —
including the project's own ``default`` agent, owner's own words: it stays
admin-only) derives NO hooks at all — byte-identical to every pre-#5084
agent and to every agent that never opts into broker participation.

Deliberately narrow: this is NOT a general "derive hooks from arbitrary
profile fields" mechanism — ``broker_identity`` is the one #5084 ③-b field
architect ruled belongs here (issuecomment-5378968277: "the identity-
reading branch and the hooks derivation are the same code — splitting them
means touching this twice"). The synthesized ``exec`` argv is deliberately
RELATIVE (``register_with_broker.py``, no path prefix) — it resolves inside
the agent's own ``base_dir`` via ``cwd`` (#5084 ④, threaded through
``HookDispatcher``'s own ``hook_cwd`` callable), not a hardcoded path per
agent, and the child process reads its own identity back out of
``REYN_AGENT_NAME`` (#5084 ④'s ``HookProcessContext``) rather than it being
baked into argv (argv is static config — ``hooks/loader.py`` rejects a
templated argv item outright).
"""
from __future__ import annotations

_REGISTER_SCRIPT_NAME = "register_with_broker.py"


def derive_broker_hooks(broker_identity: "str | None") -> list[dict]:
    """The 2 synthesized hook defs for *broker_identity*, or ``[]`` if
    absent (no broker participation — the default)."""
    if not broker_identity:
        return []
    return [
        {
            "on": "mcp_resource_updated",
            "matcher": {"server": "broker", "uri": f"broker://inbox/{broker_identity}"},
            "template_push": {
                "message": (
                    f"broker に新着があります。"
                    f'`receive_messages(session_id="{broker_identity}")` で受け取り、'
                    f"内容に応じて対応してください。返信は "
                    f'`post_message(to=<相手>, from_session="{broker_identity}", ...)`。'
                ),
                "wake": True,
            },
        },
        {
            "on": "session_start",
            "network": True,
            # Relative argv (#5084 ④): resolves against THIS agent's own
            # base_dir via HookDispatcher's hook_cwd, never a hardcoded
            # per-agent path — see this module's own docstring.
            "exec": ["python3", _REGISTER_SCRIPT_NAME],
        },
    ]


__all__ = ["derive_broker_hooks"]
