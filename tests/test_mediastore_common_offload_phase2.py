"""Tier 2: MediaStore Phase 2 — common offload service migration preservation guards.

Pins the invariants of the FP-0008 C5 #223 Phase 2 migration: the LOCAL
store+hash+read core of ``save_tool_result`` / ``read_tool_result`` is
delegated to ``services/offload/store.py``, while the return-block shape,
(text, found) contract, PermissionError boundary, and cross-host methods are
preserved BYTE-FOR-BYTE.

Covered invariants:
1. save_tool_result block shape unchanged:
   {type:"tool_result_ref", path:<project-relative>, mime_type, content_hash:"sha256:..."};
   the file is written under tool_results_dir with the original content;
   content_hash is correct.
2. read_tool_result round-trip: save then read → original content + found=True.
3. read_tool_result missing file → ("", False).
4. read_tool_result outside tool_results_dir → PermissionError (boundary preserved).
5. preview_strategy=None additive change:
   offload_value(..., preview_strategy=None) → OffloadResult.preview is None,
   store + hash still work correctly.
6. Existing phase tests (with a real strategy) still pass (additive change verified).
7. Cross-host methods (read_tool_result_by_uri / by_url / _attach_cross_host_fields)
   are unchanged — their existing tests pass (structural assertion).
8. web_fetch flow: the save_tool_result call contract + path_ref shape still holds.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch (except monkeypatch for httpx).
- No private-state assertions.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.services.offload.store import OffloadResult, offload_value

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> MediaStore:
    return MediaStore(MediaStoreConfig(), project_root=tmp_path)


# ---------------------------------------------------------------------------
# 1. save_tool_result block shape unchanged
# ---------------------------------------------------------------------------


def test_save_tool_result_block_shape_preserved(tmp_path: Path) -> None:
    """Tier 2: save_tool_result returns the expected block shape after migration.

    The block must contain exactly:
      {type:"tool_result_ref", path:<project-relative>, mime_type, content_hash:"sha256:..."}
    The file at ``path`` must exist under tool_results_dir with the original content.
    The content_hash must equal sha256 of the UTF-8 encoded content.
    """
    store = _store(tmp_path)
    content = "hello from phase 2 migration\n"
    block = store.save_tool_result(
        content, mime_type="text/plain", chain_id="abc123", tool="web_fetch", seq=1,
    )

    # Block shape
    assert block["type"] == "tool_result_ref"
    assert block["mime_type"] == "text/plain"
    # path is project-relative (no leading /)
    assert not block["path"].startswith("/")
    assert block["path"].startswith(".reyn/tool-results/")
    assert block["path"].endswith(".txt")
    assert "content_hash" in block
    assert block["content_hash"].startswith("sha256:")

    # File written under tool_results_dir with original content
    full = tmp_path / block["path"]
    assert full.exists(), "file must be written under tool_results_dir"
    assert full.read_text(encoding="utf-8") == content

    # content_hash is correct sha256 of content bytes
    expected_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    assert block["content_hash"] == expected_hash


def test_save_tool_result_html_block_shape(tmp_path: Path) -> None:
    """Tier 2: text/html mime → .html extension; block shape preserved for non-plain types."""
    store = _store(tmp_path)
    content = "<html><body>hello</body></html>"
    block = store.save_tool_result(content, mime_type="text/html", tool="web_fetch", seq=1)

    assert block["type"] == "tool_result_ref"
    assert block["mime_type"] == "text/html"
    assert block["path"].endswith(".html")
    full = tmp_path / block["path"]
    assert full.read_text(encoding="utf-8") == content
    expected_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    assert block["content_hash"] == expected_hash


def test_save_tool_result_path_is_project_relative_not_absolute(tmp_path: Path) -> None:
    """Tier 2: the block 'path' field must be project-relative, not absolute.

    After migration, offload_value returns an absolute path_ref; the wrapper
    must convert to project-relative. This test guards that conversion.
    """
    store = _store(tmp_path)
    block = store.save_tool_result("content", mime_type="text/plain")

    assert not block["path"].startswith("/"), (
        f"path must be project-relative, got absolute: {block['path']!r}"
    )
    assert block["path"].startswith(".reyn/tool-results/")


# ---------------------------------------------------------------------------
# 2. read_tool_result round-trip
# ---------------------------------------------------------------------------


def test_read_tool_result_round_trip(tmp_path: Path) -> None:
    """Tier 2: save_tool_result + read_tool_result(returned path) → original content + found=True."""
    store = _store(tmp_path)
    content = "Line 1\nLine 2\nLine 3\n"
    block = store.save_tool_result(content, mime_type="text/plain")

    text, found = store.read_tool_result(block["path"])
    assert found is True
    assert text == content


def test_read_tool_result_unicode_round_trip(tmp_path: Path) -> None:
    """Tier 2: Unicode content is preserved through the save→read round-trip.

    Verifies UTF-8 write + read is byte-identical for non-ASCII characters.
    """
    store = _store(tmp_path)
    content = "日本語テスト\n中文测试\n한국어 테스트\n"
    block = store.save_tool_result(content, mime_type="text/plain")

    text, found = store.read_tool_result(block["path"])
    assert found is True
    assert text == content


# ---------------------------------------------------------------------------
# 3. read_tool_result missing → ("", False)
# ---------------------------------------------------------------------------


def test_read_tool_result_missing_returns_not_found(tmp_path: Path) -> None:
    """Tier 2: a valid path inside tool_results_dir for a non-existent file → ("", False).

    Matches the pre-migration contract: found=False for deleted/never-written files.
    """
    store = _store(tmp_path)
    # Ensure the directory exists so path validation passes.
    store.tool_results_dir.mkdir(parents=True, exist_ok=True)
    fake_rel = str(
        (store.tool_results_dir / "does-not-exist.txt").relative_to(tmp_path)
    )

    text, found = store.read_tool_result(fake_rel)
    assert found is False
    assert text == ""


# ---------------------------------------------------------------------------
# 4. read_tool_result outside tool_results_dir → PermissionError
# ---------------------------------------------------------------------------


def test_read_tool_result_outside_boundary_raises_permission_error(tmp_path: Path) -> None:
    """Tier 2: a path outside tool_results_dir raises PermissionError.

    The error message must contain 'outside tool_results_dir' — the same
    shape as the pre-migration implementation so downstream consumers
    that match on the error string keep working.
    """
    store = _store(tmp_path)
    (tmp_path / "secret.txt").write_text("not allowed", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside tool_results_dir"):
        store.read_tool_result("secret.txt")


def test_read_tool_result_traversal_raises_permission_error(tmp_path: Path) -> None:
    """Tier 2: a ../traversal path outside tool_results_dir raises PermissionError."""
    store = _store(tmp_path)

    with pytest.raises(PermissionError, match="outside tool_results_dir"):
        store.read_tool_result("../etc/passwd")


# ---------------------------------------------------------------------------
# 5. preview_strategy=None additive change: OffloadResult.preview is None
# ---------------------------------------------------------------------------


def test_offload_value_preview_strategy_none_returns_none_preview(tmp_path: Path) -> None:
    """Tier 2: offload_value with preview_strategy=None → OffloadResult.preview is None.

    The chat/MediaStore axis passes None; the service must NOT crash and must
    return preview=None. Store + hash must still work correctly.
    """
    content = "some text content"
    result = offload_value(
        content,
        store_dir=tmp_path / "store",
        preview_strategy=None,
        filename="test.txt",
    )

    assert isinstance(result, OffloadResult)
    assert result.preview is None, (
        f"preview must be None when preview_strategy=None, got {result.preview!r}"
    )
    # Store + hash still work
    assert result.path_ref.endswith("test.txt")
    stored = Path(result.path_ref).read_text(encoding="utf-8")
    assert stored == content
    expected_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    assert result.content_hash == expected_hash


def test_offload_value_preview_strategy_none_with_dict_value(tmp_path: Path) -> None:
    """Tier 2: preview_strategy=None works for dict values (json serialisation path)."""
    value = {"key": "value", "count": 42}
    result = offload_value(
        value,
        store_dir=tmp_path / "store",
        preview_strategy=None,
        filename="test.json",
    )

    assert result.preview is None
    assert Path(result.path_ref).exists()
    import json
    stored = json.loads(Path(result.path_ref).read_text(encoding="utf-8"))
    assert stored == value


# ---------------------------------------------------------------------------
# 6. Additive change verified: existing phase caller with a real strategy still works
# ---------------------------------------------------------------------------


def test_offload_value_real_strategy_still_works_after_additive_change(tmp_path: Path) -> None:
    """Tier 2: offload_value with a real strategy (non-None) still works correctly.

    Making preview_strategy optional (= additive) must not break the phase
    axis that passes real strategies. This test guards that the additive
    change does not regress existing phase callers.
    """
    calls: list = []

    def recording_strategy(value: Any, path_ref: str) -> str:
        calls.append((value, path_ref))
        return f"preview:{path_ref}"

    value = {"data": "phase axis content"}
    result = offload_value(
        value,
        store_dir=tmp_path / "store",
        preview_strategy=recording_strategy,
    )

    assert result.preview is not None, "preview must be non-None when strategy provided"
    assert calls, "strategy must be invoked"
    assert result.preview == f"preview:{result.path_ref}"


# ---------------------------------------------------------------------------
# 7. Cross-host methods structural verification
# ---------------------------------------------------------------------------


def test_cross_host_method_read_by_uri_unchanged(tmp_path: Path) -> None:
    """Tier 2: read_tool_result_by_uri is untouched by the Phase 2 migration.

    Verifies the same-host round-trip (URI → content) still works, confirming
    cross-host methods were not accidentally modified.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="test-agent",
    )
    block = store.save_tool_result(
        "uri content\n", mime_type="text/plain", chain_id="c1",
    )

    text, found = store.read_tool_result_by_uri(block["resource_uri"])
    assert found is True
    assert text == "uri content\n"


