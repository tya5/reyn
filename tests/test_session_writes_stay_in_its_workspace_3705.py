"""#3705 — a Session writes under the workspace it was given, not the cwd.

The owner opened `reyn` and found their conversation history full of test
fixtures: `CONSOLIDATION-MARK-XYZ`, `stream please` answered with pages of
`lorem ipsum`, `XXXXXX...` padding. 656 of 1158 entries — 56% — were synthetic,
and their `.reyn/agents/` held 68 directories with names like `alice`, `bob`,
`a2a-e2e-test`, `chrome-3338-agent`.

Nobody aimed a test at their workspace. `Session` resolves several of its own
write locations from RELATIVE paths, so they land under whatever directory the
process happens to be in — while the test helper passes `tmp_path` for the
state log and the snapshot, which makes a test look contained when it is not.
Someone ran the suite with that workspace as their shell's cwd.

This is why an existing green suite is not evidence here: every session test
today passes while writing outside its own `tmp_path`. So the gate has to be
the falsifiable form — run a session that does real work from a cwd that is
NOT its workspace, and assert that nothing appeared in the cwd.

Scoped deliberately to what a Session writes on its own. `.reyn` is treated as
cwd-relative in many more places across the tree (memory, recovery snapshots,
the router host adapter's state dir, several CLI commands) — that is a wider
convention, listed in the issue, not something this module can pin without
claiming coverage it does not have.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.chat_message import ChatMessage
from tests._support.session import make_session, now


def _created_under(root: Path) -> "list[str]":
    """Everything that appeared under ``root``, as repo-relative strings."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_a_session_creates_nothing_in_the_directory_it_was_started_from(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: running from an unrelated cwd leaves that cwd untouched.

    The owner's workspace was an ordinary directory someone happened to be
    standing in. Nothing about it opted into being written to, which is exactly
    what this asserts: the cwd is not a place a Session may write.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "somebody_elses_directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    session = make_session(workspace)
    session._append_history(
        ChatMessage(role="user", content="a message worth persisting", ts=now())
    )

    leaked = _created_under(elsewhere)
    assert not leaked, (
        "a Session wrote into the directory the process was started from, "
        "which is how the owner's own conversation history came to hold test "
        f"fixtures: {leaked}"
    )


def test_the_history_lands_in_the_workspace_it_was_given(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: and it lands in the workspace — not merely nowhere.

    The paired half of the gate above: a fix that stopped writing altogether
    would satisfy "nothing in the cwd" while losing the conversation. This
    fails on that.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "somebody_elses_directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    session = make_session(workspace)
    session._append_history(
        ChatMessage(role="user", content="a message worth persisting", ts=now())
    )

    written = _created_under(workspace)
    assert any("history.jsonl" in name for name in written), (
        f"the conversation was not persisted under its own workspace: {written}"
    )
