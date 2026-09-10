"""Tier 2: #5990 (part of, stage 2) — `support_bundle._meta`'s own
`except Exception: reyn_version = "unknown"` swallowed a version-
detection failure with no visible report. Now warns before falling
back, same convention #6038 established.
"""
from __future__ import annotations

import logging

from reyn.interfaces.cli.commands.support_bundle import _meta

_LOGGER_NAME = "reyn.interfaces.cli.commands.support_bundle"


def test_version_detection_failure_warns_and_falls_back_to_unknown(
    monkeypatch, caplog,
) -> None:
    """Tier 2: `importlib.metadata.version("reyn")` raising warns AND
    `_meta` still returns `reyn_version: "unknown"` rather than raising.

    Strip-falsify (verified by hand: the `_log.warning(...)` call
    removed from `_meta`'s first `except Exception`): this test goes red
    — no record at all."""
    import importlib.metadata

    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        meta = _meta(session=None, since_raw=None, manifest=[])

    assert meta["reyn_version"] == "unknown"
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any("installed version" in r.message for r in records)


def test_normal_version_detection_does_not_warn(caplog) -> None:
    """Tier 2: negative control -- a normal (installed, non-raising)
    version lookup never reaches the except branch, so no warning
    fires."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        meta = _meta(session=None, since_raw=None, manifest=[])
    assert meta["reyn_version"] != "unknown"
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
