"""Tier 2: #4174 T0 — load_config warns (never raises) on an unknown/renamed
config key, collected in one pass across the whole policy tier.

Owner ruling (accumulated across #4174's spec revisions): no hard-fail
anywhere — including sandbox.policy, no special case. Real load_config(cwd)
seam (the same one every other test in this directory exercises), not a
bare unknown_config_keys() call in isolation — this proves the wiring, not
just the underlying primitive (already covered by
tests/security/test_sandbox_factory.py's sandbox.policy-scoped tests).
"""
from __future__ import annotations

import logging
from pathlib import Path

from reyn.config import load_config


def _load(tmp_path: Path, yaml_text: str, caplog):
    (tmp_path / "reyn.yaml").write_text(yaml_text, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="reyn.config.loader"):
        cfg = load_config(tmp_path)
    return cfg


def test_a_well_formed_config_produces_no_unknown_key_warning(tmp_path, caplog) -> None:
    """Tier 2: #4174 T0 accept-side (lead-coder's explicit false-positive-noise
    concern) — a real, valid reyn.yaml touching several nested sections
    never trips the warning. If it did, operators would learn to ignore the
    warning entirely, defeating its purpose.

    #4174 T3: `model:` moved under `llm:` — this fixture uses the live
    location so the accept-side guard isn't itself tripping the T3 rename."""
    cfg = _load(
        tmp_path,
        "llm:\n"
        "  model: standard\n"
        "sandbox:\n"
        "  mode: strict\n"
        "  policy:\n"
        "    network: false\n"
        "    timeout_seconds: 30\n",
        caplog,
    )
    assert cfg.sandbox.mode == "strict"
    assert not any("Unrecognized config key" in r.message for r in caplog.records)


def test_an_unrecognized_top_level_key_warns_not_applied(tmp_path, caplog) -> None:
    """Tier 2: #4174 T0 — a config key matching no schema entry at all (not a
    rename) logs a warning saying it was NOT APPLIED, and load_config does
    NOT raise (supersedes the pre-#4174 posture where an unrecognized
    sandbox.policy key would crash construction — see
    tests/security/test_sandbox_factory.py for that side; this test's own
    job is a totally novel top-level key, which was never a raise path even
    pre-#4174, to isolate the NEW warning wiring from any prior behavior)."""
    cfg = _load(tmp_path, "totally_made_up_top_level_key: 1\n", caplog)
    assert cfg is not None  # did not raise
    messages = [r.message for r in caplog.records]
    assert any(
        "totally_made_up_top_level_key" in m and "NOT APPLIED" in m for m in messages
    ), messages


def test_llm_model_is_now_a_known_key_not_flagged(tmp_path, caplog) -> None:
    """Tier 2: #4174 T3 — accept-side, superseding this test's former pre-T3
    shape. Architect's #4174 T0 finding was that `llm.model` was a real,
    pre-existing dead key (`_build_llm_config` only read `router`/`retry`;
    LLMConfig had no `model` field, so an operator writing `llm: {model:
    ...}` had it silently discarded, caught by T0's unknown-key walk with no
    special-casing needed). T3 closed that gap by giving `LLMConfig` a real
    `model` field and having `_build_llm_config` parse it — so the SAME
    input that used to warn "NOT APPLIED" must now warn about nothing AND
    the value must actually reach `cfg.llm.model`, or T3 only moved the
    silence rather than fixing it."""
    cfg = _load(tmp_path, "llm:\n  model: standard\n  router:\n    use: false\n", caplog)
    assert cfg is not None
    assert cfg.llm.model == "standard"
    messages = [r.message for r in caplog.records]
    assert not any("llm.model" in m and "NOT APPLIED" in m for m in messages), messages


def test_unknown_sandbox_policy_key_warning_names_the_effective_policy(
    tmp_path, caplog,
) -> None:
    """Tier 2: #4174 T0 — lead-coder's condition ①: an unknown sandbox.policy
    key's warning names the EFFECTIVE resolved policy alongside the
    unknown-key notice, since dropping a policy key makes the config
    LOOSER (not silently inert like an ordinary dropped key) — an operator
    relying on it must see what's actually in force."""
    cfg = _load(
        tmp_path,
        "sandbox:\n"
        "  policy:\n"
        "    network: false\n"
        "    typo_field_name: true\n",
        caplog,
    )
    assert cfg is not None
    messages = [r.message for r in caplog.records]
    assert any(
        "sandbox.policy.typo_field_name" in m and "NOT APPLIED" in m for m in messages
    ), messages
    assert any("Effective sandbox policy" in m and "network" in m for m in messages), (
        messages
    )


def test_a_removed_top_level_key_warns_delete_not_rewrite(tmp_path, caplog) -> None:
    """Tier 2: #4375 — a key registered in ``_REMOVED_CONFIG_KEYS`` (deleted,
    no successor) is reachable through the SAME real ``load_config`` path
    every other unknown-key case in this file goes through, and its
    warning says the key was removed (not "renamed" or "unrecognized"
    generically) — the real end-to-end wiring, not just
    ``unknown_config_keys()`` in isolation (already covered by
    ``config_schema``'s own direct-call tests)."""
    from reyn.config.config_schema import _REMOVED_CONFIG_KEYS
    assert _REMOVED_CONFIG_KEYS, "the registry must be non-empty for this test to mean anything"
    any_removed_key = next(iter(_REMOVED_CONFIG_KEYS))
    cfg = _load(tmp_path, f"{any_removed_key}: 1\n", caplog)
    assert cfg is not None  # did not raise
    messages = [r.message for r in caplog.records]
    assert any(any_removed_key in m and "no longer exists" in m for m in messages), messages


def test_renamed_sandbox_policy_key_warning_names_the_destination(
    tmp_path, caplog,
) -> None:
    """Tier 2: #4174 T0 — a renamed key (pre-#3823 `write_paths`) gets a hint
    naming the new location in the warning text, reusing the existing rich
    _RENAMED_SANDBOX_POLICY_KEYS guidance rather than a generic notice."""
    cfg = _load(
        tmp_path,
        "sandbox:\n  policy:\n    write_paths: ['/x']\n",
        caplog,
    )
    assert cfg is not None
    messages = [r.message for r in caplog.records]
    assert any(
        "sandbox.policy.write_paths" in m and "allow_write_paths" in m for m in messages
    ), messages
