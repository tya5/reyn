"""Environment preconditions for ``LLMReplay`` — #3473 face 3.

**A replay key contains the SCENARIO; the ENVIRONMENT is not a key
component but a precondition that is CHECKED.**

An LLM fixture records "for THIS conversation, the model answered THIS".
Some of what reaches the wire, though, is not the conversation — it is the
machine the conversation ran on. The canonical instance is the MCP tool
catalog: `RouterHostAdapter.ensure_mcp_tools_cached` probes each configured
MCP server with a deadline, and `tools/mcp.py`'s `_enrich_router_schema`
injects the result as the `server` / `mcp_tool_name` enums of the MCP tool
schemas. A probe that misses its deadline yields `ToolsUnknown` (#3531), the
enum is omitted, and the `tools=` payload differs — for a reason that has
nothing to do with the conversation.

While such a value is hashed INTO the key, an environment wobble is
indistinguishable from a different conversation: the symptom is
`MissingFixture: No fixture entry for model=…`, which does not name what
differed (#3473 cost three sessions to attribute exactly once). Pulling the
value OUT of the key without replacing it with anything is worse — the
fixture would then be replayed under tooling it was never recorded against,
silently and wrongly.

So an environment-derived value gets THREE declared operations here instead
of being hashed:

- ``scrub``    — remove its imprint from the key input (so the key is the
                 scenario).
- ``observe``  — read that imprint back out of a request, as a JSON-able
                 value recorded next to each fixture entry at capture time
                 and compared against at replay time. Recording and checking
                 the SAME projection that ``scrub`` removes is what keeps
                 the pair honest: nothing leaves the key without being
                 checked.
- ``capture`` / ``inject`` — snapshot the live environment at capture time,
                 and re-establish that exact snapshot before replay. This is
                 how determinism is reached WITHOUT waiting: #3473 rules out
                 sleeps, longer deadlines and retries (all of them merely
                 widen the window in which the probe happens to make it), so
                 the recorded catalog is installed directly and the probe is
                 never on the replay path at all.

Adding the next environment-derived value (another dynamic catalog, a model
list, a feature-flag-driven tool set) means implementing this one protocol
and adding it to :func:`default_preconditions` — the fixture format, the
mismatch report and the injection step already carry it.
"""
from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

#: The MCP schema properties `_enrich_router_schema` injects enums into.
#: These two names are MCP-specific across reyn's whole tool surface
#: (`tools/descriptions/mcp.py` is their only declaring module), so matching
#: on them cannot scrub an unrelated tool's schema.
_MCP_ENUM_PROPS = ("server", "mcp_tool_name")


class EnvironmentPrecondition(ABC):
    """One environment-derived value that must not be part of a replay key.

    Subclasses declare a stable :attr:`name` (the fixture's field key) and the
    four operations described in the module docstring. Every method must be
    safe to call against an arbitrary, possibly malformed request payload —
    a precondition is test infrastructure and must never be the reason a test
    errors out with something other than its own report.
    """

    #: Stable identifier, used as the fixture field name. Never rename without
    #: re-recording every fixture that carries it.
    name: str

    @abstractmethod
    def scrub(self, tools: list[dict] | None) -> list[dict] | None:
        """Return ``tools`` with this environment's imprint removed.

        Must not mutate the input. Must be a no-op (returning an equal value)
        when the imprint is absent, so that fixtures recorded on a machine
        where this environment never materialised keep byte-identical keys.
        """

    @abstractmethod
    def observe(self, tools: list[dict] | None) -> Any:
        """Return this environment's imprint on ``tools`` as a JSON-able value.

        This is exactly what :meth:`scrub` removes — the pair is what makes
        "taken out of the key" and "checked as a precondition" the same set.
        """

    @abstractmethod
    def absent_value(self) -> Any:
        """The :meth:`observe` value meaning "this environment left no imprint"."""

    @abstractmethod
    def describe_mismatch(self, expected: Any, actual: Any) -> str:
        """Return a multi-line report NAMING how ``actual`` differs from ``expected``."""

    @abstractmethod
    def capture(self) -> Any:
        """Snapshot the live environment for later injection, or ``None``.

        ``None`` means "nothing to record" — the fixture then carries no
        snapshot for this precondition and :meth:`inject` is never called.
        """

    @abstractmethod
    def inject(self, snapshot: Any) -> None:
        """Re-establish ``snapshot`` as the live environment.

        Called once, before replay begins. Must reach the state directly —
        never by waiting for, retrying or widening the deadline of whatever
        process produced the value originally (#3473).
        """


def _mcp_schema_properties(tool: Any) -> dict | None:
    """Return a tool entry's ``function.parameters.properties`` dict, or None."""
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    if not isinstance(function, dict):
        return None
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        return None
    properties = parameters.get("properties")
    return properties if isinstance(properties, dict) else None


