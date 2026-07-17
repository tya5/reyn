"""#3009 item 2 — a sandbox write denial during a TOOL CALL must be diagnosable.

#2976/#2981 taught the operator the ``write_paths`` knob, but only on the path
where the denial kills the LAUNCH: the hint lives in ``MCPClient.initialize`` and
reads the subprocess's stderr. A builtin server denied while RUNNING never
reaches it — it starts fine (its cwd is granted) and the denial lands in a tool
handler, so it comes back as JSON-RPC tool-error content and stderr stays empty.
What the operator saw was a bare sqlite error naming neither the sandbox nor the
knob, i.e. the same silence #2976 existed to end, one layer down.

These tests pin the invariant that closes it: **when the sandbox denies a write,
the operator learns that it was a denial and what to do next.** They deliberately
do not pin the hint's wording — only that the reader is pointed at the mechanism.

Two independent things have to hold for that, and each has its own test below,
because each was separately observed to fail (both MEASURED under the real
Seatbelt profile, not predicted):

  1. **The denial has to keep carrying an OS-level signature.** ``apsw`` reports
     every failed open as ``CantOpenError("unable to open database file")`` — no
     errno, indistinguishable from a typo — so the signature survives only where
     something else raises first. That made ``mkdir``'s incidental ordering the
     sole diagnosable path, and left the operator who had already created their
     target directory (mkdir → no-op → apsw is what fails) with nothing at all.
  2. **The client has to read that signature on the tool-call channel**, which is
     not the stderr channel the #2976 helper was wired to.

The end-to-end test is the real claim; the unit-level ones exist to say WHICH
half broke when it goes red.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

apsw = pytest.importorskip(
    "apsw", reason="builtin-rag extra ('pip install reyn[builtin-rag]') not installed",
)
pytest.importorskip(
    "sqlite_vec", reason="builtin-rag extra ('pip install reyn[builtin-rag]') not installed",
)

from reyn.builtin.mcp_servers.vector_store_server import (  # noqa: E402
    _connect,
    _describe_open_failure,
)
from reyn.mcp.client import MCPClient, _looks_like_write_denial  # noqa: E402
from reyn.security.sandbox import get_default_backend  # noqa: E402
from reyn.security.sandbox.self_test import enforcement_self_test  # noqa: E402

# The exact tool-error text the real builtin vector-store server returned through
# the real Seatbelt profile with a db_path outside its write scope. Kept verbatim
# as the fixture for the predicate below: this is an EXTERNAL shape (the OS's
# errno text, wrapped by FastMCP), so it is a real contract to pin, not our own
# formatting — the same reasoning by which #2976 pins real launcher stderr.
_DENIED_MKDIR_PAYLOAD = (
    "Error calling tool 'upsert': [Errno 1] Operation not permitted: '/tmp/x/sub'"
)
# ...and what the SAME denial degrades to once apsw, not mkdir, is what fails.
_MARKER_FREE_SQLITE_ERROR = "Error calling tool 'upsert': unable to open database file"


# ── 1. the signature has to exist at all (server side) ───────────────────────

def test_apsw_discards_the_reason_and_connect_restores_it(tmp_path):
    """Tier 2b: a failed open reports the OS's reason, which apsw alone drops.

    Uses a path whose parent is a FILE (ENOTDIR): a deterministic, permission-
    free way to make a real ``apsw`` open fail, so this pins the restoration
    mechanism rather than any one errno. The negative half is what makes it
    live — without it, an implementation that simply re-raised apsw's error
    would pass.
    """
    (tmp_path / "not-a-dir").write_text("x")
    db = str(tmp_path / "not-a-dir" / "docs.sqlite")

    with pytest.raises(apsw.Error) as raw:
        apsw.Connection(db)
    assert "errno" not in str(raw.value).lower()
    assert db not in str(raw.value)

    with pytest.raises(OSError) as restored:
        _connect(db)
    # The operator gets the OS's own reason AND the path it was refused for.
    assert "not a directory" in str(restored.value).lower()
    assert db in str(restored.value)


def test_no_diagnosis_is_invented_for_a_path_that_opens(tmp_path):
    """Tier 2b: the probe reports nothing when the OS refuses nothing.

    ``_connect`` must only ever attach a reason it actually observed; when the
    probe's own open succeeds there is none, and apsw's error stands unmodified.
    Pinned on the probe rather than through a forced ``_connect`` failure
    because that branch cannot be reached honestly here: ``apsw.Connection``
    opens lazily and does NOT validate the file (a readable non-database opens
    fine and only fails on first query), so every connect-time failure this
    server can actually produce is one the probe reproduces. The branch stays
    as the defensive default — "re-raise rather than guess".
    """
    db = tmp_path / "docs.sqlite"
    db.write_bytes(b"this is not a sqlite database, but it is readable\n" * 64)

    assert _describe_open_failure(str(db)) is None


def test_the_probe_leaves_no_empty_database_behind(tmp_path):
    """Tier 2b: probing a path that CAN be opened does not create a store.

    A stray 0-byte file is a valid EMPTY sqlite db — it would become the
    operator's store on their next attempt and mask the real error, so the
    diagnostic path must not leave one.
    """
    absent = tmp_path / "sub" / "docs.sqlite"
    absent.parent.mkdir()

    assert _describe_open_failure(str(absent)) is None
    assert not absent.exists()


def test_a_pre_existing_database_survives_the_probe(tmp_path):
    """Tier 2b: probing an existing db does not truncate the operator's data."""
    db = tmp_path / "docs.sqlite"
    db.write_bytes(b"payload")

    _describe_open_failure(str(db))
    assert db.read_bytes() == b"payload"


