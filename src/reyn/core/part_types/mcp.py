"""Part-type marker: mcp — an installed external MCP server (input/workflow/
output roles).

The live registry is the installed-server roster enumerator
``RouterHostAdapter.get_mcp_servers`` (an MCP server exposes tools/resources/
prompts, so it can serve input, mid-flow workflow calls, and external writes —
all three roles, proposal §2.1).
"""
from reyn.core.part_type_registry import PartTypeSpec

PART_TYPE_SPEC = PartTypeSpec(
    name="mcp",
    roles=frozenset({"input", "workflow", "output"}),
    category="mcp",
    registry_ref="reyn.runtime.services.router_host_adapter:RouterHostAdapter.get_mcp_servers",
    description="An installed external MCP server (tools/resources/prompts).",
    doc_ref="docs/concepts/tools-integrations/mcp.md",
)
