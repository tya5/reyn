"""Tier 2: services.offload.store — axis-agnostic offload infrastructure invariants.

Covered invariants:
1. offload_value writes full content + returns correct path_ref and sha256 content_hash.
2. read_offloaded returns full original content via path_ref.
3. read_offloaded integrity: correct content_hash passes; wrong content_hash raises ValueError.
4. read_offloaded path-boundary: a path outside base_dir raises PermissionError.
5. read_offloaded slice: offset/limit returns the right line window.
6. preview_strategy injection: custom strategy is invoked; its output is the preview field.
7. offload_value with explicit filename uses that exact filename.
8. read_offloaded returns (empty, False) when the file does not exist.
9. offload_value serialises dict values via json (not str).
10. content_hash format matches "sha256:<hex>" convention (MediaStore-compatible).

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch.
- No private-state assertions.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from reyn.services.offload.store import OffloadResult, offload_value, read_offloaded

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity_strategy(value: Any, path_ref: str) -> Any:
    """Preview strategy that returns the value unchanged (for testing injection)."""
    return value


def _constant_strategy(value: Any, path_ref: str) -> str:
    """Preview strategy that returns a fixed string (to verify strategy output lands in result)."""
    return f"preview:constant:{path_ref}"


def _path_embedding_strategy(value: Any, path_ref: str) -> dict:
    """Preview strategy that embeds path_ref in its output (verifies path_ref is passed)."""
    return {"ref": path_ref, "summary": "test preview"}


def _make_dict_value(size: int = 100) -> dict:
    """Build a simple dict value for testing."""
    return {"key": "A" * size, "other": 42}


# ---------------------------------------------------------------------------
# 1. offload_value writes full content + correct path_ref + sha256 content_hash
# ---------------------------------------------------------------------------


def test_offload_value_writes_full_content(tmp_path: Path) -> None:
    """Tier 2: offload_value writes full serialised content to store_dir.

    The file at path_ref must contain the complete JSON-serialised value
    so that no information is lost.
    """
    value = _make_dict_value(200)
    result = offload_value(value, store_dir=tmp_path / "store", preview_strategy=_identity_strategy)

    assert isinstance(result, OffloadResult)
    stored_text = Path(result.path_ref).read_text(encoding="utf-8")
    stored = json.loads(stored_text)
    assert stored == value, "Stored file must contain the full original value"


def test_offload_value_correct_content_hash(tmp_path: Path) -> None:
    """Tier 2: offload_value returns the correct sha256 content_hash.

    The hash must be ``"sha256:<hex>"`` of the serialised UTF-8 bytes,
    matching the MediaStore convention for Phase 2 unification.
    """
    value = {"data": "hello" * 10}
    result = offload_value(value, store_dir=tmp_path / "store", preview_strategy=_identity_strategy)

    serialized = json.dumps(value, ensure_ascii=False)
    expected_hash = "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()
    assert result.content_hash == expected_hash, (
        f"content_hash mismatch: got {result.content_hash!r}, expected {expected_hash!r}"
    )


def test_offload_value_path_ref_is_absolute(tmp_path: Path) -> None:
    """Tier 2: offload_value path_ref is an absolute filesystem path.

    Callers (including read_offloaded) must receive an unambiguous path.
    """
    value = {"x": 1}
    result = offload_value(value, store_dir=tmp_path / "store", preview_strategy=_identity_strategy)

    assert Path(result.path_ref).is_absolute(), (
        f"path_ref must be absolute, got: {result.path_ref!r}"
    )
    assert Path(result.path_ref).exists(), "path_ref must point at an existing file"


# ---------------------------------------------------------------------------
# 2. read_offloaded returns full original content via path_ref
# ---------------------------------------------------------------------------


def test_read_offloaded_returns_full_content(tmp_path: Path) -> None:
    """Tier 2: read_offloaded(path_ref) returns the full stored content.

    The round-trip offload_value → read_offloaded must recover the original.
    """
    value = {"lines": ["line1", "line2", "line3"], "count": 3}
    store_dir = tmp_path / "store"
    result = offload_value(value, store_dir=store_dir, preview_strategy=_identity_strategy)

    content, found = read_offloaded(result.path_ref, base_dir=store_dir)
    assert found is True
    recovered = json.loads(content)
    assert recovered == value, "read_offloaded must return the full original content"


# ---------------------------------------------------------------------------
# 3. Integrity check: correct hash passes, wrong hash raises ValueError
# ---------------------------------------------------------------------------


def test_read_offloaded_correct_hash_passes(tmp_path: Path) -> None:
    """Tier 2: read_offloaded with the correct content_hash succeeds without raising.

    Verifies that a matching hash is accepted — read-back works with integrity.
    """
    value = {"item": "B" * 100}
    store_dir = tmp_path / "store"
    result = offload_value(value, store_dir=store_dir, preview_strategy=_identity_strategy)

    # Should not raise
    content, found = read_offloaded(
        result.path_ref, base_dir=store_dir, content_hash=result.content_hash
    )
    assert found is True
    assert json.loads(content) == value


def test_read_offloaded_wrong_hash_raises_value_error(tmp_path: Path) -> None:
    """Tier 2: read_offloaded with a wrong content_hash raises ValueError.

    Integrity enforcement must reject tampered or mismatched content.
    """
    value = {"item": "C" * 100}
    store_dir = tmp_path / "store"
    result = offload_value(value, store_dir=store_dir, preview_strategy=_identity_strategy)

    wrong_hash = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="content_hash mismatch"):
        read_offloaded(result.path_ref, base_dir=store_dir, content_hash=wrong_hash)


# ---------------------------------------------------------------------------
# 4. Path-boundary validation: outside base_dir raises PermissionError
# ---------------------------------------------------------------------------


def test_read_offloaded_outside_base_dir_raises_permission_error(tmp_path: Path) -> None:
    """Tier 2: read_offloaded rejects paths outside base_dir with PermissionError.

    Path-traversal protection: any path that resolves outside base_dir must be
    refused, mirroring MediaStore.read_tool_result boundary enforcement.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True)

    # Write a file in a sibling directory (outside store_dir)
    sibling_dir = tmp_path / "sibling"
    sibling_dir.mkdir()
    sibling_file = sibling_dir / "secret.json"
    sibling_file.write_text('{"secret": true}', encoding="utf-8")

    with pytest.raises(PermissionError):
        read_offloaded(str(sibling_file), base_dir=store_dir)


