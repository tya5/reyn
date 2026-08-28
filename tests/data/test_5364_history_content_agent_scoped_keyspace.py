"""Tier 2: #5364 key-space fix — ``session_id`` alone is not agent-unique.

Found while reviewing #5364 §1.6's own design question (e2e-coder: "want
to confirm rather than guess before writing deletion code"), and
confirmed as a real defect in already-merged #5369 (architect ruling,
lead-coder co-vet): the default session id (``registry._DEFAULT_SID``,
the literal ``"main"``) is the SAME string for every agent, so a
history-content directory keyed on ``session_id`` alone put every
agent's main session under ONE shared directory —
``.reyn/memory/history-content/main/``.

architect's own explicit warning about #5369's own nesting test (which
this file is named to avoid repeating): "the nesting test built exactly
one session_id, zero agents — it only checked the DEPTH axis, never the
ATTRIBUTION axis." This file is the attribution-axis witness that test
never was.
"""
from __future__ import annotations

import pytest

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig


def test_the_same_sid_from_two_different_agents_lands_in_two_different_dirs(
    tmp_path,
) -> None:
    """Tier 2: two MediaStore instances sharing the SAME session_id but
    DIFFERENT agent_name must write to two DIFFERENT directories — the
    exact defect #5369 shipped (every agent's "main" session sharing
    ``history-content/main/``)."""
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
    )
    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
    )

    alice_block = alice.save_tool_result("alice's own content", mime_type="text/plain")
    bob_block = bob.save_tool_result("bob's own content", mime_type="text/plain")

    alice_dir = (tmp_path / alice_block["path"]).parent
    bob_dir = (tmp_path / bob_block["path"]).parent
    assert alice_dir != bob_dir, (
        f"same session_id ('main'), different agent — must NOT share a "
        f"directory: alice={alice_dir!r}, bob={bob_dir!r}"
    )
    # Not just "different" — each agent's own name must be IN its own
    # path, so a listing is attributable without opening every file.
    assert "alice" in str(alice_dir)
    assert "bob" in str(bob_dir)

    # Alice can never see bob's write by construction, and vice versa —
    # the actual consequence the shared-dir defect would have produced.
    alice_out, alice_found = alice.read_tool_result(bob_block["path"])
    assert alice_found is True, "the read boundary still spans history_content_root"
    assert alice_out == "bob's own content", (
        "sanity: reading bob's OWN ref by its own path still works (the "
        "boundary is project-wide, not per-agent) — the defect this test "
        "pins is about the WRITE location, not the read boundary"
    )


def test_history_content_dir_raises_without_an_agent_name(tmp_path) -> None:
    """Tier 2: the write-time directory is agent-scoped — a MediaStore
    with no agent_name refuses outright rather than falling back to a
    directory shared with every other identity-less caller (the same
    defect this whole file exists to close, reproduced by omission)."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path, session_id="main")
    with pytest.raises(ValueError, match="agent_name"):
        store.save_tool_result("x", mime_type="text/plain")
