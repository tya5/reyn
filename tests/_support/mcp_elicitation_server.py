"""Real FastMCP server that ELICITS structured input from the client (#2597 slice ③).

Run directly as a subprocess (stdio) — never imported. Every tool below calls the
real ``fastmcp.Context.elicit`` API (SEP-1686 ``elicitation/create``), so the
elicitation round-trip these tests exercise is the genuine MCP protocol
exchange, not a hand-rolled fake.

Tools:
  - ``confirm(question)``       -> ``ctx.elicit(question, response_type=bool)`` —
                                    a single-field (scalar) schema; the client's
                                    handler answers via the yes/no bool-field
                                    prompt path. Returns the elicited bool as text
                                    (``"true"``/``"false"``), or ``"declined"`` /
                                    ``"cancelled"`` on those actions — lets a test
                                    assert on the SERVER's observed action, not
                                    just the client-side ElicitResult.
  - ``ask_credential(field)``   -> a ONE-field dataclass schema whose single field
                                    name is caller-supplied (a test passes e.g.
                                    ``"api_key"`` to exercise the sensitive-field
                                    warning path, or ``"comment"`` for the
                                    non-sensitive control case).
  - ``ask_multi()``             -> a THREE-field flat dataclass schema
                                    (``name: str``, ``count: int``, ``proceed:
                                    bool``) — exercises D1's sequential
                                    per-field prompting for a genuinely
                                    multi-field flat object.
"""
from __future__ import annotations

from dataclasses import dataclass, make_dataclass

from fastmcp import Context, FastMCP

mcp = FastMCP("reyn-test-elicitation")


def _render(result) -> str:
    if result.action == "accept":
        return str(result.data)
    return result.action  # "decline" | "cancel"


@mcp.tool()
async def confirm(question: str, ctx: Context) -> str:
    result = await ctx.elicit(question, response_type=bool)
    return _render(result)


@mcp.tool()
async def ask_credential(field_name: str, ctx: Context) -> str:
    # Build a one-field dataclass named after ``field_name`` at call time so a
    # single tool can drive both the sensitive-keyword path (field_name=
    # "api_key") and the non-sensitive control case (field_name="comment").
    schema = make_dataclass("OneField", [(field_name, str)])
    result = await ctx.elicit(
        f"Please provide {field_name}", response_type=schema,
    )
    if result.action != "accept":
        return result.action
    return str(getattr(result.data, field_name))


@dataclass
class _MultiField:
    name: str
    count: int
    proceed: bool


@mcp.tool()
async def ask_multi(ctx: Context) -> str:
    result = await ctx.elicit(
        "Fill in the multi-field form", response_type=_MultiField,
    )
    if result.action != "accept":
        return result.action
    d = result.data
    return f"{d.name}|{d.count}|{d.proceed}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
