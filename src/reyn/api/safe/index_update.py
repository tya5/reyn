"""Safe-mode `index_update` entry point for RAG ingestion in CodeAct/python
steps (FP-0057 Phase 2b).

**Retires `reyn.api.safe.embed_index.embed_and_index` (clean-break, no
shim).** The old provider-direct streaming entry (append/replace + manual
batch_size) is replaced by a thin dispatch onto the `index_update` op — the
SAME incremental/delta-reconcile primitive the LLM-facing `index_update` tool
and `IndexUpdateIROp` already implement (FP-0057 Phase 2a,
``reyn.core.op_runtime.index_update``). A safe-mode python step is now just
another `index_update` caller, encapsulated (it calls the op, not
`provider.embed()` directly, not a pipeline) — no duplicated embed/index
logic in this module.

Behavioral differences from the retired `embed_and_index` (intentional,
per the op's contract — see ``core/op_runtime/index_update.py``):

- **No `mode` parameter.** `index_update` is reconcile-only: `add` (new
  `content_hash`/`source_path`), `update` (path re-supplied with a changed
  hash — stale hash removed in the same pass), `remove` (an indexed hash
  whose path is re-supplied but whose hash is not), `skip` (unchanged
  hash, no re-embed). A from-scratch rebuild is `index_drop` ->
  `index_update` on the emptied source (mirrors the CLI's ``reyn source rm``
  + re-run). The old `mode="replace"` full-clear has no direct equivalent —
  call `index_drop` first if a full rebuild is intended.
- **No streaming batches.** The caller supplies the full current chunk set
  for whatever `source_path`s it is (re-)ingesting in one call — reconcile
  needs to see the complete set for a path to detect deletions. The op
  batches the resulting to-embed chunks internally via the shared `embed`
  op (no caller-visible `batch_size`).
- **Cost surfacing.** Large-batch ingestion now surfaces an
  ``index_update_cost_warning`` audit-event + a `cost_warning` envelope
  field when the to-embed batch exceeds `embedding.cost_warn_threshold`
  (previously dead-code for this entry point) — see
  ``core/op_runtime/index_update.py``.
- **Redaction-egress seam.** The embed call is dispatched via the shared
  `embed` op, so every chunk text passes the PRE-embed `redact_secrets`
  scan (co-vet #3) before it reaches an API-backed embedding provider —
  the old entry called `provider.embed()` straight, with no scan.

Config context (self-sufficient — mirrors the retired module)
---------------------------------------------------------------
This module resolves its own workspace root, mirroring the old entry's
self-loading, so the python harness needs no extra wiring:

- **workspace_root** defaults to ``Path.cwd()`` (cwd == workspace contract;
  the index write lands under ``<cwd>/.reyn/cache/index/``).
- **embedding provider** is resolved BY THE OP ITSELF (``REYN_EMBEDDING_PROVIDER``
  env + ``load_config().embedding``, same resolution `index_update`'s
  LLM-facing tool call uses) — this module does not construct a provider.
  :func:`_set_context`'s `provider_name` override sets the
  ``REYN_EMBEDDING_PROVIDER`` env var for the duration of the override (test
  hook only — production callers never override this).

Sandbox self-gate (#1199 S3.4 Part1 parity)
--------------------------------------------
The retired module forwarded the phase's `default_sandbox_policy.write_paths`
cap straight into `SqliteIndexBackend(sandbox_write_paths=...)` so a
subprocess-side self-gate applied even with no `ctx.permission_resolver`
(the subprocess has none). The `index_update` op's own `SqliteIndexBackend`
construction does not accept that cap, so this module performs the
EQUIVALENT pre-flight check itself (same `_within_paths` primitive the
backend uses) before dispatching the op — preserving the prior safety
property without needing an op-layer change. The pre-flight gates BOTH
writes the op performs: the source's own `index.db` (via
`cache_dir_for_source`) AND the source manifest `sources.yaml` (via
`sources_manifest_path`) — mirroring the LLM-tool path's own permission
gate (`core/op_runtime/index_update.py`, resolver != None), which declares
`file.write` authority over both paths. Before this parity fix, only the DB
path was self-gated here, so a `write_paths` cap that excluded
`.reyn/config/` still let a safe-mode call mutate the manifest.

Internal layering
------------------
This module is reyn-package internal code (= not subject to the safe-mode
AST validator). It freely imports the op-runtime dispatch + workspace/event
plumbing; the validator only rejects *user-code* imports outside the
allowlist, and ``reyn.api.safe.*`` is admitted wholesale (prefix match).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Iterable

# ── Internal state ─────────────────────────────────────────────────────────
#
# All ``None`` = use the self-sufficient defaults (cwd / env). :func:`_set_context`
# overrides any subset for tests; :func:`_reset_context` restores the defaults.
# Mirrors the module-globals contract of the retired ``embed_index`` module.

_workspace_root: Path | None = None
_provider_name: str | None = None
_provider_env_override: str | None = None  # the REYN_EMBEDDING_PROVIDER value we set, for reset
_sandbox_write_paths: "list[str] | None" = None


def _set_context(
    *,
    workspace_root: str | Path | None = None,
    provider_name: str | None = None,
    sandbox_write_paths: "list[str] | None" = None,
) -> None:
    """Override the self-sufficient defaults (test / harness-wiring hook).

    ``provider_name``, when given, sets the ``REYN_EMBEDDING_PROVIDER`` env
    var — the actual resolution seam both the shared `embed` op and
    `index_update`'s cost-estimate provider read (test-only; production
    callers never pass this).
    """
    global _workspace_root, _provider_name, _provider_env_override, _sandbox_write_paths
    if workspace_root is not None:
        _workspace_root = Path(workspace_root)
    if provider_name is not None:
        _provider_name = provider_name
        _provider_env_override = provider_name
        os.environ["REYN_EMBEDDING_PROVIDER"] = provider_name
    if sandbox_write_paths is not None:
        _sandbox_write_paths = list(sandbox_write_paths)


def _reset_context() -> None:
    """Clear all overrides -> back to the self-sufficient defaults."""
    global _workspace_root, _provider_name, _provider_env_override, _sandbox_write_paths
    if _provider_env_override is not None:
        os.environ.pop("REYN_EMBEDDING_PROVIDER", None)
    _workspace_root = None
    _provider_name = None
    _provider_env_override = None
    _sandbox_write_paths = None


def _resolve_workspace_root() -> Path:
    return _workspace_root if _workspace_root is not None else Path.cwd()


class _SafeWorkspace:
    """Minimal duck-typed `Workspace` — the `index_update` handler only reads
    ``.base_dir``. Avoids constructing the real `Workspace` class (which
    requires an `EventLog` + touches CWD-relative resolution this module
    does not need)."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


