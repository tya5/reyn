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
     a pipeline registration is always exactly one file).
  2. Parse it via ``parse_pipeline_dsl`` into a ``Pipeline`` — this is the
     validation step (a malformed DSL file is refused, never registered).
  3. Resolve the registration name: the DSL's own declared ``pipeline:`` name
     is ALWAYS the identity a ``call``/``match`` step resolves against. When
     ``op.name`` is set it must match the DSL's declared name exactly — a
     mismatch is refused (fail-loud) rather than letting the config key
     silently diverge from the resolution key.
  4. Threat-scan the pipeline description via ``content_guard.scan_for_threats``
     (scope="strict") — block on blocking-severity match (same threat surface
     as a skill's SKILL.md description: free-text authored by whoever wrote the
     DSL, which for a source install is untrusted third-party content).
  5. Gate via ``PermissionResolver.require_file_write`` for the pipelines.yaml path.
  6. Read ``.reyn/config/pipelines.yaml`` (or empty dict), set
     ``pipelines.entries.<name>`` = ``{path, description, enabled}``, write back.
  7. ``record_config_generation`` on the pipelines.yaml path AFTER write —
     the truncation-surviving recovery base (#2259 / CLAUDE.md recovery gate).
  8. Emit ``pipeline_installed`` event (P6 audit trail).
  9. Request hot-reload so the installed pipeline goes live in the current session
     (the ``"pipelines"`` seam — ``Session._reapply_pipelines`` — rebuilds the
     registry from the fresh config cascade).

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


def _parse_pipeline_file(path: Path) -> "tuple[object | None, str]":
    """Read + parse a pipeline DSL file. Returns (Pipeline | None, error_message).

    On success, error_message is "". On failure (unreadable file or malformed
    DSL), the Pipeline is None and error_message describes the failure.
    """
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
    from reyn.core.pipeline.schema import SchemaRegistry

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read pipeline file {path}: {exc}"
    try:
        pipeline = parse_pipeline_dsl(text, SchemaRegistry())
    except PipelineParseError as exc:
        return None, f"malformed pipeline file {path}: {exc}"
    return pipeline, ""


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

    Local path (``op.source is None``): parses the DSL at ``op.path``, resolves
    + validates the registration name against the DSL's own declared
    ``pipeline:`` name, threat-scans the description, gates the config write,
    persists the entry, records a config generation for crash-recovery, emits
    an audit event, and requests a hot-reload.

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

    # ── 2. Parse the DSL (validation step) ────────────────────────────────────
    pipeline, parse_err = _parse_pipeline_file(dsl_path)
    if pipeline is None:
        if op.source:
            shutil.rmtree(clone_dest, ignore_errors=True)
        return {
            "kind": "pipeline_install",
            "status": "error",
            "path": op.path,
            "source": op.source or "",
            "error": parse_err,
        }

    declared_name = pipeline.name
    description = pipeline.description or ""

    # ── 3. Resolve + validate the registration name ───────────────────────────
    # The DSL's own declared `pipeline:` name is ALWAYS the resolution key a
    # call/match step resolves against — unlike skill, op.name cannot rename
    # the registered identity. A caller-supplied op.name that disagrees with
    # the DSL is a footgun (config key != resolution key) and is refused
    # rather than silently allowed to diverge.
    if op.name and op.name.strip() != declared_name:
        if op.source:
            shutil.rmtree(clone_dest, ignore_errors=True)
        return {
            "kind": "pipeline_install",
            "status": "error",
            "path": op.path,
            "source": op.source or "",
            "error": (
                f"name mismatch: op.name={op.name!r} does not match the DSL's "
                f"declared pipeline name {declared_name!r}. The declared 'pipeline:' "
                "name is the authoritative identity a call/match step resolves "
                "against, so the config key must match it exactly — pass no "
                "'name' override, or fix the DSL's 'pipeline:' key."
            ),
        }
    name = declared_name

    # SECURITY: sanitize the resolved name BEFORE it is used as a config key OR
    # (for source installs) a filesystem path component. The DSL's `pipeline:`
    # name is third-party content for a source install — a malicious
    # `pipeline: ../../../evil` would escape .reyn/pipelines/ at the rename step
    # (path-traversal → arbitrary rmtree).
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
                "component (letters, digits, '.', '_', '-'; no '/', '\\', '..', or "
                "leading '.'). Fix the DSL's 'pipeline:' key."
            ),
        }

    # For source installs: if the resolved name differs from the candidate we used
    # for the clone destination, rename the clone dir to the resolved name.
    if op.source and name != _candidate_name:
        new_dest = _pipelines_root / name
        # SECURITY: containment check BEFORE any rmtree/rename — refuse if new_dest
        # escapes .reyn/pipelines/ even after sanitization (guards a sanitizer gap).
        if not _contained_under(new_dest, _pipelines_root):
            shutil.rmtree(clone_dest, ignore_errors=True)
            return {
                "kind": "pipeline_install",
                "status": "error",
                "source": op.source or "",
                "error": (
                    f"refused: install destination for {name!r} escapes .reyn/pipelines/. "
                    "This is a path-containment violation."
                ),
            }
        if new_dest.exists():
            shutil.rmtree(new_dest)
        # Preserve the relative position of the DSL file inside the clone.
        rel_dsl = dsl_path.relative_to(clone_dest)
        clone_dest.rename(new_dest)
        clone_dest = new_dest
        dsl_path = new_dest / rel_dsl
        install_path = str(dsl_path.resolve())

    # ── 4. Threat-scan the description (scope="strict") ──────────────────────
    _ts = getattr(ctx, "threat_scan", None)
    if _ts is not None and getattr(_ts, "enabled", False) and description:
        _matches = scan_for_threats(description, _ts, scope="strict")
        if _matches:
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
                        f"install blocked: pipeline description matched threat "
                        f"pattern '{_block.pattern_id}' "
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
    existing["pipelines"]["entries"][safe_name] = entry
    _write_yaml(config_path, existing)

    # ── 7. Record config generation for crash-recovery (#2259 / CLAUDE.md gate) ─
    from reyn.core.events.config_recovery import record_config_generation  # noqa: PLC0415
    await record_config_generation(getattr(ctx, "state_log", None), config_path, existing)

    # ── 8. Emit pipeline_installed event (P6) ─────────────────────────────────
    ctx.events.emit(
        "pipeline_installed",
        name=safe_name,
        path=install_path,
        description=description,
        config_path=str(config_path),
        source=op.source or "",
    )

    # ── 9. Hot-reload: surface the installed pipeline in the current session ──
    from reyn.runtime.hot_reload import get_active_hot_reloader  # noqa: PLC0415
    _reloader = get_active_hot_reloader()
    if _reloader is not None:
        _reloader.request_reload(source="pipeline_install")

    return {
        "status": "installed",
        "name": safe_name,
        "path": install_path,
        "description": description,
        "config_path": str(config_path),
        "source": op.source or "",
    }


register("pipeline_install", handle)
