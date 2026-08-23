"""Tier 2: #5140 (part 2) — ``${REYN_AGENT_NAME}`` resolves in a per-agent
``hooks.yaml``'s ``composers:`` key, the sibling of the already-fixed
``hooks:`` key (PR #5161, ``tests/runtime/test_5140_per_agent_hooks_token_expansion.py``).

``Session._read_per_agent_composers`` (``runtime/session.py``) reads the SAME
``.reyn/agents/<name>/hooks.yaml`` file :func:`~reyn.config.loader.load_per_agent_hooks`
reads its ``hooks:`` key from, but its own ``composers:`` key — and it used
to run that key through ``expand_env`` (``security/secrets/interpolation.py``,
ADR-0030, ``os.environ``-backed). ``REYN_AGENT_NAME`` is only ever set on a
SPAWNED CHILD process's own env, never on this process's own ``os.environ``,
so ``${REYN_AGENT_NAME}`` was ALWAYS undefined at config-load time here too —
silently expanding to ``""``, indistinguishable from an operator's genuine
empty-suffix choice. This is the exact #5140 defect, in a sibling call site
PR #5161 did not touch.

Fix mirrors #5161 exactly: ``expand_with_map`` (``plugins/tokens.py``) with an
explicit ``{REYN_PROJECT_DIR, REYN_AGENT_NAME}`` map, fail-closed (whole
composers layer refused, not a wrong/empty value silently registered) on any
REMAINING ``${REYN_*}``/``${CLAUDE_*}`` token via
``find_unresolved_reyn_tokens`` — while a non-reyn ``${FOO}`` (a spawned
child process's own env var) is left untouched and still loads (the #5152
"healthy config must not trip the fail-close" shape).

Deliberately its OWN test file, separate from the ``hooks:`` witness — each
key's expansion is tested through the function that actually reads THAT key,
so one half can never be silently empty while the other carries the file.

Real ``Session`` (via ``tests._support.agent_session.make_session``) + real
files on disk — no mocks. ``_hot_reload_project_root`` falls back to
``Path.cwd()`` when the session is built with no registry (the usual case in
tests) — matches the pattern already used by
``tests/core/test_router_op_context_source_3607.py``.
"""
from __future__ import annotations

import logging
from pathlib import Path

from tests._support.agent_session import make_session

_AGENT = "coder-smith"


def _write_per_agent_hooks(project_root: Path, body: str) -> None:
    agent_dir = project_root / ".reyn" / "agents" / _AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "hooks.yaml").write_text(body, encoding="utf-8")


def _make_session_in(project_root: Path, monkeypatch, tmp_path: Path):
    # ``_hot_reload_project_root`` falls back to cwd when no registry root is
    # wired — that is the root ``_read_per_agent_composers`` re-reads
    # ``hooks.yaml`` from.
    monkeypatch.chdir(project_root)
    return make_session(
        agent_name=_AGENT,
        workspace_base_dir=project_root,
        workspace_state_dir=tmp_path / "state",
    )


def test_reyn_agent_name_resolves_to_the_real_agent_name_in_composers(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance ① — ${REYN_AGENT_NAME} in a ``composers:`` entry
    resolves to the correct per-agent name, not an empty string."""
    project = tmp_path / "proj"
    _write_per_agent_hooks(
        project,
        "composers:\n"
        "  - name: notify-${REYN_AGENT_NAME}\n"
        "    on: turn_end\n"
        "    template_push:\n"
        "      message: broker://inbox/${REYN_AGENT_NAME}\n",
    )
    session = _make_session_in(project, monkeypatch, tmp_path)

    composers = session._read_per_agent_composers()

    assert composers, "the composers layer must load, not come back empty"
    assert composers[0]["name"] == f"notify-{_AGENT}"
    assert composers[0]["template_push"]["message"] == f"broker://inbox/{_AGENT}"


def test_an_unresolved_reyn_token_refuses_to_load_the_composers_layer(
    tmp_path, monkeypatch, caplog,
) -> None:
    """Tier 2: acceptance ② — a reyn-owned token this loader does NOT supply
    a value for (``${REYN_SKILL_DIR}`` — a location token with no meaning for
    a composers file) must refuse to load the WHOLE composers layer, not
    silently register a composer with an empty/wrong value, and must surface
    why."""
    project = tmp_path / "proj"
    _write_per_agent_hooks(
        project,
        "composers:\n"
        "  - name: bad\n"
        "    on: turn_end\n"
        "    template_push:\n"
        "      message: ${REYN_SKILL_DIR}/note.txt\n",
    )
    session = _make_session_in(project, monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING):
        composers = session._read_per_agent_composers()

    assert composers == [], (
        "an unresolved reyn-owned token must refuse the WHOLE composers "
        "layer, not load a composer with a wrong/empty value"
    )
    assert any("REYN_SKILL_DIR" in r.getMessage() for r in caplog.records), (
        "the refusal must surface a reason, not fail silently"
    )


def test_a_non_reyn_token_is_left_untouched_and_still_loads_in_composers(
    tmp_path, monkeypatch, caplog,
) -> None:
    """Tier 2: acceptance ③ — a NON-reyn ``${FOO}`` (e.g. an env var meant
    for a spawned child process to resolve) must load exactly as before:
    left untouched, no fail-close. Without this, a future fail-close change
    could kill the feature and nothing would catch it (the #5152 shape)."""
    project = tmp_path / "proj"
    _write_per_agent_hooks(
        project,
        "composers:\n"
        "  - name: passthrough\n"
        "    on: turn_end\n"
        "    exec:\n"
        "      command: echo ${SOME_CHILD_PROCESS_VAR}\n",
    )
    session = _make_session_in(project, monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING):
        composers = session._read_per_agent_composers()

    assert composers, "a non-reyn token must not cause the composers layer to be refused"
    assert composers[0]["exec"]["command"] == "echo ${SOME_CHILD_PROCESS_VAR}"
    assert not any(
        "unresolved" in r.getMessage() for r in caplog.records
    ), "a non-reyn token must not trip the fail-close or warn at all"
