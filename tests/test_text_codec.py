"""Tier 2: data/workspace text_codec — decode_text_or_none + encode_text contracts.

The codec is the single decode/encode ladder for all file ops (read / grep / edit /
write).  Its ladder order is load-bearing (BOM → UTF-8 → NUL-sniff → charset-normalizer
→ None).  Pinning the paths prevents a silent regression in any step from silently
mis-decoding or corrupting files.
"""
from __future__ import annotations

import codecs

from reyn.data.workspace.text_codec import decode_text_or_none, encode_text

# ── decode_text_or_none ────────────────────────────────────────────────────


def test_decode_utf8_plain() -> None:
    """Tier 2: plain UTF-8 → (text, None) — encoding is None on the fast path."""
    text, enc = decode_text_or_none(b"hello world")
    assert text == "hello world"
    assert enc is None


def test_decode_utf8_with_multibyte() -> None:
    """Tier 2: UTF-8 multibyte (Japanese) decodes correctly."""
    raw = "こんにちは".encode("utf-8")
    text, enc = decode_text_or_none(raw)
    assert text == "こんにちは"
    assert enc is None


def test_decode_utf8_bom() -> None:
    """Tier 2: UTF-8 BOM → 'utf-8-sig' codec; BOM stripped from result."""
    raw = codecs.BOM_UTF8 + "hello".encode("utf-8")
    text, enc = decode_text_or_none(raw)
    assert text == "hello"
    assert enc == "utf-8-sig"


def test_decode_utf16_le_bom() -> None:
    """Tier 2: UTF-16-LE BOM → 'utf-16' codec; text decoded correctly."""
    raw = "hi".encode("utf-16")  # stdlib encodes utf-16 with BOM
    text, enc = decode_text_or_none(raw)
    assert text == "hi"
    assert enc == "utf-16"


def test_decode_binary_nul_returns_none() -> None:
    """Tier 2: payload with NUL bytes (no BOM, not UTF-8) → (None, None)."""
    binary = b"\xde\xad\xbe\xef\x00\xff"
    text, enc = decode_text_or_none(binary)
    assert text is None
    assert enc is None


def test_decode_empty_bytes() -> None:
    """Tier 2: empty bytes → ('', None) — empty file is valid UTF-8."""
    text, enc = decode_text_or_none(b"")
    assert text == ""
    assert enc is None


def test_decode_strict_utf8_failure_without_nul_falls_back() -> None:
    """Tier 2: non-UTF-8 without NUL bytes falls through to charset-normalizer.

    A latin-1 byte (0x80–0xFF range, no NUL) is not valid UTF-8 and has no
    BOM.  charset-normalizer can often detect the encoding; the function returns
    either a decoded string or (None, None) — never raises.
    """
    # latin-1 for 'é': 0xE9 — invalid UTF-8, no NUL
    raw = b"caf\xe9"
    text, enc = decode_text_or_none(raw)
    # Either successfully detected or gracefully returned None — never raises.
    assert isinstance(text, str) or text is None


# ── encode_text ────────────────────────────────────────────────────────────


def test_encode_text_none_encoding_uses_utf8() -> None:
    """Tier 2: encoding=None → plain UTF-8 bytes."""
    assert encode_text("hello", None) == b"hello"


def test_encode_text_utf8_sig_preserves_bom() -> None:
    """Tier 2: encoding='utf-8-sig' → UTF-8 bytes WITH BOM prefix."""
    result = encode_text("hello", "utf-8-sig")
    assert result is not None
    assert result.startswith(codecs.BOM_UTF8)


def test_encode_text_named_encoding() -> None:
    """Tier 2: named encoding ('latin-1') encodes correctly."""
    result = encode_text("café", "latin-1")
    assert result == "café".encode("latin-1")


def test_encode_text_not_representable_returns_none() -> None:
    """Tier 2: text not representable in encoding → None (caller must error)."""
    result = encode_text("こんにちは", "ascii")
    assert result is None


def test_encode_text_roundtrip_utf8_bom() -> None:
    """Tier 2: encode then decode round-trips through utf-8-sig."""
    original = "data"
    encoded = encode_text(original, "utf-8-sig")
    assert encoded is not None
    decoded, enc = decode_text_or_none(encoded)
    assert decoded == original
    assert enc == "utf-8-sig"
