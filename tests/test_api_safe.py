"""Tier 2: reyn.interfaces.api.safe wrapper behaviour."""

from __future__ import annotations

import pytest

from reyn.interfaces.api.safe import hash as safe_hash
from reyn.interfaces.api.safe import json as safe_json
from reyn.interfaces.api.safe import random as safe_random
from reyn.interfaces.api.safe import schema as safe_schema
from reyn.interfaces.api.safe import text as safe_text
from reyn.interfaces.api.safe import time as safe_time

# -- hash --------------------------------------------------------------


def test_hash_sha256_happy() -> None:
    """Tier 2: sha256 returns hex digest of known value."""
    # echo -n "" | sha256sum
    assert safe_hash.sha256(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_hash_md5_blake2b_lengths() -> None:
    """Tier 2: md5 and blake2b digests are lowercase hex of correct length."""
    md5 = safe_hash.md5(b"abc")
    blake = safe_hash.blake2b(b"abc")
    # md5 hex digest is 32 chars; blake2b-512 hex digest is 128 chars.
    # Verify by matching against a known-length hex pattern rather than pinning len().
    import re
    assert re.fullmatch(r"[0-9a-f]{32}", md5), f"md5 must be 32-char lowercase hex, got {md5!r}"
    assert re.fullmatch(r"[0-9a-f]{128}", blake), f"blake2b must be 128-char lowercase hex, got {blake!r}"


# -- schema ------------------------------------------------------------


def test_schema_validate_happy() -> None:
    """Tier 2: validate accepts conformant data."""
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    safe_schema.validate({"name": "alice", "age": 30}, schema)  # no raise


def test_schema_validate_missing_required() -> None:
    """Tier 2: validate rejects missing required field."""
    schema = {"type": "object", "required": ["name"], "properties": {}}
    with pytest.raises(safe_schema.SchemaError):
        safe_schema.validate({}, schema)


# -- text --------------------------------------------------------------


def test_text_regex_findall_named_happy() -> None:
    """Tier 2: regex_findall_named extracts named groups for every match."""
    out = safe_text.regex_findall_named(
        r"(?P<k>\w+)=(?P<v>\d+)", "a=1, b=2, c=3"
    )
    assert out == [
        {"k": "a", "v": "1"},
        {"k": "b", "v": "2"},
        {"k": "c", "v": "3"},
    ]


def test_text_template_render_safe_missing_key() -> None:
    """Tier 2: template_render_safe leaves missing keys literal."""
    assert (
        safe_text.template_render_safe("hi {name}, {unknown}!", {"name": "alice"})
        == "hi alice, {unknown}!"
    )


# -- json --------------------------------------------------------------


def test_json_loads_strict_rejects_duplicate_keys() -> None:
    """Tier 2: loads_strict raises on duplicate keys (stdlib would silently overwrite)."""
    with pytest.raises(ValueError):
        safe_json.loads_strict('{"a": 1, "a": 2}')


def test_json_dumps_canonical_sorts_keys() -> None:
    """Tier 2: dumps_canonical sorts keys for content-addressable output."""
    s = safe_json.dumps_canonical({"b": 1, "a": 2})
    assert s == '{"a": 2, "b": 1}'


# -- time --------------------------------------------------------------


def test_time_monotonic_seq_monotonic() -> None:
    """Tier 2: monotonic_seq is non-decreasing across consecutive calls."""
    t1 = safe_time.monotonic_seq()
    t2 = safe_time.monotonic_seq()
    assert isinstance(t1, float)
    assert t2 >= t1


# -- random ------------------------------------------------------------


def test_random_seeded_reproducible() -> None:
    """Tier 2: seeded RNGs with the same seed produce the same sequence."""
    r1 = safe_random.seeded(42)
    r2 = safe_random.seeded(42)
    seq1 = [r1.randint(0, 1000) for _ in range(5)]
    seq2 = [r2.randint(0, 1000) for _ in range(5)]
    assert seq1 == seq2


def test_random_seeded_independent_from_global() -> None:
    """Tier 2: seeded RNGs do not perturb the module-global random state."""
    import random as stdlib_random

    stdlib_random.seed(0)
    expected = stdlib_random.random()

    stdlib_random.seed(0)
    _ = safe_random.seeded(999).random()  # should not touch global state
    actual = stdlib_random.random()
    assert actual == expected