def test_read_offloaded_traversal_path_raises_permission_error(tmp_path: Path) -> None:
    """Tier 2: read_offloaded rejects ``../`` path traversal with PermissionError.

    A path constructed with ``../`` that resolves outside base_dir must be
    refused even when it refers to an existing file.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True)

    # Write a legitimate file outside store_dir
    outside_file = tmp_path / "outside.json"
    outside_file.write_text('{"x": 1}', encoding="utf-8")

    # Construct a path using traversal from inside store_dir
    traversal_path = str(store_dir / "../outside.json")

    with pytest.raises(PermissionError):
        read_offloaded(traversal_path, base_dir=store_dir)


# ---------------------------------------------------------------------------
# 5. Slice: offset/limit returns the right line window
# ---------------------------------------------------------------------------


def test_read_offloaded_slice_offset_and_limit(tmp_path: Path) -> None:
    """Tier 2: read_offloaded with offset/limit returns the correct line window.

    A 10-line file sliced with offset=2, limit=3 must return lines 2,3,4
    (0-indexed), preserving newlines.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True)
    lines = [f"line{i}\n" for i in range(10)]
    target = store_dir / "slice_test.txt"
    target.write_text("".join(lines), encoding="utf-8")

    content, found = read_offloaded(str(target), base_dir=store_dir, offset=2, limit=3)
    assert found is True
    result_lines = content.splitlines()
    assert result_lines == ["line2", "line3", "line4"], (
        f"Expected lines [line2, line3, line4], got {result_lines}"
    )


def test_read_offloaded_slice_offset_only(tmp_path: Path) -> None:
    """Tier 2: read_offloaded with offset only returns from that line to end."""
    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True)
    lines = [f"line{i}\n" for i in range(5)]
    target = store_dir / "offset_test.txt"
    target.write_text("".join(lines), encoding="utf-8")

    content, found = read_offloaded(str(target), base_dir=store_dir, offset=3)
    assert found is True
    result_lines = content.splitlines()
    assert result_lines == ["line3", "line4"], (
        f"Expected [line3, line4], got {result_lines}"
    )


def test_read_offloaded_slice_limit_only(tmp_path: Path) -> None:
    """Tier 2: read_offloaded with limit only returns the first N lines."""
    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True)
    lines = [f"line{i}\n" for i in range(5)]
    target = store_dir / "limit_test.txt"
    target.write_text("".join(lines), encoding="utf-8")

    content, found = read_offloaded(str(target), base_dir=store_dir, limit=2)
    assert found is True
    result_lines = content.splitlines()
    assert result_lines == ["line0", "line1"], (
        f"Expected [line0, line1], got {result_lines}"
    )


# ---------------------------------------------------------------------------
# 6. preview_strategy injection: custom strategy is invoked; output lands in preview
# ---------------------------------------------------------------------------


