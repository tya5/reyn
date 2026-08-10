"""Tier 2: llm._redact_secrets walks ALL containers, not just dict/list.

Found via bug-mining (2026-06-20). The trace/bundle redaction walker
(`reyn.llm.llm._redact_secrets`) recursed into ``dict`` values and ``list``
items, but a secret inside a ``tuple`` or ``set``, or used as a ``dict`` KEY,
passed through untouched — a redaction MISS in a security primitive. Because the
redacted copy is ``json.dumps``-ed by every caller (where a tuple already
becomes an array), a secret hidden in a tuple would be written to the trace /
support-bundle in the clear.

Falsification: pre-fix each of the container cases below leaked ``_SECRET``
(verified). The fix recurses into tuple/set and masks string dict keys.

No mocks — the real `_redact_secrets` with the real default patterns.
"""
from __future__ import annotations

import json

import pytest

from reyn.llm.llm import _redact_secrets

# openai-key-shaped token (matches the sk-[A-Za-z0-9_-]{20,} default pattern)
_SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0KLMNOP"


@pytest.fixture(autouse=True)
def _redaction_on(monkeypatch):
    # The walker is a no-op when REYN_LLM_TRACE_REDACT=off — ensure it's ON.
    monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)


def _leaks(payload: object) -> bool:
    return _SECRET in repr(_redact_secrets(payload))


def test_secret_in_dict_value_redacted_baseline() -> None:
    """Tier 2: the already-supported dict/list path still redacts (baseline)."""
    assert not _leaks({"k": _SECRET})
    assert not _leaks({"k": [_SECRET]})


def test_secret_in_tuple_is_redacted() -> None:
    """Tier 2: a secret inside a tuple is redacted (was leaked pre-fix).

    Falsification: the unfixed walker returned the tuple untouched, so the
    secret survived into the json-serialized trace.
    """
    assert not _leaks({"k": (_SECRET,)})
    assert not _leaks({"k": [(_SECRET,)]})  # nested tuple-in-list


def test_secret_in_set_is_redacted_and_serializable() -> None:
    """Tier 2: a secret inside a set is redacted AND the result json-serializes.

    A set isn't json-serializable at all; the fix emits a redacted list so the
    trace both succeeds and carries no secret.
    """
    redacted = _redact_secrets({"k": {_SECRET}})
    assert _SECRET not in repr(redacted)
    json.dumps(redacted)  # must not raise (set → list)


def test_secret_as_dict_key_is_redacted() -> None:
    """Tier 2: a secret used as a dict KEY is redacted (was leaked pre-fix)."""
    assert not _leaks({_SECRET: "value"})


def test_legit_content_not_over_redacted() -> None:
    """Tier 2: ordinary container content is untouched (no false-positive)."""
    out = _redact_secrets({"msg": "hello", "items": ("a", "b"), "n": 3})
    assert out == {"msg": "hello", "items": ["a", "b"], "n": 3}
