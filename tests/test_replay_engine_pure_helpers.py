"""Tier 2: data/replay/engine.py record-classifier pure helper contracts.

_is_wal_record(rec) classifies a record as WAL-style (has integer 'seq' + string 'kind').
_is_llm_request(rec) classifies a record as an LLM request (kind='request' + 'request_id').
_is_llm_response(rec) classifies a record as an LLM response (kind='response' + 'request_id').

These classifiers partition a flat JSONL record list into WAL events and LLM
request/response pairs without mutation or I/O.
"""
from __future__ import annotations

from reyn.data.replay.engine import _is_llm_request, _is_llm_response, _is_wal_record

# ── _is_wal_record ────────────────────────────────────────────────────────────


def test_is_wal_record_with_int_seq_and_str_kind() -> None:
    """Tier 2: integer seq + string kind → True (canonical WAL record shape)."""
    assert _is_wal_record({"seq": 1, "kind": "turn_started"}) is True


def test_is_wal_record_seq_zero_is_valid() -> None:
    """Tier 2: seq=0 is a valid integer — first WAL record is not excluded."""
    assert _is_wal_record({"seq": 0, "kind": "session_started"}) is True


def test_is_wal_record_string_seq_returns_false() -> None:
    """Tier 2: string seq fails the isinstance(seq, int) guard."""
    assert _is_wal_record({"seq": "1", "kind": "turn_started"}) is False


def test_is_wal_record_missing_seq_returns_false() -> None:
    """Tier 2: record without 'seq' is not a WAL record."""
    assert _is_wal_record({"kind": "turn_started"}) is False


def test_is_wal_record_missing_kind_returns_false() -> None:
    """Tier 2: record without 'kind' is not a WAL record."""
    assert _is_wal_record({"seq": 1}) is False


def test_is_wal_record_empty_dict_returns_false() -> None:
    """Tier 2: empty dict returns False."""
    assert _is_wal_record({}) is False


# ── _is_llm_request ───────────────────────────────────────────────────────────


def test_is_llm_request_canonical_shape() -> None:
    """Tier 2: kind='request' + 'request_id' present → True."""
    assert _is_llm_request({"kind": "request", "request_id": "req-abc"}) is True


def test_is_llm_request_missing_request_id_returns_false() -> None:
    """Tier 2: kind='request' without 'request_id' → False."""
    assert _is_llm_request({"kind": "request"}) is False


def test_is_llm_request_wrong_kind_returns_false() -> None:
    """Tier 2: kind='response' with 'request_id' is not a request."""
    assert _is_llm_request({"kind": "response", "request_id": "req-abc"}) is False


def test_is_llm_request_empty_dict_returns_false() -> None:
    """Tier 2: empty dict returns False."""
    assert _is_llm_request({}) is False


# ── _is_llm_response ──────────────────────────────────────────────────────────


def test_is_llm_response_canonical_shape() -> None:
    """Tier 2: kind='response' + 'request_id' present → True."""
    assert _is_llm_response({"kind": "response", "request_id": "req-abc"}) is True


def test_is_llm_response_missing_request_id_returns_false() -> None:
    """Tier 2: kind='response' without 'request_id' → False."""
    assert _is_llm_response({"kind": "response"}) is False


def test_is_llm_response_wrong_kind_returns_false() -> None:
    """Tier 2: kind='request' with 'request_id' is not a response."""
    assert _is_llm_response({"kind": "request", "request_id": "req-abc"}) is False


def test_is_llm_response_empty_dict_returns_false() -> None:
    """Tier 2: empty dict returns False."""
    assert _is_llm_response({}) is False


def test_request_and_response_are_mutually_exclusive() -> None:
    """Tier 2: no single record is simultaneously a request and a response."""
    rec_req = {"kind": "request", "request_id": "r1"}
    rec_resp = {"kind": "response", "request_id": "r1"}
    assert not (_is_llm_request(rec_req) and _is_llm_response(rec_req))
    assert not (_is_llm_request(rec_resp) and _is_llm_response(rec_resp))
