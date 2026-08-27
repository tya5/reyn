"""Tier 2: #5356 — a hook's write_paths declaration is eager-rejected at an
agent-writable origin (per-agent, per-session), and unaffected at either
non-agent-writable origin (startup, runtime).

Real incident this closes (architect + docs-maintainer measurement,
#5244/#5351/#5356): an agent can already write its own per-agent/per-
session hooks.yaml via the ordinary file-write op (session.py's own
"agent-writable" framing), and a hook's write_paths declaration is fully
honored by the real sandbox (verified directly against SeatbeltBackend,
no mocks) — so declaring write_paths there is a confused-deputy self-
grant, not a silent drop. Two prior design attempts for this issue were
each falsified by direct measurement before landing (a "protected-set"
add-on that #5326's own hooks_add op already writes to the same path, and
an "intersect with the agent-level allow_write_paths" design that assumed
a parent grant exists for hook shells at all — it does not: dropping
write_paths yields the hard floor, verified with a real PermissionError).
This test file exercises the FINAL, simplest correct form: reject the
declaration outright at the two agent-writable origins.
"""
from __future__ import annotations

import pytest

from reyn.hooks.loader import HookConfigError, load_hooks

_ENTRY_WITH_WRITE_PATHS = {
    "on": "turn_end",
    "exec": ["/usr/bin/true"],
    "write_paths": ["/tmp/somewhere"],
}


@pytest.mark.parametrize("origin", ["per-agent", "per-session"])
def test_write_paths_rejected_at_agent_writable_origins_accepted_elsewhere(
    origin: str,
) -> None:
    """Tier 2: acceptance + falsification contrast in one test (the SAME
    entry, only the origin differs) — without the contrast, a rejection
    triggered by an unrelated bug (a broken parser, an empty known-key set)
    would read as this test passing for the wrong reason."""
    # Deny side — an agent-writable origin rejects it outright.
    with pytest.raises(HookConfigError, match="write_paths.*not permitted"):
        load_hooks([_ENTRY_WITH_WRITE_PATHS], origin=origin)

    # Positive side — the SAME entry, at a non-agent-writable origin,
    # loads clean and the declaration survives onto the HookDef.
    for safe_origin in ("startup", "runtime"):
        registry = load_hooks([_ENTRY_WITH_WRITE_PATHS], origin=safe_origin)
        (hook,) = registry.all_defs()
        assert hook.write_paths == ("/tmp/somewhere",)


def test_entry_with_no_write_paths_is_unaffected_at_any_origin() -> None:
    """Tier 2: non-regression — a hook that never declares write_paths at
    all loads fine at every origin, including per-agent/per-session. The
    rejection is scoped to the KEY's presence, not the origin generally."""
    entry = {"on": "turn_end", "exec": ["/usr/bin/true"]}
    for origin in ("startup", "runtime", "per-agent", "per-session", "unknown"):
        registry = load_hooks([entry], origin=origin)
        (hook,) = registry.all_defs()
        assert hook.write_paths is None


def test_error_names_the_offending_origin() -> None:
    """Tier 2: the error message names which origin rejected it — a
    reader landing on this error from a real config-load failure needs to
    know WHERE to move the declaration, not just that it failed."""
    with pytest.raises(HookConfigError, match="'per-agent'"):
        load_hooks([_ENTRY_WITH_WRITE_PATHS], origin="per-agent")
