"""Tier 2 tests for LLM trace dump production hardening.

Covers:
- Size limit + rotation (_maybe_rotate_dump, _get_trace_dump_max_size)
- Secrets redaction (_redact_secrets, _get_extra_redact_patterns)

Testing policy alignment:
- No MagicMock / AsyncMock / patch — all collaborators are real or real callables.
- No private-state assertions — behaviour observed through public API (file system,
  return values, env vars via monkeypatch.setenv).
- No algorithm-level pinning.
- Each docstring first line declares Tier 2.
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Rotation tests
# ---------------------------------------------------------------------------


class TestRotation:
    """Tier 2: _maybe_rotate_dump size-limit + rotation behaviour."""

    def test_no_rotation_when_below_limit(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: file below size limit is not rotated."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        trace_file.write_text("x" * 10, encoding="utf-8")  # 10 bytes

        monkeypatch.setenv("REYN_LLM_TRACE_DUMP_MAX_SIZE", str(100))

        llm_mod._maybe_rotate_dump(str(trace_file))

        assert trace_file.exists(), "Original file must survive when below limit"
        assert not (tmp_path / "trace.jsonl.1").exists(), "No .1 file when below limit"

    def test_rotation_when_exceeds_limit(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: file exceeding size limit is renamed to <path>.1."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        trace_file.write_text("x" * 200, encoding="utf-8")  # 200 bytes

        monkeypatch.setenv("REYN_LLM_TRACE_DUMP_MAX_SIZE", str(100))

        llm_mod._maybe_rotate_dump(str(trace_file))

        rotated = tmp_path / "trace.jsonl.1"
        assert rotated.exists(), ".1 file must exist after rotation"
        assert not trace_file.exists(), "Original file must be gone after rotation"

    def test_rotation_overwrites_existing_dot1(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: pre-existing <path>.1 is replaced (single-generation policy)."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        trace_file.write_text("new" * 100, encoding="utf-8")  # 300 bytes

        old_rotated = tmp_path / "trace.jsonl.1"
        old_rotated.write_text("old_content", encoding="utf-8")

        monkeypatch.setenv("REYN_LLM_TRACE_DUMP_MAX_SIZE", str(100))

        llm_mod._maybe_rotate_dump(str(trace_file))

        assert old_rotated.exists(), ".1 file must still exist"
        # Old content replaced — new rotation content is "new" * 100
        assert old_rotated.read_text(encoding="utf-8") != "old_content", (
            ".1 must be overwritten by the rotated file"
        )

    def test_max_size_env_var_overrides_default(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: REYN_LLM_TRACE_DUMP_MAX_SIZE env var overrides the 100 MB default."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        trace_file.write_text("x" * 50, encoding="utf-8")  # 50 bytes

        # With a limit of 200, no rotation expected
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP_MAX_SIZE", str(200))
        llm_mod._maybe_rotate_dump(str(trace_file))
        assert trace_file.exists(), "No rotation when file is below the custom limit"

        # With a limit of 10, rotation expected
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP_MAX_SIZE", str(10))
        llm_mod._maybe_rotate_dump(str(trace_file))
        assert not trace_file.exists(), "Rotation triggered by custom limit"

    def test_rotation_failure_is_silent(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: OSError during rotation falls through silently (dump continues)."""
        import reyn.llm.llm as llm_mod

        # Point to a non-existent file — _maybe_rotate_dump should return without error
        non_existent = str(tmp_path / "no_such_file.jsonl")
        # No exception should be raised
        llm_mod._maybe_rotate_dump(non_existent)

    def test_dump_request_rotates_before_write(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_request triggers rotation when file exceeds limit."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        # Fill file beyond the tiny limit
        trace_file.write_text("x" * 200, encoding="utf-8")

        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP_MAX_SIZE", str(100))

        rid = llm_mod._dump_llm_request({"model": "m", "messages": []})

        assert rid is not None
        assert trace_file.exists(), "New dump file must be created after rotation"
        rotated = tmp_path / "trace.jsonl.1"
        assert rotated.exists(), ".1 file must contain the pre-rotation content"

        # The new file has the fresh request record
        lines = [l for l in trace_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines, "new trace file must contain at least one record after rotation"
        record = json.loads(lines[0])
        assert record["kind"] == "request"


# ---------------------------------------------------------------------------
# Redaction tests
# ---------------------------------------------------------------------------


class TestRedaction:
    """Tier 2: _redact_secrets pattern matching and opt-out behaviour."""

    def test_openai_key_is_redacted(self, monkeypatch) -> None:
        """Tier 2: sk-... string is replaced with [REDACTED:openai-key]."""
        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT_PATTERNS", raising=False)

        result = llm_mod._redact_secrets({"content": "key=sk-FAKE-FOR-TESTING-DO-NOT-USE-A"})
        assert "[REDACTED:openai-key]" in result["content"]
        assert "sk-abcDEF" not in result["content"]

    def test_slack_token_is_redacted(self, monkeypatch) -> None:
        """Tier 2: xoxb-... string is replaced with [REDACTED:slack-token]."""
        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT_PATTERNS", raising=False)

        result = llm_mod._redact_secrets({"token": "xoxb-FAKE-FOR-TESTING-DO-NOT-USE"})
        assert "[REDACTED:slack-token]" in result["token"]
        assert "xoxb-" not in result["token"]

    def test_private_key_block_is_redacted(self, monkeypatch) -> None:
        """Tier 2: PEM private key block is replaced with [REDACTED:private-key]."""
        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT_PATTERNS", raising=False)

        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ...\n-----END RSA PRIVATE KEY-----"
        result = llm_mod._redact_secrets({"key_data": pem})
        assert "[REDACTED:private-key]" in result["key_data"]
        assert "BEGIN RSA PRIVATE KEY" not in result["key_data"]

    def test_custom_pattern_via_env_var(self, monkeypatch) -> None:
        """Tier 2: REYN_LLM_TRACE_REDACT_PATTERNS adds extra masking patterns."""
        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
        # Match "MYSECRET-" followed by alphanumeric chars
        monkeypatch.setenv("REYN_LLM_TRACE_REDACT_PATTERNS", r"MYSECRET-[A-Za-z0-9]+")

        result = llm_mod._redact_secrets({"msg": "token=MYSECRET-abc123xyz"})
        assert "[REDACTED:custom-0]" in result["msg"]
        assert "MYSECRET-" not in result["msg"]

    def test_redact_off_disables_redaction(self, monkeypatch) -> None:
        """Tier 2: REYN_LLM_TRACE_REDACT=off returns payload unchanged."""
        import reyn.llm.llm as llm_mod

        monkeypatch.setenv("REYN_LLM_TRACE_REDACT", "off")
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT_PATTERNS", raising=False)

        payload = {"content": "sk-FAKE-FOR-TESTING-DO-NOT-USE-A is here"}
        result = llm_mod._redact_secrets(payload)
        assert result["content"] == payload["content"], (
            "Payload must be unchanged when REYN_LLM_TRACE_REDACT=off"
        )

    def test_recursive_redaction_in_nested_dict(self, monkeypatch) -> None:
        """Tier 2: redaction walks nested dicts and lists."""
        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT_PATTERNS", raising=False)

        payload = {
            "messages": [
                {"role": "user", "content": "my key is sk-FAKE-FOR-TESTING-DO-NOT-USE-A ok?"},
                {"role": "assistant", "content": "sure"},
            ],
            "meta": {"nested": {"deep": "sk-FAKE-FOR-TESTING-DO-NOT-USE-B end"}},
        }
        result = llm_mod._redact_secrets(payload)

        assert "[REDACTED:openai-key]" in result["messages"][0]["content"]
        assert "[REDACTED:openai-key]" in result["meta"]["nested"]["deep"]
        assert result["messages"][1]["content"] == "sure"

    def test_non_string_values_are_untouched(self, monkeypatch) -> None:
        """Tier 2: integers, booleans, and None pass through redaction unchanged."""
        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT_PATTERNS", raising=False)

        payload = {"count": 42, "flag": True, "nothing": None}
        result = llm_mod._redact_secrets(payload)

        assert result["count"] == 42
        assert result["flag"] is True
        assert result["nothing"] is None

    def test_dump_request_applies_redaction(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_request writes redacted content to the dump file."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT_PATTERNS", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_DUMP_MAX_SIZE", raising=False)

        payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "api key: sk-FAKE-FOR-TESTING-DO-NOT-USE-C"}],
        }
        llm_mod._dump_llm_request(payload)

        content = trace_file.read_text(encoding="utf-8")
        assert "sk-ABCDEFGHIJ" not in content, "Raw API key must not appear in dump"
        assert "[REDACTED:openai-key]" in content

    def test_dump_response_applies_redaction(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_response writes redacted content to the dump file."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_REDACT_PATTERNS", raising=False)
        monkeypatch.delenv("REYN_LLM_TRACE_DUMP_MAX_SIZE", raising=False)

        # Write a request first so the file exists
        rid = llm_mod._dump_llm_request({"model": "m", "messages": []})

        llm_mod._dump_llm_response(
            rid,
            {"content": "secret=xoxb-FAKE-FOR-TESTING-DO-NOT-USE", "finish_reason": "stop", "usage": {}},
        )

        content = trace_file.read_text(encoding="utf-8")
        assert "xoxb-" not in content, "Raw Slack token must not appear in dump"
        assert "[REDACTED:slack-token]" in content
