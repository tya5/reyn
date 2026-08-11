"""Tier 2c: `load_config()` exports `REYN_FETCH_ALLOW_PRIVATE_IPS` from the real
`web_fetch.allow_private_ips` yaml key (#4317).

#4174 T4 split the old `web:` key into top-level `web_fetch:` / `gateway:`
blocks. `loader.py`'s `REYN_FETCH_ALLOW_PRIVATE_IPS` export (#1956 — the
config-less SSRF-guard surfaces' only entry point: the `safe.http` subprocess
and the registry main-process modules) was never updated to the new key name —
it kept reading `merged.get("web").get("fetch")`, which is always `None` post-T4
(`web:` is now an unknown key), so the export silently stopped firing. Fail-secure
(the guard's own deny-private default took over) but "configured yet not
applied" is its own bug, not a hole.

This pins the REAL loader behavior end to end: write a `web_fetch.allow_private_ips:
true` yaml, run it through the real `load_config()`, assert the env var comes out
set. No assertion on the OLD key's non-effect — a deleted key not doing anything
is not this test's job (six questions § 2: that's self-evident, not a behavior
this file owns).

Falsify-verified: reverting `loader.py`'s export block to read
`merged.get("web").get("fetch")` (the pre-fix shape) makes this go RED.
"""
from __future__ import annotations

import os
from pathlib import Path

from reyn.config.loader import load_config


def test_web_fetch_allow_private_ips_true_exports_the_env_var(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2c: `web_fetch.allow_private_ips: true` in reyn.yaml, through the
    real loader, sets REYN_FETCH_ALLOW_PRIVATE_IPS so the config-less SSRF-guard
    surfaces (safe.http subprocess / registry main-process modules) see the
    operator's opt-in."""
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    (tmp_path / "reyn.yaml").write_text(
        "llm:\n  model: standard\nweb_fetch:\n  allow_private_ips: true\n"
    )

    load_config(cwd=tmp_path)

    assert os.environ.get("REYN_FETCH_ALLOW_PRIVATE_IPS") == "1"


def test_web_fetch_allow_private_ips_absent_leaves_the_env_var_unset(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2c: the fail-secure default — no `web_fetch.allow_private_ips` in
    yaml, and no pre-set env var, leaves REYN_FETCH_ALLOW_PRIVATE_IPS unset (the
    guard's own deny-private default governs)."""
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n")

    load_config(cwd=tmp_path)

    assert os.environ.get("REYN_FETCH_ALLOW_PRIVATE_IPS") is None


def test_operator_set_env_var_wins_over_config(tmp_path: Path, monkeypatch) -> None:
    """Tier 2c: an explicit operator-set env var is never overwritten by the
    config-derived export — the loader only sets the var when it isn't already
    present."""
    monkeypatch.setenv("REYN_FETCH_ALLOW_PRIVATE_IPS", "0")
    (tmp_path / "reyn.yaml").write_text(
        "llm:\n  model: standard\nweb_fetch:\n  allow_private_ips: true\n"
    )

    load_config(cwd=tmp_path)

    assert os.environ.get("REYN_FETCH_ALLOW_PRIVATE_IPS") == "0"
