"""Tier 3a: FP-0063 ARC-level reachable-for-purpose witness (#3119).

Closes the gap #3119 names between the two existing RAG-turnkey test files:

  - ``test_fp0063_p3_rag_pipelines.py`` drives the ingest/query pipeline
    MECHANISM end-to-end, but explicitly bypasses ``install_plugin``
    (its own docstring: "this file's job is the PIPELINE BEHAVIOR" — it points
    a project-local ``reyn.yaml`` straight at the plugin's shipped files) and
    has no LLM in the loop at all.
  - ``test_fp0063_p4_builtin_rag_skill.py`` pins the ``rag`` skill's STATIC
    correctness (pipeline-name resolution, tool catalog, doc paths) — again no
    LLM, no live dispatch.

Neither witnesses "an operator hands the LLM the ``rag`` skill + a corpus, and
the LLM DRIVES install -> register -> ingest -> query to a real retrieval
result" — the reachable-for-purpose property
([[feedback-complete-means-reachable-for-purpose]]) that this file pins, for
real, through the REAL production dispatch path (``install_plugin``,
the pipeline installer's capability auto-registration, ``run_pipeline``'s
attached-driver-session execution, the real sqlite-vec store) with the LLM's
own DECISIONS driven through ``LLMReplay`` — this is genuinely Tier 3, not
Tier 2c's "LLM faked via a scripted real callable" carve-out
(``docs/deep-dives/contributing/testing.md`` "Tier 3 — LLM-replay tests").

Driver: NOT ``run_agent_step`` (``runtime/session_api.py``) — its
``_build_agent_step_narrowing`` STRUCTURALLY denies ``run_pipeline`` for any
caller (R5/R6 S3: an agent-step worker must never itself launch a pipeline,
so a nested pipeline can't dodge its own launch-time cost-bound approval).
That rule exists for a worker spawned FROM a pipeline step -- it does not
apply to a top-level operator turn, which is exactly what this witness is.
So this file calls the SAME two lower primitives ``run_agent_step`` itself
composes -- ``spawn_ephemeral_session`` + ``MessageBus.request`` -- directly,
with NO narrowing, giving a freshly spawned worker's ordinary (un-narrowed)
capability envelope. This is still 100% production wiring (the identical
``AgentRegistry`` / ``Session`` / real ``RouterLoopDriver`` / real
``MessageBus`` stack ``test_pipeline_r5_run_agent_step.py`` already exercises
at Tier 2c for the SAME two primitives) -- only the LLM completion call
changes, from that file's ``_ScriptedAgentReply`` (a real callable wired at
the ``_llm_caller`` test seam) to the REAL ``call_llm_tools`` -> real
``litellm.acompletion``, intercepted by the REAL ``LLMReplay`` Fake -- the
literal Tier 2c -> Tier 3 upgrade path testing.md's own Tier 3 section names.

#3520 re-keyed ``turn2_ingest_query.jsonl`` (regenerated via
``REYN_FP0063_ARC_WITNESS_GENERATE=1``; the prompts and the authored responses
are byte-identical, only the hashed keys moved, and ``turn1_install.jsonl``
regenerated unchanged). Turn 1 installs a plugin that registers two MCP
servers; the tools cache's populated-guard used to be one-shot, so those
servers were never probed and turn 2's ``tools=`` carried no
``mcp_tool_name`` entries for them — the model was not told about capabilities
the install had just given it (the shape #3512 reported). #3520 made the guard
per-server, so turn 2 now probes them and their tools reach the payload; that
payload is part of ``LLMReplay.key``'s hashed input, hence the new keys.
MEASURED, not inferred: with the fix, turn 2's server→tools projection is
``{reyn_markitdown: [convert_to_markdown], reyn_chunker: [chunk],
reyn_vector_store: [upsert, query, list_metadata, delete]}`` against turn 1's
``{reyn_markitdown: [convert_to_markdown]}``.

#3556 re-keyed BOTH files the same way, for a smaller reason: it extended the
``session_spawn`` ``narrowing`` parameter DESCRIPTION (the composition is now stated
to the model), and a tool's JSON schema is part of the ``tools=`` payload
``LLMReplay.key`` hashes. Regenerated with the same switch; the authored responses are
byte-identical, only ``key`` / ``key_components`` moved. Verified re-keyed rather than
merely re-saved: the NEW fixtures against the OLD description raise ``MissingFixture``
attributing the drift to ``session_spawn: schema differs``.

#3630 re-keyed BOTH files again, for the same reason at a different tool: the
``present`` schema changed (``blueprint`` object -> array, plus its parameter
description). Same switch, same verification — the NEW fixtures against the OLD
schema raise ``MissingFixture`` attributing the drift to ``present: schema
differs (08b2323ddf3a -> 4ab741e16061)``.

#4556 re-keyed BOTH files again, for the same #3556 shape: ``spawn_session``
(the renamed ``session_spawn``, #4004) gained two new optional parameters
(``agent``/``session``) and matching schema/description entries. Same switch,
same verification, but observed in the OTHER direction from #3556's own note —
the pre-existing (OLD) fixtures against the NEW code raised ``MissingFixture``
FIRST (before regeneration), attributing the drift to exactly
``spawn_session: schema differs (b3b75ea9ec35 -> 49ced6eb14c5)`` — the same
attribution mechanism, run the direction that occurs naturally when a schema
changes and the fixtures haven't caught up yet, rather than deliberately
reversed afterward to double-check the regenerated ones.

#4602 re-keyed BOTH files again (``install_plugin`` sits in every turn's
tool catalog, not just turn 1's own call — its key moved in ``turn2`` too,
even though turn 2 never calls it), for the 4th occurrence of the SAME
#3556 shape: #4570 conversion A's manifest-relocation change edited
``install_plugin``'s own tool DESCRIPTION text
(``tools/descriptions/plugin_management.py``, ``.reyn-plugin/plugin.json`` ->
``plugin.json``), and a tool's description is part of the ``tools=`` payload
``LLMReplay.key`` hashes. Same switch, same verification — CI attributed the
drift to exactly ``install_plugin: schema differs (690799f988fd ->
bda7f3d3e3eb)`` before regeneration, matching #4556's own "observed in the
direction that occurs naturally" note. This is the 4th time a
``tools/descriptions/`` edit has re-keyed this fixture (#3556 / #3630 /
#4556 / #4602) — worth knowing if a future PR touches that directory.

**Historical hazard, fixed by #3634 — regenerating in place used to APPEND.**
``REYN_FP0063_ARC_WITNESS_GENERATE=1`` used to record into whatever was already
there, and ``LLMReplay.flush`` only ever appended: the old keys survived
alongside the new ones and the fixture then matched BOTH the old and the new
schema — green either way, witnessing neither. Measured at the time (#3630):
regenerating in place took ``turn1`` from 6 keys to 8 with all 6 old ones intact,
and the result passed against the pre-change ``present`` schema; deleting the
file first produced 2 keys, and that fixture failed against the old schema as
it should. The same measurement showed the committed fixture had ALREADY been
accumulating this way: ``turn1`` carried the SAME two logical calls three times
over with six distinct keys, i.e. three schema generations layered by earlier
in-place regenerations, any of which would have satisfied a run.

#3634 made this class of accumulation impossible instead of merely
work-around-able: ``LLMReplay.flush`` now REPLACES an on-disk entry with the
freshly-recorded one when a completion this session just recorded shares its
``reyn.dev.testing.replay_stacking.group_signature`` (model + tool_choice +
per-message digests — i.e. everything the key hashes over except ``tools``,
the one component a schema change is expected to move), so regenerating THIS
file in place with ``REYN_FP0063_ARC_WITNESS_GENERATE=1`` (no manual delete
required) now leaves exactly the arc's actual call count (``turn1`` 2 keys,
``turn2`` 3), not a reduction in coverage — and
``tests/dev/test_replay_fixture_no_stacking_3634.py`` is the CI gate that fails if
any committed fixture (this one or any other) holds a stacked group anyway.

Why the fixture responses are AUTHORED, not live-recorded (disclosed, not
silently passed off as a live LLM transcript -- same disclosure norm as this
plugin's markitdown-mcp stub substitution below): this sandbox's LLM
provider quota is exhausted (verified directly: a real ``litellm.acompletion``
call here raises ``litellm.RateLimitError`` -- "You exceeded your current
quota" -- the SAME proxy-quota boundary #3119 itself names as blocking a live
embed call; per CLAUDE.md, an API-key/proxy failure is the proxy operator's
responsibility, never something a test works around by inspecting or
re-deriving credentials). ``_rounds_script`` below stands in for the LIVE
model ONLY at generation time (guarded by ``generate`` -- true when
``REYN_FP0063_ARC_WITNESS_GENERATE=1`` or a fixture is missing), using
the REAL ``LLMReplay`` class's own key-derivation + JSONL-serialization
machinery (``LLMReplay(fixture_path, mode="record")``, with
``litellm.acompletion`` pre-monkeypatched to the scripted function BEFORE
``LLMReplay.install()`` captures it as ``_original_acompletion`` -- so the
committed fixture's SHA-256 keys are byte-derived from the real production
message/tool payloads, not guessed). Every CI/normal run reads the
COMMITTED fixture back through the real, unmodified ``LLMReplay`` in
"replay" mode -- no network, no live model, ever.

Two environment-derived system-prompt fields are PINNED for LLMReplay
key-stability across days/machines (disclosed, narrow, single-purpose
monkeypatches of ``RouterHostAdapter.get_cwd`` / ``get_environment_info`` --
not a fake of Session/RouterLoop/AgentRegistry, all of which stay real):
``RouterHostAdapter.get_environment_info`` always injects today's host-clock
date into the system prompt (``router_host_adapter.py``'s own docstring:
"Always returns: date -- today ISO-8601"), and ``get_cwd`` embeds pytest's
per-run ``tmp_path``. Both flow into the ``messages`` list ``LLMReplay.key()``
hashes, so left live, EITHER would silently invalidate a committed fixture the
day after recording (or on a different machine) -- freezing them is the same
"neutralise the ambient wall clock" move every deterministic test suite makes,
scoped to exactly the two fields responsible, not a stand-in for Session
itself. ``test_pipeline_r5_run_agent_step.py``'s own docstring calls out this
exact hazard as the reason it stays Tier 2c today ("a real recorded LLMReplay
fixture would also embed the live host clock ... into its cache key, breaking
replay across days") -- pinning the two offending fields is what removes it.

Proxy-free / CI-durable (the re-scope #3119 exists for): the plugin is
installed from a LOCAL COPY with `requirements.txt` deliberately absent (this
sandbox's ``builtin-rag`` extra already provides apsw/chonkie/sqlite_vec, and
the copy's ``mcp.json`` points its two REAL servers -- chunker + vector-store
-- at ``sys.executable`` with ``PYTHONPATH`` pinned to this checkout, mirroring
``test_fp0063_p3_rag_pipelines.py``'s ``_server_env()`` precedent) --
so ``plugin_install``'s dependency-materialisation step (real ``python -m
venv`` + real ``pip install``, real network) never triggers at all, and the
``require_http_get("pypi.org")``
permission gate it would need is never reached. The THIRD server
(markitdown) is the SAME disclosed real-FastMCP-stub substitution p3 uses
(the real ``markitdown-mcp`` PyPI package is not installed here). Only the
embedding PROVIDER is faked (``FakeEmbeddingProvider``, the exact same
monkeypatch p3 uses on ``reyn.core.op_runtime.embed.get_provider``) so ingest
needs no real embedding API key/network either.

strip-falsify (co-vet, architect-specified in #3119, all independently
verified while developing this test):
  (a) drop the ``install_plugin`` tool_call from the fixture ->
      ``run_pipeline`` fails immediately (`rag_ingest.ingest`/`rag_query.query`
      never registered) -- the arc's install-dependency is real, not narrated.
  (b) remove the ``FakeEmbeddingProvider`` monkeypatch -> ingest's embed call
      reaches the real provider resolver, which needs live network/API-key
      credentials this sandbox does not have -- reproduces the exact
      proxy-quota blocker #3119 re-scopes around (RateLimitError), proving the
      fake is load-bearing for CI-durability, not decorative.
  (c) the top-result assertion reads the chunk BACK OUT of the real sqlite
      store by its stored ``source_path`` metadata (mirrors p3's own
      non-vacuity discipline) -- an empty/wrong result fails this test, so it
      cannot pass vacuously against a broken retrieval path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
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
from tests._support.agent_session import make_session
from tests._support.paths import REPO_ROOT
from tests._support.rag_plugin_venv import rag_plugin_python as _rag_plugin_python

_RAG_PLUGIN_DIR = Path(_builtin_pkg.__file__).resolve().parent / "plugins" / "rag"

_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "llm" / "fp0063_arc_witness"
_INSTALL_FIXTURE_PATH = _FIXTURE_DIR / "turn1_install.jsonl"
_INGEST_QUERY_FIXTURE_PATH = _FIXTURE_DIR / "turn2_ingest_query.jsonl"

# Real FastMCP stub standing in for markitdown-mcp -- byte-identical
# disclosed substitution to test_fp0063_p3_rag_pipelines.py's own stub
# (same module docstring rationale: not installed in this sandbox).
_STUB_MARKITDOWN_SERVER = '''
import base64
from urllib.parse import urlsplit
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("stub-markitdown")


@mcp.tool()
def convert_to_markdown(uri: str) -> str:
    if uri.startswith("data:"):
        _, _, payload = uri.partition(",")
        return base64.b64decode(payload).decode("utf-8")
    if uri.startswith("file://"):
        # #3102: a real markitdown-mcp REJECTS a non-empty, non-localhost
        # netloc -- reproducing that check (rather than a naive strip) is
        # what makes this stub exercise the real-world failure mode. See
        # test_fp0063_p3_rag_pipelines.py's identical stub for the full note.
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


class FakeEmbeddingProvider:
    """Deterministic real EmbeddingProvider -- byte-identical precedent to
    ``test_fp0063_p3_rag_pipelines.py``'s own fixture (mirrors
    ``tests/core/test_op_embed.py``): fixed-length vector per text, no real
    embedding API call. See that file's docstring for why resolving
    "standard" -> "fake/standard" (not echoing it back) matters."""

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


def _server_env(src_root: str) -> dict[str, str]:
    """Env for the plugin's real MCP server subprocesses -- pins PYTHONPATH to
    *src_root* (the ``out_of_process_reyn`` fixture value, threaded in from
    the calling test rather than a fresh ``import reyn``/``__file__``
    computation here; see test_fp0063_p3_rag_pipelines.py's ``_server_env``
    for the full rationale: the MCP SDK's stdio transport passes only a
    6-key env whitelist that drops PYTHONPATH, so a plain ``env=os.environ``
    inherit is not enough in a multi-worktree dev box)."""
    passthrough = {
        k: v for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LOGNAME", "SHELL", "TERM", "USER", "TMPDIR")
    }
    return {**passthrough, "PYTHONPATH": src_root}


def _prepare_local_plugin_copy(tmp_path: Path, src_root: str) -> Path:
    """Copy the real ``rag`` plugin tree into ``tmp_path``, then:

    - DROP ``requirements.txt`` entirely, so ``plugin_install``'s dependency
      materialisation step (real ``python -m venv`` + ``pip install`` + real
      network fetch) never triggers -- this is a LOCAL install, not the
      builtin fast-path, so the copy is what gets network-audited.
    - Rewrite ``mcp.json``: the plugin's own two REAL servers (chunker,
      vector-store -- the ONLY two it declares; markitdown is deliberately
      NOT part of the plugin bundle, per the pipeline's own X1 pre-flight
      message: "install it yourself" -- same as ``test_fp0063_p3_rag_pipelines.py``,
      which wires ``reyn_markitdown`` directly into ``reyn.yaml``'s
      ``mcp.servers``, not the plugin) get ``command:`` a DEDICATED venv's
      interpreter (``tests/_support/rag_plugin_venv.py``, #4302 option-A) +
      the pinned ``_server_env()`` (so the installed entry runs under THIS
      tree via ``PYTHONPATH``, not whatever bare "python" resolves to on
      PATH at spawn time). Not the ambient sandbox venv: these two servers
      import standalone ``fastmcp``, which reyn's own dev venv deliberately
      does NOT carry (#4302's "core has no fastmcp" invariant) -- the
      dedicated venv is what actually satisfies chonkie/apsw/sqlite_vec +
      fastmcp for THIS subprocess, independent of what the ambient venv
      carries for this module's own direct imports.

    Returns the prepared plugin source directory (pass as
    ``source={"kind": "local", "path": str(...)}`` to ``install_plugin``).
    """
    import shutil

    dest = tmp_path / "rag_plugin_src"
    shutil.copytree(_RAG_PLUGIN_DIR, dest)
    (dest / "requirements.txt").unlink(missing_ok=True)

    mcp_json_path = dest / "mcp.json"
    mcp_json = json.loads(mcp_json_path.read_text(encoding="utf-8"))
    env = _server_env(src_root)
    for name, spec in mcp_json["mcpServers"].items():
        spec["command"] = _rag_plugin_python()
        spec["env"] = env
    mcp_json_path.write_text(json.dumps(mcp_json, indent=2), encoding="utf-8")
    return dest


def _write_markitdown_stub_server(tmp_path: Path) -> Path:
    """Write the disclosed markitdown-mcp stand-in (byte-identical to
    ``test_fp0063_p3_rag_pipelines.py``'s own stub) as a standalone script --
    wired into ``reyn.yaml``'s ``mcp.servers`` directly (mirrors p3's
    ``_write_project``), NOT into the plugin bundle (see
    ``_prepare_local_plugin_copy``'s docstring for why)."""
    stub_path = tmp_path / "stub_markitdown_server.py"
    stub_path.write_text(_STUB_MARKITDOWN_SERVER, encoding="utf-8")
    return stub_path


# ---------------------------------------------------------------------------
# Deterministic LLM decision script -- one tool_call per round, matching the
# real dispatch order this arc requires. See module docstring for why the
# CONTENT is authored (not live-recorded) but the KEYS are real.
# ---------------------------------------------------------------------------


def _tool_call_response(name: str, arguments: dict, *, call_id: str) -> dict:
    return {
        "id": f"gen-{call_id}",
        "created": 1_700_000_000,
        "model": "fake/arc-witness",
        "object": "chat.completion",
        "choices": [{
            "finish_reason": "tool_calls",
            "index": 0,
            "message": {
                "content": None,
                "role": "assistant",
                "tool_calls": [{
                    "index": 0,
                    "id": f"call_{call_id}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
        }],
        "usage": {"completion_tokens": 8, "prompt_tokens": 40, "total_tokens": 48},
    }


def _text_response(content: str) -> dict:
    return {
        "id": "gen-final",
        "created": 1_700_000_000,
        "model": "fake/arc-witness",
        "object": "chat.completion",
        "choices": [{
            "finish_reason": "stop",
            "index": 0,
            "message": {"content": content, "role": "assistant", "tool_calls": []},
        }],
        "usage": {"completion_tokens": 6, "prompt_tokens": 40, "total_tokens": 46},
    }


def _rounds_script(rounds: list[dict], *, label: str):
    """Ordinal scripted acompletion over a fixed round list. Purely
    ORDER-based (no message-content sniffing) -- safe because the fixture is
    self-consistent: replaying round N's canned response always reproduces
    the SAME round-(N+1) message history that was captured when the fixture
    was generated (see module docstring)."""
    state = {"i": 0}

    async def _script(model: str, messages: list[dict], **kwargs: Any):
        import litellm

        idx = state["i"]
        state["i"] += 1
        if idx >= len(rounds):
            raise AssertionError(
                f"arc witness script ({label}) exhausted after {len(rounds)} "
                f"rounds -- the real RouterLoop asked for a {idx + 1}th completion"
            )
        return litellm.ModelResponse(**rounds[idx])

    return _script


def _make_install_script(plugin_src: Path):
    """Turn 1's 2-round script: install the plugin, then a final text turn.

    Two SEPARATE turns (spawn -> drive -> spawn -> drive), not one turn with
    4 rounds, deliberately: ``run_pipeline``'s registry lookup
    (``pipeline_verbs.py::_handle_run_pipeline``) reads
    ``ctx.router_state.pipeline_registry`` fresh per tool call, and the
    install's mid-turn hot-reload (``dispatch_install_reload`` ->
    ``HotReloader.apply_now``) DOES report success for the "pipelines" seam --
    but a SEPARATE, well-established mechanism is what #3097 actually
    hardens and what the rest of the codebase already relies on for
    "installed-then-used" reachability: a FRESH ``spawn_ephemeral_session``
    rebuilds its ``PipelineRegistry`` from the CURRENT on-disk config cascade
    at spawn time (``Session.refresh_config_projections`` ->
    ``Session._reapply_pipelines``, wired through
    ``spawn_session_recorded`` -- see ``session_api.py``'s module docstring).
    Splitting the arc across two spawned turns rides that already-hardened
    path instead of the same-turn immediate-reload path, which is a
    narrower, seam-scoped optimisation (#2761 PR-2, "resolution mid-turn,
    discovery next turn") not designed or tested for a same-turn NEW-NAME
    pipeline `run_pipeline` lookup specifically."""
    return _rounds_script([
        _tool_call_response(
            "install_plugin",
            {"source": {"kind": "local", "path": str(plugin_src)}},
            call_id="install",
        ),
        _text_response("Installed the rag plugin."),
    ], label="install")


def _make_ingest_query_script(project_root: Path):
    """Turn 2's 3-round script (fresh ephemeral spawn -- see
    ``_make_install_script`` for why): ingest -> query -> final text."""
    return _rounds_script([
        _tool_call_response(
            "run_pipeline",
            {
                "name": "rag_ingest.ingest",
                "input": {
                    "input_path": str(project_root / "docs"),
                    "output_db": str(project_root / "rag.sqlite"),
                },
            },
            call_id="ingest",
        ),
        _tool_call_response(
            "run_pipeline",
            {
                "name": "rag_query.query",
                "input": {
                    "query_text": "what is reyn",
                    "db": str(project_root / "rag.sqlite"),
                    "top_k": 3,
                },
            },
            call_id="query",
        ),
        _text_response("Ingested the corpus and queried it."),
    ], label="ingest_query")


# ---------------------------------------------------------------------------
# Real Session/AgentRegistry wiring (mirrors test_pipeline_r5_run_agent_step.py's
# ``_registry`` helper) -- every collaborator is the real production object;
# only litellm.acompletion is intercepted (by LLMReplay, at the real boundary).
# ---------------------------------------------------------------------------


def _build_registry(tmp_path: Path, project_root: Path):
    from reyn.core.events.state_log import StateLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session
    from reyn.runtime.session_params import PresentationWiring
    from reyn.security.permissions.permissions import PermissionResolver

    state_log = StateLog(project_root / ".reyn" / "wal.jsonl")
    resolver = ModelResolver({"standard": "gemini/gemini-2.5-flash-lite"})

    # ``mcp.<server>: allow`` mirrors the ``permissions:`` block written into
    # this test's ``reyn.yaml`` -- BUT ``PermissionResolver`` is built here
    # by hand (not via ``load_config``), so that file's ``permissions:``
    # section is otherwise never read into THIS resolver. ``require_mcp``'s
    # AgentLayer checks ``server in decl.mcp`` directly (no zone/approval
    # fallback for the MCP axis, unlike file/network) -- ``decl.mcp`` for the
    # ``call_mcp_tool`` op is built from ``ctx.permission_resolver``'s config
    # at the router's ``op_context_factory`` seam, which is what actually
    # reads this dict.
    perm_resolver = PermissionResolver(
        config_permissions={"mcp": {
            "reyn_chunker": "allow", "reyn_vector_store": "allow",
            "reyn_markitdown": "allow",
        }},
        project_root=project_root, interactive=False,
    )
    # Actor must match the STRING plugin_install's OpContext stamps
    # (``ctx.actor = "install_plugin"``, plugin_management_verbs.py)
    # -- PermissionResolver's approval key is actor-scoped
    # (``f"{actor}/{kind}/{path}"``), and the pipeline/mcp sub-installs reuse
    # this SAME ctx (so the same actor covers their writes too).
    from reyn.core.op_runtime.plugin_install import plugins_root
    _install_actor = "install_plugin"
    perm_resolver.session_approve_path(
        str(plugins_root()), _install_actor, "file.write", recursive=True,
    )
    for cfg in ("mcp.yaml", "pipelines.yaml", "skills.yaml"):
        perm_resolver.session_approve_path(
            str(project_root / ".reyn" / "config" / cfg), _install_actor, "file.write",
        )

    # Session's own DEFAULT SandboxPolicy (auto-built when ``sandbox_config``
    # is omitted) restricts ``write_paths`` to the repo checkout only -- a
    # SandboxLayer veto sits alongside (and is a HARDER restriction than) the
    # PermissionResolver approval above (EffectivePermission is an ALL-layers
    # conjunction, ``effective.py``'s own docstring: "grant-back forbidden").
    # An explicit policy widening ``write_paths`` to this test's own plugin +
    # project roots is required for the install to reach the approval check
    # above at all.
    from reyn.config.infra import SandboxConfig
    sandbox_config = SandboxConfig(
        policy={
            "network": False,
            "allow_write_paths": [str(plugins_root()), str(project_root)],
            # #3823: reyn.yaml's config vocabulary is decoupled from
            # SandboxPolicy's internal field names (was: a direct
            # transcription, #3901 PR-B ④) — subprocess=False is the
            # positive-framed config key, translated internally to
            # deny_subprocess=True (see policy.py's
            # _translate_sandbox_policy_config).
            "subprocess": False,
        },
    )

    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            # #3121 step1 folded presentation_consumer / intervention_bridge into
            # the PresentationWiring parameter object.
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer,
                intervention_bridge=intervention_bridge,
            ),
            resolver=resolver, permission_resolver=perm_resolver,
            sandbox_config=sandbox_config,
        )

    reg = AgentRegistry(project_root=project_root, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("operator")
    return reg, perm_resolver


def _child_pids(parent_pid: int) -> "set[int]":
    """OS-level snapshot of ``parent_pid``'s DIRECT child pids (``pgrep -P``).

    #4759 structural-witness support: this is deliberately an OS-process-table
    read, not a reyn-internal one (``MCPClient._child_pid`` or similar) -- the
    child either exists in the kernel's process table or it doesn't,
    independent of anything reyn's own bookkeeping claims about it, which is
    the whole point of a *structural* witness over a *symptom* one (see the
    test's own #4759 comment below). All of this arc's real MCP server
    subprocesses (chunker, vector-store) are spawned in-process via
    ``anyio.open_process``, so they are direct children of the pytest worker
    process itself -- no need to walk a process tree.
    """
    proc = subprocess.run(
        ["pgrep", "-P", str(parent_pid)], capture_output=True, text=True, check=False,
    )
    return {int(tok) for tok in proc.stdout.split()}


def _pid_is_alive(pid: int) -> bool:
    """True iff the OS process table still has ``pid`` (any owner)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not signalable by us -- still alive
    return True


async def _drive_one_turn(registry, prompt: str, timeout: float) -> str:
    """The SAME two primitives ``run_agent_step`` composes
    (``spawn_ephemeral_session`` + ``MessageBus.request``), called directly
    with NO agent-step narrowing -- see module docstring for why
    ``run_agent_step`` itself cannot be used here (it structurally denies
    ``run_pipeline``)."""
    from reyn.runtime.message_bus import MessageBus
    from reyn.runtime.session_api import spawn_ephemeral_session
    from reyn.runtime.spawn_routing import AuditOnlyNoSurface
    from reyn.runtime.transport import SystemRef

    routing = AuditOnlyNoSurface()
    sid = await spawn_ephemeral_session(
        registry, identity="operator", narrowing=None,
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    session = registry.get_session("operator", sid)
    assert session is not None

    bus = MessageBus()
    replies = await bus.request(
        session, kind="user",
        payload={"text": prompt, "chain_id": uuid.uuid4().hex},
        reply_to=SystemRef(), timeout=timeout,
    )
    return "\n\n".join(r.text for r in replies if r.kind == "agent")


# ---------------------------------------------------------------------------
# The arc witness
# ---------------------------------------------------------------------------


#: The arc's base dir. FIXED and hardcoded (NOT ``tmp_path``) on purpose: its
#: STRING form is baked into the committed LLMReplay fixture -- both into the
#: hashed key and into the replayed tool-call ARGUMENTS the runtime actually
#: acts on (turn 2's ``run_pipeline`` input paths come back OUT of the fixture),
#: so a per-run unique dir would need the fixture re-recorded on every run. See
#: the test's own docstring for the two-part portability rationale.
_ARC_BASE_DIR = Path("/tmp/reyn_fp0063_arc_witness_dir")  # noqa: S108 -- see above
_ARC_LOCK_PATH = Path("/tmp/reyn_fp0063_arc_witness_dir.lock")  # noqa: S108


@pytest.fixture
def arc_base_dir():
    """#3473: hand the test its base dir under a cross-process EXCLUSIVE lock.

    ``_ARC_BASE_DIR`` is machine-global and the test wipes it on entry, so two
    concurrent instances of THIS test -- a co-located suite in another worktree,
    two CI jobs sharing a runner, a standalone run beside a full ``-n auto``
    suite -- were not merely racing, they were deleting each other's live tree
    (including the process CWD the test chdir's into). Measured on #3473: the
    reported symptom, ``FileNotFoundError: '.reyn/agents/operator/state/
    sessions/<sid>/history.jsonl'`` (a RELATIVE path -- the CWD anchor itself
    was gone), reproduced only in the arm whose co-located load suite ALSO ran
    this file, and never in the arm that excluded it; two bare concurrent runs
    reproduce it in seconds as ``FileExistsError`` on the ``mkdir`` below.

    The lock is mutual EXCLUSION, not a wait-and-hope: a second instance cannot
    enter the directory at all until the first has left it, so interleaving is
    structurally impossible rather than unlikely. (A sleep/retry here would only
    have made "usually makes it in time" into "makes it in time more often" --
    the shape #3473 explicitly rules out.) ``flock`` is advisory but every
    participant is this same fixture, which is what makes advisory sufficient.
    """
    import fcntl
    import shutil

    fd = os.open(_ARC_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Wiped + recreated so each run starts clean -- safe to do destructively
        # ONLY because the lock above guarantees no peer is inside it right now.
        shutil.rmtree(_ARC_BASE_DIR, ignore_errors=True)
        _ARC_BASE_DIR.mkdir(parents=True)
        yield _ARC_BASE_DIR
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _install_key_normalizer(monkeypatch: pytest.MonkeyPatch, base_dir: Path) -> None:
    """Make ``LLMReplay.key`` base-dir-INDEPENDENT for this run.

    See the test's own docstring for the full why + the two-part fix this is
    half of. Mechanism: wrap the ``LLMReplay.key`` staticmethod so that, before
    the SHA-256 over ``messages`` (and ``tools``), every occurrence of the
    test's ``base_dir`` -- in BOTH its raw form (``base_dir``) and its
    symlink-resolved form (``base_dir.resolve()``, the ``/private/tmp/...`` shape
    plugin_install stamps on macOS but NOT on Linux, where ``/tmp`` is not a
    symlink) -- is replaced with a fixed sentinel. This is what neutralises the
    ONE cross-OS difference the fixed ``/tmp`` base does not already erase: the
    macOS ``/private`` prefix that plugin_install's ``.resolve()`` adds to the
    tool-RESULT paths (``plugin_root`` / ``config_path`` / ``source_path``).
    Applied to record AND replay symmetrically (the same patched ``key``
    computes both). The wrap only touches KEY COMPUTATION -- the stored
    response, the replayed response, and (in record mode) the real scripted
    call all still see the original, unmodified ``messages``."""
    from reyn.dev.testing import replay as _replay_mod

    tokens = [str(base_dir.resolve()), str(base_dir)]
    sentinel = "<ARC_TMP>"

    def _scrub(obj: Any) -> Any:
        if isinstance(obj, str):
            s = obj
            for tok in tokens:
                s = s.replace(tok, sentinel)
            return s
        if isinstance(obj, list):
            return [_scrub(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _scrub(v) for k, v in obj.items()}
        return obj

    _orig_key = _replay_mod.LLMReplay.key

    def _patched_key(
        model: str, messages: list, tools: "list | None" = None,
        tool_choice: "str | None" = None, **kwargs: Any,
    ) -> str:
        # ``**kwargs`` forwards ``preconditions`` (#3473) unchanged: this wrap
        # owns ONE key input (the base-dir strings) and must stay transparent
        # to every other, or it silently disables them.
        return _orig_key(
            model, _scrub(messages),
            tools=_scrub(tools) if tools else tools, tool_choice=tool_choice,
            **kwargs,
        )

    monkeypatch.setattr(_replay_mod.LLMReplay, "key", staticmethod(_patched_key))


@pytest.mark.asyncio
async def test_llm_driven_install_ingest_query_arc_reaches_the_ingested_chunk(
    monkeypatch: pytest.MonkeyPatch, out_of_process_reyn: str, arc_base_dir: Path,
) -> None:
    """Tier 3a: an LLM (replayed via LLMReplay at the real litellm.acompletion
    boundary) drives install_plugin -> run_pipeline(rag_ingest.ingest)
    -> run_pipeline(rag_query.query) end-to-end through REAL reyn dispatch
    (real Session/RouterLoop, real plugin install + capability registration,
    real MCP chunker/vector-store servers, real sqlite-vec store), proxy-free
    (only the embedding PROVIDER and the markitdown-mcp server are faked/
    stubbed -- see module docstring), and the query returns the ingested
    chunk as its top result -- read back from the real store, not narrated by
    the LLM's own text.

    Scope note (over-claim guard): this is a MECHANISM-REGRESSION witness --
    it proves the LLM-driven install -> register -> ingest -> query path stays
    reachable-for-purpose (any of the classes fixed under #2955 -- registration
    seam, pipeline registry, on_error, glob projection, MCP grant -- silently
    breaking makes THIS go RED). It is NOT a proof of any no-nudge / autonomy
    property: the LLM's decisions here are a fixed scripted sequence
    (``_rounds_script``), so it witnesses that the DISPATCH PATH works when the
    right calls are made, not that a live model would choose them unprompted.

    Portable ``LLMReplay`` keys across machines/OSes (the #3122-review fix -- the
    first cut hit exactly this and went RED on CI[Linux] while green on
    local[macOS]). ``LLMReplay.key`` hashes over ``messages``, and this arc bakes
    absolute paths into ``messages`` in TWO distinct ways, each needing its own
    half of the fix:

      1. **LLM-OUTPUT paths** -- the install source path / ingest+query
         input paths are emitted by the scripted responses (``_make_install_script``
         / ``_make_ingest_query_script``) and RECORDED VERBATIM into the fixture.
         A replayed round then re-injects the RECORDING run's path into the next
         round's ``messages`` -- so a per-run ``tmp_path`` (pytest's, or
         ``tempfile.gettempdir()`` which is itself OS-specific: macOS
         ``/var/folders/...`` vs Linux ``/tmp``) bakes the recording machine's
         dir into the committed key and no later run can scrub it (its own scrub
         token is a DIFFERENT dir). Fix: a FIXED, hardcoded ``/tmp`` base
         (``_ARC_BASE_DIR``) whose STRING form is byte-identical on macOS and
         Linux -- so the recorded path == every replay run's path.
      2. **Tool-RESULT paths** -- ``plugin_root`` / ``config_path`` /
         ``source_path`` are stamped by ``plugin_install`` via ``.resolve()``,
         which on macOS prepends ``/private`` (``/tmp`` is a symlink there) but
         does NOT on Linux. So even with the fixed ``/tmp`` base, the resolved
         RESULT paths still differ by OS. Fix: ``_install_key_normalizer`` scrubs
         BOTH the raw (``/tmp/...``) and resolved (``/private/tmp/...``) base
         forms to a sentinel before the hash.

    Together the committed key contains no machine- or OS-specific absolute path
    at all. The price is that ``_ARC_BASE_DIR`` is a MACHINE-GLOBAL, fixed path
    that this test wipes on entry -- so it is a shared mutable resource, and the
    ``arc_base_dir`` fixture's exclusive lock (see its docstring, #3473) is what
    makes concurrent occupancy structurally impossible rather than merely
    unlikely."""
    import reyn.core.op_runtime.embed as embed_mod
    from reyn.runtime.services.router_host_adapter import RouterHostAdapter

    tmp_path = arc_base_dir

    _install_key_normalizer(monkeypatch, tmp_path)

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    fake_embed = FakeEmbeddingProvider()
    monkeypatch.setattr(embed_mod, "get_provider", lambda *a, **k: fake_embed)

    # Pin the two system-prompt fields that would otherwise silently
    # invalidate the committed fixture's LLMReplay keys (see module docstring).
    monkeypatch.setattr(RouterHostAdapter, "get_cwd", lambda self: "/fixture/project")
    monkeypatch.setattr(
        RouterHostAdapter, "get_environment_info", lambda self: {"date": "2024-01-01"},
    )

    project_root = tmp_path / "proj"
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "notes.txt").write_text(
        "Reyn is an operating system for LLM agents.", encoding="utf-8",
    )
    # Workspace/config-write resolution (``_resolve_project_root(ctx.workspace)``,
    # pipeline_install.py / mcp_install.py) anchors on the process CWD when no
    # explicit workspace_base_dir is threaded through Session -- chdir into the
    # tmp project so ``.reyn/config/*.yaml`` writes land under project_root, not
    # this checkout's own tree (mirrors test_fp0063_p3_rag_pipelines.py's own
    # ``monkeypatch.chdir(tmp_path)``).
    monkeypatch.chdir(project_root)
    # ``reyn.yaml`` is the PROJECT-ROOT MARKER ``load_config``'s
    # ``_find_project_root`` walks up from cwd looking for
    # (``config/loader.py``) -- without it, ``load_config`` never enters its
    # project-tier merge at all, so ``.reyn/config/{pipelines,mcp,skills}.yaml``
    # (exactly what plugin_install writes) is silently never read back, on
    # EVERY reload/spawn-refresh, not just the first. Mirrors
    # ``test_fp0063_p3_rag_pipelines.py``'s own ``_write_project``.
    # markitdown-mcp is NOT installed in this sandbox and is not part of the
    # ``rag`` plugin bundle (see ``_prepare_local_plugin_copy``'s docstring) --
    # wired directly into ``mcp.servers`` here, exactly as
    # ``test_fp0063_p3_rag_pipelines.py``'s ``_write_project`` does.
    markitdown_stub = _write_markitdown_stub_server(tmp_path)
    (project_root / "reyn.yaml").write_text(
        yaml.dump({
            "model": "standard", "models": {"standard": "gemini/gemini-2.5-flash-lite"},
            "mcp": {"servers": {
                "reyn_markitdown": {
                    "type": "stdio", "command": sys.executable,
                    "args": [str(markitdown_stub)], "env": _server_env(out_of_process_reyn),
                },
            }},
            # A plain ``reyn pipe run`` CLI invocation auto-grants any MCP
            # server it finds configured in ``.reyn/config/mcp.yaml`` (the
            # error message ``run_pipeline`` raised while developing this
            # test names this explicitly) -- but this witness drives the
            # SAME pipeline through an LLM ``run_pipeline`` TOOL CALL inside
            # a live Session, which has no such CLI-only auto-grant, so the
            # 3 servers the ``rag`` plugin installs need an explicit
            # declared grant here (mirrors what a real operator's own
            # ``reyn.yaml`` would carry after installing this plugin).
            "permissions": {"mcp": {
                "reyn_chunker": "allow", "reyn_vector_store": "allow",
                "reyn_markitdown": "allow",
            }},
        }, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    plugin_src = _prepare_local_plugin_copy(tmp_path, out_of_process_reyn)
    registry, _perm_resolver = _build_registry(tmp_path, project_root)

    # #4759: this arc's Turn 1 (install_plugin) launches the rag plugin's
    # REAL chunker/vector-store MCP servers as real OS subprocesses (a
    # dedicated venv interpreter, _prepare_local_plugin_copy's own
    # docstring) -- unlike every other test in this file's own family
    # (test_fp0063_p3_rag_pipelines.py explicitly bypasses install_plugin;
    # test_pipeline_r5_run_agent_step.py never touches MCP at all), so this
    # is the ONE test in the family that actually opens one. Without an
    # explicit registry.shutdown() (the #2714 production teardown seam,
    # AgentRegistry's own method, NOT a test-invented mechanism -- it
    # already closes every loaded session's held MCP connections on the
    # normal-exit path), the child process's fate is left to whatever
    # implicit cleanup the OS/asyncio-runner teardown happens to trigger.
    # Investigated (#4759, architect): pytest-asyncio's own runner teardown
    # (_cancel_all_tasks -> os.waitpid(pid, 0), BLOCKING, no WNOHANG) hits
    # this leaked child and hangs INDEFINITELY if it hasn't already exited
    # on its own by the time teardown reaches that point -- a genuine race
    # (measured 1/6 reproduction rate), not a "slow" test; no amount of
    # extra time budget fixes a wait that never returns. try/finally here
    # closes the held MCP connections (and, by extension, terminates the
    # subprocess -- MCPClient.close's own belt-and-suspenders reap,
    # test_2714_mcp_shutdown_teardown.py's own precedent) BEFORE the
    # function returns and pytest-asyncio's runner teardown ever begins,
    # removing the race rather than budgeting more time for it.
    #
    # The SYMPTOM (this test not hanging) is not a sufficient witness that
    # the above claim is what's actually happening: the underlying race
    # resolves favorably (the child happens to have exited on its own by the
    # time teardown reaches the blocking waitpid) roughly 5/6 of the time
    # even with registry.shutdown() removed entirely -- so "the test passed"
    # would stay green under a strip-falsify of the fix most of the time,
    # which makes it a weak witness (lead-coder, #4759: a raw repeat-count
    # would need n >= 17 clean runs for ~5% false-negative confidence, and
    # even then never explains WHY). The MECHANISM claim -- that
    # registry.shutdown() is what actually terminates the child, not luck --
    # is instead witnessed structurally below: real OS pids captured while
    # the arc's servers are confirmed running, asserted gone from the OS
    # process table after registry.shutdown() returns. Stripping the
    # shutdown() call must flip ONLY that assertion red, independent of
    # whether this particular run happened to hang.
    _baseline_children = _child_pids(os.getpid())
    _arc_children: "set[int]" = set()
    try:
        from reyn.dev.testing.replay import LLMReplay

        generate = (
            os.environ.get("REYN_FP0063_ARC_WITNESS_GENERATE") == "1"
            or not _INSTALL_FIXTURE_PATH.exists()
            or not _INGEST_QUERY_FIXTURE_PATH.exists()
        )

        # -- Turn 1: LLM drives install_plugin ----------------------
        if generate:
            monkeypatch.setattr(
                "litellm.acompletion", _make_install_script(plugin_src), raising=False,
            )
        replay_install = LLMReplay(
            _INSTALL_FIXTURE_PATH, mode="record" if generate else "replay",
        )
        replay_install.install()
        try:
            install_text = await _drive_one_turn(
                registry, "Install the rag plugin.", timeout=60.0,
            )
        finally:
            replay_install.restore()
            if generate:
                replay_install.flush()
        assert install_text, "turn 1 (install) must reach a final assistant turn"

        # -- Turn 2 (a FRESH ephemeral spawn -- see _make_install_script's
        # docstring for why): LLM drives ingest then query -----------------------
        if generate:
            monkeypatch.setattr(
                "litellm.acompletion", _make_ingest_query_script(project_root), raising=False,
            )
        replay_query = LLMReplay(
            _INGEST_QUERY_FIXTURE_PATH, mode="record" if generate else "replay",
        )
        replay_query.install()
        try:
            final_text = await _drive_one_turn(
                registry,
                "Ingest the docs/ corpus into rag.sqlite, then query it for 'what is reyn'.",
                timeout=60.0,
            )
        finally:
            replay_query.restore()
            if generate:
                replay_query.flush()
        assert final_text, "turn 2 (ingest/query) must reach a final assistant turn"

        # #4759 structural witness, part 1: capture the arc's real MCP server
        # subprocess(es) while still confirmed alive, BEFORE registry.shutdown()
        # runs (below, in the finally). An empty set here would mean this
        # witness has nothing to observe -- fail loudly rather than let part 2
        # vacuously pass.
        _arc_children = _child_pids(os.getpid()) - _baseline_children
        assert _arc_children, (
            "expected the rag plugin's real chunker/vector-store MCP server "
            "subprocess(es) to be running as direct children of this process "
            "at this point in the arc -- found none, so the #4759 structural "
            "witness below would vacuously pass"
        )
        assert all(_pid_is_alive(pid) for pid in _arc_children), (
            f"one or more of the arc's own captured child pids {_arc_children} "
            "were already dead before registry.shutdown() ran"
        )

        from reyn.builtin.plugins.rag.scripts.vector_store_server import SqliteVecStore

        with SqliteVecStore(str(project_root / "rag.sqlite")) as store:
            rows = store.list_metadata()
        assert rows, (
            "the LLM-driven arc must have ingested at least one real chunk into "
            "the sqlite store -- an empty store means install/register/ingest "
            "never actually ran, regardless of what the final turn's text claims"
        )
        indexed = {Path(r["metadata"]["source_path"]).name for r in rows}
        assert indexed == {"notes.txt"}, (
            f"expected the ingested corpus to contain exactly notes.txt, got {sorted(indexed)}"
        )
    finally:
        await registry.shutdown()

    # #4759 structural witness, part 2: registry.shutdown() (the #2714
    # production teardown seam) must have actually reaped the arc's own MCP
    # server subprocess(es) at the OS level -- not merely "the test function
    # returned without hanging" (see the #4759 comment above this test for
    # why that symptom alone is not trustworthy evidence). Strip-falsify:
    # commenting out the ``await registry.shutdown()`` line above must flip
    # ONLY this assertion red.
    _still_alive = {pid for pid in _arc_children if _pid_is_alive(pid)}
    if _still_alive:  # diagnostic aid on failure -- which server, not just which pid
        for _pid in _still_alive:
            _r = subprocess.run(
                ["ps", "-p", str(_pid), "-o", "pid,ppid,command"],
                capture_output=True, text=True, check=False,
            )
            print(f"#4759 survivor {_pid}:\n{_r.stdout}\n{_r.stderr}")
    assert not _still_alive, (
        f"MCP server subprocess(es) {_still_alive} survived registry.shutdown() "
        "-- the #2714 teardown seam did not actually terminate them"
    )
