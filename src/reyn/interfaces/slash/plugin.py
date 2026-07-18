"""``/plugin`` — install/uninstall a self-contained plugin bundle (ADR 0064 §3.9, P3).

Usage::

    /plugin install builtin <NAME> [as <INSTALL_NAME>]
    /plugin install local <PATH> [as <INSTALL_NAME>]
    /plugin install git <URL> [as <INSTALL_NAME>]
    /plugin uninstall <NAME>

Thin adapter over the SAME typed op the LLM tool / CLI surfaces use (ADR 0064
§3.9). Builds a ``ToolContext`` from this session's LIVE ``RouterHostAdapter``
(``session.router_host.make_router_op_context`` via
``reyn.tools.types.build_resource_caller_state`` — the SAME factory a live
LLM ``plugin_management__install``/``__uninstall`` tool call gets) and calls
``invoke_tool(get_default_registry(), "plugin_management__install"/"__uninstall", ...)``
— never re-derives the composite permission decl or the ``{kind:git}``
run-code trust gate (``tools/plugin_management_verbs.py`` / ``require_plugin_git_run_code_trust``
own that, exactly once). Because this session's real intervention bus is
threaded through, a ``{kind:git}`` install prompts interactively (the
operator-trust decision §3.10 requires) exactly like a live LLM tool call
would.

The typed ``kind`` discriminator (§3.8) is carried by the explicit slash arg
(``builtin``/``local``/``git``) — never a form-sniffed string.
"""
from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.runtime.session import Session

_USAGE = (
    "usage: /plugin install builtin|local|git <SOURCE> [as <INSTALL_NAME>]  "
    "|  /plugin uninstall <NAME>"
)


async def _build_plugin_tool_context(session: "Session") -> Any:
    from reyn.tools.types import ToolContext, build_resource_caller_state

    host = session.router_host
    router_state = await build_resource_caller_state(host)
    return ToolContext(
        events=host.events,
        permission_resolver=getattr(host, "permission_resolver", None),
        workspace=getattr(host, "workspace", None),
        caller_kind="router",
        router_state=router_state,
        resolver=getattr(host, "resolver", None),
        hot_reloader=getattr(host, "hot_reloader", None),
        state_log=getattr(host, "state_log", None),
    )


async def _invoke_plugin_tool(name: str, args: dict, ctx: Any) -> dict:
    from reyn.tools import get_default_registry
    from reyn.tools.dispatch import invoke_tool

    return await invoke_tool(get_default_registry(), name, args, ctx)


def _extract_error(result: dict) -> "str | None":
    if result.get("status") != "ok":
        return str(result.get("data", {}).get("error", result.get("error", "(unknown error)")))
    data = result.get("data", {})
    if isinstance(data, dict) and data.get("status") == "error":
        return str(data.get("error", "(unknown error)"))
    return None


@slash(
    "plugin",
    summary="Install/uninstall a self-contained reyn plugin bundle",
    usage=_USAGE,
    see_also=("docs/deep-dives/proposals/0064-plugin-model.md",),
)
async def plugin_cmd(session: "Session", args: str) -> None:
    try:
        parts = shlex.split(args)
    except ValueError as exc:
        await reply_error(session, f"could not parse arguments: {exc}. {_USAGE}")
        return

    if len(parts) < 2:
        await reply_error(session, _USAGE)
        return

    subcmd = parts[0]

    if subcmd == "install":
        if len(parts) < 3:
            await reply_error(session, _USAGE)
            return
        kind = parts[1]
        source_value = parts[2]
        rest = parts[3:]

        if kind == "builtin":
            source: dict = {"kind": "builtin", "name": source_value}
        elif kind == "local":
            source = {"kind": "local", "path": source_value}
        elif kind == "git":
            source = {"kind": "git", "url": source_value}
        else:
            await reply_error(
                session,
                f"unknown source kind {kind!r} — expected builtin/local/git. {_USAGE}",
            )
            return

        install_name: "str | None" = None
        if rest:
            if len(rest) == 2 and rest[0] == "as":
                install_name = rest[1]
            else:
                await reply_error(session, f"unexpected trailing arguments: {rest!r}. {_USAGE}")
                return

        tool_args: dict = {"source": source}
        if install_name:
            tool_args["name"] = install_name

        ctx = await _build_plugin_tool_context(session)
        try:
            result = await _invoke_plugin_tool("plugin_management__install", tool_args, ctx)
        except PermissionError as exc:
            await reply_error(session, f"permission denied: {exc}")
            return
        except Exception as exc:
            await reply_error(session, f"plugin install failed: {exc}")
            return

        err = _extract_error(result)
        if err is not None:
            await reply_error(session, f"plugin install failed: {err}")
            return

        await reply(
            session,
            f"✓ plugin installed (kind={kind}, source={source_value}).\n"
            f"{json.dumps(result.get('data', result), indent=2, ensure_ascii=False)}",
        )
        return

    if subcmd == "uninstall":
        name = parts[1]
        ctx = await _build_plugin_tool_context(session)
        try:
            result = await _invoke_plugin_tool("plugin_management__uninstall", {"name": name}, ctx)
        except PermissionError as exc:
            await reply_error(session, f"permission denied: {exc}")
            return
        except Exception as exc:
            await reply_error(session, f"plugin uninstall failed: {exc}")
            return

        err = _extract_error(result)
        if err is not None:
            await reply_error(session, f"plugin uninstall failed: {err}")
            return

        await reply(
            session,
            f"✓ plugin {name!r} uninstalled.\n"
            f"{json.dumps(result.get('data', result), indent=2, ensure_ascii=False)}",
        )
        return

    await reply_error(session, f"unknown subcommand {subcmd!r}. {_USAGE}")
