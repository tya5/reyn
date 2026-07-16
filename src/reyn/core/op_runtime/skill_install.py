"""skill_install kind handler — register a skill (local or from git/URL) into the project config.

Handler logic (one-shot, no sub-phases):

Local-path install (``op.source is None``):
  1. Resolve SKILL.md: ``op.path`` may be a directory (→ ``<dir>/SKILL.md``)
     or a direct path to the SKILL.md file. Read it and extract frontmatter.
  2. Extract name (frontmatter ``name:`` key, else directory basename) and
     description (frontmatter ``description:`` key, else empty). Apply
     ``op.name`` override when set.
  3. Threat-scan the skill description via ``content_guard.scan_for_threats``
     (scope="strict") — block on a blocking-severity match.
  4. Gate via ``PermissionResolver.require_file_write`` for the skills.yaml path.
  5. Read ``.reyn/config/skills.yaml`` (or empty dict), set
     ``skills.entries.<name>`` = ``{path, description, enabled, visibility}``,
     write back.
  6. ``record_config_generation`` on the skills.yaml path AFTER write —
     the truncation-surviving recovery base (#2259 / CLAUDE.md recovery gate).
  7. Emit ``skill_installed`` event (P6 audit trail).
  8. Reload so the installed skill goes live (#2761 PR-2): a PURE ADDITION on a
     live per-session reloader (``ctx.hot_reloader``) applies IMMEDIATELY (mid-turn)
     — the new skill is resolvable this same turn; a same-name overwrite
     (clobber-update) or no per-session reloader keeps the deferred turn-boundary path.

Source/git install (``op.source`` set — #2548 PR-D):
  Same pipeline as local, but step 0 fetches the skill first:
  0a. Gate ``require_http_get`` for the source host (mirrors mcp_install.py).
  0b. Shallow-clone the git repo (or subdir via ``//`` separator) to
      ``.reyn/skills/<name>/``. Subdir convention: a ``"//subdir"`` suffix
      in the source URL selects ``subdir`` inside the cloned repo; if absent,
      the repo root is used.
  0c. Locate the SKILL.md in the cloned dir (same dir-or-file resolution).
  Steps 1–8 then proceed against the cloned SKILL.md path; the registered
  ``path`` points at the installed copy under ``.reyn/skills/<name>/``.

This is a P5 exception mirror of ``mcp_install``: ``.reyn/config/skills.yaml``
lives outside the workspace data channel but is written directly here (same
rationale — gated behind ``require_file_write`` + recorded via event for the
P6 audit trail).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlparse

from reyn.data.skills.registry import VISIBILITY_MENU
from reyn.schemas.models import SkillInstallIROp

# Module-level import so tests can monkeypatch the threat-scan callables;
# the guard helpers are pure-function with no I/O and add negligible import cost.
from reyn.security.content_guard import first_blocking_match, scan_for_threats

from . import register
from .context import OpContext
from .context import provenance_from_ctx as _provenance_from_ctx
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_skill_md(path_str: str) -> Path:
    """Resolve the SKILL.md path from an op.path value.

    If ``path_str`` points at a directory, returns ``<dir>/SKILL.md``.
    Otherwise returns the path as-is (direct file reference).
    """
    p = Path(path_str)
    if p.is_dir():
        return p / "SKILL.md"
    return p


def _read_skill_metadata(skill_md: Path) -> tuple[str, str]:
    """Read a SKILL.md file and return (name, description) from frontmatter.

    Returns empty strings for any field that is absent or unreadable —
    the caller applies the dir-basename fallback for name.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    from reyn.core.frontmatter import split_frontmatter
    fm, _body = split_frontmatter(text)
    name = str(fm.get("name") or "").strip()
    description = str(fm.get("description") or "").strip()
    return name, description


def _skills_config_path(project_root: Path) -> Path:
    """Canonical path for the dynamic skills registry config."""
    return project_root / ".reyn" / "config" / "skills.yaml"


def _resolve_project_root(workspace: object) -> Path:
    """Resolve the project root from a workspace object (mirrors mcp_install)."""
    root = getattr(workspace, "base_dir", None) or getattr(workspace, "root", None)
    return Path(root) if root is not None else Path.cwd()


