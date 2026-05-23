"""FP-0006 Component B — version snapshot helper for skill_improver finalize phase.

Public functions:
  save_snapshot(artifact)         → dict   (snapshot pre-apply skill.md)
  update_current_pointer(artifact) → dict  (advance `current` after copy-back)
  decide_on_propose_action(...)   → str    (pure config-to-action mapping)

FP-0042 Phase 2.7 (2026-05-23): migrated from mode: unsafe to mode: safe.
File reads / writes / mkdir / delete go through ``reyn.safe.file``;
``glob.glob`` covers directory enumeration for the max-versions cap.

The legacy ``read_on_propose_config`` (which called
``reyn.config.load_config()`` — a ``reyn.*`` import the safe-mode AST
validator rejects) was removed. Its only consumer was the test suite;
the test helper now lives in
``tests/test_skill_improver_version_snapshot.py``. Production code uses
the file_read run_op + ``parse_on_propose_config_minimal`` chain in
``version_snapshot_pure.py`` (= Wave 3b).

Snapshot semantics (unchanged):
  - reads the current skill.md from the original skill directory
  - writes a snapshot file to ``.reyn/skill-versions/<name>/v<N>.md``
  - manages the ``current`` pointer file in that directory

Called as a python preprocessor step in ``phases/finalize.md``. The
snapshot is taken of the PRE-APPLY skill.md (= the version about to be
replaced). The caller (finalize.md) performs the actual copy-back; this
step only captures the before-state so rollback is always possible.

Path manipulation uses plain string operations because ``pathlib`` is
not on the safe-mode import allowlist.
"""
from __future__ import annotations

import glob as _glob_mod

from reyn.safe import file as _safe_file

# POSIX stat-mode constants (= stat.S_IFMT / S_IFREG). Hard-coded because
# the ``stat`` module is not on the safe-mode import allowlist.
_S_IFMT = 0o170000
_S_IFREG = 0o100000


# ── public entry point ─────────────────────────────────────────────────────────


