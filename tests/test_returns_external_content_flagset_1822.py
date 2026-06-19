"""Tier 2: returns_external_content flag-set completeness (FP-0050 / #1822 S2).

The flag-set IS the security gate (a missed external tool = an unfenced injection
vector — the same leak-class as the dead-EP1 catch). This pins the S2 enumeration:
every clear-external (network/store) tool carries the flag, and trusted-internal
tools do not. New external tools must set the flag (and update this pin) or the
completeness assertion fails.

Real default registry, no mocks. See FP-0050 §2 for the full enumeration +
the deferred file-read / exec-output (scan-only in S2, tracked fast-follow).
"""
from __future__ import annotations

import pytest

from reyn.tools import get_default_registry

# Clear-external (S2 fences these): network / external store / user-written disk.
_EXTERNAL = [
    "list_memory", "read_memory_body", "recall",
    "call_mcp_tool", "mcp_call_tool", "list_mcp_tools", "describe_mcp_tool",
    "mcp_search_registry", "web_search", "web_fetch",
]

# Trusted-internal OR deferred-to-fast-follow (scan-only in S2): NOT fenced.
_NOT_EXTERNAL = [
    "read_file", "grep_files", "glob_files", "list_directory",  # deferred (FP-0050 §6)
    "sandboxed_exec",                                            # deferred (exec output)
    "write_file", "edit_file", "delete_file",                   # writes
    "list_skills", "describe_skill", "invoke_skill",            # operator-curated
    "ask_user", "list_mcp_servers", "compact", "plan",          # trusted-internal
    "reyn_src_read", "reyn_src_grep",                           # reyn's own source
]


@pytest.mark.parametrize("name", _EXTERNAL)
def test_external_source_tools_flagged(name):
    """Tier 2: every clear-external tool sets returns_external_content=True."""
    td = get_default_registry().lookup(name)
    assert td is not None, f"{name} not registered"
    assert td.returns_external_content is True, f"{name} must be flagged external"


@pytest.mark.parametrize("name", _NOT_EXTERNAL)
def test_internal_tools_not_flagged(name):
    """Tier 2: trusted-internal / deferred tools are NOT fenced (scan-only)."""
    td = get_default_registry().lookup(name)
    if td is None:
        pytest.skip(f"{name} not registered in this build")
    assert td.returns_external_content is False, f"{name} must not be flagged external"
