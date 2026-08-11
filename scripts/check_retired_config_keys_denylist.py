#!/usr/bin/env python3
"""#4327 — a retired top-level `reyn.yaml` key must not appear, at top level,
in operator-facing docs or the shipped config example.

## The gap this closes

`tests/config/test_config_mirror_coverage_1056.py` already gates two files
(`reyn.local.yaml.example` / `docs/reference/config/reyn-yaml.md`) for
COVERAGE — every schema field name must appear somewhere in the doc. That
gate is directional: it asks "does the doc mention every current key" and
never asks "does the doc still mention a key that's gone". A rename doesn't
break coverage (the new name gets added), so a doc that keeps the OLD name
around passes it silently, forever, on exactly the 2 files it covers.

#4322/#4323 measured the cost directly: 8 operator-facing docs (none of
them one of that gate's 2 covered files) still showed pre-#4174-T3/T4 shapes
(top-level `models:` / `litellm:` / `web:`) after the renames landed —
`docs/guide/getting-started/01-installation.md` among them, the page a new
operator reads FIRST. A reader who copies that block writes a config that
doesn't load, silently (the unknown-key path is a WARN, not a hard fail).

## Why a denylist, not "check every YAML example against the schema"

Docs carry plenty of YAML that ISN'T a `reyn.yaml` example — `.mcp.json`
equivalents, pipeline/hook definitions, dogfood tooling's own config
schemas, cost-tracing dumps. Comparing every fenced YAML block's key set
against `ReynConfig`'s schema produces false positives on all of them: a
key set mismatch there means "different schema", not "drift".

A RETIRED key is different: once a key is renamed, the OLD name is no
longer valid ANYWHERE, in ANY reyn.yaml-shaped document — its appearance at
YAML top level is drift by construction, not a matter of which schema the
surrounding example happens to be for (mostly — see the "different schema
still collides" note below, which is exactly why a small file-level
allowlist exists alongside this denylist).

## Single source: `_RENAMED_CONFIG_KEYS`, not a second hand-kept list

The denylist is `reyn.config.config_schema._RENAMED_CONFIG_KEYS` — the SAME
registry `reyn config validate`/`migrate` already reads, already populated
incrementally, in the SAME PR as each rename, by #4174's own T1-T7 practice
(see that module's docstring). This script adds no maintenance burden of
its own: a future rename that registers a `RenamedKeyHint` is picked up
here for free. It deliberately does NOT hand-roll a parallel list — CLAUDE.md's
own recurring lesson (`control-ir.md` vs `OP_KIND_MODEL_MAP`) is that a second
registry nobody is obligated to update is worse than no registry.

## Detection: line-START only, not "key anywhere"

Only a match at column 0 (`^key:`) counts — a *top-level* YAML mapping key
position. This deliberately does NOT catch:

- **dotted mentions** (`web.fetch.max_download_bytes`, prose citing a
  nested field by its full path) — these aren't claiming the OLD top-level
  shape, they're just naming a leaf.
- **indented nested keys that happen to share a retired name** — `models:`
  is retired at top level, but `llm:\n  models:` is the CURRENT correct
  shape; requiring column 0 is exactly what keeps that legitimate case
  green.
- **prose mentions** — `` `models:` moved under `llm:` `` (this docstring
  itself does this) is explaining the move, not demonstrating the stale
  shape; a mid-sentence or indented occurrence is invisible to this gate on
  purpose, at the cost of also being invisible to a prose sentence that
  legitimately quotes the old key at the START of its own line (rare; not
  observed in a real measurement as of #4327).

Column 0 was picked BEFORE building the retired-key list, not after seeing
what would need excluding, per lead-coder's #4327 review comment: narrow
the detection shape by asking "can this be written another way" first, then
name in this comment what got left out — not build the widest possible
regex and hand-carve exceptions around it.

## Two exclusion classes — both discovered by a real pre-flight measurement,
## not asserted from the design alone

**1. Historical-record directories.** `docs/deep-dives/spec/` and ADRs
(`docs/deep-dives/decisions/`) were the two lead-coder named explicitly —
design docs record the design AS IT WAS, and a retired key is a legitimate
historical fact there, not drift (ADRs are immutable by policy; spec/ is a
point-in-time proposal record). Re-running the SAME reasoning against a
pre-flight scan of the whole tree surfaced two MORE directories that are
structurally identical, not named in the original brief but excluded here
for the identical reason, not a new one:

- `docs/deep-dives/proposals/` — FP-NNNN proposal docs (`Status: proposed`
  / partially landed) that show the schema shape AS PROPOSED, sometimes
  years before or after a later, differently-named field actually shipped
  (FP-0016 Component E proposes `agent: {id: ...}` — the eventual shape
  became `agent_id:`, a T5 VALUE-TRANSFORM rename, not the plain rename the
  proposal assumed).
- `docs/deep-dives/journal/` — dated dogfood-run / feature-verify logs that
  quote the EXACT `reyn.local.yaml` block used for that run, at the time it
  was run — the same "records what happened" class `check_tests_path_literal_reference.py`
  already carves out for `CHANGELOG.md` (see that script's own module
  docstring for the identical argument spelled out in full).

**2. Different-schema single files — keyed per FILE × KEY, not per file.**
Column-0 anchoring solves the "legitimate nested key" case, but NOT the
case where two UNRELATED schemas share a top-level field name — e.g.
`reyn.yaml`'s `model:` (moved to `llm.model:`) vs.
`scripts/dogfood_variant_replay.py`'s OWN config format, which also has a
top-level `model:` field with a completely different meaning (which LLM to
replay against), or the built-in model CATALOG entry's own per-entry
schema (`{model: ..., max_completion_tokens: ...}`,
`src/reyn/llm/builtin_models.py`'s `BUILTIN_MODELS` dict shape), which is a
sub-document fragment, not a `reyn.yaml` example, but still starts at
column 0 in its own fenced block. Measured, not guessed — a real pre-flight
scan is what surfaced these, not a hypothetical:

- `docs/deep-dives/contributing/dogfood-tooling.md` — `variant_ablation.yaml`
  example is `dogfood_variant_replay.py`'s own config.
- `docs/reference/builtin-models.md` / `.ja.md` — every fenced block is one
  catalog entry's OWN field shape, never a full `reyn.yaml` document.

`_EXCLUDED_FILE_KEYS` maps `file -> {retired_key, ...}`, NOT `file ->`
"exclude everything" — lead-coder's #4332 review block, caught by an
independent re-measurement of exactly which keys collide per file: all 3
files above collide ONLY on `model` (their own catalog-entry / replay-
config field of that name); a whole-file exclusion silently stops watching
the OTHER 7 denylist keys on that file too. `builtin-models.md` is
precisely the file #4322 had to fix for a `models:` drift the same night
this gate was written — a whole-file exclusion would have meant this gate
never watches that file for that exact recurrence again. A file/key pair
lands here only after the WHOLE file is read and confirmed to contain no
genuine `reyn.yaml`-shaped example anywhere in it for that key (not just
the specific hit line).

## Not a ratchet — a hard gate

Unlike `check_tests_path_literal_reference.py`, this is NOT a baseline
ratchet: a real pre-flight scan of the whole tree (as of #4327, after the
two exclusion classes above) returns ZERO hits. Requiring a baseline would
mean committing an empty one — simpler to just fail on any hit, which also
means a new violation surfaces immediately instead of needing
`--write-baseline` run first to notice it slipped through.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# An editable `reyn` install elsewhere on the machine can otherwise shadow
# THIS repo's own src/ (same class of trap pytest's `pythonpath = ["src"]`
# setting exists to avoid) — insert this repo's src/ first so
# `_retired_keys()` always reads THIS tree's `_RENAMED_CONFIG_KEYS`, not a
# stale one from an unrelated checkout.
sys.path.insert(0, str(_ROOT / "src"))

# Historical-record directories: a retired key legitimately appears here as
# a fact about the past, not present-tense drift. See module docstring
# "Two exclusion classes" §1.
_EXCLUDED_DIR_PREFIXES = (
    "docs/deep-dives/spec/",
    "docs/deep-dives/decisions/",
    "docs/deep-dives/proposals/",
    "docs/deep-dives/journal/",
)

# Different-schema single files: SPECIFIC retired keys in these files are a
# DIFFERENT vocabulary that happens to share that one field name with a
# retired `reyn.yaml` key, never a `reyn.yaml` example itself. See module
# docstring "Two exclusion classes" §2.
#
# Keyed file -> {retired_key, ...}, NOT file -> "exclude everything" —
# lead-coder's #4332 review block: a whole-file exclusion was measured to
# be wider than the actual collision. All 3 files below collide ONLY on
# `model` (their own `{model: ..., max_completion_tokens: ...}` catalog-
# entry / dogfood-replay-config field); a whole-file exclusion would have
# silently stopped watching the other 7 denylist keys on these files too —
# `builtin-models.md` is exactly the file #4322 had to fix for a `models:`
# drift the same night this gate was written, so silently no longer
# watching it for exactly that key would defeat the gate's own purpose.
# Confirm per-key (read the whole file, not just one hit line) before
# adding or widening an entry here.
_EXCLUDED_FILE_KEYS: "dict[str, frozenset[str]]" = {
    "docs/deep-dives/contributing/dogfood-tooling.md": frozenset({"model"}),
    "docs/reference/builtin-models.md": frozenset({"model"}),
    "docs/reference/builtin-models.ja.md": frozenset({"model"}),
}


def _retired_keys() -> "dict[str, str]":
    """The denylist itself: ``{old_top_level_key: note}`` straight from
    ``_RENAMED_CONFIG_KEYS`` — see module docstring "Single source"."""
    from reyn.config.config_schema import _RENAMED_CONFIG_KEYS
    return {key: hint.note for key, hint in _RENAMED_CONFIG_KEYS.items()}


def _pattern(keys: "list[str]") -> "re.Pattern[str]":
    alt = "|".join(re.escape(k) for k in sorted(keys))
    # Column-0 anchor (`^`, no leading whitespace) = top-level YAML mapping
    # key position — see module docstring "Detection: line-START only".
    return re.compile(r"^(" + alt + r"):(\s|$)")


def _iter_scan_files(root: Path = _ROOT):
    """Every tracked `docs/**` file plus the repo's own `*.example` file —
    `git ls-files` so generated/gitignored trees (`site/`, worktrees under
    `.claude/`) are never in scope, with no exclusion list of our own to
    keep in sync (same reasoning as `check_tests_path_literal_reference.py`'s
    own population section)."""
    proc = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True,
    )
    for line in proc.stdout.splitlines():
        rel = Path(line)
        rel_s = str(rel)
        if not (rel_s.startswith("docs/") or rel.name.endswith(".example")):
            continue
        if any(rel_s.startswith(p) for p in _EXCLUDED_DIR_PREFIXES):
            continue
        yield root / rel


def offending_lines(root: Path = _ROOT) -> "list[tuple[Path, int, str, str]]":
    """Every ``(file, line_number, retired_key, note)`` where a retired
    top-level key appears at YAML top-level (column 0) — the gate's entire
    decision, isolated from CLI/printing so it is directly testable."""
    keys = _retired_keys()
    pattern = _pattern(list(keys))
    offenders: list[tuple[Path, int, str, str]] = []
    for path in _iter_scan_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel_s = str(path.relative_to(root))
        excluded_keys = _EXCLUDED_FILE_KEYS.get(rel_s, frozenset())
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = pattern.match(line)
            if not m:
                continue
            key = m.group(1)
            if key in excluded_keys:
                continue
            offenders.append((path, lineno, key, keys[key]))
    return offenders


def main() -> int:
    offenders = offending_lines(_ROOT)

    if not offenders:
        print(
            "retired-config-key denylist OK: 0 hits "
            f"({len(_retired_keys())} retired top-level key(s) checked)."
        )
        return 0

    print("retired-config-key denylist FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} line(s) show a retired top-level reyn.yaml key "
        "still in its old shape:",
        file=sys.stderr,
    )
    for path, lineno, key, note in offenders:
        rel = path.relative_to(_ROOT)
        print(f"  {rel}:{lineno}: `{key}:` — {note}", file=sys.stderr)
    print(
        "\nUpdate the example to the current key location. If this is a "
        "genuine historical record (a dated journal/proposal/spec entry, "
        "or a fenced example for a DIFFERENT config schema that happens to "
        "share a field name), add it to the appropriate exclusion in this "
        "script — see the module docstring's \"Two exclusion classes\" "
        "section for the bar each class has to clear.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