def test_cross_host_method_read_by_url_unchanged(tmp_path: Path) -> None:
    """Tier 2: read_tool_result_by_url is untouched by the Phase 2 migration.

    Verifies the same-host URL short-circuit still works, confirming
    cross-host methods were not accidentally modified.
    """
    store = MediaStore(
        MediaStoreConfig(),
        project_root=tmp_path,
        agent_name="test-agent",
        base_url="https://reyn.example.com",
    )
    block = store.save_tool_result(
        "url content\n", mime_type="text/plain", chain_id="c1", tool="web_fetch", seq=1,
    )

    text, found = store.read_tool_result_by_url(block["url"])
    assert found is True
    assert text == "url content\n"


def test_attach_cross_host_fields_unchanged_with_agent_name(tmp_path: Path) -> None:
    """Tier 2: _attach_cross_host_fields is untouched — cross-host block fields
    are still emitted with the same schema when agent_name is set.
    """
    store = MediaStore(
        MediaStoreConfig(),
        project_root=tmp_path,
        agent_name="researcher",
        base_url="https://reyn.example.com",
    )
    block = store.save_tool_result(
        "body", mime_type="text/plain", chain_id="chain1",
    )

    assert block["source_agent"] == "researcher"
    assert block["source_chain_id"] == "chain1"
    assert block["resource_uri"].startswith("reyn-tool-result://researcher/")
    assert "url" in block
    filename = Path(block["path"]).name
    assert block["url"].endswith(f"/tool-results/{filename}")


