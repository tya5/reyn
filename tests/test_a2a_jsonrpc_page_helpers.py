"""Tier 2: pure helpers in interfaces/web/routers/a2a.py.

  ``_jsonrpc_error(req_id, code, message, data)`` — JSON-RPC 2.0 error envelope
  ``_jsonrpc_result(req_id, result)``             — JSON-RPC 2.0 success envelope
  ``_extract_text_from_parts(parts)``             — text parts → concatenated str
  ``_task_sort_key(task)``                        — (created_at, task_id) sort tuple
  ``_encode_page_token(task)``                    — base64-urlsafe page cursor
  ``_decode_page_token(token)``                   — inverse; None on malformed input
"""
from __future__ import annotations

from types import SimpleNamespace

from reyn.interfaces.web.routers.a2a import (
    _decode_page_token,
    _encode_page_token,
    _extract_text_from_parts,
    _jsonrpc_error,
    _jsonrpc_result,
    _task_sort_key,
)

# ---------------------------------------------------------------------------
# _jsonrpc_error
# ---------------------------------------------------------------------------


def test_jsonrpc_error_envelope_shape() -> None:
    """Tier 2: error envelope has jsonrpc='2.0', id, and error sub-dict."""
    resp = _jsonrpc_error("req-1", -32600, "Invalid Request")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "req-1"
    assert resp["error"]["code"] == -32600
    assert resp["error"]["message"] == "Invalid Request"


def test_jsonrpc_error_without_data_omits_data_key() -> None:
    """Tier 2: error dict has no 'data' key when data arg is None."""
    resp = _jsonrpc_error(1, -32601, "Method not found")
    assert "data" not in resp["error"]


def test_jsonrpc_error_with_data_includes_it() -> None:
    """Tier 2: data is included in the error dict when provided."""
    resp = _jsonrpc_error(1, -32602, "Invalid params", data={"field": "x"})
    assert resp["error"]["data"] == {"field": "x"}


def test_jsonrpc_error_null_req_id_allowed() -> None:
    """Tier 2: req_id=None is valid per JSON-RPC spec (parse errors)."""
    resp = _jsonrpc_error(None, -32700, "Parse error")
    assert resp["id"] is None


# ---------------------------------------------------------------------------
# _jsonrpc_result
# ---------------------------------------------------------------------------


def test_jsonrpc_result_envelope_shape() -> None:
    """Tier 2: result envelope has jsonrpc='2.0', id, and result field."""
    resp = _jsonrpc_result("req-2", {"status": "ok"})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "req-2"
    assert resp["result"] == {"status": "ok"}


def test_jsonrpc_result_none_result_allowed() -> None:
    """Tier 2: result=None is a valid JSON-RPC success response."""
    resp = _jsonrpc_result(42, None)
    assert resp["result"] is None


# ---------------------------------------------------------------------------
# _extract_text_from_parts
# ---------------------------------------------------------------------------


def test_extract_text_from_parts_single_text_part() -> None:
    """Tier 2: single text part returns its text string."""
    parts = [{"kind": "text", "text": "hello"}]
    assert _extract_text_from_parts(parts) == "hello"


def test_extract_text_from_parts_type_key_also_works() -> None:
    """Tier 2: 'type' key is accepted as an alias for 'kind'."""
    parts = [{"type": "text", "text": "world"}]
    assert _extract_text_from_parts(parts) == "world"


def test_extract_text_from_parts_multiple_parts_joined_by_newline() -> None:
    """Tier 2: multiple text parts are joined by newline."""
    parts = [{"kind": "text", "text": "line1"}, {"kind": "text", "text": "line2"}]
    assert _extract_text_from_parts(parts) == "line1\nline2"


def test_extract_text_from_parts_non_text_skipped() -> None:
    """Tier 2: non-text parts (file, data) are silently skipped."""
    parts = [{"kind": "file", "url": "https://x.com/f"}, {"kind": "text", "text": "ok"}]
    assert _extract_text_from_parts(parts) == "ok"


def test_extract_text_from_parts_non_dict_entries_skipped() -> None:
    """Tier 2: non-dict entries in parts list are silently skipped."""
    parts = ["not-a-dict", {"kind": "text", "text": "real"}]
    assert _extract_text_from_parts(parts) == "real"


def test_extract_text_from_parts_empty_list_returns_empty() -> None:
    """Tier 2: empty parts list returns empty string."""
    assert _extract_text_from_parts([]) == ""


# ---------------------------------------------------------------------------
# _task_sort_key
# ---------------------------------------------------------------------------


def test_task_sort_key_returns_created_at_and_task_id() -> None:
    """Tier 2: sort key is (str(created_at), task_id) tuple."""
    task = SimpleNamespace(created_at="2026-01-01T00:00:00Z", task_id="task-abc")
    key = _task_sort_key(task)
    assert key == ("2026-01-01T00:00:00Z", "task-abc")


def test_task_sort_key_created_at_coerced_to_str() -> None:
    """Tier 2: non-string created_at is coerced to str."""
    task = SimpleNamespace(created_at=12345, task_id="t1")
    key = _task_sort_key(task)
    assert key[0] == "12345"


# ---------------------------------------------------------------------------
# _encode_page_token / _decode_page_token round-trip
# ---------------------------------------------------------------------------


def test_encode_decode_page_token_round_trip() -> None:
    """Tier 2: encoded token decodes back to (created_at, task_id)."""
    task = SimpleNamespace(created_at="2026-06-01T12:00:00Z", task_id="task-xyz")
    token = _encode_page_token(task)
    result = _decode_page_token(token)
    assert result == ("2026-06-01T12:00:00Z", "task-xyz")


def test_decode_page_token_malformed_returns_none() -> None:
    """Tier 2: malformed token string returns None."""
    assert _decode_page_token("not-valid-base64!!!") is None


def test_decode_page_token_valid_base64_but_wrong_json_returns_none() -> None:
    """Tier 2: valid base64 but invalid JSON content returns None."""
    import base64
    bad = base64.urlsafe_b64encode(b"not json at all").decode()
    assert _decode_page_token(bad) is None


def test_decode_page_token_missing_id_key_returns_none() -> None:
    """Tier 2: JSON missing 'id' key returns None (KeyError → None)."""
    import base64
    import json
    raw = base64.urlsafe_b64encode(json.dumps({"created_at": "x"}).encode()).decode()
    assert _decode_page_token(raw) is None
