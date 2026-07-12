"""pipeline_install kind handler — register a pipeline (local or from git/URL) into the project config.

Mirrors ``skill_install.py`` as closely as possible (same shape: local-path
install vs git/URL source install), reusing its generic path-safety + sandboxed
git-clone helpers verbatim (``_safe_skill_name`` / ``_contained_under`` /
``_parse_source_spec`` / ``_source_host`` / ``_shallow_clone`` / ``_read_yaml``
/ ``_write_yaml`` / ``_resolve_project_root`` carry no skill-specific logic —
importing them here avoids re-implementing the sandbox-routed git clone +
path-traversal guards a second time).

Handler logic (one-shot, no sub-phases):

Local-path install (``op.source is None``):
  1. Resolve the pipeline DSL file: ``op.path`` must point directly at a
     ``*.yaml`` file (unlike skill, there is no directory-or-file resolution —
     a pipeline registration is always exactly one file, though that file may
     hold MULTIPLE ``pipeline:`` documents — #2722).
  2. Parse it via ``parse_pipeline_docs`` into one-or-more ``Pipeline``s — this
     is the validation step (a malformed DSL file is refused, never registered).
  3. Resolve the registration namespace KEY (#2722): the config entry key is a
     pure namespace label (``op.name``, or the DSL file stem when omitted) — it
     no longer must equal any declared ``pipeline:`` name (the old key==name
     coupling is gone). ``.`` is reserved (R1) and rejected in the key. Every
     ``pipeline:`` document registers as ``{key}.{declared-name}``; the FULL set
     is enumerated in the result + audit event (H6 — no silent scope creep).
  4. Threat-scan EVERY pipeline's description via ``content_guard.scan_for_threats``
     (scope="strict") — block on any blocking-severity match (same threat surface
     as a skill's SKILL.md description: free-text authored by whoever wrote the
     DSL, which for a source install is untrusted third-party content).
  5. Gate via ``PermissionResolver.require_file_write`` for the pipelines.yaml path.
  6. Read ``.reyn/config/pipelines.yaml`` (or empty dict), set
     ``pipelines.entries.<name>`` = ``{path, description, enabled}``, write back.
  7. ``record_config_generation`` on the pipelines.yaml path AFTER write —
     the truncation-surviving recovery base (#2259 / CLAUDE.md recovery gate).
  8. Emit ``pipeline_installed`` event (P6 audit trail).
  9. Reload so the installed pipeline goes live (#2761 PR-2): a PURE ADDITION on a
     live per-session reloader (``ctx.hot_reloader``) applies the ``"pipelines"`` seam
     (``Session._reapply_pipelines`` — rebuilds the registry from the fresh config
     cascade) IMMEDIATELY (mid-turn) — resolvable this same turn; a same-name overwrite
     (clobber-update) or no per-session reloader keeps the deferred turn-boundary path.

Source/git install (``op.source`` set):
  Same pipeline as local, but step 0 fetches the DSL file first:
  0a. Gate ``require_http_get`` for the source host (mirrors mcp_install.py / skill_install.py).
  0b. Shallow-clone the git repo (or subdir via ``//`` separator) to
      ``.reyn/pipelines/<name>/``. Subdir convention: a ``"//subdir"`` suffix
      in the source URL selects ``subdir`` inside the cloned repo; if absent,
      the repo root is used.
  0c. Locate the DSL file inside the clone: ``op.path`` (relative to the
      clone root/subdir) selects it; when omitted, the clone root/subdir must
      contain exactly one ``*.yaml`` file (an ambiguous multi-file clone
      without an explicit ``path`` is refused, never guessed).
  Steps 1–9 then proceed against the cloned DSL file path; the registered
  ``path`` points at the installed copy under ``.reyn/pipelines/<name>/``.

This is a P5 exception mirror of ``mcp_install``/``skill_install``:
``.reyn/config/pipelines.yaml`` lives outside the workspace data channel but is
written directly here (same rationale — gated behind ``require_file_write`` +
recorded via event for the P6 audit trail).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from reyn.schemas.models import PipelineInstallIROp

# Module-level import so tests can monkeypatch the threat-scan callables;
# the guard helpers are pure-function with no I/O and add negligible import cost.
from reyn.security.content_guard import first_blocking_match, scan_for_threats

from . import register
from .context import OpContext
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx

# Reuse skill_install's generic (skill-agnostic) helpers verbatim rather than
# re-implementing the sandboxed git-clone + path-traversal guards a second
# time — these carry no skill-specific behavior.
from .skill_install import (
    _contained_under,
    _parse_source_spec,
    _read_yaml,
    _resolve_project_root,
    _shallow_clone,
    _source_host,
    _write_yaml,
)
from .skill_install import (
    _safe_skill_name as _safe_name_component,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _pipelines_config_path(project_root: Path) -> Path:
    """Canonical path for the dynamic pipelines registry config."""
    return project_root / ".reyn" / "config" / "pipelines.yaml"


def _parse_pipeline_file(path: Path) -> "tuple[list | None, str]":
    """Read + parse a pipeline DSL file. Returns (list[Pipeline] | None, error).

    A file may hold MORE than one ``pipeline:`` document (#2722) — every one is
    parsed and returned. On success, error_message is "". On failure (unreadable
    file or malformed DSL), the list is None and error_message describes the
    failure. R1 (a reserved ``.`` in a declared name) and R2 (an intra-file
    duplicate declared name) surface here as a malformed-file error."""
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_docs
    from reyn.core.pipeline.schema import SchemaRegistry

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read pipeline file {path}: {exc}"
    try:
        pipelines = parse_pipeline_docs(text, SchemaRegistry())
    except PipelineParseError as exc:
        return None, f"malformed pipeline file {path}: {exc}"
    return pipelines, ""


def _find_sole_yaml(directory: Path) -> "Path | None":
    """Return the single ``*.yaml`` file directly under ``directory``, or None
    when there is not exactly one (absent or ambiguous — caller refuses rather
    than guessing which file is the pipeline to install)."""
    candidates = sorted(directory.glob("*.yaml")) if directory.is_dir() else []
    return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle(
    op: PipelineInstallIROp,
    ctx: OpContext,
) -> dict:
    """Execute a pipeline_install op — register a local or source-fetched pipeline.

    Local path (``op.source is None``): parses the DSL at ``op.path`` (one or
    more ``pipeline:`` documents — #2722), resolves the namespace key (``op.name``
    or the file stem; ``.`` reserved), threat-scans every description, gates the
    config write, persists the entry, records a config generation for
    crash-recovery, emits an audit event (enumerating all ``{key}.{name}`` global
    names registered — H6), and requests a hot-reload.

    Source/git path (``op.source`` set): additionally gates the source host
    via ``require_http_get`` and shallow-clones the repo before the same
    pipeline; the registered path points at the installed clone.
    """
    project_root = _resolve_project_root(ctx.workspace)

    # ── 0. Source-fetch path (git/GitHub URL) ─────────────────────────────────
    if op.source:
        git_url, subdir = _parse_source_spec(op.source)
        host = _source_host(git_url)

        # 0a. Permission gate: require_http_get for the source host.
        # Skipped for local file:// refs (host is None) — no HTTP gate needed.
        if ctx.permission_resolver is not None and host is not None:
            _sandbox = _sandbox_policy_from_ctx(ctx)
            await ctx.permission_resolver.require_http_get(
                ctx.permission_decl,
                host,
                ctx.intervention_bus,
                ctx.actor,
                sandbox_policy=_sandbox,
            )

        # 0b. Determine install destination — a stable name under .reyn/pipelines/.
        # Use op.name override if set; otherwise derive a candidate from the URL
        # (last path segment without .git); will be validated against the DSL's
        # own declared name once parsed.
        _url_basename = git_url.rstrip("/").split("/")[-1]
        if _url_basename.endswith(".git"):
            _url_basename = _url_basename[:-4]
        _raw_candidate = (op.name or "").strip() or (subdir.split("/")[-1] if subdir else _url_basename)

        # SECURITY: sanitize the candidate name BEFORE any path construction.
        # op.name is caller-controlled and the URL basename is attacker-influenced;
        # an unsafe name would let clone_dest escape .reyn/pipelines/ (path-traversal
        # → arbitrary rmtree). Reject rather than silently rewrite.
        _candidate_name = _safe_name_component(_raw_candidate)
        if _candidate_name is None:
            return {
                "kind": "pipeline_install",
                "status": "error",
                "source": op.source,
                "error": (
                    f"invalid pipeline name derived from source: {_raw_candidate!r}. "
                    "The install destination name must be a single path component "
                    "(letters, digits, '.', '_', '-'; no '/', '\\', '..', or leading '.'). "
                    "Set a safe 'name' or use a repo/subdir with a valid basename."
                ),
            }

        _pipelines_root = project_root / ".reyn" / "pipelines"
        clone_dest = _pipelines_root / _candidate_name

        # SECURITY: belt-and-suspenders containment — refuse if clone_dest escapes
        # .reyn/pipelines/ even after sanitization (guards a sanitizer gap). No
        # filesystem mutation happens before this check passes.
        if not _contained_under(clone_dest, _pipelines_root):
            return {
                "kind": "pipeline_install",
                "status": "error",
                "source": op.source,
                "error": (
                    f"refused: install destination for {_candidate_name!r} escapes "
                    ".reyn/pipelines/. This is a path-containment violation."
                ),
            }

        # 0c. Shallow-clone the repo.
        clone_err = await _shallow_clone(git_url, clone_dest, ctx)
        if clone_err:
            return {
                "kind": "pipeline_install",
                "status": "error",
                "source": op.source,
                "error": clone_err,
            }

        # 0d. Locate the DSL file inside the clone (root or subdir).
        pipeline_root = clone_dest / subdir if subdir else clone_dest
        if op.path:
            dsl_path = pipeline_root / op.path
        else:
            dsl_path = _find_sole_yaml(pipeline_root)

        if dsl_path is None or not dsl_path.exists():
            shutil.rmtree(clone_dest, ignore_errors=True)
            return {
                "kind": "pipeline_install",
                "status": "error",
                "source": op.source,
                "error": (
                    f"pipeline DSL file not found in cloned repo at '{pipeline_root}'. "
                    "Pass 'path' (relative to the repo/subdir) to select the file, "
                    "or ensure the repo root (or subdir) contains exactly one *.yaml file."
                ),
            }

        # Steps 1–9 now proceed using the cloned DSL file path.
        install_path = str(dsl_path.resolve())

    else:
        # ── 1. Resolve the DSL file (local path) ─────────────────────────────
        if not op.path:
            return {
                "kind": "pipeline_install",
                "status": "error",
                "path": op.path,
                "error": "path is required for a local install (no source set)",
            }
        dsl_path = Path(op.path)
        if not dsl_path.exists() or not dsl_path.is_file():
            return {
                "kind": "pipeline_install",
                "status": "error",
                "path": op.path,
                "error": (
                    f"pipeline DSL file not found at '{dsl_path}'. "
                    "Provide the direct path to the pipeline's *.yaml DSL file."
                ),
            }
        install_path = str(dsl_path.resolve())

    # ── 2. Parse the DSL (validation step) — a file may hold N pipelines (#2722)
    pipelines, parse_err = _parse_pipeline_file(dsl_path)
    if pipelines is None:
        if op.source:
            shutil.rmtree(clone_dest, ignore_errors=True)
        return {
            "kind": "pipeline_install",
            "status": "error",
            "path": op.path,
            "source": op.source or "",
            "error": parse_err,
        }

    # The config entry's description is metadata surfaced alongside the namespace
    # in the catalog — use the first document's (the file's own summary).
    description = pipelines[0].description or ""

    # ── 3. Resolve the registration namespace KEY (#2722) ─────────────────────
    # The config entry key IS the namespace label — every pipeline in the file
    # registers as `{key}.{declared-name}`. The old `op.name == declared-name`
    # coupling is GONE: the key no longer must equal any declared pipeline name.
    # For a source install the key was derived pre-clone (`_candidate_name`); for
    # a local install it is `op.name` or the DSL file stem.
    if op.source:
        name = _candidate_name
    else:
        name = (op.name or "").strip() or dsl_path.stem

    # #2722 R1: '.' is RESERVED as the namespace separator — reject it in the key
    # (stricter than _safe_name_component, which permits an interior '.'). A key
    # with a '.' would make the derived global name '<key>.<doc>' ambiguous.
    if "." in name:
        if op.source:
            shutil.rmtree(clone_dest, ignore_errors=True)
        return {
            "kind": "pipeline_install",
            "status": "error",
            "path": op.path,
            "source": op.source or "",
            "error": (
                f"invalid pipeline namespace key {name!r}: '.' is reserved as the "
                "namespace separator (registered names are '<key>.<pipeline-name>'). "
                "Choose a 'name' with no '.'."
            ),
        }

    # SECURITY: sanitize the key BEFORE it is used as a config key OR (for source
    # installs) a filesystem path component. For a source install the key derives
    # from an attacker-influenced URL basename — a malicious name would escape
    # .reyn/pipelines/ (path-traversal → arbitrary rmtree).
    safe_name = _safe_name_component(name)
    if safe_name is None:
        if op.source:
            shutil.rmtree(clone_dest, ignore_errors=True)
        return {
            "kind": "pipeline_install",
            "status": "error",
            "path": op.path,
            "source": op.source or "",
            "error": (
                f"invalid pipeline name {name!r}. The name must be a single path "
                "component (letters, digits, '_', '-'; no '.', '/', '\\', '..', or "
                "leading '.'). Pass a safe 'name'."
            ),
        }

    # #2722 H6: the FULL set of global names this install registers — every
    # `pipeline:` document in the file, namespaced under the key. Enumerated in
    # the approval-visible result + the audit event so approving one op.name
    # never silently registers extra pipelines behind the operator's back.
    registered_names = [f"{safe_name}.{p.name}" for p in pipelines]

    # ── 4. Threat-scan EVERY pipeline's description (scope="strict") ──────────
    # A multi-doc file has N author-written descriptions; each is untrusted
    # free-text (esp. for a source install) — scan them all, block on any match.
    _ts = getattr(ctx, "threat_scan", None)
    if _ts is not None and getattr(_ts, "enabled", False):
        for _p in pipelines:
            _desc = _p.description or ""
            if not _desc:
                continue
            _matches = scan_for_threats(_desc, _ts, scope="strict")
            if not _matches:
                continue
            for _m in _matches:
                ctx.events.emit(
                    "pipeline_install_threat_match",
                    pattern_id=_m.pattern_id,
                    severity=_m.severity,
                    scope=_m.scope,
                )
            _block = first_blocking_match(
                _matches, getattr(_ts, "block_severity", "block")
            )
            if _block is not None:
                ctx.events.emit(
                    "pipeline_install_threat_blocked",
                    pattern_id=_block.pattern_id,
                    severity=_block.severity,
                    name=safe_name,
                )
                # Remove the clone on block — don't leave untrusted content on disk.
                if op.source:
                    shutil.rmtree(clone_dest, ignore_errors=True)
                return {
                    "kind": "pipeline_install",
                    "status": "blocked",
                    "name": safe_name,
                    "source": op.source or "",
                    "path": install_path,
                    "error": (
                        f"install blocked: pipeline {_p.name!r} description matched "
                        f"threat pattern '{_block.pattern_id}' "
                        f"({_block.scope}/{_block.severity}). The description "
                        f"contains a prohibited pattern. Do not install this pipeline."
                    ),
                }

    # ── 5. Permission gate: pipelines.yaml write (+.reyn/pipelines/ for source) ─
    config_path = _pipelines_config_path(project_root)
    if ctx.permission_resolver is not None:
        _sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.actor,
            sandbox_policy=_sandbox,
        )

    # ── 6. Write pipelines.entries.<name> to .reyn/config/pipelines.yaml ──────
    existing = _read_yaml(config_path)
    if "pipelines" not in existing or not isinstance(existing.get("pipelines"), dict):
        existing["pipelines"] = {}
    if "entries" not in existing["pipelines"] or not isinstance(existing["pipelines"].get("entries"), dict):
        existing["pipelines"]["entries"] = {}
    entry: dict = {
        "path": install_path,
        "description": description,
        "enabled": True,
    }
    if op.source:
        entry["source"] = op.source
    # proposal 0060 Phase 1 Layer A (A9): provenance is stamped from the single
    # OS-authoritative source (ctx.turn_origin, set by Session._stamp_execution_context
    # — A7) — never from an op field, so an auto-improvement turn cannot self-declare
    # "user_directed" to bypass the Phase-4 gate. The `builtin` value is stamped on a
    # DIFFERENT seam (the future builtin-tier registry-build loader, not this install
    # path) — never written here.
    entry["provenance"] = ctx.turn_origin
    # #2761 PR-2: capture pure-addition-vs-overwrite BEFORE the write mutates entries,
    # so step 9 routes a NEW name to the immediate mid-turn apply and a same-name
    # overwrite (clobber-update — pipeline's only update path) to the deferred path.
    from reyn.runtime.hot_reload import is_pure_addition  # noqa: PLC0415
    _is_addition = is_pure_addition(safe_name, existing["pipelines"]["entries"])
    existing["pipelines"]["entries"][safe_name] = entry
    _write_yaml(config_path, existing)

    # ── 7. Record config generation for crash-recovery (#2259 / CLAUDE.md gate) ─
    from reyn.core.events.config_recovery import record_config_generation  # noqa: PLC0415
    await record_config_generation(getattr(ctx, "state_log", None), config_path, existing)

    # ── 8. Emit pipeline_installed event (P6) ─────────────────────────────────
    ctx.events.emit(
        "pipeline_installed",
        name=safe_name,
        registered_names=registered_names,
        path=install_path,
        description=description,
        config_path=str(config_path),
        source=op.source or "",
    )

    # ── 9. Hot-reload: surface the installed pipeline in the current session ──
    # #2761 PR-2: a PURE ADDITION on a live per-session reloader (ctx.hot_reloader)
    # applies IMMEDIATELY (mid-turn) so the just-installed NEW pipeline is resolvable
    # this turn (a call/match step can target it same execution); a same-name overwrite
    # (clobber-update) or no per-session reloader (CLI separate process) keeps the
    # existing deferred turn-boundary behavior — which also confines the R7 pending-call
    # target hazard to the deferred path it already lives on.
    from reyn.runtime.hot_reload import dispatch_install_reload  # noqa: PLC0415
    await dispatch_install_reload(
        getattr(ctx, "hot_reloader", None),
        source="pipeline_install",
        is_addition=_is_addition,
    )

    return {
        "status": "installed",
        "name": safe_name,
        "registered_names": registered_names,
        "path": install_path,
        "description": description,
        "config_path": str(config_path),
        "source": op.source or "",
    }


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("pipeline_install", handle, canonical=STRUCTURED_PASSTHROUGH)
