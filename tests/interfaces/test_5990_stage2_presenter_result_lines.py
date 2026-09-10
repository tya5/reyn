"""Tier 2: #5990 (part of, stage 2) — `presenter._result_detail_lines`'s
own `except Exception: text = str(result)` swallowed a
non-JSON-serializable tool result with no visible report. Now warns
before falling back to `str(result)`, same convention #6038 established.
"""
from __future__ import annotations

import logging

from reyn.interfaces.inline.textual_chat._meta_keys import RESULT_META_KEY
from reyn.interfaces.inline.textual_chat.presenter import _result_detail_lines
from reyn.runtime.outbox import OutboxMessage

_LOGGER_NAME = "reyn.interfaces.inline.textual_chat.presenter"


class _Unserializable:
    """A plain object with no `__repr__` override -- `json.dumps` cannot
    encode it and raises `TypeError`."""


def _settled_tool(result: object) -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_started", text="some_tool",
        meta={RESULT_META_KEY: {"result": result}},
    )


def test_non_serializable_result_warns_and_falls_back_to_str(caplog) -> None:
    """Tier 2: a tool result that `json.dumps` cannot encode warns AND
    `_result_detail_lines` still returns `str(result)`'s own lines
    rather than raising.

    Strip-falsify (verified by hand: the `_log.warning(...)` call
    removed from `_result_detail_lines`'s second `except Exception`):
    this test goes red — no record at all."""
    msg = _settled_tool(_Unserializable())
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        lines = _result_detail_lines(msg)
    assert lines and "_Unserializable" in lines[0]
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any("not JSON-serializable" in r.message for r in records)


def test_serializable_result_does_not_warn(caplog) -> None:
    """Tier 2: negative control -- a JSON-serializable non-dict result
    (a list) parses through with no warning at all."""
    msg = _settled_tool([1, 2, 3])
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _result_detail_lines(msg)
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
