"""Tier 2: #5990 (part of, stage 2) — `renderer.summarize_tool_result`'s
own `except Exception: summary = _short(result, 80)` swallowed a
summarization failure with no visible report. Now warns before falling
back to a short repr, same convention #6038 established.
"""
from __future__ import annotations

import logging

import reyn.interfaces.repl.renderer as renderer_module

_LOGGER_NAME = "reyn.interfaces.repl.renderer"


def test_summarize_failure_warns_and_falls_back_to_short_repr(monkeypatch, caplog) -> None:
    """Tier 2: `_summarize_result` raising warns AND
    `summarize_tool_result` still returns a (neutralized) short repr
    rather than raising.

    Strip-falsify (verified by hand: the `_log.warning(...)` call
    removed from `summarize_tool_result`'s `except Exception`): this
    test goes red — no record at all."""
    def _raise(tool, result):
        raise RuntimeError("simulated summarization failure")

    monkeypatch.setattr(renderer_module, "_summarize_result", _raise)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        summary = renderer_module.summarize_tool_result("read_file", "some result")

    assert summary  # a non-empty fallback string, never an exception
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any("read_file" in r.message for r in records)


def test_normal_summarize_does_not_warn(caplog) -> None:
    """Tier 2: negative control -- a normal (non-raising) summarization
    never reaches the except branch, so no warning fires."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        renderer_module.summarize_tool_result("read_file", "some result")
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
