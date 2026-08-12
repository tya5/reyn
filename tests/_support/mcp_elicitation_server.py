"""Real FastMCP-shaped server that ELICITS structured input from the client (#2597 slice ③).

Run directly as a subprocess (stdio) — never imported. Every tool below calls a
real elicitation API (SEP-1686 ``elicitation/create``), so the elicitation
round-trip these tests exercise is the genuine MCP protocol exchange, not a
hand-rolled fake.

#4302: ported from the standalone ``fastmcp`` package to the official ``mcp``
SDK's own bundled server framework — ``mcp.server.fastmcp`` on the 1.x line
this file originally shipped against; #4412 (arc #4368) later bumped the
pin to ``mcp>=2.0,<3.0`` and renamed the import to
``mcp.server.mcpserver``/``MCPServer`` (see below), same decorator API.
One real capability gap, not just an ergonomic one, holds on both lines:
``Context.elicit()`` validates its ``schema`` and REJECTS an enum/
``Literal`` field (only ``str``/``int``/``float``/``bool`` are accepted —
``mcp/server/elicitation.py``'s ``_is_primitive_field``, an actual
restriction absent from standalone fastmcp, not merely a different
ergonomic; verified live against the installed mcp 1.29.0). ``pick()`` below
works around it by calling ``ctx.session.elicit_form()`` directly — the raw
primitive under ``Context.elicit()``, which sends a hand-built JSON schema
over the wire with no such restriction — instead of ``ctx.elicit()``.

Tools:
  - ``confirm(question)``       -> ``ctx.elicit(question, schema=_BoolValue)`` —
                                    a single-field (scalar) schema; the client's
                                    handler answers via the yes/no bool-field
                                    prompt path. Returns the elicited bool as text
                                    (``"true"``/``"false"``), or ``"declined"`` /
                                    ``"cancelled"`` on those actions — lets a test
                                    assert on the SERVER's observed action, not
                                    just the client-side ElicitResult.
  - ``ask_credential(field)``   -> a ONE-field pydantic model schema whose single
                                    field name is caller-supplied (a test passes
                                    e.g. ``"api_key"`` to exercise the sensitive-
                                    field warning path, or ``"comment"`` for the
                                    non-sensitive control case).
  - ``pick(question)``          -> a single (scalar) ENUM-schema field (raw
                                    ``session.elicit_form`` — see the module
                                    docstring above); the sibling of ``confirm``
                                    in the single-closed-set-field class
                                    (#2622). Returns the elicited value as
                                    text, or ``"decline"`` / ``"cancel"``.
  - ``ask_multi()``             -> a THREE-field flat pydantic-model schema
                                    (``name: str``, ``count: int``, ``proceed:
                                    bool``) — exercises D1's sequential
                                    per-field prompting for a genuinely
                                    multi-field flat object.
"""
from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, create_model

mcp = MCPServer("reyn-test-elicitation")


class _BoolValue(BaseModel):
    value: bool


def _render_scalar(result) -> str:
    # #4302: standalone fastmcp auto-deconstructs a wrapped primitive back to
    # the bare scalar (``result.data`` was the raw ``True``/``False``); the
    # bundled SDK's ``Context.elicit()`` always returns the full model
    # instance, so unwrap the single ``value`` field ourselves.
    if result.action == "accept":
        return str(result.data.value)
    return result.action  # "decline" | "cancel"


@mcp.tool()
async def pick(question: str, ctx: Context) -> str:
    # Bypasses Context.elicit()'s primitive-only schema validation (see module
    # docstring) — the raw session primitive Context.elicit() sits on top of,
    # with a hand-built JSON schema carrying the real enum.
    schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string", "enum": ["red", "green", "blue"]},
        },
        "required": ["value"],
    }
    result = await ctx.session.elicit_form(
        message=question, requested_schema=schema, related_request_id=ctx.request_id,
    )
    if result.action != "accept":
        return result.action  # "decline" | "cancel"
    return str((result.content or {}).get("value"))


@mcp.tool()
async def confirm(question: str, ctx: Context) -> str:
    result = await ctx.elicit(question, schema=_BoolValue)
    return _render_scalar(result)


@mcp.tool()
async def ask_credential(field_name: str, ctx: Context) -> str:
    # Build a one-field pydantic model named after ``field_name`` at call time
    # so a single tool can drive both the sensitive-keyword path (field_name=
    # "api_key") and the non-sensitive control case (field_name="comment").
    schema = create_model("OneField", **{field_name: (str, ...)})
    result = await ctx.elicit(f"Please provide {field_name}", schema=schema)
    if result.action != "accept":
        return result.action
    return str(getattr(result.data, field_name))


class _MultiField(BaseModel):
    name: str
    count: int
    proceed: bool


@mcp.tool()
async def ask_multi(ctx: Context) -> str:
    result = await ctx.elicit("Fill in the multi-field form", schema=_MultiField)
    if result.action != "accept":
        return result.action
    d = result.data
    return f"{d.name}|{d.count}|{d.proceed}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