def _read_yaml(path: Path) -> dict:
    """Read a YAML config file; return {} if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_yaml(path: Path, data: dict) -> None:
    """Write a dict as YAML to path, creating parent dirs as needed."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _safe_skill_name(raw: str) -> str | None:
    """Reduce a raw skill name to a safe SINGLE path component, or None if unsafe.

    Third-party content (SKILL.md frontmatter ``name:``) and caller-supplied
    ``op.name`` both flow into the ``.reyn/skills/<name>/`` clone destination,
    which is then ``shutil.rmtree``-d and cloned into. An unsanitized ``name``
    like ``../../../../tmp/victim`` escapes ``.reyn/skills/`` and deletes an
    arbitrary directory (path-traversal → arbitrary rmtree). This helper is the
    FIRST line of defence: it rejects any name that is not a plain slug.

    Rules (all must hold; else return None):
      - non-empty after ``strip()``
      - no path separators (``/`` or ``\\``)
      - no ``..`` anywhere (traversal), no leading ``.`` (hidden / ``.``/``..``)
      - charset restricted to ``[A-Za-z0-9._-]`` (no NUL, no whitespace, no
        shell metacharacters, no unicode homoglyphs)
      - not a reserved single/double dot component

    Returns the name UNCHANGED when it is already safe (deterministic; no
    silent slugification that could collide two distinct skills). Callers that
    get None must refuse the install with a clear error — never construct a
    filesystem path from an unsafe name.
    """
    import re

    name = (raw or "").strip()
    if not name:
        return None
    # Reject path separators and traversal outright (defence-in-depth: the
    # charset check below also catches "/" and "\\", but ".." is within charset).
    if "/" in name or "\\" in name or ".." in name:
        return None
    # Reject leading dot ("." / hidden files) — keeps the component non-special.
    if name.startswith("."):
        return None
    # Restrict to a conservative slug charset.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    return name


