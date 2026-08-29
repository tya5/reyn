"""Tier 2: #5351 witness 4 (lead-coder measurement, issue thread) +
architect BLOCKING on the first version of this fix (PR #5503, head
0237874a0) — the shared ``reyn.yaml`` (policy tier) load point was NOT
silent about an unresolved ``${REYN_AGENT_NAME}``: ``expand_env`` (ADR-0030)
already warns "Config references undefined environment variable:
${REYN_AGENT_NAME}" and degrades it to ``""``. The real defect is that this
EXISTING signal points at the WRONG fix — it reads exactly like a genuine
unset env var, so an operator who "fixes" it via ``export
REYN_AGENT_NAME=...`` succeeds: ``expand_env``'s warning disappears, the
token silently resolves to whichever agent's name happens to be in this
process's env, and the shared, project-wide config is now pinned to one
agent with ZERO remaining signal.

This test proves the actual fix: a SEPARATE, correctly-aimed ``UserWarning``
now fires BEFORE ``expand_env`` (so before any ``os.environ`` lookup),
catching the case the existing warning's own wrong fix produces —
including after the operator has already exported the var. Never a
refusal (#5166's fail-close scoping to the 4 hooks.yaml layers only is
unchanged).

Real ``load_config`` + a real ``reyn.yaml`` on disk — no mocks."""
from __future__ import annotations

import warnings

import pytest

from reyn.config.loader import load_config

# Matches ONLY the new #5351 warning, never expand_env's own pre-existing
# "undefined environment variable" warning (both warnings legitimately
# mention REYN_AGENT_NAME by name — the first version of this test matched
# on that shared substring and stayed green with the #5351 fix entirely
# deleted, architect's own strip-falsify finding on PR #5503).
_NEW_WARNING_MATCH = r"has no per-agent context to resolve"


def test_unresolved_reyn_agent_name_warns_before_expand_env_and_before_export(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the new warning fires even when the operator has already
    "fixed" the pre-existing expand_env warning by exporting
    REYN_AGENT_NAME — the exact failure mode #5351 exists to catch (the
    existing signal's own wrong fix silences the existing signal, but
    must not silence THIS one)."""
    monkeypatch.setenv("REYN_AGENT_NAME", "coder-brown")
    (tmp_path / "reyn.yaml").write_text(
        "mcp:\n"
        "  servers:\n"
        "    myserver:\n"
        "      type: stdio\n"
        "      command: echo\n"
        "      env:\n"
        "        AGENT_TAG: ${REYN_AGENT_NAME}\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match=_NEW_WARNING_MATCH):
        cfg = load_config(tmp_path)

    env = cfg.mcp["servers"]["myserver"]["env"]
    assert env["AGENT_TAG"] == "coder-brown", (
        "sanity: the exported env var IS what expand_env resolves the "
        f"token to (this is the silent-pin failure mode itself) -- got {env!r}"
    )


def test_unresolved_reyn_agent_name_still_warns_and_degrades_without_export(
    tmp_path,
) -> None:
    """Tier 2: without any export, the config still loads (no fail-close,
    #5166's ruling is unchanged) and the token degrades exactly like any
    other undefined ${VAR} (the SAME shape #5166's own
    ``test_a_non_reyn_var_degrades_exactly_as_expand_env_always_has``
    documents) -- the #5351 warning never mutates the value, only adds a
    correctly-aimed second signal alongside expand_env's existing one."""
    (tmp_path / "reyn.yaml").write_text(
        "mcp:\n"
        "  servers:\n"
        "    myserver:\n"
        "      type: stdio\n"
        "      command: echo\n"
        "      env:\n"
        "        AGENT_TAG: ${REYN_AGENT_NAME}\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match=_NEW_WARNING_MATCH):
        cfg = load_config(tmp_path)

    env = cfg.mcp["servers"]["myserver"]["env"]
    assert env["AGENT_TAG"] == "", (
        "no fail-close (#5166's ruling is unchanged) -- the config must "
        "still load, with the unresolved token degrading via the later "
        f"expand_env pass exactly like any other undefined ${{VAR}} -- "
        f"got {env!r}"
    )


def test_a_resolvable_reyn_project_dir_does_not_warn(tmp_path) -> None:
    """Tier 2: the accept-side witness -- a real, resolvable
    ${REYN_PROJECT_DIR} in the same load point must NOT trip the new
    warning (it would make the negative claim above meaningless if the
    warning fired unconditionally on any reyn token at all, resolved or
    not)."""
    (tmp_path / "reyn.yaml").write_text(
        "mcp:\n"
        "  servers:\n"
        "    myserver:\n"
        "      type: stdio\n"
        "      command: echo\n"
        "      env:\n"
        "        PROJECT_TAG: ${REYN_PROJECT_DIR}\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = load_config(tmp_path)

    new_warnings = [
        w for w in caught
        if issubclass(w.category, UserWarning)
        and "has no per-agent context to resolve" in str(w.message)
    ]
    assert not new_warnings, (
        f"a fully-resolved ${{REYN_PROJECT_DIR}} must not trip the #5351 "
        f"warning -- got {[str(w.message) for w in new_warnings]}"
    )
    env = cfg.mcp["servers"]["myserver"]["env"]
    assert env["PROJECT_TAG"] == str(tmp_path.resolve())
