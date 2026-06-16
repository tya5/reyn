"""`reyn skills` — list available skills, show details, or validate consistency.

Usage
-----
``reyn skills``
    List all installed skills.

``reyn skills <name>``
    Show usage details for one skill.

``reyn skills validate <name>``
    Validate op/permission cross-layer consistency for a skill (FP-0026).
    Exit 0 = OK, non-zero = inconsistency detected.

``reyn skills validate --all``
    Validate all installed skills (project, local, stdlib) and report
    inconsistencies.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..skill_loader import stdlib_root

_VALIDATE_SUBCMD = "validate"


def register(sub) -> None:
    p = sub.add_parser(
        "skills",
        help=(
            "List available skills, show usage details, or validate "
            "op/permission consistency (FP-0026)"
        ),
    )
    # Capture everything as positional so `reyn skills validate <name>` and
    # `reyn skills <name>` both work.  Dispatch is done manually in run().
    p.add_argument(
        "args_rest", nargs=argparse.REMAINDER, metavar="[validate | SKILL]",
        help=(
            "Skill name to show details for, "
            "or 'validate [<name>|--all]' to check op/permission consistency"
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    rest = list(getattr(args, "args_rest", []) or [])

    # Dispatch to validate sub-handler if first token is "validate".
    if rest and rest[0] == _VALIDATE_SUBCMD:
        _run_validate_from_rest(rest[1:])
        return

    sl = stdlib_root()
    search_roots: list[tuple[str, Path]] = [
        ("project", Path("reyn") / "project"),
        ("local",   Path("reyn") / "local"),
        ("stdlib",  sl / "skills"),
    ]

    skill_name = rest[0] if rest else None
    if skill_name:
        _print_detail(skill_name, search_roots)
        return

    _print_list(search_roots)


# ---------------------------------------------------------------------------
# validate subcommand
# ---------------------------------------------------------------------------


def _run_validate_from_rest(rest: list[str]) -> None:
    """Dispatch `reyn skills validate [<name> | --all]` from parsed REMAINDER."""
    validate_all = "--all" in rest
    skill_name = next((r for r in rest if not r.startswith("-")), None)

    sl = stdlib_root()
    search_roots: list[tuple[str, Path]] = [
        ("project", Path("reyn") / "project"),
        ("local",   Path("reyn") / "local"),
        ("stdlib",  sl / "skills"),
    ]

    if not validate_all and not skill_name:
        print(
            "Error: provide a skill name or --all.\n"
            "  reyn skills validate <skill_name>\n"
            "  reyn skills validate --all",
            file=sys.stderr,
        )
        sys.exit(1)

    _run_validate(
        skill_name=skill_name,
        validate_all=validate_all,
        search_roots=search_roots,
    )


def _run_validate(
    skill_name: str | None,
    validate_all: bool,
    search_roots: list[tuple[str, Path]],
) -> None:
    """Core validate logic — separated for testability."""
    from reyn.skill.validator import validate_skill_dir

    skill_dirs: list[tuple[str, Path]] = []

    if validate_all:
        for label, skills_dir in search_roots:
            if not skills_dir.exists():
                continue
            for entry in sorted(skills_dir.iterdir()):
                if entry.is_dir() and (entry / "skill.md").exists():
                    skill_dirs.append((label, entry))
    else:
        found = _find_skill_dir(skill_name, search_roots)
        if found is None:
            print(f"Error: skill '{skill_name}' not found.", file=sys.stderr)
            sys.exit(1)
        skill_dirs.append(("found", found))

    any_error = False
    results = []

    for _label, skill_dir in skill_dirs:
        result = validate_skill_dir(skill_dir)
        results.append(result)
        if result.errors or result.warnings:
            print(str(result))
        else:
            if not validate_all:
                print(f"Skill '{result.skill_name}': OK — no cross-layer inconsistencies.")
        if result.errors:
            any_error = True

    if validate_all:
        total = len(skill_dirs)
        n_error = sum(1 for r in results if r.errors)
        n_warn = sum(1 for r in results if not r.errors and r.warnings)
        print(
            f"\nValidated {total} skill(s). "
            f"Errors in {n_error}, warnings-only in {n_warn}."
        )

    if any_error:
        sys.exit(1)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_skill_md(skill_md: Path) -> tuple[dict, str]:
    from reyn.compiler.parser import _split_frontmatter
    try:
        fm, body = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
        return fm, body
    except Exception:
        return {}, ""


def _find_skill_dir(name: str, search_roots: list[tuple[str, Path]]) -> Path | None:
    for _, skills_dir in search_roots:
        candidate = skills_dir / name
        if candidate.is_dir() and (candidate / "skill.md").exists():
            return candidate
    return None


def _find_skill(name: str, search_roots: list[tuple[str, Path]]) -> Path | None:
    skill_dir = _find_skill_dir(name, search_roots)
    return (skill_dir / "skill.md") if skill_dir else None


def _print_detail(name: str, search_roots: list[tuple[str, Path]]) -> None:
    skill_md = _find_skill(name, search_roots)
    if skill_md is None:
        print(f"Skill '{name}' not found.")
        return
    fm, body = _read_skill_md(skill_md)
    print(f"\n{fm.get('name', name)}")
    if fm.get("description"):
        print(f"{fm['description']}\n")
    if body.strip():
        print(body.strip())
    else:
        print("(no documentation)")
    print()


def _print_list(search_roots: list[tuple[str, Path]]) -> None:
    found_any = False
    seen: set[str] = set()
    for label, skills_dir in search_roots:
        if not skills_dir.exists():
            continue
        entries = sorted(p for p in skills_dir.iterdir()
                         if p.is_dir() and (p / "skill.md").exists())
        if not entries:
            continue
        print(f"\n{label}  ({skills_dir})")
        for skill_dir in entries:
            name = skill_dir.name
            fm, _ = _read_skill_md(skill_dir / "skill.md")
            desc = (fm.get("description") or "").strip().splitlines()[0] if fm.get("description") else ""
            shadowed = " [shadowed]" if name in seen else ""
            desc_str = f"  — {desc}" if desc else ""
            print(f"  {name}{desc_str}{shadowed}")
            seen.add(name)
        found_any = True

    if not found_any:
        print("No skills found.")
    print()
    print("Run 'reyn skills <name>' for usage details.")
