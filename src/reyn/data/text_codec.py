"""#1452: single-source text codec — decode/encode ladder for file ops.

`read_file` / `grep` / `edit_file` / `write_file` all need to turn raw bytes
into text (and back) without (a) dumping garbled binary into context, (b)
silently mis-decoding legacy encodings (SJIS / EUC-JP / UTF-16) to replacement
chars, or (c) silently transcoding a non-UTF-8 file on edit. This module is the
ONE place that decides — extracted so every consumer shares it (the extract-the-
seam pattern, cf. #1446's op-context bridge).

Decode ladder (`decode_text_or_none`): BOM → UTF-8 fast-path → NUL-sniff binary
reject → charset-normalizer detection. Re-encode (`encode_text`) round-trips
through the detected codec (restoring a BOM for utf-8-sig/utf-16/utf-32), and
returns None when the text is not representable (→ the caller errors instead of
corrupting the file).
"""
from __future__ import annotations

import codecs

# Head bytes sampled for the NUL-byte binary sniff. A NUL never occurs in valid
# UTF-8/legacy text, so its presence (after the BOM + UTF-8 checks) is a cheap,
# false-positive-free binary signal.
_BINARY_SNIFF_BYTES = 8192

# BOM signatures, checked BEFORE the NUL-sniff (UTF-16/32 ASCII text is NUL-heavy
# and would be mis-rejected). UTF-32 is listed before UTF-16 because BOM_UTF32_LE
# starts with BOM_UTF16_LE's bytes — the longer signature must match first.
_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_text_or_none(raw: bytes) -> tuple[str | None, str | None]:
    """Return ``(text, encoding)`` for a text payload, or ``(None, None)`` for
    binary.

    ``encoding`` is ``None`` on the plain-UTF-8 fast path (so the common-case
    result shape is unchanged); it names the codec when a BOM or a
    charset-normalizer detection was used. Ladder order is load-bearing:

    1. **BOM first** — UTF-16/32 ASCII text is NUL-heavy and would be mis-rejected
       by the NUL-sniff below; the codecs strip the BOM on decode.
    2. **UTF-8 strict** — the dominant case; fast path, no detection cost.
    3. **NUL-sniff** — a NUL byte (no BOM, not UTF-8) → binary fast-reject.
    4. **charset-normalizer** — best-guess for legacy encodings (SJIS, EUC-JP,
       latin-1, …); no confident match → binary (the safe fallback).
    """
    for bom, enc in _BOM_ENCODINGS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc), enc
            except (UnicodeDecodeError, LookupError):
                return None, None
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        pass
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        return None, None
    from charset_normalizer import from_bytes

    match = from_bytes(raw).best()
    if match is None:
        return None, None
    return str(match), match.encoding


def encode_text(text: str, encoding: str | None) -> bytes | None:
    """Re-encode ``text`` with ``encoding`` (as detected by
    :func:`decode_text_or_none`), preserving a BOM where the codec implies one
    (``utf-8-sig`` / ``utf-16`` / ``utf-32`` re-add it). ``encoding=None`` (the
    UTF-8 fast path) → plain UTF-8.

    Returns ``None`` when ``text`` is not representable in ``encoding`` (e.g. an
    emoji written into a Shift-JIS file) — the caller MUST then error and leave
    the file untouched rather than silently transcode it (#1452).
    """
    try:
        return text.encode(encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return None
