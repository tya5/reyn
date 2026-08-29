"""Tier 2: #5350 — ``broker_session_for`` derives (never stores) the broker
``session_id`` corresponding to a reyn agent, by joining the one fact both
sides already carry: a path (agent ``base_dir`` == session ``working_dir``).

Architect's 5 witnesses (quoted, #5350):

  | # | 構成 | 期待 |
  |---|---|---|
  | 1 | agent の base_dir ＝ ある session の working_dir | その session_id が返る |
  | 2 | 一致する session が無い | None（例外でも空文字でもない） |
  | 3 | 同じ working_dir に 2 agent | 両方とも同じ session_id を得る（多対一） |
  | 4 | 片側だけ symlink 経由のパス | 一致する（正規化の証拠） |
  | 5 | strip: 正規化を外す | 4 が赤 |

"2 と 3 が本質" (architect): returning None as a normal answer, and never
assuming 1:1.
"""
from __future__ import annotations

from reyn.runtime.services.broker_session import broker_session_for


def _session(session_id: str, working_dir: str) -> dict:
    return {"session_id": session_id, "working_dir": working_dir, "role": None, "status": "idle"}


def test_a_matching_working_dir_returns_that_sessions_id(tmp_path) -> None:
    """Tier 2: witness 1 — agent base_dir == a session's working_dir ->
    that session_id is returned."""
    agent_dir = tmp_path / "agents" / "tui-coder"
    agent_dir.mkdir(parents=True)
    sessions = [
        _session("tui-coder", str(agent_dir)),
        _session("other-agent", str(tmp_path / "agents" / "other")),
    ]

    assert broker_session_for(agent_dir, sessions) == "tui-coder"


def test_no_matching_session_returns_none_not_an_exception(tmp_path) -> None:
    """Tier 2: witness 2 — the CENTRAL no-match case. "No live broker
    session for this agent right now" is a normal, current answer — None,
    never an exception, never an empty string standing in for absence."""
    agent_dir = tmp_path / "agents" / "lonely-agent"
    agent_dir.mkdir(parents=True)
    sessions = [_session("someone-else", str(tmp_path / "agents" / "someone-else"))]

    result = broker_session_for(agent_dir, sessions)

    assert result is None
    assert result != "", "absence must be None, never an empty-string stand-in"


def test_two_agents_sharing_one_working_dir_both_resolve_to_the_same_session(
    tmp_path,
) -> None:
    """Tier 2: witness 3 — THE central witness (architect: this is the one
    that proves the "never assume 1:1" design actually holds). Two
    DIFFERENT reyn agents (sub-agents in the same project) share one
    base_dir; both queries must return the SAME session_id — proof this
    module asks "this agent's session?" (many-to-one, always answerable)
    and never builds a 1:1 index that would only have room for one."""
    shared_dir = tmp_path / "agents" / "shared-project"
    shared_dir.mkdir(parents=True)
    sessions = [_session("the-one-session", str(shared_dir))]

    # Two distinct callers (different agent identities), same base_dir.
    result_a = broker_session_for(shared_dir, sessions)
    result_b = broker_session_for(str(shared_dir), sessions)  # str form too

    assert result_a == "the-one-session"
    assert result_b == "the-one-session"
    assert result_a == result_b


def test_a_symlinked_path_on_either_side_still_matches(tmp_path) -> None:
    """Tier 2: witness 4 — normalization proof. The agent side is reached
    through a symlink; the session's working_dir is the real path. Without
    ``.resolve()`` on both sides these compare unequal even though they
    name the same directory — a silent, wrong "no session" answer."""
    real_dir = tmp_path / "real-project"
    real_dir.mkdir()
    symlinked_dir = tmp_path / "link-to-project"
    symlinked_dir.symlink_to(real_dir)
    sessions = [_session("real-session", str(real_dir))]

    assert broker_session_for(symlinked_dir, sessions) == "real-session"


def test_a_session_with_no_working_dir_key_is_skipped_not_fatal(tmp_path) -> None:
    """Tier 2: a malformed/partial session entry (missing working_dir) must
    not raise — this function does not trust its caller's data blindly,
    it skips the entry and keeps looking."""
    agent_dir = tmp_path / "agents" / "tui-coder"
    agent_dir.mkdir(parents=True)
    sessions = [{"session_id": "no-working-dir"}, _session("tui-coder", str(agent_dir))]

    assert broker_session_for(agent_dir, sessions) == "tui-coder"
