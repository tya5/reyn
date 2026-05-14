"""`reyn skill` — version history and rollback for installed skills (FP-0006 Component E).

Usage
-----
``reyn skill versions <name>``
    List saved version snapshots for a skill, with timestamps and current marker.

``reyn skill rollback <name>``
    Restore the previous version (current - 1).

``reyn skill rollback <name> --to vN``
    Restore a specific saved version.

Version snapshots live in ``.reyn/skill-versions/<name>/``, created by
FP-0006 Component B (skill_improver finalize step).  This module is read/restore
only; it never writes snapshots.

P6 note: no EventStore is wired in standalone CLI context (there is no
active skill-run EventLog).  The rollback is instead confirmed via a printed
audit line.  A future PR may attach a lightweight CLI EventStore to
``.reyn/events/cli/<date>.jsonl`` for full audit coverage.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from reyn.skill.skill_paths import (
    SkillNotFoundError,
    is_stdlib_skill,
    resolve_skill_path,
)

# Root directory for skill version snapshots (relative to CWD / project root).
_VERSIONS_DIR = Path(".reyn") / "skill-versions"


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(sub) -> None:
    p = sub.add_parser(
        "skill",
        help="Skill version history and rollback (FP-0006)",
    )
    skill_sub = p.add_subparsers(dest="skill_cmd", metavar="<subcommand>")
    skill_sub.required = True

    # reyn skill versions <name>
    p_versions = skill_sub.add_parser(
        "versions",
        help="List saved version snapshots for a skill",
    )
    p_versions.add_argument("skill_name", metavar="SKILL_NAME")
    p_versions.set_defaults(func=cmd_versions)

    # reyn skill rollback <name> [--to vN]
    p_rollback = skill_sub.add_parser(
        "rollback",
        help="Restore a previous version of a skill",
    )
    p_rollback.add_argument("skill_name", metavar="SKILL_NAME")
    p_rollback.add_argument(
        "--to",
        metavar="vN",
        dest="target_version",
        default=None,
        help="Version to roll back to (e.g. v2). Defaults to current-1.",
    )
    p_rollback.set_defaults(func=cmd_rollback)

    p.set_defaults(func=_no_subcommand)


def _no_subcommand(args: argparse.Namespace) -> None:
    print("Error: provide a subcommand: versions or rollback", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# run() shim — delegates to func set by sub-subparser
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    args.func(args)


# ---------------------------------------------------------------------------
# reyn skill versions
# ---------------------------------------------------------------------------


def cmd_versions(args: argparse.Namespace) -> None:
    skill_name = args.skill_name
    versions_dir = _VERSIONS_DIR / skill_name

    # If the snapshots directory doesn't exist, exit gracefully.
    if not versions_dir.exists():
        print(f"No versions saved for skill '{skill_name}'.")
        return

    current_num = _read_current(versions_dir)

    # Collect vN.md files, sorted numerically.
    snapshots = _collect_snapshots(versions_dir)
    if not snapshots:
        print(f"No versions saved for skill '{skill_name}'.")
        return

    print(f"{skill_name} version history:")
    for ver_num, ver_path in snapshots:
        mtime = os.path.getmtime(str(ver_path))
        ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        current_marker = "  -> current" if ver_num == current_num else ""
        print(f"  v{ver_num}  {ts}{current_marker}")


# ---------------------------------------------------------------------------
# reyn skill rollback
# ---------------------------------------------------------------------------


def cmd_rollback(args: argparse.Namespace) -> None:
    skill_name = args.skill_name
    target_version_str = args.target_version  # e.g. "v2" or None

    versions_dir = _VERSIONS_DIR / skill_name

    # Resolve skill path — errors if skill is not installed.
    try:
        skill_dir, _skill_root = resolve_skill_path(skill_name)
    except SkillNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    # Refuse to roll back stdlib skills.
    if is_stdlib_skill(skill_dir):
        print(
            f"Cannot roll back stdlib skill '{skill_name}'. "
            "Stdlib skills are read-only. "
            "Copy to reyn/project/ to customize.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Ensure the versions directory exists.
    if not versions_dir.exists():
        print(
            f"No versions saved for skill '{skill_name}'. "
            "Nothing to roll back.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Determine current version number.
    current_num = _read_current(versions_dir)
    if current_num is None:
        print(
            f"Error: '{versions_dir / 'current'}' is missing or unreadable.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Resolve target version.
    if target_version_str is not None:
        # Strip leading 'v' if present.
        raw = target_version_str.lstrip("v")
        try:
            target_num = int(raw)
        except ValueError:
            print(
                f"Error: invalid version '{target_version_str}'. "
                "Expected a version like 'v2'.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        # Default: one step back.
        target_num = current_num - 1
        if target_num < 1:
            print(
                f"Error: already at the earliest version (v{current_num}). "
                "No previous version to restore.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Verify the target snapshot file exists.
    target_snapshot = versions_dir / f"v{target_num}.md"
    if not target_snapshot.exists():
        print(
            f"Error: version v{target_num} not found at '{target_snapshot}'.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Atomic write: tmpfile in the same directory, then rename.
    skill_md = skill_dir / "skill.md"
    content = target_snapshot.read_text(encoding="utf-8")
    _atomic_write(skill_md, content)

    # Update the current pointer.
    current_file = versions_dir / "current"
    _atomic_write(current_file, str(target_num))

    # Confirmation output (P6 audit substitute — see module docstring).
    print(f"Rolled back '{skill_name}' from v{current_num} to v{target_num}.")
    print(
        f"skill.md content restored from "
        f"{target_snapshot}."
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_current(versions_dir: Path) -> int | None:
    """Read the integer from the 'current' pointer file; return None on failure."""
    current_file = versions_dir / "current"
    try:
        raw = current_file.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def _collect_snapshots(versions_dir: Path) -> list[tuple[int, Path]]:
    """Return (version_num, path) pairs for all vN.md files, sorted numerically."""
    result: list[tuple[int, Path]] = []
    for entry in versions_dir.iterdir():
        if entry.suffix == ".md" and entry.stem.startswith("v"):
            try:
                num = int(entry.stem[1:])
                result.append((num, entry))
            except ValueError:
                continue
    result.sort(key=lambda x: x[0])
    return result


def _atomic_write(dest: Path, content: str) -> None:
    """Write *content* to *dest* atomically via a sibling tempfile + rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(dest))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
