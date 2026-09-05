"""Tier 2: #5351 witness 4 (lead-coder measurement, issue thread), UPDATED
by #5801 (owner ruling, 2026-09-05: "環境変数の展開漏れなんてのは、環境変数の
展開ルールを yaml ないで統一して構造化すべきだからね" — reyn's own token
expansion rule is now ONE rule, applied the same way by every
reyn-token-aware yaml face, not "warn but keep going" for this one face
and "fail closed" for hooks.yaml).

The original #5351 finding stands unchanged: the shared ``reyn.yaml``
(policy tier) load point was NOT silent about an unresolved
``${REYN_AGENT_NAME}`` — ``expand_env`` (ADR-0030) already warned "Config
references undefined environment variable: ${REYN_AGENT_NAME}" and
degraded it to ``""``, and an operator who "fixed" that via ``export
REYN_AGENT_NAME=...`` silently pinned the shared, project-wide config to
one agent's name with ZERO remaining signal.

#5351's OWN fix (PR #5503) deliberately did not refuse the file — "no
fail-close (#5166's ruling is unchanged)" was #5351's explicit scoping
at the time. #5801 supersedes that scoping: this file's own
``${REYN_AGENT_NAME}`` is now a face going through the SAME
``_load_yaml``/``expand_yaml_tokens_or_refuse`` every other
reyn-token-aware face does, and that shared rule fails closed
(#5801 req②) — REFUSING the whole layer, not just warning + degrading
one field. This is a STRICTLY LOUDER outcome for the exact failure mode
#5351 exists to catch (an operator's `export` "fix" now doesn't even
make the config load with a wrong value silently baked in — it refuses
outright, both with and without the export).

Real ``load_config`` + a real ``reyn.yaml`` on disk — no mocks. Witness
is the real content (``cfg.mcp["servers"]`` genuinely absent), not just
"a warning fired" (lead-coder's own standing correction on this exact
family of finding, #5801: "witness は「warning が出ない」ではなく「中身が
system prompt に入る」で")."""
from __future__ import annotations

import warnings

import pytest

from reyn.config.loader import load_config

# Matches the #5801 shared expand_yaml_tokens_or_refuse warning -- fires
# on ANY unresolved reyn token this face's map doesn't cover, not just
# REYN_AGENT_NAME specifically (the vocabulary lives in the token_map,
# not in a second hand-written check here -- #5801 req③).
_REFUSAL_WARNING_MATCH = r"left reyn token\(s\).*unresolved -- refusing"


def test_unresolved_reyn_agent_name_fails_closed_even_after_export(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the file is refused even when the operator has already
    "fixed" the pre-existing expand_env warning by exporting
    REYN_AGENT_NAME -- #5801's fail-closed rule does not consult
    os.environ at all (never did; that was always #5166's point), so
    exporting the var changes nothing about this outcome."""
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

    with pytest.warns(UserWarning, match=_REFUSAL_WARNING_MATCH):
        cfg = load_config(tmp_path)

    # Real-content witness (not just "a warning fired"): the whole
    # reyn.yaml layer -- including the otherwise-valid `myserver` entry
    # -- never made it into the merged config.
    assert not cfg.mcp.get("servers"), (
        "reyn.yaml must be refused wholesale on an unresolved reyn token "
        f"-- got {cfg.mcp.get('servers')!r}"
    )


def test_unresolved_reyn_agent_name_fails_closed_without_export(tmp_path) -> None:
    """Tier 2: without any export, the same refusal fires -- #5801's rule
    does not distinguish "operator tried to fix it" from "never tried";
    both are reyn's own bug (it could not supply a value it owns), not
    an operator env-var problem to work around either way."""
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

    with pytest.warns(UserWarning, match=_REFUSAL_WARNING_MATCH):
        cfg = load_config(tmp_path)

    assert not cfg.mcp.get("servers"), (
        f"got {cfg.mcp.get('servers')!r}"
    )


def test_a_resolvable_reyn_project_dir_does_not_fail_closed(tmp_path) -> None:
    """Tier 2: the accept-side witness -- a real, resolvable
    ${REYN_PROJECT_DIR} in the same load point must NOT trip the
    refusal warning, and its real, expanded value must land in the
    merged config (not just "no warning" -- the actual content)."""
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

    refusals = [
        w for w in caught
        if issubclass(w.category, UserWarning) and "unresolved -- refusing" in str(w.message)
    ]
    assert not refusals, (
        f"a fully-resolved ${{REYN_PROJECT_DIR}} must not be refused -- "
        f"got {[str(w.message) for w in refusals]}"
    )
    env = cfg.mcp["servers"]["myserver"]["env"]
    assert env["PROJECT_TAG"] == str(tmp_path.resolve())
