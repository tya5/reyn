"""Tier 2: MediaStore — flat-file image + tool-result storage (issue #383 PR-C).

Pins the storage layer that all multimodal cluster consumers (web_fetch
binary, file_read binary, mcp image, /image attach) emit path-refs
against.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reyn.workspace.media_store import MediaStore, MediaStoreConfig


def _store(tmp_path: Path) -> MediaStore:
    return MediaStore(MediaStoreConfig(), project_root=tmp_path)


# ── save_image ─────────────────────────────────────────────────────────


def test_save_image_writes_file_under_media_dir(tmp_path):
    """Tier 2: save_image writes the binary under .reyn/media/ and the
    returned path-ref's ``path`` is project-relative.
    """
    store = _store(tmp_path)
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    block = store.save_image(
        data, mime_type="image/png", chain_id="abc123", tool="web_fetch", seq=1,
    )

    assert block["type"] == "image"
    assert block["mime_type"] == "image/png"
    assert block["content_hash"] == "sha256:" + hashlib.sha256(data).hexdigest()
    # Path is project-relative and lives inside .reyn/media/.
    assert block["path"].startswith(".reyn/media/")
    full = tmp_path / block["path"]
    assert full.exists()
    assert full.read_bytes() == data


def test_save_image_filename_encodes_metadata(tmp_path):
    """Tier 2: filename has timestamp + chain_short + tool + seq + extension."""
    store = _store(tmp_path)
    block = store.save_image(
        b"x", mime_type="image/png", chain_id="abc123def", tool="web_fetch", seq=2,
    )
    name = Path(block["path"]).name
    # Anchor on the structural pieces; the exact timestamp varies.
    assert "abc123" in name  # chain_short = first 6 of chain_id
    assert "web_fetch" in name
    assert name.endswith("-2.png")


def test_save_image_unknown_mime_no_extension(tmp_path):
    """Tier 2: unknown MIME type → filename written without extension; user
    can rename with their preferred tool. Storage still works.
    """
    store = _store(tmp_path)
    block = store.save_image(
        b"x", mime_type="application/octet-stream",
        chain_id="", tool="test", seq=1,
    )
    name = Path(block["path"]).name
    # No extension expected for unknown MIME.
    assert "." not in name.split("-")[-1] or name.endswith("-1")


def test_save_image_sanitises_tool_token(tmp_path):
    """Tier 2: tool names with slashes / spaces are sanitised to safe tokens."""
    store = _store(tmp_path)
    block = store.save_image(
        b"x", mime_type="image/png", chain_id="abc",
        tool="mcp/playwright tool", seq=1,
    )
    name = Path(block["path"]).name
    # Slashes and spaces replaced with underscores.
    assert "/" not in name
    assert " " not in name
    assert "mcp_playwright_tool" in name


# ── read_image ─────────────────────────────────────────────────────────


def test_read_image_round_trips_saved_block(tmp_path):
    """Tier 2: save then read returns the same bytes."""
    store = _store(tmp_path)
    data = b"hello world bytes"
    block = store.save_image(data, mime_type="image/png", tool="test", seq=1)

    out, found = store.read_image(block["path"])
    assert found is True
    assert out == data


def test_read_image_returns_not_found_for_missing(tmp_path):
    """Tier 2: missing path → (b"", False)."""
    store = _store(tmp_path)
    out, found = store.read_image(".reyn/media/nope.png")
    assert out == b""
    assert found is False


def test_read_image_rejects_path_outside_media_dir(tmp_path):
    """Tier 2: path traversal outside media_dir raises PermissionError —
    defends against adversarial / corrupted path-ref ChatMessage content.
    """
    store = _store(tmp_path)
    (tmp_path / "secret.txt").write_text("not media")
    with pytest.raises(PermissionError, match="outside media_dir"):
        store.read_image("secret.txt")


def test_read_image_rejects_traversal_attempt(tmp_path):
    """Tier 2: a ../ traversal also rejected."""
    store = _store(tmp_path)
    with pytest.raises(PermissionError):
        store.read_image("../etc/passwd")


# ── save_tool_result + read_tool_result ────────────────────────────────


def test_save_tool_result_writes_to_tool_results_dir(tmp_path):
    """Tier 2: save_tool_result writes under .reyn/tool-results/ with the
    parallel naming convention as save_image.
    """
    store = _store(tmp_path)
    block = store.save_tool_result(
        "hello world", mime_type="text/plain",
        chain_id="xyz", tool="web_fetch_text", seq=1,
    )
    assert block["type"] == "tool_result_ref"
    assert block["mime_type"] == "text/plain"
    assert block["path"].startswith(".reyn/tool-results/")
    assert block["path"].endswith(".txt")
    full = tmp_path / block["path"]
    assert full.exists()
    assert full.read_text(encoding="utf-8") == "hello world"


def test_save_tool_result_html_extension(tmp_path):
    """Tier 2: text/html MIME → .html extension."""
    store = _store(tmp_path)
    block = store.save_tool_result(
        "<html>...</html>", mime_type="text/html", tool="web_fetch", seq=1,
    )
    assert Path(block["path"]).suffix == ".html"


def test_read_tool_result_round_trip(tmp_path):
    """Tier 2: save + read for text content round-trips identically."""
    store = _store(tmp_path)
    content = "Line 1\nLine 2\nLine 3\n"
    block = store.save_tool_result(content, mime_type="text/plain")

    out, found = store.read_tool_result(block["path"])
    assert found is True
    assert out == content


def test_read_tool_result_rejects_outside_dir(tmp_path):
    """Tier 2: path traversal outside tool_results_dir raises
    PermissionError — same defence as read_image.
    """
    store = _store(tmp_path)
    (tmp_path / "leak.txt").write_text("secret")
    with pytest.raises(PermissionError, match="outside tool_results_dir"):
        store.read_tool_result("leak.txt")


# ── isolation across separate save_* calls ─────────────────────────────


def test_image_and_tool_result_dirs_are_distinct(tmp_path):
    """Tier 2: save_image writes to media_dir only; save_tool_result writes
    to tool_results_dir only. Each path-ref carries its own ``type``.
    """
    store = _store(tmp_path)
    img_block = store.save_image(b"img", mime_type="image/png")
    txt_block = store.save_tool_result("txt", mime_type="text/plain")

    assert (tmp_path / ".reyn" / "media").is_dir()
    assert (tmp_path / ".reyn" / "tool-results").is_dir()
    assert img_block["type"] == "image"
    assert txt_block["type"] == "tool_result_ref"
    # Each path lives only in its own dir.
    assert "/media/" in img_block["path"]
    assert "/tool-results/" in txt_block["path"]


def test_custom_dirs_via_config(tmp_path):
    """Tier 2: MediaStoreConfig overrides the default subdirectory names."""
    cfg = MediaStoreConfig(
        media_dir=".alt/img", tool_results_dir=".alt/text",
    )
    store = MediaStore(cfg, project_root=tmp_path)
    img = store.save_image(b"x", mime_type="image/png")
    txt = store.save_tool_result("y", mime_type="text/plain")
    assert img["path"].startswith(".alt/img/")
    assert txt["path"].startswith(".alt/text/")


# ── Cross-host capable path-ref shape (#385 β core impl sub-task 1) ────


def test_save_tool_result_without_agent_name_keeps_legacy_shape(tmp_path):
    """Tier 2: when MediaStore has no ``agent_name``, save_tool_result
    returns the pre-β path-ref shape (= no resource_uri / source_agent /
    source_chain_id). Backward compat for legacy callers and test stubs.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    block = store.save_tool_result("body", mime_type="text/plain", chain_id="c1")

    assert "resource_uri" not in block
    assert "source_agent" not in block
    assert "source_chain_id" not in block
    # Legacy fields still present.
    assert block["type"] == "tool_result_ref"
    assert "path" in block
    assert "content_hash" in block