def test_save_image_is_unchanged_by_phase2_migration(tmp_path: Path) -> None:
    """Tier 2: save_image is NOT migrated in Phase 2 (out of scope) and
    must continue to work byte-for-byte identically.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="vision",
    )
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    block = store.save_image(
        data, mime_type="image/png", chain_id="c2", tool="web_fetch", seq=1,
    )

    assert block["type"] == "image"
    assert block["path"].startswith(".reyn/media/")
    full = tmp_path / block["path"]
    assert full.read_bytes() == data
    expected_hash = "sha256:" + hashlib.sha256(data).hexdigest()
    assert block["content_hash"] == expected_hash


# ---------------------------------------------------------------------------
# 8. web_fetch flow: save_tool_result call site + path_ref block shape
# ---------------------------------------------------------------------------


def test_web_fetch_call_site_block_shape_via_media_store(tmp_path: Path) -> None:
    """Tier 2: the web_fetch/web.py call site (ctx.media_store.save_tool_result(...))
    must produce a path_ref block whose shape is identical to the pre-migration contract.

    This test verifies the contract from the web.py caller's perspective:
    save_tool_result returns {type:"tool_result_ref", path:..., mime_type:..., content_hash:...}
    and the file exists + is readable at that path.
    """
    store = _store(tmp_path)
    # Simulate the web.py call: text content with text/html mime type.
    extracted_body = "<html><head><title>T</title></head><body><p>content</p></body></html>"
    block = store.save_tool_result(
        extracted_body, mime_type="text/html; charset=utf-8",
        chain_id="chain123", tool="web_fetch", seq=1,
    )

    # Block shape as expected by web.py + preview generation
    assert block["type"] == "tool_result_ref"
    assert "path" in block
    assert "mime_type" in block
    assert "content_hash" in block
    assert block["content_hash"].startswith("sha256:")
    # File exists and contains the full body
    full = tmp_path / block["path"]
    assert full.exists()
    assert full.read_text(encoding="utf-8") == extracted_body
