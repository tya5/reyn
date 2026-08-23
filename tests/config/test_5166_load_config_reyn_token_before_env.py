"""Tier 2: #5166 acceptance③ — ``load_config``'s ``:741`` now runs a reyn-
token pass (``expand_with_map``, ``${REYN_PROJECT_DIR}`` only — this
project-wide, agent-less load has no ``${REYN_AGENT_NAME}`` value to
supply) BEFORE ``expand_env`` (the ``os.environ``-backed pass MCP server
``env:`` blocks and other real secrets rely on).

Order is the whole point (lead-coder, #5166 supplement): reversing it
would let ``expand_env``'s ``os.environ`` lookup consume
``${REYN_PROJECT_DIR}`` first — undefined there, silently degrading to
``""`` (the #5140 failure shape one layer up, at the project-wide config
instead of a per-agent hooks.yaml).

Real ``load_config`` + a real ``reyn.yaml`` on disk + a real env var — no
mocks."""
from __future__ import annotations

from reyn.config.loader import load_config


def test_reyn_project_dir_resolves_and_mcp_env_still_resolves_too(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: both passes must land: reyn's own ${REYN_PROJECT_DIR} AND a
    real MCP server env var, in the SAME config load, neither one
    breaking the other."""
    monkeypatch.setenv("TEST_5166_MCP_API_KEY", "secret-value-123")
    (tmp_path / "reyn.yaml").write_text(
        "mcp:\n"
        "  servers:\n"
        "    myserver:\n"
        "      type: stdio\n"
        "      command: echo\n"
        "      env:\n"
        "        API_KEY: ${TEST_5166_MCP_API_KEY}\n"
        "        PROJECT_TAG: ${REYN_PROJECT_DIR}\n",
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    env = cfg.mcp["servers"]["myserver"]["env"]

    assert env["API_KEY"] == "secret-value-123", (
        "a real MCP server env var (expand_env, os.environ-backed) must "
        f"still resolve exactly as before — got {env!r}"
    )
    assert env["PROJECT_TAG"] == str(tmp_path.resolve()), (
        "${REYN_PROJECT_DIR} must resolve to the real project root, not "
        f"stay literal — got {env!r}"
    )


def test_a_non_reyn_var_degrades_exactly_as_expand_env_always_has(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: a non-reyn ``${FOO}`` the operator never set must reach
    ``expand_env`` UNTOUCHED by the new reyn-token pass, so it degrades to
    ``expand_env``'s own pre-existing behavior for a genuinely-undefined
    var (``""``, verified directly against ``expand_env`` itself) — never
    a NEW/different shape introduced by the reyn-token pass now running
    first."""
    monkeypatch.delenv("TOTALLY_UNSET_5166_VAR", raising=False)
    (tmp_path / "reyn.yaml").write_text(
        "mcp:\n"
        "  servers:\n"
        "    myserver:\n"
        "      type: stdio\n"
        "      command: echo\n"
        "      env:\n"
        "        SOME_VAR: ${TOTALLY_UNSET_5166_VAR}\n",
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    env = cfg.mcp["servers"]["myserver"]["env"]

    assert env["SOME_VAR"] == "", (
        f"a non-reyn undefined var must still degrade to expand_env's own "
        f"pre-existing empty-string shape, unmodified by the reyn-token "
        f"pass — got {env!r}"
    )
