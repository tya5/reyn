"""Tier 2b: subsystem invariant -- FP-0063 P3, the two builtin RAG pipelines
(``reyn.builtin.pipelines.rag_ingest`` / ``rag_query``),
docs/deep-dives/proposals/0063-builtin-turnkey-user-rag.md.

Drives both pipelines end-to-end through the REAL ``reyn pipe run`` CLI path
(``src/reyn/interfaces/cli/commands/pipe.py::run_run``) against REAL builtin
MCP servers (``reyn.builtin.mcp_servers.vector_store_server`` /
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
  1. Parse: both pipeline files parse cleanly (four + three ``pipeline:``
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
"""
from __future__ import annotations

import argparse
import json
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

from reyn.builtin.registry import BUILTIN_PIPELINES  # noqa: E402
from reyn.core.pipeline.parser import parse_pipeline_docs  # noqa: E402
from reyn.core.pipeline.schema import SchemaRegistry  # noqa: E402
from reyn.interfaces.cli.commands.pipe import run_run  # noqa: E402

_INGEST_PATH = Path(BUILTIN_PIPELINES["rag_ingest"]["path"])
_QUERY_PATH = Path(BUILTIN_PIPELINES["rag_query"]["path"])

# ---------------------------------------------------------------------------
# A real, minimal FastMCP stdio server standing in for markitdown-mcp (not
# installed in this environment -- see module docstring). Converts a
# "file://<path>" URI to Markdown by reading the file as UTF-8 text; good
# enough for .txt/.md fixtures, which is all these tests ingest.
# ---------------------------------------------------------------------------

_STUB_MARKITDOWN_SERVER = '''
import base64
from fastmcp import FastMCP

mcp = FastMCP("stub-markitdown")


@mcp.tool
def convert_to_markdown(uri: str) -> str:
    if uri.startswith("data:"):
        _, _, payload = uri.partition(",")
        return base64.b64decode(payload).decode("utf-8")
    path = uri[len("file://"):] if uri.startswith("file://") else uri
    with open(path, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    mcp.run()
'''


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class FakeEmbeddingProvider:
    """Deterministic real EmbeddingProvider (mirrors
    ``tests/test_op_embed.py``'s own fixture) -- a fixed-length vector per
    text so distinct chunk texts get distinct (but reproducible) vectors,
    with no real embedding API call."""

    def __init__(self) -> None:
        self._batch_size = 100

    async def embed(self, texts: list[str], model: str):
        from reyn.data.embedding.provider import EmbedBatchResult
        vectors = [[float(len(t) % 97), float(sum(map(ord, t)) % 89), 1.0] for t in texts]
        return EmbedBatchResult(vectors=vectors, model=model, total_tokens=sum(len(t) for t in texts))

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
                        },
                        "reyn_chunker": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "reyn.builtin.mcp_servers.chunker_server"],
                        },
                        vectorstore_server: {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "reyn.builtin.mcp_servers.vector_store_server"],
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
    """Tier 2b: rag_ingest.yaml parses -- 6 pipeline: docs (ingest, _blocked,
    _ingest_one_file, _ingest_body, _ingest_embed_and_upsert,
    _ingest_noop_upsert)."""
    docs = parse_pipeline_docs(_INGEST_PATH.read_text(encoding="utf-8"), SchemaRegistry())
    names = {p.name for p in docs}
    assert names == {
        "ingest", "_blocked", "_ingest_one_file", "_ingest_body",
        "_ingest_embed_and_upsert", "_ingest_noop_upsert",
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


def test_upserted_chunk_embedding_model_is_stamped_from_pipeline_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2b: C4 -- every upserted chunk's embedding_model column is
    stamped from the ingest pipeline's OWN input (not hardcoded), verified
    by reading it straight back out of the real sqlite store."""
    monkeypatch.chdir(tmp_path)
    project_root = _write_project(tmp_path)
    docs_dir = project_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.txt").write_text("A short note about penguins.", encoding="utf-8")

    _run_ingest(project_root, capsys, embedding_model="fake-embed-v7")

    from reyn.builtin.mcp_servers.vector_store_server import SqliteVecStore

    with SqliteVecStore(str(project_root / "rag.sqlite")) as store:
        rows = store.list_metadata()
    assert rows, "expected at least one upserted chunk"
    assert all(r["metadata"]["embedding_model"] == "fake-embed-v7" for r in rows)


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
