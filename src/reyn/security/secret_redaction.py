"""Secret redaction for compaction input (FP-0050 / #1822 S3, #1820).

A pattern-driven redaction pass: replace credential / token / key VALUES with a
``[REDACTED:<kind>]`` placeholder so secrets in tool results / turn text are not
baked into the persisted compaction summary (history.jsonl). S1 confirmed there
is no existing redaction fn to reuse (``security/secrets/`` is interpolation +
oauth, not scrubbing), so this is the new redaction surface.

Distinct from ``threat_patterns.scan`` (which *detects* injection) and from the
fence (which *marks* untrusted content): redaction *removes* the secret value.
The patterns mirror the secret/exfil intent of ``threat_patterns`` but capture
the value group for substitution. Kept high-confidence + tight to protect legit
content (FP-rate is the design gate, FP-0050 §3.4): specific key names + a
minimum value length, well-known key formats, and PEM private-key blocks.

Pure: no I/O, no config, no skill knowledge.
"""
from __future__ import annotations

import re

_PLACEHOLDER = "[REDACTED:{kind}]"

# (regex, kind, value_group) — re.sub replaces the value group with the
# placeholder, preserving the surrounding key/label so the summarizer still
# sees that a credential WAS present (just not its value).
_REDACTIONS: tuple[tuple["re.Pattern[str]", str, int], ...] = (
    # key = "value" / token: VALUE — specific credential key names + len>=16.
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|client[_-]?secret|token|access[_-]?token|"
            r"auth[_-]?token|refresh[_-]?token|password|passwd|pwd)\b['\"]?\s*[=:]\s*['\"]?"
            r"([A-Za-z0-9+/=_\-\.]{16,})['\"]?"
        ),
        "credential",
        2,
    ),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9+/=_\-\.]{16,})"), "bearer", 2),
    # AWS access key id.
    (re.compile(r"\b(AKIA)([0-9A-Z]{16})\b"), "aws_key", 2),
    # GitHub-style tokens (ghp_/gho_/ghs_/github_pat_…).
    (re.compile(r"\b(gh[poasu]_|github_pat_)([A-Za-z0-9_]{20,})"), "gh_token", 2),
    # PEM private key block.
    (
        re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)([\s\S]*?)(-----END [A-Z ]*PRIVATE KEY-----)"),
        "private_key",
        2,
    ),
)


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential/token/key VALUES replaced by placeholders.

    No-op when ``text`` contains no recognised secret. The label/key portion is
    preserved (only the secret value is removed), so the summarizer can still
    note that a credential was referenced.
    """
    if not text:
        return text
    for rx, kind, vgrp in _REDACTIONS:
        def _sub(m: "re.Match[str]", _kind: str = kind, _vgrp: int = vgrp) -> str:
            s = m.group(0)
            value = m.group(_vgrp)
            return s.replace(value, _PLACEHOLDER.format(kind=_kind), 1)

        text = rx.sub(_sub, text)
    return text