def test_preview_strategy_is_invoked(tmp_path: Path) -> None:
    """Tier 2: offload_value invokes the injected preview_strategy.

    The result.preview must be the exact return value of the strategy.
    """
    value = {"data": "hello"}
    store_dir = tmp_path / "store"

    calls: list[tuple] = []

    def recording_strategy(v: Any, path_ref: str) -> str:
        calls.append((v, path_ref))
        return f"recorded:{path_ref}"

    result = offload_value(value, store_dir=store_dir, preview_strategy=recording_strategy)

    assert calls, "Strategy must be called at least once"
    v_arg, path_ref_arg = calls[-1]
    assert v_arg == value, "Strategy must receive the original value"
    assert path_ref_arg == result.path_ref, "Strategy must receive the path_ref"
    assert result.preview == f"recorded:{result.path_ref}", (
        "result.preview must be the strategy's return value"
    )


def test_preview_strategy_path_ref_embedding(tmp_path: Path) -> None:
    """Tier 2: preview_strategy receives the written path_ref for embedding in truncation markers.

    Strategies that embed the path_ref (e.g. "full content at <path>") must
    receive it correctly from offload_value.
    """
    value = {"big": "X" * 1000}
    store_dir = tmp_path / "store"
    result = offload_value(value, store_dir=store_dir, preview_strategy=_path_embedding_strategy)

    assert isinstance(result.preview, dict)
    assert result.preview["ref"] == result.path_ref, (
        "preview_strategy must receive the exact path_ref that was written"
    )


# ---------------------------------------------------------------------------
# 7. Explicit filename
# ---------------------------------------------------------------------------


def test_offload_value_explicit_filename_used(tmp_path: Path) -> None:
    """Tier 2: when filename is provided, offload_value uses that exact filename.

    This enables callers (like the phase axis) to embed idx/uid in the name
    for human-readable workspace inspection.
    """
    value = {"x": 1}
    store_dir = tmp_path / "store"
    explicit_name = "0001_abc12345.json"

    result = offload_value(
        value,
        store_dir=store_dir,
        preview_strategy=_identity_strategy,
        filename=explicit_name,
    )

    assert Path(result.path_ref).name == explicit_name, (
        f"Expected filename {explicit_name!r}, got {Path(result.path_ref).name!r}"
    )


# ---------------------------------------------------------------------------
# 8. File-not-found returns (empty, False)
# ---------------------------------------------------------------------------


def test_read_offloaded_missing_file_returns_not_found(tmp_path: Path) -> None:
    """Tier 2: read_offloaded returns ("", False) when the file does not exist.

    Matches MediaStore.read_tool_result's convention for past-EOF / deleted files.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True)
    nonexistent = store_dir / "does_not_exist.json"

    content, found = read_offloaded(str(nonexistent), base_dir=store_dir)
    assert found is False
    assert content == ""


# ---------------------------------------------------------------------------
# 9. Dict serialisation via json (not str())
# ---------------------------------------------------------------------------


def test_offload_value_dict_serialised_as_json(tmp_path: Path) -> None:
    """Tier 2: offload_value serialises dict values via json.dumps (not str()).

    The stored file must be valid JSON parseable back to the original dict.
    """
    value = {"nested": {"a": 1, "b": [2, 3]}, "flag": True}
    store_dir = tmp_path / "store"
    result = offload_value(value, store_dir=store_dir, preview_strategy=_identity_strategy)

    raw = Path(result.path_ref).read_text(encoding="utf-8")
    # Must parse as valid JSON
    parsed = json.loads(raw)
    assert parsed == value, "Stored dict must round-trip through JSON without loss"


# ---------------------------------------------------------------------------
# 10. content_hash format: "sha256:<hex>"
# ---------------------------------------------------------------------------


def test_content_hash_format_sha256_prefix(tmp_path: Path) -> None:
    """Tier 2: content_hash is in the ``"sha256:<hex>"`` format.

    This matches MediaStore.save_tool_result's convention
    (``"sha256:" + hashlib.sha256(content.encode()).hexdigest()``)
    so Phase 2 unification requires no format migration.
    """
    value = {"sample": "data"}
    result = offload_value(value, store_dir=tmp_path / "s", preview_strategy=_identity_strategy)

    assert result.content_hash.startswith("sha256:"), (
        f"content_hash must start with 'sha256:', got: {result.content_hash!r}"
    )
    hex_part = result.content_hash[len("sha256:"):]
    # SHA-256 hex digest contains only hex characters
    assert hex_part and all(c in "0123456789abcdef" for c in hex_part), (
        f"content_hash hex part is not a valid hex digest: {hex_part!r}"
    )