def save_snapshot(artifact: dict) -> dict:
    """Snapshot the pre-apply skill.md to .reyn/skill-versions/<name>/.

    Reads the current (pre-apply) skill.md from original_skill_root, saves it
    as v<N>.md, and manages the `current` pointer file.

    Noop semantics:
      When copy-back will be skipped (termination_reason != score_threshold_met,
      or original_skill_root is empty, or path starts with src/), this function
      returns a noop result dict with null version fields so the LLM can detect
      the skip condition from `data._snapshot.saved_version is null`.

    Apply semantics:
      - First-ever save: writes v1.md (= the original, before any improvement),
        sets `current` → "1".  After the actual apply, the LLM issues a file
        write to `versions_dir/current` with `next_version` (= "2").
      - Subsequent saves: reads `current` file → N, writes v<N+1>.md (the
        pre-apply state), sets `current` → N+1.  The LLM then updates
        `current` → N+2 after copy-back.

    Returns:
        {
          "saved_version": int | null,  # the N just written (v<N>.md); null = noop
          "snapshot_path": str,         # path to the newly created vN.md (or "")
          "next_version": int | null,   # N+1 — what `current` should be after apply
          "versions_dir": str,          # .reyn/skill-versions/<name>/ directory (or "")
          "original_skill_root": str    # echoed back for traceability (or "")
        }

    Raises:
        OSError  if reading the original skill.md fails (write zone issue etc.)
    """
    data = artifact.get("data", {}) if isinstance(artifact, dict) else {}

    # Check noop conditions — mirrors finalize Step 1 guard conditions.
    termination_reason = data.get("termination_reason", "")
    original_skill_root = _resolve_original_skill_root(data)

    if (
        termination_reason != "score_threshold_met"
        or not original_skill_root
        or original_skill_root.startswith("src/")
    ):
        # Noop — copy-back will be skipped; no snapshot needed.
        return {
            "saved_version": None,
            "snapshot_path": "",
            "next_version": None,
            "versions_dir": "",
            "original_skill_root": original_skill_root,
        }

    skill_name = _skill_name_from_root(original_skill_root)
    versions_dir = f".reyn/skill-versions/{skill_name}"
    _safe_file.mkdir(versions_dir, parents=True, exist_ok=True)

    # --- determine version number ---
    current_file = f"{versions_dir}/current"
    if _safe_file.exists(current_file):
        try:
            current_n = int(_safe_file.read(current_file).strip())
        except (ValueError, OSError):
            current_n = 0
        save_n = current_n + 1  # the snapshot we write is the pre-apply state
    else:
        # First save: v1 is the original (pre-any-improvement) skill.md.
        save_n = 1

    # --- read the pre-apply skill.md ---
    original_skill_md = f"{original_skill_root}/skill.md"
    try:
        skill_md_content = _safe_file.read(original_skill_md)
    except OSError as exc:
        raise OSError(
            f"version_snapshot.save_snapshot: cannot read {original_skill_md}: {exc}"
        ) from exc

    # --- write the snapshot ---
    snapshot_path = f"{versions_dir}/v{save_n}.md"
    _safe_file.write(snapshot_path, skill_md_content)

    # --- apply the `current` pointer (points to the snapshot just written) ---
    # For the first save, `current` → "1" (pre-apply original).
    # After the actual apply-back, `update_current_pointer` is called to bump
    # it to save_n+1 so the CLI sees "the applied version is live now".
    _safe_file.write_atomic(current_file, str(save_n))

    next_version = save_n + 1

    # --- enforce max_versions cap ---
    # ``data._on_propose_config.max_versions`` is set by the Wave 3b
    # ``parse_on_propose_config_minimal`` preprocessor step that reads
    # reyn.yaml via the file_read run_op. Default 10 if absent.
    max_versions = _get_max_versions_from_artifact(data)
    _apply_max_versions_cap(versions_dir, current_n=save_n, max_versions=max_versions)

    return {
        "saved_version": save_n,
        "snapshot_path": snapshot_path,
        "next_version": next_version,
        "versions_dir": versions_dir,
        "original_skill_root": original_skill_root,
    }


def decide_on_propose_action(on_propose: str, score: float, threshold: float) -> str:
    """Pure decision function for the on_propose gate (FP-0006 Component D).

    Maps the config value to a concrete action string.  The score/threshold
    parameters are accepted for forward-compatibility but the current gate is
    config-only (the eval gate is already enforced by apply_improvements).

    Returns one of:
      "ask"        — on_propose == "ask_user": show InterventionBus prompt
      "auto_apply" — on_propose == "auto": apply without prompting (CI mode)
      "dry_run"    — on_propose == "disabled": log event, skip copy-back

    The caller is responsible for the actual InterventionBus interaction;
    this function only maps config → intent, keeping the logic unit-testable
    without needing an InterventionBus instance (Tier 2 territory).
    """
    if on_propose == "auto":
        return "auto_apply"
    if on_propose == "disabled":
        return "dry_run"
    # Default: "ask_user" (and any unknown value is treated as ask_user for safety)
    return "ask"


def update_current_pointer(artifact: dict) -> dict:
    """Update the `current` pointer AFTER the improved files have been copied back.

    Called as a second preprocessor step in finalize.md, AFTER the LLM has
    performed the copy-back file ops. Sets `current` to the value that
    save_snapshot returned as `next_version` (= the now-live improved version).

    Returns:
        {"current_version": int, "versions_dir": str}
    """
    data = artifact.get("data", {}) if isinstance(artifact, dict) else {}

    snapshot_info = data.get("_snapshot", {}) if isinstance(data, dict) else {}
    next_version = snapshot_info.get("next_version")
    versions_dir_str = snapshot_info.get("versions_dir", "")

    if not versions_dir_str or next_version is None:
        return {"current_version": None, "versions_dir": versions_dir_str}

    if _safe_file.exists(versions_dir_str):
        _safe_file.write_atomic(f"{versions_dir_str}/current", str(next_version))

    return {"current_version": next_version, "versions_dir": versions_dir_str}


