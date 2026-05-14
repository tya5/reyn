"""FP-0006 Component B — version snapshot helper for skill_improver finalize phase.

Runs in UNSAFE mode from the finalize preprocessor because it:
  - reads the current skill.md from the original skill directory (Path.read_text)
  - writes snapshot files to .reyn/skill-versions/<name>/v<N>.md
  - manages the `current` pointer file in that directory

Called as a python preprocessor step in finalize.md:
  - Input:  the improvement_result artifact (after apply_improvements hands off)
  - Output: {"saved_version": N, "snapshot_path": str, "next_version": N+1}

The snapshot is taken of the PRE-APPLY skill.md (= the version about to be
replaced). The caller (finalize.md) performs the actual copy-back; this step
only captures the before-state so rollback is always possible.

NOTE: Do NOT add 'from __future__ import annotations' and do NOT import reyn
modules at the top level — keep module-level imports minimal so the function is
importable without the full reyn install if needed.
"""

import os
from pathlib import Path

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
    versions_dir = Path(".reyn") / "skill-versions" / skill_name
    versions_dir.mkdir(parents=True, exist_ok=True)

    # --- determine version number ---
    current_file = versions_dir / "current"
    if current_file.exists():
        try:
            current_n = int(current_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            current_n = 0
        save_n = current_n + 1  # the snapshot we write is the pre-apply state
    else:
        # First save: v1 is the original (pre-any-improvement) skill.md.
        save_n = 1

    # --- read the pre-apply skill.md ---
    original_skill_md = Path(original_skill_root) / "skill.md"
    try:
        skill_md_content = original_skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(
            f"version_snapshot.save_snapshot: cannot read {original_skill_md}: {exc}"
        ) from exc

    # --- write the snapshot ---
    snapshot_path = versions_dir / f"v{save_n}.md"
    snapshot_path.write_text(skill_md_content, encoding="utf-8")

    # --- apply the `current` pointer (points to the snapshot just written) ---
    # For the first save, `current` → "1" (pre-apply original).
    # After the actual apply-back, `update_current_pointer` is called to bump
    # it to save_n+1 so the CLI sees "the applied version is live now".
    current_file.write_text(str(save_n), encoding="utf-8")

    next_version = save_n + 1

    # --- enforce max_versions cap ---
    _apply_max_versions_cap(versions_dir, current_n=save_n, max_versions=_get_max_versions())

    return {
        "saved_version": save_n,
        "snapshot_path": str(snapshot_path),
        "next_version": next_version,
        "versions_dir": str(versions_dir),
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


def read_on_propose_config(artifact: dict) -> dict:
    """FP-0006 Component D — read self_improvement config from reyn.yaml.

    Runs in unsafe mode: calls load_config() which reads reyn.yaml from disk.
    Returns the on_propose + max_versions values so the LLM can apply the gate
    without needing to know how the config is loaded.

    Returns:
        {"on_propose": str, "max_versions": int}

    Falls back to safe defaults (ask_user, 10) on any config-load error so
    that a broken reyn.yaml does not abort the finalize phase.
    """
    try:
        from reyn.config import load_config
        cfg = load_config()
        si = cfg.self_improvement
        return {"on_propose": si.on_propose, "max_versions": si.max_versions}
    except Exception:
        return {"on_propose": "ask_user", "max_versions": 10}


def update_current_pointer(artifact: dict) -> dict:
    """Update the `current` pointer AFTER the improved files have been copied back.

    Called as a second preprocessor step in finalize.md, AFTER the LLM has
    performed the copy-back file ops. Sets `current` to the value that
    save_snapshot returned as `next_version` (= the now-live improved version).

    Returns:
        {"current_version": int, "versions_dir": str}
    """
    data = artifact.get("data", {}) if isinstance(artifact, dict) else {}

    # Read what save_snapshot stored
    snapshot_info = data.get("_snapshot", {}) if isinstance(data, dict) else {}
    next_version = snapshot_info.get("next_version")
    versions_dir_str = snapshot_info.get("versions_dir", "")

    if not versions_dir_str or next_version is None:
        # Snapshot step was skipped (no-copy path) — nothing to update.
        return {"current_version": None, "versions_dir": versions_dir_str}

    versions_dir = Path(versions_dir_str)
    current_file = versions_dir / "current"
    if versions_dir.exists():
        current_file.write_text(str(next_version), encoding="utf-8")

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
    """
    return Path(original_skill_root).name or original_skill_root


def _get_max_versions() -> int:
    """Read self_improvement.max_versions from config, default 10.

    Avoids a hard import dependency on reyn.config so this module stays
    importable in testing and non-Reyn contexts. Falls back to 10 on
    any error.
    """
    try:
        from reyn.config import load_config
        cfg = load_config()
        return cfg.self_improvement.max_versions
    except Exception:
        return 10


def _apply_max_versions_cap(versions_dir: Path, current_n: int, max_versions: int) -> None:
    """Delete the OLDEST versioned files if total count exceeds max_versions.

    Rules:
      - Count all v<N>.md files in versions_dir.
      - If count > max_versions: delete the OLDEST (smallest N).
      - NEVER delete the file pointed to by `current_n`.
      - Repeats until count <= max_versions or no deletable files remain.
    """
    if max_versions <= 0:
        return

    # Collect all vN.md files and their version numbers
    version_files: list[tuple[int, Path]] = []
    for entry in versions_dir.iterdir():
        if entry.name.startswith("v") and entry.name.endswith(".md"):
            try:
                n = int(entry.name[1:-3])
                version_files.append((n, entry))
            except ValueError:
                pass

    version_files.sort(key=lambda t: t[0])  # oldest first

    while len(version_files) > max_versions:
        # Find the oldest that is NOT current
        deleted = False
        for i, (n, path) in enumerate(version_files):
            if n != current_n:
                try:
                    os.remove(path)
                except OSError:
                    pass
                version_files.pop(i)
                deleted = True
                break
        if not deleted:
            # All remaining files are protected (current_n) — can't cap further.
            break
