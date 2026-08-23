"""Tier 2: #5140 — ``${REYN_AGENT_NAME}`` resolves in a per-agent hooks.yaml.

Root cause (e2e-coder, found via #5091's own witness): ``load_per_agent_hooks``
(``config/loader.py``) ran a per-agent ``hooks.yaml`` through ``expand_env``
(``security/secrets/interpolation.py``, ADR-0030) — an ``os.environ``-backed
expander meant for a SPAWNED CHILD process's config-time env-injection.
``REYN_AGENT_NAME`` is only ever set on a child process's own env
(``hooks/shell_runner.py`` and friends), never on this process's own
``os.environ`` — so ``${REYN_AGENT_NAME}`` was ALWAYS undefined at config-load
time, silently expanding to ``""`` (a ``UserWarning``, then a
syntactically-valid-but-wrong value, e.g. ``broker://inbox/`` instead of
``broker://inbox/coder-smith``, indistinguishable from an operator's genuine
empty-suffix choice).

Architect ruling (issuecomment-5383725162): this is a layering mistake, not a
genuinely undefined token — the value (``agent_name``) is already an argument
to this function. Fix: ``expand_with_map`` (``plugins/tokens.py``, #3629's own
mechanism, already used by ``registry_bootstrap.py``/``session.py`` for
``REYN_PROJECT_DIR``) with an explicit ``{"REYN_AGENT_NAME": agent_name, ...}``
map — never ``os.environ``. Plus a fail-close SCOPED TO REYN'S OWN TOKEN
VOCABULARY: a remaining ``${REYN_*}``/``${CLAUDE_*}`` placeholder after
expansion is reyn's own bug (a token reyn should always be able to supply),
so that hooks layer is refused rather than loaded with a wrong value — but a
non-reyn ``${FOO}`` (an env var a spawned child process may resolve later)
must NOT be caught by this check (the #5152 "does the trigger fire on a
healthy path" test: here, a healthy config with reyn's own tokens correctly
supplied never trips it).

Real ``load_per_agent_hooks`` + real files on disk — no mocks.
"""
from __future__ import annotations

import warnings
from pathlib import Path

from reyn.config.loader import load_per_agent_hooks

_AGENT = "coder-smith"


def _write_per_agent_hooks(tmp_path: Path, body: str) -> None:
    agent_dir = tmp_path / ".reyn" / "agents" / _AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "hooks.yaml").write_text(body, encoding="utf-8")


def test_reyn_agent_name_resolves_to_the_real_agent_name(tmp_path: Path) -> None:
    """Tier 2: acceptance ① — ${REYN_AGENT_NAME} resolves to the correct
    per-agent name, not an empty string."""
    _write_per_agent_hooks(
        tmp_path,
        "hooks:\n"
        "  - on: turn_end\n"
        "    template_push:\n"
        "      message: broker://inbox/${REYN_AGENT_NAME}\n"
        "      wake: true\n",
    )
    hooks = load_per_agent_hooks(tmp_path, _AGENT)
    assert hooks, "the hooks layer must load, not come back empty"
    assert hooks[0]["template_push"]["message"] == f"broker://inbox/{_AGENT}"


def test_an_unresolved_reyn_token_refuses_to_load_the_hooks_layer(
    tmp_path: Path,
) -> None:
    """Tier 2: acceptance ② — a reyn-owned token this loader does NOT supply
    a value for (simulated here via a token this map never populates,
    ``${REYN_SKILL_DIR}`` — a location token with no meaning for a hooks
    file) must refuse to load the WHOLE hooks layer, not silently register
    a matcher with an empty/wrong value, and must surface why."""
    _write_per_agent_hooks(
        tmp_path,
        "hooks:\n"
        "  - on: turn_end\n"
        "    template_push:\n"
        "      message: ${REYN_SKILL_DIR}/note.txt\n"
        "      wake: true\n",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hooks = load_per_agent_hooks(tmp_path, _AGENT)

    assert hooks == [], (
        "an unresolved reyn-owned token must refuse the WHOLE hooks layer, "
        "not load a hook with a wrong/empty value"
    )
    assert any(
        "REYN_SKILL_DIR" in str(w.message) and issubclass(w.category, UserWarning)
        for w in caught
    ), "the refusal must surface a reason, not fail silently"


def test_a_non_reyn_token_is_left_untouched_and_still_loads(tmp_path: Path) -> None:
    """Tier 2: acceptance ③ — a NON-reyn ``${FOO}`` (e.g. an env var meant
    for a spawned child process to resolve) must load exactly as before:
    left untouched, no fail-close. Without this, #5152's own retracted
    fail-closed regression would repeat here (a healthy config tripping
    the fail-close on every load)."""
    _write_per_agent_hooks(
        tmp_path,
        "hooks:\n"
        "  - on: turn_end\n"
        "    exec:\n"
        "      command: echo ${SOME_CHILD_PROCESS_VAR}\n"
        "      wake: true\n",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hooks = load_per_agent_hooks(tmp_path, _AGENT)

    assert hooks, "a non-reyn token must not cause the hooks layer to be refused"
    assert hooks[0]["exec"]["command"] == "echo ${SOME_CHILD_PROCESS_VAR}"
    assert not any(
        issubclass(w.category, UserWarning) for w in caught
    ), "a non-reyn token must not trip the fail-close or warn at all"


def test_no_undefined_env_var_warning_fires_on_a_healthy_reyn_token(
    tmp_path: Path,
) -> None:
    """Tier 2: acceptance ④ — the OLD `warnings.warn("Config references
    undefined environment variable…")` (expand_env's own, fired on every
    single load because REYN_AGENT_NAME was never actually in os.environ)
    is gone from this path: a healthy hooks.yaml with a correctly-supplied
    reyn token produces NO warning at all."""
    _write_per_agent_hooks(
        tmp_path,
        "hooks:\n"
        "  - on: turn_end\n"
        "    template_push:\n"
        "      message: broker://inbox/${REYN_AGENT_NAME}\n"
        "      wake: true\n",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_per_agent_hooks(tmp_path, _AGENT)

    assert not any(
        "undefined environment variable" in str(w.message) for w in caught
    )