def _contained_under(candidate: Path, root: Path) -> bool:
    """Return True iff ``candidate`` resolves to a path inside ``root``.

    Belt-and-suspenders containment check performed BEFORE any rmtree / clone /
    rename, so even a gap in ``_safe_skill_name`` cannot let a filesystem
    mutation escape ``.reyn/skills/``. Both paths are ``resolve()``-d (symlinks
    + ``..`` collapsed) before the relativity test.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _parse_source_spec(source: str) -> tuple[str, str]:
    """Split a source specifier into (git_url, subdir).

    Convention: ``"https://github.com/user/repo"`` → ``(url, "")``.
    ``"https://github.com/user/repo//skills/my-skill"`` → ``(url_without_subdir, "skills/my-skill")``.

    The ``//`` separator was chosen to mirror the Terraform module subdir
    convention — it is not a valid part of a URL path and therefore unambiguous.
    """
    if "//" in source:
        # Split on the FIRST "//" that appears after the scheme+host portion.
        # For "https://github.com/user/repo//subdir", rfind gives us the last "//",
        # which is exactly what we want for the Terraform-style subdir convention.
        double_slash_idx = source.find("//", source.find("//") + 2)
        if double_slash_idx != -1:
            git_url = source[:double_slash_idx]
            subdir = source[double_slash_idx + 2:].strip("/")
            return git_url, subdir
    return source, ""


def _source_host(git_url: str) -> str | None:
    """Extract the hostname from a git URL for permission gating.

    Handles https:// and ssh:// URLs and git@host:path SSH URLs.
    Returns ``None`` for local schemes (``file://``) where no HTTP gate is needed.
    Returns the raw URL string as a fallback when parsing fails (safe for gate checks).
    """
    if git_url.startswith("git@"):
        # git@github.com:user/repo.git → github.com
        rest = git_url[4:]
        return rest.split(":")[0]
    try:
        parsed = urlparse(git_url)
        # Local file:// refs do not require an HTTP permission gate.
        if parsed.scheme == "file":
            return None
        return parsed.hostname or git_url
    except Exception:
        return git_url


async def _shallow_clone(git_url: str, dest: Path, ctx: OpContext) -> str | None:
    """Shallow-clone a git repo to ``dest`` — routed through the sandbox
    abstraction (#2620: agent-reachable subprocess launches must never bypass
    ``reyn.security.sandbox``; a NoopBackend passthrough on an unsupported
    platform is acceptable, a raw ``subprocess.run`` that never consults any
    backend is not).

    Returns ``None`` on success, or an error message string on failure.
    The destination directory is REMOVED before cloning so this function
    is idempotent (re-install overwrites the previous clone).

    Uses ``ctx.sandbox_backend`` (an injected stateful backend) or
    ``get_default_backend(ctx.sandbox_config)`` for backend SELECTION (so the
    operator's configured enforcement mechanism governs it, same as
    ``sandboxed_exec``) but a purpose-built ``SandboxPolicy`` for this specific
    operation: ``network=True`` is not overridable here because cloning over
    the network is what the operation IS — an operator policy that forces
    ``network: false`` globally would make every git-sourced skill install
    fail outright, so the clone's policy is scoped independently, write-tight
    to the install root (``dest.parent`` = ``.reyn/skills/``).
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    from reyn.security.sandbox import SandboxPolicy, get_default_backend

    backend = ctx.sandbox_backend or get_default_backend(ctx.sandbox_config)
    policy = SandboxPolicy(
        network=True,
        write_paths=[str(dest.parent)],
        timeout_seconds=120,
        # git internally forks a subprocess for the transport helper
        # (git-upload-pack over ssh/https) even for a plain clone — without
        # this a fork-denying backend (Seatbelt's default policy denies
        # process-fork) makes EVERY clone fail with "cannot fork()", not just
        # a hardened corner case. Scoped to this one already-gated operation
        # (require_http_get already ran), not a global relaxation.
        allow_subprocess=True,
        # git needs more than PATH to actually work (HOME for ~/.gitconfig,
        # SSH/proxy/CA vars for non-file:// remotes) — pass these through so
        # routing the clone through the sandbox abstraction does not itself
        # break git's normal env-dependent behavior (parity with the
        # previously-inherited-full-env ``subprocess.run`` call).
        env_passthrough=[
            "HOME", "PATH", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
            "https_proxy", "http_proxy", "no_proxy",
            "SSL_CERT_FILE", "SSL_CERT_DIR",
        ],
    )
    try:
        result = await backend.run(
            ["git", "clone", "--depth", "1", "--", git_url, str(dest)],
            policy,
            cwd=str(dest.parent),
        )
    except Exception as exc:  # noqa: BLE001 — surface as a clone error, not a crash
        return f"git clone error: {exc}"

    if result.returncode != 0:
        detail = (
            result.stderr.decode("utf-8", errors="replace").strip()
            or result.stdout.decode("utf-8", errors="replace").strip()
        )
        if result.returncode == -1:
            return (
                f"git clone did not complete: {detail or 'timed out or failed to start'}. "
                "Ensure git is installed and the repository is reachable."
            )
        return f"git clone failed (exit {result.returncode}): {detail}"
    return None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle(
    op: SkillInstallIROp,
    ctx: OpContext,
) -> dict:
    """Execute a skill_install op — register a local or source-fetched skill.

    Local path (``op.source is None``): resolves SKILL.md from ``op.path``,
    scans for threats, gates the config write, persists the entry, records a
    config generation for crash-recovery, emits an audit event, and requests
    a hot-reload.

    Source/git path (``op.source`` set): additionally gates the source host
    via ``require_http_get`` and shallow-clones the repo before the same
    pipeline; the registered path points at the installed clone.
    """
    project_root = _resolve_project_root(ctx.workspace)

    # ── 0. Source-fetch path (PR-D: git/GitHub URL) ───────────────────────────
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

        # 0b. Determine install destination — a stable name under .reyn/skills/.
        # Use op.name override if set; otherwise derive a candidate from the URL
        # (last path segment without .git); will be overridden after SKILL.md read.
        _url_basename = git_url.rstrip("/").split("/")[-1]
        if _url_basename.endswith(".git"):
            _url_basename = _url_basename[:-4]
        _raw_candidate = (op.name or "").strip() or (subdir.split("/")[-1] if subdir else _url_basename)

        # SECURITY: sanitize the candidate name BEFORE any path construction.
        # op.name is caller-controlled and the URL basename is attacker-influenced;
        # an unsafe name would let clone_dest escape .reyn/skills/ (path-traversal
        # → arbitrary rmtree). Reject rather than silently rewrite.
        _candidate_name = _safe_skill_name(_raw_candidate)
        if _candidate_name is None:
            return {
                "kind": "skill_install",
                "status": "error",
                "source": op.source,
                "error": (
                    f"invalid skill name derived from source: {_raw_candidate!r}. "
                    "The install destination name must be a single path component "
                    "(letters, digits, '.', '_', '-'; no '/', '\\', '..', or leading '.'). "
                    "Set a safe 'name' or use a repo/subdir with a valid basename."
                ),
            }

        _skills_root = project_root / ".reyn" / "skills"
        clone_dest = _skills_root / _candidate_name

        # SECURITY: belt-and-suspenders containment — refuse if clone_dest escapes
        # .reyn/skills/ even after sanitization (guards a sanitizer gap). No
        # filesystem mutation happens before this check passes.
        if not _contained_under(clone_dest, _skills_root):
            return {
                "kind": "skill_install",
                "status": "error",
                "source": op.source,
                "error": (
                    f"refused: install destination for {_candidate_name!r} escapes "
                    ".reyn/skills/. This is a path-containment violation."
                ),
            }

        # 0c. Shallow-clone the repo.
        clone_err = await _shallow_clone(git_url, clone_dest, ctx)
        if clone_err:
            return {
                "kind": "skill_install",
                "status": "error",
                "source": op.source,
                "error": clone_err,
            }

        # 0d. Locate the SKILL.md inside the clone (root or subdir).
        skill_root = clone_dest / subdir if subdir else clone_dest
        skill_md = _resolve_skill_md(str(skill_root))

        if not skill_md.exists():
            shutil.rmtree(clone_dest, ignore_errors=True)
            return {
                "kind": "skill_install",
                "status": "error",
                "source": op.source,
                "error": (
                    f"SKILL.md not found in cloned repo at '{skill_md}'. "
                    "The repo root (or specified subdir) must contain a SKILL.md file."
                ),
            }

        # Steps 1–8 now proceed using the cloned SKILL.md path.
        install_path = str(skill_md.parent.resolve())

    else:
        # ── 1. Resolve SKILL.md (local path) ─────────────────────────────────
        skill_md = _resolve_skill_md(op.path)
        if not skill_md.exists():
            return {
                "kind": "skill_install",
                "status": "error",
                "path": op.path,
                "error": (
                    f"SKILL.md not found at '{skill_md}'. "
                    "Provide the directory containing SKILL.md or the direct path."
                ),
            }
        install_path = str(Path(op.path).resolve())

    # ── 2. Extract name + description from frontmatter ────────────────────────
    fm_name, description = _read_skill_metadata(skill_md)

    # Name resolution precedence: op.name override > frontmatter name > dir basename.
    if op.name:
        raw_name = op.name.strip()
    elif fm_name:
        raw_name = fm_name
    else:
        # Default: directory name (or stem of a direct SKILL.md reference)
        raw_name = skill_md.parent.name if skill_md.name == "SKILL.md" else skill_md.stem

    # SECURITY: sanitize the resolved name BEFORE it is used as a config key OR
    # (for source installs) a filesystem path component. The frontmatter ``name:``
    # is third-party content — a malicious ``name: ../../../evil`` would escape
    # .reyn/skills/ at the rename step (path-traversal → arbitrary rmtree).
    name = _safe_skill_name(raw_name)
    if name is None:
        if op.source:
            shutil.rmtree(clone_dest, ignore_errors=True)
        return {
            "kind": "skill_install",
            "status": "error",
            "path": op.path,
            "source": op.source or "",
            "error": (
                f"invalid skill name {raw_name!r}. The name must be a single path "
                "component (letters, digits, '.', '_', '-'; no '/', '\\', '..', or "
                "leading '.'). Fix the SKILL.md 'name:' frontmatter or pass a safe 'name'."
            ),
        }

    # For source installs: if the resolved name differs from the candidate we used
    # for the clone destination, rename the clone dir to the resolved name.
    if op.source and name != _candidate_name:
        new_dest = _skills_root / name
        # SECURITY: containment check BEFORE any rmtree/rename — refuse if new_dest
        # escapes .reyn/skills/ even after sanitization (guards a sanitizer gap).
        if not _contained_under(new_dest, _skills_root):
            shutil.rmtree(clone_dest, ignore_errors=True)
            return {
                "kind": "skill_install",
                "status": "error",
                "source": op.source or "",
                "error": (
                    f"refused: install destination for {name!r} escapes .reyn/skills/. "
                    "This is a path-containment violation."
                ),
            }
        if new_dest.exists():
            shutil.rmtree(new_dest)
        clone_dest.rename(new_dest)
        clone_dest = new_dest
        install_path = str((new_dest / subdir if subdir else new_dest).resolve())

    # ── 3. Threat-scan the description (scope="strict") ──────────────────────
    _ts = getattr(ctx, "threat_scan", None)
    if _ts is not None and getattr(_ts, "enabled", False) and description:
        _matches = scan_for_threats(description, _ts, scope="strict")
        if _matches:
            for _m in _matches:
                ctx.events.emit(
                    "skill_install_threat_match",
                    pattern_id=_m.pattern_id,
                    severity=_m.severity,
                    scope=_m.scope,
                )
            _block = first_blocking_match(
                _matches, getattr(_ts, "block_severity", "block")
            )
            if _block is not None:
                ctx.events.emit(
                    "skill_install_threat_blocked",
                    pattern_id=_block.pattern_id,
                    severity=_block.severity,
                    name=name,
                )
                # Remove the clone on block — don't leave untrusted content on disk.
                if op.source:
                    shutil.rmtree(clone_dest, ignore_errors=True)
                return {
                    "kind": "skill_install",
                    "status": "blocked",
                    "name": name,
                    "source": op.source or "",
                    "path": install_path,
                    "error": (
                        f"install blocked: SKILL.md description matched threat "
                        f"pattern '{_block.pattern_id}' "
                        f"({_block.scope}/{_block.severity}). The description "
                        f"contains a prohibited pattern. Do not install this skill."
                    ),
                }

    # ── 4. Permission gate: skills.yaml write (+.reyn/skills/ for source) ────
    config_path = _skills_config_path(project_root)
    if ctx.permission_resolver is not None:
        _sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.actor,
            sandbox_policy=_sandbox,
        )

    # ── 5. Write skills.entries.<name> to .reyn/config/skills.yaml ───────────
    existing = _read_yaml(config_path)
    if "skills" not in existing or not isinstance(existing.get("skills"), dict):
        existing["skills"] = {}
    if "entries" not in existing["skills"] or not isinstance(existing["skills"].get("entries"), dict):
        existing["skills"]["entries"] = {}
    # #2971: `visibility: menu` replaces the removed `auto_invoke: true`. An
    # explicitly-installed skill lands on the system-prompt menu — the operator
    # named it deliberately, so advertising it is the behavior they asked for
    # (identical to what `auto_invoke: true` produced). They can narrow it to
    # `on_demand` / `hidden` afterwards by editing the written entry.
    entry: dict = {
        "path": install_path,
        "description": description,
        "enabled": True,
        "visibility": VISIBILITY_MENU,
    }
    if op.source:
        entry["source"] = op.source
    # proposal 0060 Phase 1 Layer A (A9): provenance is stamped from the single
    # OS-authoritative source (ctx.turn_origin, set by Session._stamp_execution_context
    # — A7) — never from an op field, so an auto-improvement turn cannot self-declare
    # "user_directed" to bypass the Phase-4 gate. The `builtin` value is stamped on a
    # DIFFERENT seam (the future builtin-tier registry-build loader, not this install
    # path) — never written here. LOAD-BEARING fail-safe: provenance_from_ctx
    # collapses an unset ctx.turn_origin (a bridge-fallback path that didn't thread
    # it) to the stricter "auto_improvement" — a provenance=None install would be
    # UNGATED (escape the Phase-4 gate), so this closes that gate-bypass.
    entry["provenance"] = _provenance_from_ctx(ctx)
    # #2761 PR-2: capture pure-addition-vs-overwrite BEFORE the write mutates entries,
    # so step 8 can route a NEW name to the immediate mid-turn apply and a same-name
    # overwrite (clobber-update — skill's only update path) to the deferred path.
    from reyn.runtime.hot_reload import is_pure_addition  # noqa: PLC0415
    _is_addition = is_pure_addition(name, existing["skills"]["entries"])
    existing["skills"]["entries"][name] = entry
    _write_yaml(config_path, existing)

    # ── 6. Record config generation for crash-recovery (#2259 / CLAUDE.md gate) ─
    from reyn.core.events.config_recovery import record_config_generation  # noqa: PLC0415
    await record_config_generation(getattr(ctx, "state_log", None), config_path, existing)

    # ── 7. Emit skill_installed event (P6) ────────────────────────────────────
    ctx.events.emit(
        "skill_installed",
        name=name,
        path=install_path,
        description=description,
        config_path=str(config_path),
        source=op.source or "",
    )

    # ── 8. Hot-reload: surface the installed skill in the current session ─────
    # #2761 PR-2: a PURE ADDITION on a live per-session reloader (ctx.hot_reloader)
    # applies IMMEDIATELY (mid-turn) so the just-installed NEW skill is resolvable this
    # turn; a same-name overwrite (clobber-update) or no per-session reloader (CLI
    # separate process) keeps the existing deferred turn-boundary behavior.
    from reyn.runtime.hot_reload import dispatch_install_reload  # noqa: PLC0415
    await dispatch_install_reload(
        getattr(ctx, "hot_reloader", None),
        source="skill_install",
        is_addition=_is_addition,
    )

    return {
        "status": "installed",
        "name": name,
        "path": install_path,
        "description": description,
        "config_path": str(config_path),
        "source": op.source or "",
    }


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("skill_install", handle, canonical=STRUCTURED_PASSTHROUGH)
