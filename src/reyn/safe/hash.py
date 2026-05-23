"""Cryptographic and non-cryptographic hash helpers.

All functions accept ``bytes`` and return a lowercase hex digest
string. Wrap stdlib ``hashlib`` — output is a pure function of input.
"""

from __future__ import annotations

import hashlib as _hashlib


def sha256(b: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``b``."""
    return _hashlib.sha256(b).hexdigest()


def md5(b: bytes) -> str:
    """Return the lowercase hex MD5 digest of ``b``.

    MD5 is not cryptographically secure; use for non-security
    fingerprints only.
    """
    return _hashlib.md5(b).hexdigest()


def blake2b(b: bytes) -> str:
    """Return the lowercase hex BLAKE2b digest of ``b``."""
    return _hashlib.blake2b(b).hexdigest()
