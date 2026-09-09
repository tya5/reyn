#!/usr/bin/env python3
"""#6014 follow-up gate — every top-level ``scripts/*.py`` file must
declare its own CI role, and the declared role's own obligation must
actually hold.

## Why this exists

#6014's own census found 6 gate scripts that were REFERENCED from a
CLAUDE.md (or looked like a gate by name) but had never once run against
the real tree — some were wired into neither a per-PR workflow nor
``main-invariant-sweep.yml``'s explicit enumeration; nothing mechanical
would ever have caught that absence. A human census (lead-coder's) missed
2 of them on the first pass by grepping only the ROOT ``CLAUDE.md`` (there
are 7), and separately conflated "referenced by name" with "will actually
run" for a THIRD script (``check_claude_md_doc_overlap.py``) that reports
but never fails. The same night surfaced a fourth state (#5265,
``detect_5265_missing_required_context.py``): a script referenced from
neither a workflow nor a CLAUDE.md, kept alive only by one person's
recurring habit of running it by hand — invisible to any grep, and more
dangerous than an outright absence because its results *look* like
enforcement (memory pin: an invariant held up by someone's routine looks
enforced and is not).

## The declaration

A single line, matched anywhere inside the module's own docstring (not
necessarily the first line — unlike ``test_tier_audit.py``'s Tier
convention, this repo's own scripts already use their first docstring
line for an ``#NNNN — <one-line description>`` convention this gate does
not want to displace)::

    CI: gate
    CI: report -- <who reads it, non-empty>
    CI: manual -- <who runs it and by what act, non-empty>

Design ruling (issue #6014, lead-coder): the three values are NOT the
same check wearing three names -- each carries a DIFFERENT obligation,
because collapsing ``report`` into ``gate`` would have this gate itself
lie about what "declared" means (a ``report`` script is exactly the
"wired, green, enforces nothing" state #6014 exists to make visible, not
paper over):

  - ``gate``    -- a real violation must turn CI red. Checked: the
    script's own filename appears in some ``.github/workflows/*.yml``.
  - ``report``  -- wired into CI, but deliberately never fails the run
    (an audit/census tool). Checked: same wiring presence check as
    ``gate``, PLUS a non-empty reason naming who reads the output. A
    ``report`` that cannot name a reader is not a report -- it is nobody
    watching, wearing a report's clothes.
  - ``manual``  -- never runs unattended; a human invokes it. Checked:
    a non-empty reason naming who runs it and by what act (a PR review
    step, a periodic personal sweep, a one-off investigation). A
    ``manual`` that cannot name a runner and an act is state 4 (#5265) --
    "someone's habit" wearing ``manual``'s clothes instead of gate's.

What this gate can verify mechanically stops at "the reason text is
non-empty" -- it cannot read whether the reason genuinely names a person
and an act, only a human reviewing the PR that adds or edits a
declaration can. The value of requiring the line at all is exactly that a
script whose maintainer cannot HONESTLY write a one-line reason is
disclosing something by that failure, not by this gate's own logic.

## Deliberately excluded from the population

- Any ``scripts/_*.py`` (an existing informal convention already in use
  -- ``_markers.py``, ``_file_depth_predicate.py``, ``_reyn_web_proc.py``
  -- for a helper imported by other scripts/tests, never invoked as its
  own CLI entry point).
- Anything under a ``scripts/`` SUBDIRECTORY (``litellm_proxy_patch/``,
  ``spike_lib/``, ``_sitecustomize_fake_embed/``) -- every CLAUDE.md
  reference to a script names a flat ``scripts/<name>.py`` path (verified
  by grep across all 7 ``CLAUDE.md`` files during #6014's design), never
  a nested one; widening the population to nested files would pull in
  library modules (``spike_lib/__init__.py`` has no CLI role to declare
  at all) with no CLAUDE.md-derived reason to.

## Deliberately NOT a ratchet

#6014's own ruling: a baseline that grandfathers "not yet declared" is
the exact silence this gate exists to close -- a script landing today
with no ``CI:`` line is the SAME state as the 92 pre-existing scripts
this PR migrates, and a ratchet would let today's version of that
population go on being invisible to a NEW run for the same reason
#6014's original 6 were. Every run checks every script in the population;
there is no baseline file to fall behind.

CI: gate
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _ROOT / "scripts"
_WORKFLOWS_DIR = _ROOT / ".github" / "workflows"

_CI_LINE_RE = re.compile(
    r"^CI:\s*(gate|report|manual)(?:\s*--\s*(.*))?\s*$",
    re.MULTILINE,
)

_VALID_ROLES = frozenset({"gate", "report", "manual"})


def _population() -> "list[Path]":
    """Every top-level ``scripts/*.py`` file, excluding underscore-prefixed
    helpers. Non-recursive -- see module docstring for why nested files
    (``litellm_proxy_patch/``, ``spike_lib/``) are out of scope."""
    return sorted(
        p for p in _SCRIPTS_DIR.glob("*.py")
        if not p.name.startswith("_")
    )


def _docstring(path: Path) -> "str | None":
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return None
    return ast.get_docstring(tree)


def _declared_role(docstring: "str | None") -> "tuple[str, str] | None | list[str]":
    """Returns ``(role, reason)`` for exactly one match, ``None`` for no
    match, or a list of ALL matched raw lines when more than one ``CI:``
    line is present (ambiguous -- the caller reports this as its own
    violation kind rather than silently taking the first or last)."""
    if docstring is None:
        return None
    matches = list(_CI_LINE_RE.finditer(docstring))
    if not matches:
        return None
    if len(matches) > 1:
        return [m.group(0).strip() for m in matches]
    role = matches[0].group(1)
    reason = (matches[0].group(2) or "").strip()
    return role, reason


def _wiring_haystack(scripts: "list[Path]", scripts_dir: Path, workflows_dir: Path) -> str:
    """"Wired" is not always a direct ``.github/workflows/*.yml`` mention —
    3 real scripts in this repo's own population are reached one layer
    removed: ``verify_env_identity.py`` is invoked from ``tests/**/
    conftest.py`` autouse fixtures (a real, structural pytest-collection
    invocation, not a workflow step), and ``wheel_parity_probe.py`` /
    ``wheel_plugin_install_probe.py`` are spawned by ``wheel_reachability_
    smoke.py`` (itself directly workflow-wired), never named in any
    workflow file themselves.

    Two real invocation mechanisms, not a hand-typed name list: workflow
    files ∪ conftest.py files form the base haystack; any script whose OWN
    name appears there is ITSELF added to the haystack (one level of
    transitive closure) — this is how a sibling-script dispatch chain like
    wheel_reachability_smoke.py -> wheel_parity_probe.py resolves without
    naming either script here. Deliberately NOT extended to every
    script's source (only ones already confirmed directly wired) — that
    would let an unrelated script's passing docstring mention of another
    script's filename count as "wired", the exact false-accept shape this
    whole gate exists to close."""
    workflow_text = "\n".join(
        p.read_text() for p in sorted(workflows_dir.glob("*.yml"))
    )
    conftest_text = "\n".join(
        p.read_text()
        for p in sorted(scripts_dir.parent.glob("tests/**/conftest.py"))
    )
    haystack = workflow_text + "\n" + conftest_text
    directly_wired = [s for s in scripts if s.name in haystack]
    return haystack + "\n" + "\n".join(s.read_text() for s in directly_wired)


def find_violations(scripts_dir: Path, workflows_dir: Path) -> "list[str]":
    scripts = sorted(
        p for p in scripts_dir.glob("*.py") if not p.name.startswith("_")
    )
    haystack = _wiring_haystack(scripts, scripts_dir, workflows_dir)
    violations: "list[str]" = []
    for path in scripts:
        rel = path.relative_to(scripts_dir.parent)
        docstring = _docstring(path)
        declared = _declared_role(docstring)
        if declared is None:
            violations.append(
                f"{rel}: no `CI: gate|report|manual` line in its own module "
                f"docstring"
            )
            continue
        if isinstance(declared, list):
            violations.append(
                f"{rel}: {len(declared)} `CI:` lines found, ambiguous — "
                f"exactly one required: {declared!r}"
            )
            continue
        role, reason = declared
        wired = path.name in haystack
        if role == "gate":
            if not wired:
                violations.append(
                    f"{rel}: declares `CI: gate` but its own filename does "
                    f"not appear in any .github/workflows/*.yml — never "
                    f"actually wired"
                )
        elif role == "report":
            if not wired:
                violations.append(
                    f"{rel}: declares `CI: report` but its own filename "
                    f"does not appear in any .github/workflows/*.yml"
                )
            if not reason:
                violations.append(
                    f"{rel}: declares `CI: report` with no reason naming "
                    f"who reads it — `CI: report -- <reader>` required"
                )
        elif role == "manual":
            if not reason:
                violations.append(
                    f"{rel}: declares `CI: manual` with no reason naming "
                    f"who runs it and by what act — `CI: manual -- "
                    f"<runner, act>` required"
                )
        else:  # pragma: no cover — regex already restricts to _VALID_ROLES
            assert role in _VALID_ROLES
    return violations


def main(argv: "list[str] | None" = None) -> int:
    del argv
    scripts = _population()
    # Vacuity guard (#4846-class): a scan that found zero scripts is a
    # scanner failure -- a moved scripts/ dir, a broken glob -- not a
    # clean population. See suspected_time_dependence_ratchet.py's own
    # identical guard for the same reasoning.
    if not scripts:
        print(
            "check_ci_role_declared FAILED: found 0 scripts under "
            f"{_SCRIPTS_DIR} — this is a scanner failure, not a clean "
            "population.",
            file=sys.stderr,
        )
        return 1

    violations = find_violations(_SCRIPTS_DIR, _WORKFLOWS_DIR)
    if violations:
        print(
            f"check_ci_role_declared FAILED: {len(violations)} of "
            f"{len(scripts)} script(s) in scripts/ fail their own "
            "declared (or missing) CI role:\n",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            "\nSee this script's own module docstring for the `CI: "
            "gate|report|manual` grammar and what each value obligates.",
            file=sys.stderr,
        )
        return 1

    print(
        f"check_ci_role_declared OK: {len(scripts)} script(s) in scripts/, "
        "all declare a CI role and satisfy its obligation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
