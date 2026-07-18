"""Tier 2b: subsystem invariant -- the two builtin RAG pipelines
(``rag_ingest`` / ``rag_query``), now shipped as part of the builtin ``rag``
plugin (ADR 0064 P5, ``src/reyn/builtin/plugins/rag/pipelines/``; originally
authored under FP-0063 P3,
docs/deep-dives/proposals/0063-builtin-turnkey-user-rag.md).

This test drives the pipeline FILES directly (a project-local
``.reyn/config.yaml`` declaring ``pipelines.entries`` by absolute path, see
``_write_project`` below) rather than going through a real
``plugin_install`` -- the install mechanism itself (copy, materialise deps
into a per-plugin venv, register) has its own coverage in
``tests/test_plugin_install.py`` and ``scripts/wheel_plugin_install_probe.py``;
this file's job is the PIPELINE BEHAVIOR, so it stays fast/offline by
pointing straight at the plugin's shipped files with the ``builtin-rag``
extra installed for direct import, instead of paying a real ``uv``
materialise + network fetch on every test run.

Drives both pipelines end-to-end through the REAL ``reyn pipe run`` CLI path
(``src/reyn/interfaces/cli/commands/pipe.py::run_run``) against REAL builtin
MCP servers (``reyn.builtin.plugins.rag.scripts.vector_store_server`` /
``chunker_server``, started as real ``python -m ...`` stdio subprocesses --
the ``builtin-rag`` extra IS installed in this environment, verified by
``pytest.importorskip`` guarding the whole module so a base install still
collects). The THIRD server (markitdown) is substituted by a small, real
FastMCP stdio server written to disk for the test (the real
``markitdown-mcp`` PyPI package is not installed here, and fetching it via
``uvx`` would need network) -- this substitution is disclosed in the PR
body, not silently passed off as "real markitdown-mcp".

Only the embedding PROVIDER is faked (mirrors ``tests/test_op_embed.py``'s
``FakeEmbeddingProvider`` precedent -- monkeypatching
``reyn.core.op_runtime.embed.get_provider``, NOT ``unittest.mock``) so the
tests need no real embedding API key/network. Everything else -- the pipeline
parser/executor, the MCP client/gateway/permission gate, the two real
builtin MCP servers, sqlite-vec/apsw/chonkie -- is real.

Coverage:
  1. Parse: both pipeline files parse cleanly (eight + three ``pipeline:``
     docs respectively) via ``parse_pipeline_docs``.
  2. C5 add/update/remove convergence + X5 dedup visibility: ingest a
     2-file folder, re-ingest unchanged (0 upserts, dedup skip reported),
     modify one file and re-ingest (that file's chunks update), delete one
     file and re-ingest (its chunks are removed).
  3. C4: every upserted chunk's stored ``embedding_model`` matches the
     pipeline's own ``embedding_model`` input (stamped, not hardcoded).
  4. X1 pre-flight: pointing ``vectorstore_server`` at an unconfigured name
     blocks the run with a decision-enabling message naming that server +
     a concrete remedy, WITHOUT a bare transport exception -- falsified by
     asserting the SAME run with a valid name proceeds past pre-flight.
  5. rag_query: after ingest, querying returns the ingested chunk as its
     top-1 nearest result.
  6. #2972: the ingest runs no python of its own -- it completes under a
     hostile ambient ``python3`` (the pipx / non-activated-venv class) and
     spawns no subprocess at all, and it passes ``max_results`` explicitly
     so a folder larger than ``glob_files``' silent 50-file default cap is
     still ingested whole.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip(
    "apsw", reason="builtin-rag extra ('pip install reyn[builtin-rag]') not installed",
)
pytest.importorskip(
    "chonkie", reason="builtin-rag extra ('pip install reyn[builtin-rag]') not installed",
)
pytest.importorskip(
    "sqlite_vec", reason="builtin-rag extra ('pip install reyn[builtin-rag]') not installed",
)

import reyn.builtin as _builtin_pkg  # noqa: E402
from reyn.core.pipeline.parser import parse_pipeline_docs  # noqa: E402
from reyn.core.pipeline.schema import SchemaRegistry  # noqa: E402
from reyn.interfaces.cli.commands.pipe import run_run  # noqa: E402

_RAG_PLUGIN_DIR = Path(_builtin_pkg.__file__).resolve().parent / "plugins" / "rag"
_INGEST_PATH = _RAG_PLUGIN_DIR / "pipelines" / "rag_ingest.yaml"
_QUERY_PATH = _RAG_PLUGIN_DIR / "pipelines" / "rag_query.yaml"

# ---------------------------------------------------------------------------
# A real, minimal FastMCP stdio server standing in for markitdown-mcp (not
# installed in this environment -- see module docstring). Converts a
# "file://<path>" URI to Markdown by reading the file as UTF-8 text; good
# enough for .txt/.md fixtures, which is all these tests ingest.
# ---------------------------------------------------------------------------

_STUB_MARKITDOWN_SERVER = '''
import base64
from urllib.parse import urlsplit
from fastmcp import FastMCP

mcp = FastMCP("stub-markitdown")


@mcp.tool
def convert_to_markdown(uri: str) -> str:
    if uri.startswith("data:"):
        _, _, payload = uri.partition(",")
        return base64.b64decode(payload).decode("utf-8")
    if uri.startswith("file://"):
        # #3102: real markitdown-mcp parses the URI and REJECTS a non-empty,
        # non-localhost netloc -- which is exactly what a naive 'file://' +
        # <relative path> concatenation produces ('file://docs/a.txt' parses
        # with netloc='docs', path='/a.txt'). Reproducing that check here
        # (instead of the earlier lenient `uri[len("file://"):]` strip, which
        # happened to still resolve a relative match against this test's own
        # CWD and silently masked the bug) is what makes this stub actually
        # exercise the real-world failure mode instead of passing vacuously.
        parsed = urlsplit(uri)
        if parsed.netloc not in ("", "localhost"):
            raise ValueError(
                f"Unsupported file URI: {uri}. Netloc must be empty or localhost."
            )
        path = parsed.path
    else:
        path = uri
    with open(path, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    mcp.run()
'''


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _server_env() -> dict[str, str]:
    """Env for the builtin MCP server subprocesses, pinning them to the SAME
    reyn tree this test process imported.

    Without this the servers resolve ``reyn`` from whatever the ambient
    editable install points at. In CI that happens to be the checkout under
    test, so it passes; in a multi-worktree dev box it can be a DIFFERENT
    worktree, and the tests then silently exercise code that is not the code
    being changed (observed while writing #2972: the chunker subprocess ran
    another worktree's copy, so its new fields were "missing" and every
    ingest assertion failed for a reason that had nothing to do with the
    diff). Pinning it makes the module docstring's "REAL builtin MCP servers"
    claim true about THIS tree in both environments.

    ``env`` REPLACES the subprocess environment rather than extending it (the
    MCP SDK otherwise passes a 6-key whitelist that excludes PYTHONPATH), so
    the handful of vars the interpreter and uvx actually need are carried
    over explicitly.
    """
    import reyn

    src_root = str(Path(reyn.__file__).resolve().parent.parent)
    passthrough = {
        k: v for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LOGNAME", "SHELL", "TERM", "USER", "TMPDIR")
    }
    return {**passthrough, "PYTHONPATH": src_root}


class FakeEmbeddingProvider:
    """Deterministic real EmbeddingProvider (mirrors
    ``tests/test_op_embed.py``'s own fixture) -- a fixed-length vector per
    text so distinct chunk texts get distinct (but reproducible) vectors,
    with no real embedding API call.

    **``embed`` RESOLVES the requested model to ``fake/<model>``** rather
    than echoing it back -- exactly as ``tests/test_op_embed.py``'s fixture
    does, and as the real ``RoutingEmbeddingProvider`` does when handed a
    model-CLASS alias (``"standard"`` -> a concrete provider model id). This
    is load-bearing for the C4 test: were the resolved name identical to the
    requested one, "stamped from the pipeline's INPUT" and "stamped from
    ``envelope.model``" would be indistinguishable and the assertion would
    pass vacuously against either implementation.

    ``total_tokens`` is deliberately NOT the chars/4 value the pipeline's
    own ``est_tokens`` heuristic would compute -- it is ``len(text)``, ~4x
    larger. That gap is what lets the X2a test prove the reported figure
    came from the metered envelope rather than the estimate.
    """

    def __init__(self) -> None:
        self._batch_size = 100

    async def embed(self, texts: list[str], model: str):
        from reyn.data.embedding.provider import EmbedBatchResult
        vectors = [[float(len(t) % 97), float(sum(map(ord, t)) % 89), 1.0] for t in texts]
        return EmbedBatchResult(
            vectors=vectors, model=f"fake/{model}", total_tokens=sum(len(t) for t in texts),
        )

    def estimate_tokens(self, texts: list[str]) -> int:
        return sum(len(t) for t in texts)

    def get_dimension(self, model: str) -> int:
        return 3


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import reyn.core.op_runtime.embed as embed_mod

    fake = FakeEmbeddingProvider()
    monkeypatch.setattr(embed_mod, "get_provider", lambda *a, **k: fake)


def _write_project(tmp_path: Path, *, vectorstore_server: str = "reyn_vector_store") -> Path:
    """Write a reyn.yaml wiring the 3 MCP servers (2 real, 1 stub) + the two
    builtin RAG pipeline entries, and return project_root."""
    stub_path = tmp_path / "stub_markitdown_server.py"
    stub_path.write_text(_STUB_MARKITDOWN_SERVER, encoding="utf-8")

    (tmp_path / "reyn.yaml").write_text(
        yaml.dump(
            {
                "model": "standard",
                "models": {"standard": "openai/gpt-4o-mini"},
                "mcp": {
                    "servers": {
                        "reyn_markitdown": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": [str(stub_path)],
                            "env": _server_env(),
                        },
                        "reyn_chunker": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "reyn.builtin.plugins.rag.scripts.chunker_server"],
                            "env": _server_env(),
                        },
                        vectorstore_server: {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "reyn.builtin.plugins.rag.scripts.vector_store_server"],
                            "env": _server_env(),
                        },
                    },
                },
                "pipelines": {
                    "entries": {
                        "rag_ingest": {"path": str(_INGEST_PATH)},
                        "rag_query": {"path": str(_QUERY_PATH)},
                    },
                },
            },
            allow_unicode=True, default_flow_style=False,
        ),
        encoding="utf-8",
    )
    return tmp_path


def _run_ingest(project_root: Path, capsys: pytest.CaptureFixture, **input_overrides: Any) -> dict:
    seed = {
        "input_path": str(project_root / "docs"),
        "output_db": str(project_root / "rag.sqlite"),
        **input_overrides,
    }
    args = _ns(
        name="rag_ingest.ingest", input=json.dumps(seed),
        project=str(project_root), async_=False,
    )
    run_run(args)
    out = capsys.readouterr().out
    return json.loads(out)


# ---------------------------------------------------------------------------
# 1. Parse
# ---------------------------------------------------------------------------


def test_rag_ingest_pipeline_parses() -> None:
    """Tier 2b: rag_ingest.yaml parses -- 8 pipeline: docs (ingest, _blocked,
    _ingest_one_file, _ingest_chunk_file, _ingest_skipped_file, _ingest_body,
    _ingest_embed_and_upsert, _ingest_noop_upsert)."""
    docs = parse_pipeline_docs(_INGEST_PATH.read_text(encoding="utf-8"), SchemaRegistry())
    names = {p.name for p in docs}
    assert names == {
        "ingest", "_blocked", "_ingest_one_file", "_ingest_chunk_file",
        "_ingest_skipped_file", "_ingest_body", "_ingest_embed_and_upsert",
        "_ingest_noop_upsert",
    }


def test_rag_query_pipeline_parses() -> None:
    """Tier 2b: rag_query.yaml parses -- 3 pipeline: docs (query,
    _query_blocked, _query_body)."""
    docs = parse_pipeline_docs(_QUERY_PATH.read_text(encoding="utf-8"), SchemaRegistry())
    names = {p.name for p in docs}
    assert names == {"query", "_query_blocked", "_query_body"}


# ---------------------------------------------------------------------------
# 2/3/5. End-to-end: C5 add/update/remove, X5 dedup, C4 stamping, rag_query
# ---------------------------------------------------------------------------


def test_ingest_add_then_dedup_then_update_then_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: C5's full add/update/remove convergence + X5's dedup
    visibility, driven end-to-end via 'reyn pipe run' against the REAL
    chunker + vector-store MCP servers."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("Alpha document about apples and oranges.", encoding="utf-8")
    (docs_dir / "b.txt").write_text("Beta document about bananas and grapes.", encoding="utf-8")

    # -- add: first ingest -- both files' chunks get upserted, none removed.
    result_1 = _run_ingest(project_root, capsys)
    summary_1 = result_1["named_stores"]["result"]
    assert summary_1["files_scanned"] == 2
    assert summary_1["chunks_upserted"] >= 2
    assert summary_1["chunks_removed"] == 0
    assert summary_1["chunks_unchanged_skipped"] == 0

    # -- dedup (X5): re-ingest the SAME, unchanged folder -- zero upserts,
    # every chunk reported as unchanged/skipped.
    result_2 = _run_ingest(project_root, capsys)
    summary_2 = result_2["named_stores"]["result"]
    assert summary_2["chunks_upserted"] == 0, "re-ingesting an unchanged folder must cost ~0 upserts"
    assert summary_2["chunks_unchanged_skipped"] == summary_1["chunks_upserted"]
    assert summary_2["estimated_tokens_saved_by_dedup"] > 0

    # -- update: change a.txt's content -- only a.txt's chunk(s) re-upsert.
    (docs_dir / "a.txt").write_text("Alpha document REWRITTEN about cherries.", encoding="utf-8")
    result_3 = _run_ingest(project_root, capsys)
    summary_3 = result_3["named_stores"]["result"]
    assert summary_3["chunks_upserted"] >= 1
    assert summary_3["chunks_removed"] == 0

    # -- remove: delete b.txt entirely -- its chunk(s) are removed, a.txt's
    # (now stable) chunk is reported unchanged.
    (docs_dir / "b.txt").unlink()
    result_4 = _run_ingest(project_root, capsys)
    summary_4 = result_4["named_stores"]["result"]
    assert summary_4["files_scanned"] == 1
    assert summary_4["chunks_removed"] >= 1
    assert summary_4["chunks_upserted"] == 0


def test_only_real_document_content_reaches_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2c: a file that yields no usable document is NOT indexed, and IS reported (#3010).

    The invariant: what lands in the operator's vector store is document content -- never a
    converter's error message, never reyn's own prose about the absence of content -- and a file that
    contributed nothing is NAMED, because "the operator cannot tell what happened" is the failure
    this pipeline shipped with. Asserted against the REAL sqlite store, not only the summary: a clean
    ``chunks_upserted`` over a poisoned index IS the bug.

    Two of the three ways a file yields nothing are exercised here:

    - the conversion FAILS (``meta.isError``) -- the MCP layer raises nothing, so ``for_each``'s
      ``on_error: continue`` never fires; the error MESSAGE was chunked+embedded as content;
    - the conversion SUCCEEDS but yields no text -- caught here by the ZERO-CHUNK gate. Note which
      gate fires and why: this stub runs on a ``structuredContent``-era MCP SDK, so an empty
      conversion arrives as ``{"content": "", "structuredContent": {"result": ""}}`` -- a structured
      attachment, and an attachment-carrying result never takes the explicit-empty path. So
      ``meta.empty`` is ABSENT here, and before the zero-chunk gate this file vanished unreported
      (measured: ``files_scanned: 3, files_skipped: 1``, notext.pdf in neither index nor report).

    The third way -- ``meta.empty``, the marker path that motivated #3010 -- is NOT witnessed by this
    test and cannot be, for the SDK reason above. It is pinned SDK-independently against the non-MCP
    producers in ``tests/test_3010_empty_success_fact_data_path.py``.

    Stub fidelity (the stub stands in for markitdown-mcp -- see module docstring): both branches ride
    the stub's own natural behavior over real fixture bytes, mirroring what the real library was
    MEASURED to do at this boundary (``MarkItDown().convert_uri(...).markdown`` on the #3010
    fixtures): a no-text-layer PDF returns ``''`` (here: an empty file the stub reads as ``''``) and
    an unparseable PDF RAISES, surfaced as ``isError`` (here: undecodable bytes raise
    UnicodeDecodeError). No sentinel or special-case branch is added to the stub.
    """
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    # A real document -- the falsify direction: the gates must skip the unusable files WITHOUT
    # skipping this one (a fix that indexed nothing would satisfy every "no garbage" assertion).
    (docs_dir / "good.txt").write_text("Penguins huddle together for warmth.", encoding="utf-8")
    # Converts fine, holds no text (the scanned-PDF class) -> "" -> "(no content)".
    (docs_dir / "notext.pdf").write_bytes(b"")
    # Cannot be parsed at all -> the converter raises -> isError.
    (docs_dir / "corrupt.pdf").write_bytes(b"\xff\xfe\x00\x01broken")

    summary = _run_ingest(project_root, capsys)["named_stores"]["result"]

    from reyn.builtin.plugins.rag.scripts.vector_store_server import SqliteVecStore

    with SqliteVecStore(str(project_root / "rag.sqlite")) as store:
        rows = store.list_metadata()

    indexed = {Path(r["metadata"]["source_path"]).name for r in rows}
    assert indexed == {"good.txt"}, (
        "only real document content may be indexed -- a file that yielded no document must "
        f"contribute no chunks, got {sorted(indexed)}"
    )
    assert summary["files_scanned"] == 3
    assert summary["files_skipped"] == 2, (
        "a discovered-but-unusable file must be REPORTED; a clean chunks_upserted over a poisoned "
        "index is exactly the failure this guards"
    )
    # The report NAMES each file, so the operator can tell which documents their corpus lacks.
    skipped = {Path(s["source_path"]).name: s["reason"] for s in summary["skipped_files"]}
    assert set(skipped) == {"notext.pdf", "corrupt.pdf"}
    assert all(reason for reason in skipped.values()), "each skip must carry a reason"


def test_a_none_conversion_is_reported_as_a_failure_not_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2c: a conversion whose whole output is "None" is treated as a failed conversion (#3010).

    markitdown MANUFACTURES the string "None" from a file it cannot decode and reports SUCCESS
    (``PlainTextConverter`` runs ``str(from_bytes(...).best())`` with no None check, and
    ``str(None) == "None"``), which suppresses markitdown's own FileConversionException path. reyn
    cannot see that from the outside, so the pipeline defends locally -- and reports through the
    SAME named-with-a-reason path as any other failed conversion, rather than inventing a third.

    The boundary is what makes this a filter and not a blunt instrument, so both directions are
    pinned: EXACT equality drops the manufactured value, while a real document that merely MENTIONS
    None is indexed untouched.
    """
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    # The bug's signature: the converter's entire output is "None".
    (docs_dir / "undecodable.txt").write_text("None", encoding="utf-8")
    # A real document that merely mentions None -- must survive (the falsify direction: an
    # `"None" in text` filter would silently drop this legitimate content).
    (docs_dir / "real.txt").write_text(
        "The function returns None when the cache is cold.", encoding="utf-8",
    )

    summary = _run_ingest(project_root, capsys)["named_stores"]["result"]

    from reyn.builtin.plugins.rag.scripts.vector_store_server import SqliteVecStore

    with SqliteVecStore(str(project_root / "rag.sqlite")) as store:
        indexed = {Path(r["metadata"]["source_path"]).name for r in store.list_metadata()}

    assert indexed == {"real.txt"}, (
        "a document mentioning None is CONTENT and must be indexed; only the exact-match "
        f"manufactured value is dropped -- got {sorted(indexed)}"
    )
    skipped = {Path(s["source_path"]).name: s["reason"] for s in summary["skipped_files"]}
    assert set(skipped) == {"undecodable.txt"}
    assert "None" in skipped["undecodable.txt"], "the reason must name what was seen"
    assert "filter_none_conversions" in skipped["undecodable.txt"], (
        "the reason must name the opt-out, since a document whose real content IS 'None' is "
        "indistinguishable here -- the operator needs the lever named at the point of loss"
    )


def test_the_none_filter_is_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2c: an operator can turn the None filter off, and then "None" is indexed as content.

    The false positive is structural (a .txt whose real content is "None" reaches the pipeline
    identically to the markitdown bug's output), so the escape hatch has to actually work -- a
    default that cannot be overridden would make the corpus lossy with no recourse.
    """
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "literally_none.txt").write_text("None", encoding="utf-8")

    summary = _run_ingest(project_root, capsys, filter_none_conversions=False)["named_stores"]["result"]

    from reyn.builtin.plugins.rag.scripts.vector_store_server import SqliteVecStore

    with SqliteVecStore(str(project_root / "rag.sqlite")) as store:
        indexed = {Path(r["metadata"]["source_path"]).name for r in store.list_metadata()}

    assert indexed == {"literally_none.txt"}, (
        "with the filter off, 'None' is ordinary content and must be indexed"
    )
    assert summary["files_skipped"] == 0


def test_upserted_chunk_embedding_model_is_the_resolved_model_not_the_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: C4 -- every upserted chunk's embedding_model column names the
    model that ACTUALLY produced its vector (embed's envelope.model), not the
    model-CLASS alias the pipeline was invoked with.

    The provider resolves "standard" -> "fake/standard" (see
    FakeEmbeddingProvider), so the two candidate sources are distinguishable:
    stamping the pipeline's own input would write "standard" -- a model that
    never produced these vectors, i.e. the "column becomes a lie" failure
    FP-0057's C1 gate exists to prevent. Read straight back out of the real
    sqlite store."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("A short note about penguins.", encoding="utf-8")

    _run_ingest(project_root, capsys, embedding_model="standard")

    from reyn.builtin.plugins.rag.scripts.vector_store_server import SqliteVecStore

    with SqliteVecStore(str(project_root / "rag.sqlite")) as store:
        rows = store.list_metadata()
    assert rows, "expected at least one upserted chunk"
    assert all(r["metadata"]["embedding_model"] == "fake/standard" for r in rows), (
        "C4 VIOLATION: the embedding_model column must name the RESOLVED model "
        "(envelope.model = 'fake/standard'), not the requested alias 'standard' "
        f"-- got {sorted({r['metadata']['embedding_model'] for r in rows})}"
    )


def test_ingest_reports_metered_spend_not_the_chars4_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: X2a -- the summary's tokens_embedded is embed's OWN METERED
    total_tokens (envelope meta), not the pipeline's chars/4 estimate.

    The provider meters len(text) while the pipeline's est_tokens heuristic
    computes len(text)//4, so the two differ ~4x and the reported figure is
    attributable to exactly one source. Also pins that the resolved model
    and the priced flag ride the same envelope meta."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    body = "Alpha document about apples and oranges. " * 20
    (docs_dir / "a.txt").write_text(body, encoding="utf-8")

    summary = _run_ingest(project_root, capsys)["named_stores"]["result"]

    # The metered figure is ~4x the chars/4 estimate of the same text; assert
    # against the metered side, and explicitly NOT against the estimate.
    assert summary["tokens_embedded"] > 0
    assert summary["tokens_embedded"] > summary["estimated_tokens_saved_by_dedup"]
    approx_estimate = len(body) // 4
    assert summary["tokens_embedded"] > approx_estimate * 2, (
        "X2a REGRESSION: tokens_embedded looks like the chars/4 ESTIMATE "
        f"(~{approx_estimate}), not embed's metered total_tokens (~{len(body)})"
    )
    assert summary["embedding_model"] == "fake/standard"
    assert summary["priced"] is False, (
        "the fake model is unpriceable by litellm, so priced must be False and "
        "cost_usd None -- an unpriced model must degrade VISIBLY, never as $0.00"
    )
    assert summary["cost_usd"] is None


def test_rag_query_returns_the_ingested_chunk_as_top_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: rag_query.query, run after rag_ingest.ingest, returns a
    top-k result whose metadata source_path is the ingested file."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    target = docs_dir / "notes.txt"
    target.write_text("Reyn is an operating system for LLM agents.", encoding="utf-8")

    _run_ingest(project_root, capsys)

    args = _ns(
        name="rag_query.query",
        input=json.dumps({
            "query_text": "what is reyn",
            "db": str(project_root / "rag.sqlite"),
            "top_k": 3,
        }),
        project=str(project_root), async_=False,
    )
    run_run(args)
    out = capsys.readouterr().out
    result = json.loads(out)
    top_k = result["named_stores"]["result"]
    assert isinstance(top_k, list) and len(top_k) >= 1
    assert top_k[0]["metadata"]["source_path"] == str(target.resolve())


# ---------------------------------------------------------------------------
# 4. X1 pre-flight: decision-enabling block + falsifying control
# ---------------------------------------------------------------------------


def test_ingest_preflight_blocks_on_unreachable_vectorstore_with_named_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: X1 -- pointing vectorstore_server at a name NOT present in
    mcp.servers blocks the run with a decision-enabling message naming that
    server + a concrete remedy (never a bare transport exception), and does
    NOT attempt any embedding spend (falsified below by the working case)."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)  # only registers "reyn_vector_store"
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("content", encoding="utf-8")

    result = _run_ingest(project_root, capsys, vectorstore_server="not_configured_server")
    blocked = result["named_stores"]["result"]
    assert isinstance(blocked, str)
    assert "not_configured_server" in blocked
    assert "pip install" in blocked, "the remedy must name a concrete fix, not just the cause"


# ---------------------------------------------------------------------------
# 4b. #3095: file-discovery aborts CLEAN on a glob_files failure, instead of
# corrupting the downstream fold's list-only assumption.
# ---------------------------------------------------------------------------


def test_ingest_file_discovery_aborts_clean_on_unreadable_input_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: #3095 -- `_ingest_body`'s file-discovery `for_each` over
    `glob_files` (one call per extension pattern PLUS `input_path` itself,
    see the pipeline's own comment) must abort CLEANLY when a `glob_files`
    call fails -- e.g. `input_path` names a folder OUTSIDE the reyn project
    root with no `file.read` permission granted for it yet, the ordinary
    case for a real corpus (the reported dogfood witness pointed at
    `/tmp/rag_witness5_docs`, well outside the project).

    Before the fix, a `glob_files` failure returned NORMALLY (op_runtime's
    own `except PermissionError`/`except Exception` degrade every op error
    to a `status`-carrying result dict rather than raising) with a payload
    whose `.structured` was the WHOLE raw error dict (`error_to_canonical`'s
    deliberate, lossless, uniform shape for ANY producer's error case) --
    NOT the list `glob_files`' own SUCCESS shape always produces. The
    `for_each`'s already-declared `on_error: abort` never saw this as a
    failure (only a raised Python exception trips it), so the bad item
    flowed on into `fold: {do: {transform: {value: "acc + item.structured"}}}`,
    which broke with an opaque `arithmetic '+' requires two numbers ... got
    list and dict` several steps downstream of the actual cause.

    The fix closes this in two parts (both required -- see the strip-falsify
    note below): (1) `_handle_glob`/`_handle_list` (src/reyn/tools/file.py)
    now preserve `status` on their error branch (previously dropped,
    `{"error": ...}` with no `status` key at all -- an asymmetric contract
    vs. their own `status: "ok"` success shape); (2) the `glob_files`
    `tool:` step in `_ingest_body`'s `for_each` now declares
    `schema: PreflightCheck` (the SAME `status == "ok"` gate X1 already uses
    per MCP server, reused here) so a non-"ok" status now FAILS schema
    validation and raises -- which is what actually makes the pipeline's own
    already-declared `on_error: abort` engage. Every item that survives to
    the `fold` is now GUARANTEED `status: "ok"`, so `.structured` is
    guaranteed a list.

    strip-falsify: reverting either (1) or (2) alone reproduces the original
    'list and dict' failure (both were independently verified against this
    test while developing the fix).
    """
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    # A folder OUTSIDE project_root with no permission grant -- Workspace's
    # own default-deny boundary for absolute paths outside base_dir/state_dir
    # (see tests/test_workspace_glob_outside_root_perm.py) with `reyn pipe
    # run`'s non-interactive resolver (`_build_run_tool_context`: "fail-
    # closed by default"), so the PermissionError is real, not simulated.
    outside_docs = tmp_path.parent / f"{tmp_path.name}_outside_docs"
    outside_docs.mkdir()
    (outside_docs / "a.txt").write_text("content", encoding="utf-8")

    seed = {
        "input_path": str(outside_docs),
        "output_db": str(project_root / "rag.sqlite"),
    }
    args = _ns(
        name="rag_ingest.ingest", input=json.dumps(seed),
        project=str(project_root), async_=False,
    )
    with pytest.raises(SystemExit) as exc_info:
        run_run(args)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "arithmetic" not in err and "list and dict" not in err, (
        f"regressed to the opaque fold list+dict TypeError instead of a clean "
        f"abort at the real failure site: {err!r}"
    )
    # Decision-enabling: names the failing tool + the real cause (glob_files'
    # own denial message, surfaced via the schema gate's #3070 detail
    # extraction), not a bare downstream arithmetic exception.
    assert "glob_files" in err
    assert "not permitted" in err or "permission" in err.lower()


def test_ingest_is_unaffected_by_a_hostile_ambient_python3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: #2972 -- the ingest completes normally even when the ambient
    `python3` is broken, because the pipeline runs no python of its own.

    reyn does not own the operator's python runtime. This drives the exact
    environment that used to break the arc (a `python3` first on PATH that
    exits non-zero -- the `pipx install reyn` / non-activated-venv /
    different-PATH class, reproduced through the real PATH that
    `sandboxed_exec` forwards, not simulated at a seam) and asserts the
    ingest is INDIFFERENT to it: chunks are embedded and stored.

    The inverse of the test it replaces: that one pinned reyn's pre-flight
    correctly REPORTING a python3 it should never have depended on. The bug
    was the dependency, so the fix deletes the question -- and this asserts
    the deletion behaviourally rather than by grepping the pipeline for the
    absence of a `shell:` step (see the sibling structural test for that)."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("Alpha document about apples.", encoding="utf-8")

    # A python3 that exists and runs but fails -- stands in for "not reyn's
    # interpreter". Under the old shell-out this alone sank the whole run.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ.get('PATH', '')}")

    result = _run_ingest(project_root, capsys)
    summary = result["named_stores"]["result"]
    assert isinstance(summary, dict), (
        "a broken ambient python3 must not affect an ingest that runs no "
        f"python -- got a blocked/str result instead: {summary!r:.300}"
    )
    assert summary["files_scanned"] == 1
    assert summary["chunks_upserted"] >= 1, (
        "the ingest must actually store chunks with a hostile python3 on PATH"
    )


def test_ingest_pipeline_shells_out_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: #2972 -- the ingest pipeline reaches the shell zero times.

    The behavioural sibling above proves a BROKEN python3 does not break the
    run; this proves the stronger property that no subprocess is spawned at
    all, by making any subprocess fatal at the chokepoint every `shell` step
    funnels through (`op_runtime.sandboxed_exec.handle`). A surviving
    shell-out would fail the run (or, for a step whose failure is swallowed,
    drop chunks), so this cannot pass while one remains -- unlike a grep for
    `shell:`, which a differently-spelled subprocess would slip past."""
    import reyn.core.op_runtime.sandboxed_exec as sandboxed_exec_mod

    async def _explode(*a: Any, **k: Any):
        raise AssertionError(
            "the rag_ingest pipeline spawned a subprocess -- #2972 requires it "
            "to run no python/shell of its own (MCP tools + reyn ops only)"
        )

    monkeypatch.setattr(sandboxed_exec_mod, "handle", _explode)
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("Alpha document about apples.", encoding="utf-8")

    summary = _run_ingest(project_root, capsys)["named_stores"]["result"]
    assert isinstance(summary, dict) and summary["chunks_upserted"] >= 1


def test_ingest_scans_every_file_past_the_glob_default_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: #2972/#2994 -- a folder with more files than `glob_files`'
    own 50 default is ingested WHOLE, because the pipeline passes
    `max_results` explicitly.

    `glob_files` truncates at 50 SILENTLY (no error, no warning -- #2994
    pins that default deliberately), so a pipeline that omits `max_results`
    loses document 51+ of a real corpus with nothing anywhere to say so.
    Strip `max_results` from rag_ingest.yaml's glob step and this goes RED
    (60 -> 50): that is the whole point of asserting it end-to-end on a
    corpus straddling the cap rather than pinning the YAML text."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    for i in range(60):
        (docs_dir / f"doc{i:03d}.txt").write_text(f"Document number {i}.", encoding="utf-8")

    summary = _run_ingest(project_root, capsys)["named_stores"]["result"]
    assert summary["files_scanned"] == 60, (
        "every file must be discovered -- glob_files defaults to 50 and "
        "truncates silently, so the pipeline must pass max_results itself"
    )
    assert summary["chunks_upserted"] >= 60


def test_ingest_preflight_falsify_proceeds_with_the_real_server_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: FALSIFY control for the block above -- the SAME setup with
    the real, configured server name proceeds past pre-flight into the
    real ingest body (proves the sibling test's block is attributable to
    the unreachable server, not a broken pre-flight gate)."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("content", encoding="utf-8")

    result = _run_ingest(project_root, capsys)  # default vectorstore_server matches config
    summary = result["named_stores"]["result"]
    assert isinstance(summary, dict) and "chunks_upserted" in summary


# ---------------------------------------------------------------------------
# 7. #3102: a RELATIVE input_path must index the corpus, not silently skip
# every file via a malformed `file://` URI while still reporting status: ok.
# ---------------------------------------------------------------------------


def test_ingest_with_relative_input_path_indexes_the_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: #3102 -- a RELATIVE ``input_path`` (the natural way an
    operator points this pipeline at a project-root corpus, e.g. ``docs`` or
    ``./docs`` -- the most common real input shape, not an edge case) must
    index the corpus exactly as an absolute ``input_path`` does.

    Root cause: ``_ingest_body``'s file-discovery builds each glob PATTERN
    from ``ctx.input_path`` verbatim (``ctx.input_path + '/**/*.' + e``). A
    relative ``input_path`` makes the pattern relative, and
    ``Workspace.glob_files``' relative branch (BY DESIGN -- it resolves
    under ``base_dir``/CWD and returns project-relative matches, a contract
    many OTHER callers rely on for display paths) then returns
    project-relative matches too (``docs/a.txt``, not
    ``/abs/project/docs/a.txt``). ``_ingest_one_file`` builds
    ``'file://' + ctx.source_path`` from that match verbatim -- a relative
    match there produces a MALFORMED URI (``file://docs/a.txt``, which a URI
    parser reads ``docs`` as the netloc/host, not a path segment).
    markitdown then rejects every file with "Netloc must be empty or
    localhost", ``for_each: on_error: continue`` swallows each per-file
    failure through the ordinary #3010 skip path (a real, intentional
    non-error outcome for a PARTIAL skip), and the run completes with
    ``status: ok`` and zero chunks -- a silent-success data-loss shape, not
    a crash.

    The fix (rag_ingest.yaml's file-discovery ``glob_files`` step) passes
    the new ``absolute: true`` arg (#3102, ``Workspace.glob_files``'
    opt-in absolute-path mode) so THIS caller's own contract --
    ``file://`` is always built from an absolute path -- holds
    unconditionally, without touching ``glob_files``' default
    project-relative return that every other caller still depends on.

    strip-falsify: reverting the ``absolute: true`` line in
    rag_ingest.yaml's file-discovery step reproduces the original
    ``files_skipped == files_scanned``, ``chunks_upserted == 0``,
    ``status: ok`` silent-zero shape (independently verified while
    developing this fix).
    """
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("Alpha document about apples.", encoding="utf-8")

    summary = _run_ingest(project_root, capsys, input_path="docs")["named_stores"]["result"]
    assert isinstance(summary, dict), (
        f"expected a summary dict (real ingest), got a blocked/str result instead: {summary!r:.300}"
    )
    assert summary["files_scanned"] == 1
    assert summary["files_skipped"] == 0, (
        "#3102 REGRESSION: a relative input_path must not be silently skipped via a "
        f"malformed file:// URI -- got skipped_files={summary.get('skipped_files')}"
    )
    assert summary["all_discovered_files_skipped"] is False
    assert summary["chunks_upserted"] >= 1, (
        "#3102 REGRESSION: relative input_path produced 0 chunks despite status: ok"
    )

    from reyn.builtin.plugins.rag.scripts.vector_store_server import SqliteVecStore

    with SqliteVecStore(str(project_root / "rag.sqlite")) as store:
        rows = store.list_metadata()
    assert rows, "expected at least one indexed chunk"
    # The fixed contract: file:// (and the stored source_path column riding
    # the same value) is always built from an ABSOLUTE path, regardless of
    # whether the operator's own input_path was relative or absolute.
    assert all(Path(r["metadata"]["source_path"]).is_absolute() for r in rows), (
        f"stored source_path must be absolute even from a relative input_path -- "
        f"got {[r['metadata']['source_path'] for r in rows]}"
    )


def test_ingest_all_discovered_files_skipped_flag_names_the_zero_yield_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: #3102 -- when EVERY discovered file is unusable, the
    summary's ``all_discovered_files_skipped`` flag is True: 0 chunks
    indexed despite ``status: ok`` is a structural fact a caller should not
    have to re-derive itself from ``files_scanned == files_skipped``.

    Falsified in the same test: a normal, at-least-partially-successful
    ingest reports False (not a flag that is vacuously always True)."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "corrupt.pdf").write_bytes(b"\xff\xfe\x00\x01broken")

    summary = _run_ingest(project_root, capsys)["named_stores"]["result"]
    assert summary["files_scanned"] == 1
    assert summary["files_skipped"] == 1
    assert summary["chunks_upserted"] == 0
    assert summary["all_discovered_files_skipped"] is True, (
        "a run that indexed nothing from a non-empty discovery must be NAMED as such"
    )

    # falsify: adding one usable file alongside the unusable one flips it False.
    (docs_dir / "good.txt").write_text("Penguins huddle together for warmth.", encoding="utf-8")
    summary_2 = _run_ingest(project_root, capsys)["named_stores"]["result"]
    assert summary_2["files_skipped"] == 1
    assert summary_2["chunks_upserted"] >= 1
    assert summary_2["all_discovered_files_skipped"] is False, (
        "the flag must not be vacuously True whenever ANY file is skipped -- "
        "only when discovery yielded NOTHING usable at all"
    )