# ── Core ───────────────────────────────────────────────────────────────────


async def index_update_async(
    chunks: Iterable[dict],
    source: str,
    model: str = "standard",
    *,
    description: str | None = None,
    path: str | None = None,
) -> dict:
    """Reconcile ``chunks`` into ``source``'s index via the `index_update` op.

    ``chunks`` is the full current chunk set for whatever `source_path`s this
    call is (re-)ingesting — each chunk carries `text` + `metadata` with
    `content_hash`/`source_path` (see `IndexUpdateIROp`). Adds new hashes,
    re-embeds changed ones, removes stale ones for any `source_path`
    mentioned, and skips unchanged ones (no re-embed) — see the module
    docstring for the full add/update/remove/skip contract.

    Returns the op's envelope: ``{kind, source, added, updated, removed,
    skipped, chunk_count, embedding_model, cost_warning}``.
    """
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.index.backend import cache_dir_for_source, sources_manifest_path
    from reyn.data.index.backends.sqlite import _within_paths
    from reyn.schemas.models import IndexUpdateIROp
    from reyn.security.permissions.permissions import PermissionDecl

    workspace_root = _resolve_workspace_root()

    # #1199 S3.4 Part1 parity: self-gate the index DB path against the
    # phase sandbox write_paths cap (the op has no ctx.permission_resolver
    # here to gate through, mirroring the retired embed_and_index's direct
    # SqliteIndexBackend(sandbox_write_paths=...) construction). The DB path
    # is derived via the SAME `cache_dir_for_source` helper `SqliteIndexBackend.
    # _db_path` uses for the actual write — so the gate checks exactly the path
    # the backend writes, guaranteed-equal by construction (not by two
    # hand-agreeing hardcoded formulas).
    #
    # F3 (RAG FP-0057 post-merge sweep): the op ALSO upserts the source
    # manifest (`sources.yaml`) on every call (see
    # `core/op_runtime/index_update.py`'s `SourceManifest.upsert`) — a write
    # this self-gate previously did not constrain, unlike the LLM-tool path
    # (resolver != None), which gates both the DB path AND the manifest path.
    # Gate the manifest path too, via the SAME `sources_manifest_path` SSoT
    # helper `SourceManifest.__init__` and the op's own permission-check path
    # use for the actual write, so the gated path is guaranteed-equal to the
    # write path by construction (not a third hand-agreed literal).
    if _sandbox_write_paths is not None:
        db_path = cache_dir_for_source(workspace_root, source) / "index.db"
        if not _within_paths(db_path, _sandbox_write_paths):
            raise PermissionError(
                f"index_update: source {source!r} index path is outside the "
                f"phase sandbox write_paths policy "
                f"(write_paths={_sandbox_write_paths!r})."
            )
        sources_yaml = sources_manifest_path(workspace_root)
        if not _within_paths(sources_yaml, _sandbox_write_paths):
            raise PermissionError(
                f"index_update: source {source!r} manifest path "
                f"({sources_yaml}) is outside the phase sandbox write_paths "
                f"policy (write_paths={_sandbox_write_paths!r})."
            )

    events = EventLog()
    ctx = OpContext(
        workspace=_SafeWorkspace(workspace_root),
        events=events,
        permission_decl=PermissionDecl(),
        # No dedicated permission gate on this entry point (matches the
        # retired embed_and_index's posture) — a safe-mode python step
        # runs under the calling phase's ordinary python-step permissions,
        # not a RAG-specific one; cost control is the Phase-1 cost_preflight
        # decision the LLM already makes before the step runs.
        permission_resolver=None,
        actor="safe_mode_index_update",
    )

    op = IndexUpdateIROp(
        kind="index_update",
        source=source,
        chunks=list(chunks),
        embedding_model=model,
        description=description,
        path=path,
    )
    return await execute_op(op, ctx)


def index_update(
    chunks: Iterable[dict],
    source: str,
    model: str = "standard",
    *,
    description: str | None = None,
    path: str | None = None,
) -> dict:
    """Synchronous entry point for safe-mode python steps.

    The chunker runs synchronously in the harness subprocess (no running
    event loop), so wrap the async core via :func:`asyncio.run` (mirrors
    the retired `embed_and_index`'s sync wrapper). Returns the same
    envelope as :func:`index_update_async`.
    """
    return asyncio.run(
        index_update_async(
            chunks, source, model, description=description, path=path,
        )
    )


__all__ = [
    "index_update",
    "index_update_async",
    "_set_context",
    "_reset_context",
]