def test_save_tool_result_with_agent_name_emits_cross_host_fields(tmp_path):
    """Tier 2: when MediaStore is constructed with ``agent_name``,
    save_tool_result emits resource_uri + source_agent + source_chain_id
    so cross-host consumers can dispatch back to the producing agent
    (#385 β core impl frozen contract, 2026-05-22).
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="researcher",
    )
    block = store.save_tool_result(
        "body", mime_type="text/plain", chain_id="chain42",
    )

    assert block["source_agent"] == "researcher"
    assert block["source_chain_id"] == "chain42"
    # resource_uri = reyn-tool-result://<agent>/<filename>; filename is
    # the basename of the same-host path field.
    assert block["resource_uri"].startswith("reyn-tool-result://researcher/")
    filename = Path(block["path"]).name
    assert block["resource_uri"].endswith("/" + filename)
    # Same-host path is still there as the fast-path fallback.
    assert block["path"].startswith(".reyn/tool-results/")


def test_save_image_with_agent_name_also_carries_resource_uri(tmp_path):
    """Tier 2: the cross-host field augmentation applies uniformly to
    both save_image and save_tool_result — the path-ref contract is the
    same shape regardless of media type.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="vision",
    )
    block = store.save_image(b"\x89PNG\r\n", mime_type="image/png", chain_id="c2")

    assert block["source_agent"] == "vision"
    assert block["resource_uri"].startswith("reyn-tool-result://vision/")
    assert "source_chain_id" in block


