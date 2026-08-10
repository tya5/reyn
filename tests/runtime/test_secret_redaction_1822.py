"""Tier 2: secret redaction for compaction input (FP-0050 / #1822 S3, #1820).

The redaction pass strips credential/token VALUES from turn text before it
enters the summarizer (so secrets aren't baked into the persisted summary).
Real patterns + the real compaction seam, no mocks.

Falsification: the legit-content tests prove the patterns don't over-redact
(FP gate); the seam tests prove redaction is load-bearing — without the
``redact`` fn the secret survives into the compactor input.
"""
from __future__ import annotations

from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import _turn_to_compactor_input
from reyn.security.secret_redaction import redact_secrets

_SECRET = "AKIAIOSFODNN7EXAMPLE1234"


def test_redacts_credential_assignment():
    """Tier 2: a credential key=value has its value redacted, label preserved."""
    out = redact_secrets(f'api_key = "{_SECRET}"')
    assert _SECRET not in out
    assert "REDACTED" in out
    assert "api_key" in out  # the label survives so the summary notes a key was used


def test_redacts_bearer_token():
    """Tier 2: Authorization: Bearer <token> is redacted."""
    out = redact_secrets("Authorization: Bearer abcdef0123456789abcdefXY")
    assert "abcdef0123456789abcdefXY" not in out
    assert "Bearer" in out


def test_redacts_aws_and_github_tokens():
    """Tier 2: well-known key formats (AWS AKIA, GitHub ghp_) are redacted."""
    out = redact_secrets("aws AKIAIOSFODNN7EXAMPLE then gh ghp_1234567890abcdefghijklmno")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "ghp_1234567890abcdefghijklmno" not in out


def test_redacts_pem_private_key():
    """Tier 2: a PEM private-key block body is redacted, markers preserved."""
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabcDEF1234secretbody\n-----END RSA PRIVATE KEY-----"
    out = redact_secrets(pem)
    assert "MIIabcDEF1234secretbody" not in out
    assert "BEGIN RSA PRIVATE KEY" in out


def test_preserves_legit_content():
    """Tier 2: ordinary prose is unchanged (FP gate)."""
    legit = "The function reads a config file and returns its contents as a string token list."
    assert redact_secrets(legit) == legit


def test_short_value_not_redacted():
    """Tier 2: sub-16-char values are not redacted (FP guard — see §3.4 note)."""
    out = redact_secrets('password = "short"')
    assert "short" in out


# ── compaction seam (EP2) ──────────────────────────────────────────────────

def test_compactor_input_redacts_with_fn():
    """Tier 2: _turn_to_compactor_input(redact=) strips the secret from text."""
    t = ChatMessage(role="tool", content=f'{{"token": "{_SECRET}"}}', ts="t")
    out = _turn_to_compactor_input(t, redact=redact_secrets)
    assert _SECRET not in out["text"]


def test_compactor_input_no_redact_passthrough():
    """Tier 2: without redact the secret survives into the input (falsify —
    proves the redaction is load-bearing, not incidental)."""
    t = ChatMessage(role="tool", content=f'{{"token": "{_SECRET}"}}', ts="t")
    out = _turn_to_compactor_input(t)
    assert _SECRET in out["text"]