class MCPCatalogPrecondition(EnvironmentPrecondition):
    """The MCP server/tool catalog — #3473's motivating instance.

    Imprint: the ``server`` and ``mcp_tool_name`` enums `_enrich_router_schema`
    injects into the MCP tool schemas, which are derived from
    `RouterHostAdapter`'s probe results.

    Injection target: ``<state_dir>/mcp_tools_cache.json``, the persistent
    cache `ensure_mcp_tools_cached` warm-starts from before it probes anything
    (FP-0037 S1) — the same file `reyn mcp refresh` writes. A server the file
    already answers for is not in the adapter's ``unanswered`` list, so it is
    never probed and the deadline that makes the catalog timing-dependent is
    never on the path. Nothing is faked: the production reader reads a
    production-shaped file written by the production writer.

    ``state_dir`` defaults to `RouterHostAdapter`'s own default,
    ``.reyn/state`` resolved against the CURRENT working directory — the two
    must agree, and a replay test that chdirs into its project (as an MCP
    test must, since config resolution anchors on the process CWD) gets the
    right file for free. Pass an explicit path when the session under test
    was given one.
    """

    name = "mcp_catalog"

    def __init__(self, state_dir: "Path | str | None" = None) -> None:
        self._state_dir = Path(state_dir) if state_dir is not None else None

    def _resolve_state_dir(self) -> Path:
        if self._state_dir is not None:
            return self._state_dir
        # Mirrors ``router_host_adapter._DEFAULT_STATE_DIR`` — deliberately
        # resolved at call time, not at construction, because a replay test
        # chdirs into its project between the two.
        return Path(".reyn") / "state"

    def scrub(self, tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return tools
        scrubbed = copy.deepcopy(tools)
        for tool in scrubbed:
            properties = _mcp_schema_properties(tool)
            if properties is None:
                continue
            for prop_name in _MCP_ENUM_PROPS:
                prop = properties.get(prop_name)
                if isinstance(prop, dict):
                    prop.pop("enum", None)
        return scrubbed

    def observe(self, tools: list[dict] | None) -> dict[str, list[str]]:
        servers: list[str] = []
        tool_names: list[str] = []
        for tool in tools or []:
            properties = _mcp_schema_properties(tool)
            if properties is None:
                continue
            for prop_name, sink in (
                ("server", servers), ("mcp_tool_name", tool_names),
            ):
                prop = properties.get(prop_name)
                if not isinstance(prop, dict):
                    continue
                for value in prop.get("enum") or []:
                    if str(value) not in sink:
                        sink.append(str(value))
        return {"servers": servers, "tools": tool_names}

    def absent_value(self) -> dict[str, list[str]]:
        return {"servers": [], "tools": []}

    def describe_mismatch(self, expected: Any, actual: Any) -> str:
        """Report the catalog difference PER SERVER.

        The failure this exists for is one server's tools vanishing while
        every other part of the payload is identical, so a whole-value dump
        would bury the one line that attributes it.
        """
        expected = expected if isinstance(expected, dict) else {}
        actual = actual if isinstance(actual, dict) else {}
        exp_servers = [str(s) for s in expected.get("servers") or []]
        act_servers = [str(s) for s in actual.get("servers") or []]
        exp_tools = self._tools_by_server(expected.get("tools") or [])
        act_tools = self._tools_by_server(actual.get("tools") or [])

        lines: list[str] = []
        for server in exp_servers:
            if server not in act_servers:
                lines.append(
                    f"  - server={server!r}: configured at capture time, "
                    f"NOT configured in this run"
                )
                continue
            want, got = exp_tools.get(server, []), act_tools.get(server, [])
            if want == got:
                continue
            if want and not got:
                lines.append(
                    f"  - server={server!r}: expected tools={want} "
                    f"(count {len(want)}), got NONE — this run's catalog has no "
                    f"answer for it (an MCP probe that did not answer leaves no "
                    f"cache entry, so the enum omits the server entirely)"
                )
            else:
                lines.append(
                    f"  - server={server!r}: expected tools={want}, got {got}"
                )
        for server in act_servers:
            if server not in exp_servers:
                lines.append(
                    f"  - server={server!r}: present in this run, "
                    f"NOT configured at capture time"
                )
        if not lines:
            lines.append(f"  - expected {expected!r}, got {actual!r}")
        return "\n".join(lines)

    @staticmethod
    def _tools_by_server(qualified_names: Any) -> dict[str, list[str]]:
        """Group ``["srv.tool", …]`` (the enum's own shape) by server."""
        grouped: dict[str, list[str]] = {}
        for qualified in qualified_names or []:
            server, _, tool = str(qualified).partition(".")
            grouped.setdefault(server, []).append(tool or str(qualified))
        return grouped

    def capture(self) -> dict[str, list[dict]] | None:
        """Read the live MCP tools cache file back as a plain mapping."""
        from reyn.runtime.services.mcp_cache_file import cache_file_path, read_cache

        answered = read_cache(cache_file_path(self._resolve_state_dir()))
        if not answered:
            return None
        return {name: entry.tools for name, entry in answered.items()}

    def inject(self, snapshot: Any) -> None:
        """Write ``snapshot`` into the MCP tools cache file.

        Uses the production writer, so the file is version-stamped and
        atomically replaced exactly as `reyn mcp refresh` leaves it — a
        hand-written JSON blob here would drift from `read_cache`'s accepted
        version the moment the format moves.
        """
        if not isinstance(snapshot, dict) or not snapshot:
            return
        from reyn.runtime.services.mcp_cache_file import (
            ToolsAnswered,
            cache_file_path,
            write_cache,
        )

        write_cache(
            cache_file_path(self._resolve_state_dir()),
            {
                str(server): ToolsAnswered(tools=list(tools or []))
                for server, tools in snapshot.items()
            },
        )


def default_preconditions() -> tuple[EnvironmentPrecondition, ...]:
    """The preconditions every ``LLMReplay`` applies unless told otherwise.

    A fresh tuple per call: preconditions hold resolution state (a state dir)
    and must not be shared across replays.
    """
    return (MCPCatalogPrecondition(),)