def test_save_with_agent_name_but_no_chain_id_omits_audit_field(tmp_path):
    """Tier 2: ``source_chain_id`` is an audit annotation, optional. When
    no chain_id is supplied, the field is omitted rather than emitted
    as empty/null — the path-ref stays minimal.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="agentX",
    )
    block = store.save_tool_result("body", mime_type="text/plain")

    assert "source_agent" in block
    assert "resource_uri" in block
    assert "source_chain_id" not in block


# ── parse_resource_uri ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("reyn-tool-result://agent/artifact.txt", ("agent", "artifact.txt")),
        (
            "reyn-tool-result://researcher/20260522T010203-abc123-web_fetch-1.txt",
            ("researcher", "20260522T010203-abc123-web_fetch-1.txt"),
        ),
        # Nested-path artifacts: only the FIRST '/' is the agent boundary.
        (
            "reyn-tool-result://a/nested/path/in/artifact",
            ("a", "nested/path/in/artifact"),
        ),
    ],
)
def test_parse_resource_uri_valid(uri, expected):
    """Tier 2: well-formed URIs split into (agent, artifact)."""
    from reyn.workspace.media_store import parse_resource_uri

    assert parse_resource_uri(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "not-a-uri",
        "http://example.com/x",
        "reyn-tool-result://",        # no agent, no artifact
        "reyn-tool-result://agent",   # no artifact, no slash
        "reyn-tool-result:///artifact",  # empty agent
        "reyn-tool-result://agent/",  # empty artifact
    ],
)
def test_parse_resource_uri_invalid_returns_none(uri):
    """Tier 2: malformed URIs return None (= not an exception). The
    handler treats None as a structured-error signal.
    """
    from reyn.workspace.media_store import parse_resource_uri

    assert parse_resource_uri(uri) is None


# ── read_tool_result_by_uri (same-host + cross-host stub) ──────────────


def test_read_tool_result_by_uri_same_host_round_trip(tmp_path):
    """Tier 2: a path-ref minted by this store can be re-read by its own
    ``resource_uri`` — same-host fast-path through the URI dispatcher.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="me",
    )
    block = store.save_tool_result("hello\nworld\n", mime_type="text/plain")
    out, found = store.read_tool_result_by_uri(block["resource_uri"])

    assert found is True
    assert out == "hello\nworld\n"


def test_read_tool_result_by_uri_cross_host_raises_stub_error(tmp_path):
    """Tier 2: when the URI's source_agent doesn't match this store's
    identity, ``read_tool_result_by_uri`` raises ValueError with a clear
    "cross-host not yet supported" message. Sub-task 3 of the #385 β
    core impl will lift this; the stub is the dispatcher contract.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="local",
    )
    other_uri = "reyn-tool-result://remote/some-artifact.txt"

    with pytest.raises(ValueError, match="cross-host"):
        store.read_tool_result_by_uri(other_uri)


def test_read_tool_result_by_uri_invalid_uri_raises(tmp_path):
    """Tier 2: a malformed URI raises ValueError (= structured error,
    not a silent miss). The handler relays the message to the LLM.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="me",
    )

    with pytest.raises(ValueError, match="invalid resource_uri"):
        store.read_tool_result_by_uri("not-a-uri")


def test_read_tool_result_by_uri_missing_agent_name_raises(tmp_path):
    """Tier 2: a store constructed WITHOUT agent_name can't resolve
    cross-host URIs (= it has no identity to compare against). Raises
    ValueError to make the misconfiguration visible.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)

    with pytest.raises(ValueError, match="no agent_name"):
        store.read_tool_result_by_uri(
            "reyn-tool-result://anyone/something.txt",
        )


def test_read_tool_result_by_uri_missing_file_returns_not_found(tmp_path):
    """Tier 2: a syntactically valid same-host URI for a file that doesn't
    exist returns ``("", False)`` — matches the past-EOF / deleted-file
    convention of the path-based ``read_tool_result``.
    """
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="me",
    )
    out, found = store.read_tool_result_by_uri(
        "reyn-tool-result://me/never-written.txt",
    )

    assert out == ""
    assert found is False


def test_agent_name_property_returns_set_identity(tmp_path):
    """Tier 2: the ``agent_name`` property mirrors the constructor arg
    so dispatchers / introspection code can verify which identity a
    given MediaStore instance carries.
    """
    no_id = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    with_id = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alpha",
    )
    assert no_id.agent_name is None
    assert with_id.agent_name == "alpha"