# ── internal helpers ───────────────────────────────────────────────────────────


def _resolve_original_skill_root(data: dict) -> str:
    """Extract original_skill_root from the improvement_result data dict.

    Tries two locations (the field lives at top-level in finalize's input):
      1. data.original_skill_root          (direct — what finalize receives)
      2. data._resolved_paths.original_skill_root (fallback — session paths)
    """
    if not isinstance(data, dict):
        return ""
    direct = data.get("original_skill_root")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().rstrip("/")
    resolved = data.get("_resolved_paths") or {}
    if isinstance(resolved, dict):
        via_resolved = resolved.get("original_skill_root")
        if isinstance(via_resolved, str) and via_resolved.strip():
            return via_resolved.strip().rstrip("/")
    return ""


def _skill_name_from_root(original_skill_root: str) -> str:
    """Derive the skill name (last path component) from the skill root path.

    Examples:
      "reyn/local/my_skill"              → "my_skill"
      ".reyn/skill_improver_work/my_skill" → "my_skill"
      "src/reyn/stdlib/skills/eval"      → "eval"

    Replacement for ``pathlib.Path(...).name`` (= pathlib is not on the
    safe-mode allowlist).
    """
    if not original_skill_root:
        return original_skill_root
    last = original_skill_root.rstrip("/").rsplit("/", 1)[-1]
    return last or original_skill_root


def _get_max_versions_from_artifact(data: dict) -> int:
    """Read ``self_improvement.max_versions`` from the artifact preprocessor chain.

    The Wave 3b file_read + ``parse_on_propose_config_minimal`` chain leaves
    the parsed values at ``data._on_propose_config``. Default 10 on absence
    (= matches SelfImprovementConfig).
    """
    if not isinstance(data, dict):
        return 10
    cfg = data.get("_on_propose_config") or {}
    if isinstance(cfg, dict):
        try:
            return int(cfg.get("max_versions") or 10)
        except (TypeError, ValueError):
            return 10
    return 10


def _is_regular_file(path: str) -> bool:
    """Return True iff ``path`` exists and is a regular file.

    Replacement for ``os.path.isfile`` (= ``os`` is not on the safe-mode
    allowlist).
    """
    try:
        info = _safe_file.stat(path)
    except (OSError, PermissionError):
        return False
    return (int(info.get("mode", 0)) & _S_IFMT) == _S_IFREG


def _apply_max_versions_cap(versions_dir: str, current_n: int, max_versions: int) -> None:
    """Delete the OLDEST versioned files if total count exceeds max_versions.

    Rules:
      - Count all v<N>.md files in versions_dir.
      - If count > max_versions: delete the OLDEST (smallest N).
      - NEVER delete the file pointed to by `current_n`.
      - Repeats until count <= max_versions or no deletable files remain.

    FP-0042 Phase 2.7: replaces ``Path.iterdir`` + ``os.remove`` with
    ``glob.glob`` + ``reyn.safe.file.delete``. Directory enumeration via
    glob covers what iterdir did; the regular-file filter is implicit
    because the pattern only matches ``v*.md``.
    """
    if max_versions <= 0:
        return

    pattern = f"{versions_dir}/v*.md"
    matches = _glob_mod.glob(pattern)

    version_files: list[tuple[int, str]] = []
    for entry in matches:
        if not _is_regular_file(entry):
            continue
        name = entry.rsplit("/", 1)[-1]  # "v<N>.md"
        if not (name.startswith("v") and name.endswith(".md")):
            continue
        try:
            n = int(name[1:-3])
            version_files.append((n, entry))
        except ValueError:
            continue

    version_files.sort(key=lambda t: t[0])  # oldest first

    while len(version_files) > max_versions:
        # Find the oldest that is NOT current
        deleted = False
        for i, (n, path) in enumerate(version_files):
            if n != current_n:
                try:
                    _safe_file.delete(path)
                except (OSError, PermissionError):
                    pass
                version_files.pop(i)
                deleted = True
                break
        if not deleted:
            # All remaining files are protected (current_n) — can't cap further.
            break