# ── 2. the client has to read it on the tool-call channel ────────────────────

@pytest.mark.parametrize(
    "text",
    [
        _DENIED_MKDIR_PAYLOAD,
        # The same predicate serves both channels; the launch-time shapes #2976
        # measured must keep matching after being repointed at tool-error content.
        "npm error code EPERM\nnpm error syscall open",
    ],
)
def test_a_denial_is_recognised_in_tool_error_content(text):
    """Tier 2b: the write-denial predicate reads tool-error text, not just stderr.

    The channel, not the wording, is the point: a denial inside a running
    server's handler never touches stderr.
    """
    assert _looks_like_write_denial(text) is True


def test_a_marker_free_sqlite_error_is_not_flagged():
    """Tier 2b: an error carrying no OS signature does not get the hint.

    Guards the honest half of the contract — reyn must not blame the sandbox
    for a failure it cannot see. It is also why the server restores the errno
    rather than the client guessing from this string.
    """
    assert _looks_like_write_denial(_MARKER_FREE_SQLITE_ERROR) is False


# ── 3. the invariant itself, end to end ──────────────────────────────────────

def _enforcing_backend_or_skip():
    """Skip unless THIS host can actually witness a sandbox write denial.

    Uses #3016's enforcement self-test rather than ``available()``: a backend
    that merely imports cannot deny anything, and a test that asserts on a
    denial that never fired would pass by observing nothing. On a host with no
    working backend (Linux today — Landlock is unreachable per #2980, so the
    resolver lands on NoopBackend) there is no denial to diagnose and this test
    has no subject.
    """
    backend = get_default_backend()
    reason = enforcement_self_test(backend)
    if reason is not None:
        pytest.skip(f"sandbox backend {backend.name!r} does not enforce here: {reason}")
    return backend


@pytest.mark.parametrize("precreate_target_dir", [False, True], ids=["dir-absent", "dir-exists"])
def test_a_denied_tool_write_tells_the_operator_the_knob(
    precreate_target_dir, monkeypatch, reyn_console_scripts, out_of_process_reyn
):
    """Tier 2c: a real server denied a real write names the sandbox and the knob.

    The whole chain, real throughout — real sandbox backend, real spawned MCP
    server, real apsw, real denial. Reads what ``op_runtime/mcp.py`` hands its
    reader (it joins the text blocks), so this asserts on what the operator and
    the LLM actually see, not on an internal shape.

    Both parametrisations were separately observed failing: with the target
    directory absent the denial surfaces from ``mkdir``, and with it present
    from the open — and only the first was ever diagnosable.
    """
    _enforcing_backend_or_skip()
    granted = Path(tempfile.mkdtemp(prefix="reyn-3009-cwd-"))
    denied = Path(tempfile.mkdtemp(prefix="reyn-3009-denied-"))
    monkeypatch.chdir(granted)
    if precreate_target_dir:
        (denied / "sub").mkdir()

    # The mcp SDK hands the child an allowlisted env subset that drops
    # PYTHONPATH, so an editable/src-layout checkout must pass it explicitly or
    # the SERVER silently runs a different tree than the one under test.
    # `out_of_process_reyn` derives and verifies that pin; `reyn_console_scripts`
    # states that this test runs `reyn-rag-vector-store` by name (#3024).
    client = MCPClient(
        {"type": "stdio", "command": "reyn-rag-vector-store",
         "env": {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": out_of_process_reyn}},
        server_name="reyn_vector_store",
    )

    async def _drive():
        await client.initialize()
        try:
            return await client.call_tool("upsert", {
                "db_path": str(denied / "sub" / "docs.sqlite"),
                "items": [{"source_path": "a.md", "content_hash": "h",
                           "chunk_index": 0, "size_tokens": 1}],
                "vectors": [[0.1] * 8],
                "embedding_model": "test-model",
            })
        finally:
            await client.close()

    import asyncio
    result = asyncio.run(_drive())

    assert result["isError"] is True
    seen = "\n".join(
        item.get("text", "") for item in result["content"]
        if isinstance(item, dict) and item.get("type") == "text"
    )
    # The invariant, in its two halves: the operator learns it was a DENIAL...
    assert "sandbox" in seen.lower()
    # ...and learns the knob that resolves it, named exactly as the config key
    # they must type (the one token here worth pinning — they grep for it).
    assert "write_paths" in seen
