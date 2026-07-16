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
                            "args": ["-m", "reyn.builtin.mcp_servers.chunker_server"],
                            "env": _server_env(),
                        },
                        vectorstore_server: {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "reyn.builtin.mcp_servers.vector_store_server"],
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

    from reyn.builtin.mcp_servers.vector_store_server import SqliteVecStore

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
