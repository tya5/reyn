"""
Skill resolution and loading shared by `run` and `eval` subcommands.

`resolve_skill_path` — name → directory under reyn/local, reyn/project, stdlib.
`load_skill_from_args` — handles all three CLI ways of pointing at a skill
                       (positional name, --skill-path, --module).
"""
from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

from reyn.schemas.models import Skill
from reyn.skill.skill_paths import (
    SkillNotFoundError,
    stdlib_root,
)
from reyn.skill.skill_paths import (  # re-exported for convenience
    resolve_skill_path as _resolve_skill_path_raw,
)

__all__ = [
    "LoadedSkill", "load_skill_from_args",
    "resolve_skill_path", "stdlib_root", "SkillNotFoundError",
]


def resolve_skill_path(name: str):
    """CLI-friendly wrapper: print + sys.exit(1) on lookup failure.

    The runtime layer calls reyn.skill.skill_paths.resolve_skill_path
    directly so the SkillNotFoundError can propagate as a regular op
    failure (no SystemExit leaking through eval workflows).
    """
    try:
        return _resolve_skill_path_raw(name)
    except SkillNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


@dataclass
class LoadedSkill:
    skill: Skill
    skill_md: Path | None      # None when source == "module"
    skill_root: str | None
    source: str                # "name" | "path" | "module"


def load_skill_from_args(args: argparse.Namespace) -> LoadedSkill:
    """Resolve `args.skill_name | args.skill_path | args.module` and load the Skill."""
    if getattr(args, "skill_path", None):
        skill_dir = Path(args.skill_path)
        skill_md = skill_dir / "skill.md"
        skill_root = args.skill_root
        return LoadedSkill(
            skill=_compile(skill_md, skill_root),
            skill_md=skill_md, skill_root=skill_root, source="path",
        )

    if getattr(args, "skill_name", None):
        skill_dir, inferred_root = resolve_skill_path(args.skill_name)
        skill_root = args.skill_root or str(inferred_root)
        skill_md = skill_dir / "skill.md"
        print(f"resolved        : {skill_md}  (skill-root: {skill_root})")
        return LoadedSkill(
            skill=_compile(skill_md, skill_root),
            skill_md=skill_md, skill_root=skill_root, source="name",
        )

    if getattr(args, "module", None):
        try:
            module = importlib.import_module(args.module)
        except ModuleNotFoundError as e:
            print(f"Error: cannot import module '{args.module}': {e}", file=sys.stderr)
            sys.exit(1)
        if not hasattr(module, "skill"):
            print(f"Error: module '{args.module}' has no 'skill' attribute.", file=sys.stderr)
            sys.exit(1)
        return LoadedSkill(skill=module.skill, skill_md=None, skill_root=None, source="module")

    print("Error: provide a skill name (positional), --skill-path DIR, or --module.",
          file=sys.stderr)
    sys.exit(1)


def _compile(skill_md: Path, skill_root: str | None) -> Skill:
    from reyn.compiler import load_dsl_skill
    try:
        return load_dsl_skill(str(skill_md), skill_root=skill_root)
    except Exception as e:
        print(f"Error: failed to compile DSL '{skill_md}': {e}", file=sys.stderr)
        sys.exit(1)
