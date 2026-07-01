"""Tier 2: inline app config-extraction helpers — _extract_cron_jobs,
_extract_mcp_servers, _extract_hooks.

Each helper is a pure config → list[dict] converter that backs the "More" status-bar
expansion.  They must be graceful on None / absent / malformed config so startup
with an incomplete reyn config can't crash the status bar.
"""
from __future__ import annotations

from reyn.interfaces.inline.app import (
    _extract_cron_jobs,
    _extract_hooks,
    _extract_mcp_servers,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _ns(**kwargs):
    """SimpleNamespace-like for test configs."""
    from types import SimpleNamespace
    return SimpleNamespace(**kwargs)


# ── _extract_cron_jobs ─────────────────────────────────────────────────────


def test_cron_no_cron_attr_returns_empty() -> None:
    """Tier 2: config with no .cron attr → []."""
    assert _extract_cron_jobs(object()) == []


def test_cron_none_jobs_attr_returns_empty() -> None:
    """Tier 2: config.cron with no .jobs attr → []."""
    config = _ns(cron=_ns())  # no jobs
    assert _extract_cron_jobs(config) == []


def test_cron_empty_jobs_returns_empty() -> None:
    """Tier 2: config.cron.jobs = [] → []."""
    config = _ns(cron=_ns(jobs=[]))
    assert _extract_cron_jobs(config) == []


def test_cron_valid_job_extracted() -> None:
    """Tier 2: a valid cron job yields a dict with name, schedule, enabled."""
    job = _ns(name="nightly", schedule="0 0 * * *", enabled=True)
    config = _ns(cron=_ns(jobs=[job]))
    result = _extract_cron_jobs(config)
    assert result, "expected one job entry"
    assert result[0]["name"] == "nightly"
    assert result[0]["schedule"] == "0 0 * * *"
    assert result[0]["enabled"] is True


def test_cron_multiple_jobs_all_extracted() -> None:
    """Tier 2: multiple valid jobs → one dict per job."""
    jobs = [
        _ns(name="a", schedule="* * * * *", enabled=True),
        _ns(name="b", schedule="0 1 * * *", enabled=False),
    ]
    config = _ns(cron=_ns(jobs=jobs))
    result = _extract_cron_jobs(config)
    assert {d["name"] for d in result} == {"a", "b"}


def test_cron_malformed_job_skipped_gracefully() -> None:
    """Tier 2: a job object that raises on attribute access is silently skipped."""

    class _BadJob:
        @property
        def name(self):
            raise RuntimeError("broken")

    config = _ns(cron=_ns(jobs=[_BadJob()]))
    result = _extract_cron_jobs(config)
    assert result == []


# ── _extract_mcp_servers ───────────────────────────────────────────────────


def test_mcp_none_returns_empty() -> None:
    """Tier 2: config.mcp is None → []."""
    config = _ns(mcp=None)
    assert _extract_mcp_servers(config) == []


def test_mcp_no_attr_returns_empty() -> None:
    """Tier 2: config with no .mcp attr → []."""
    assert _extract_mcp_servers(object()) == []


def test_mcp_not_dict_returns_empty() -> None:
    """Tier 2: config.mcp is a non-dict object → []."""
    config = _ns(mcp=_ns(servers=None))  # namespace, not dict
    assert _extract_mcp_servers(config) == []


def test_mcp_dict_with_servers_subkey() -> None:
    """Tier 2: config.mcp = {'servers': {'srv1': {...}, 'srv2': {...}}} → 2 name dicts."""
    config = _ns(mcp={"servers": {"srv1": {}, "srv2": {}}})
    result = _extract_mcp_servers(config)
    names = {d["name"] for d in result}
    assert names == {"srv1", "srv2"}


def test_mcp_flat_dict_style() -> None:
    """Tier 2: flat mcp dict (no 'servers' key) → each dict-value entry by name."""
    config = _ns(mcp={"myserver": {"url": "http://x"}, "other": {"url": "http://y"}})
    result = _extract_mcp_servers(config)
    names = {d["name"] for d in result}
    assert names == {"myserver", "other"}


def test_mcp_flat_dict_skips_non_dict_values() -> None:
    """Tier 2: flat mcp dict with non-dict values (e.g. a string) skips them."""
    config = _ns(mcp={"real": {"url": "http://x"}, "bad": "not_a_dict"})
    result = _extract_mcp_servers(config)
    assert result, "expected one server entry"
    assert result[0]["name"] == "real"


# ── _extract_hooks ─────────────────────────────────────────────────────────


def test_hooks_no_attr_returns_empty() -> None:
    """Tier 2: config with no .hooks attr → []."""
    assert _extract_hooks(object()) == []


def test_hooks_none_returns_empty() -> None:
    """Tier 2: config.hooks is None → []."""
    config = _ns(hooks=None)
    assert _extract_hooks(config) == []


def test_hooks_empty_list_returns_empty() -> None:
    """Tier 2: config.hooks = [] → []."""
    config = _ns(hooks=[])
    assert _extract_hooks(config) == []


def test_hooks_dict_with_event_key_uses_event_as_label() -> None:
    """Tier 2: a hook dict with an 'event' key uses that value as the label."""
    config = _ns(hooks=[{"event": "pre_commit", "cmd": "lint"}])
    result = _extract_hooks(config)
    assert result, "expected one hook entry"
    assert result[0]["label"] == "pre_commit"


def test_hooks_dict_without_recognized_key_uses_first_key() -> None:
    """Tier 2: a hook dict without recognized event keys falls back to first key's value."""
    config = _ns(hooks=[{"action": "deploy", "when": "nightly"}])
    result = _extract_hooks(config)
    assert result, "expected one hook entry"
    assert "deploy" in result[0]["label"]


def test_hooks_non_dict_entry_converted_to_str_label() -> None:
    """Tier 2: a non-dict hook entry (e.g. a string) gets str()-converted as label."""
    config = _ns(hooks=["simple_hook"])
    result = _extract_hooks(config)
    assert result, "expected one hook entry"
    assert "simple_hook" in result[0]["label"]


def test_hooks_multiple_entries_all_extracted() -> None:
    """Tier 2: multiple hook entries → one dict per entry."""
    config = _ns(hooks=[
        {"event": "alpha"},
        {"event": "beta"},
    ])
    result = _extract_hooks(config)
    labels = {d["label"] for d in result}
    assert labels == {"alpha", "beta"}
