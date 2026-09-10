#!/usr/bin/env python3
"""#6084 — a ratchet over user-facing-text sink call sites carrying an
undecided (Japanese-literal) string, under `src/reyn/`.

## Scope widened from `src/reyn/interfaces/` to `src/reyn/` (hole ⑶)

#6104's CI caught a real user-facing duplicate OUTSIDE the original
`src/reyn/interfaces/` scope (`src/reyn/runtime/lifecycle_forwarder.py`,
fixed by #6106/#6114) — this gate's own "0 findings" never saw it, because
the file was never in `_iter_scan_files`'s population at all, not because
its content had no kana. lead-coder's own #6084 hole ⑶ measurement
(e2e-coder, `#6084` issue thread comment): the correct discriminator for
this gate was ALWAYS "does a literal reach one of `_SINK_SPECS`'s sink
calls", never "which directory is it in" — a `path`-only scope restriction
was an accident of the gate's own first draft, not a deliberate boundary.
Widening the scanned PATH to all of `src/reyn/` does not loosen the
discriminator (`_SINK_SPECS` is unchanged) — it only lets that SAME
discriminator see more of the tree. `scripts/`/`docs/`/`tests/` remain
OUT of scope — `tests/` in particular carries this gate's own fixture
files, which exist specifically to contain unflagged/flagged literals for
the test suite and must never feed the real baseline.

## What this gate is, precisely — read lead-coder's ruling on #6084 first

lead-coder's #6084 ruling (quoted, not re-derived here): this gate's job is
NOT "delete the existing ~89 findings" — it is "stop the NEXT one after the
89th." A CONTRIBUTOR ADDING A NEW SINK CALL WRITES THE LITERAL AT THE CALL
SITE, so the range this gate can see (a literal string argument, directly at
a named sink's call site) IS the range where a new violation would land.

## Scope: named sinks only — this is NOT full population coverage

#6084's own investigation comment (file:line evidence, read it before
touching this script) established that the population of "text a user sees"
does NOT reduce to a single AST pattern: a literal can reach a sink through a
dict lookup, another function's return value, or several hops of indirection
(concrete examples: `compaction_progress.py:154158` — a dict-literal lookup
several lines above the f-string that actually renders; `compaction_progress.
py:258261` — a 3-hop dict-return through `compaction_failure_text()`;
`voice.py:8391` — a same-shape indirection, in English, showing the pattern
is language-neutral). None of those are IN SCOPE here. **This script gates
ONLY these sinks, and only when the argument is a string literal (or an
f-string whose literal segments are checked) written directly at the call
site**:

- `OutboxMessage(text=...)` — direct construction (keyword `text`)
- `reply(ctx, text, ...)` / `reply_error(ctx, text)` — the slash-reply
  helpers (`src/reyn/interfaces/slash/__init__.py`); `text` is their second
  POSITIONAL argument
- `argparse` `.add_argument(help=...)` and `.add_parser(..., help=...,
  description=...)`
- `DrawerRow(label=..., note=..., state=...)` — the custom-widget kwargs
  that reach the operator (`src/reyn/interfaces/inline/textual_chat/
  chrome.py`). #6084 gate-hole ⑴ (lead-coder): the ORIGINAL sink spec here
  registered only `label` — `note` (and `state`) were the same shape,
  never registered, and a real kana literal (`chrome.py:1223/1225`) sat
  unflagged from this gate's own first day. `_SINK_SPECS["DrawerRow"]`
  now covers every kwarg :class:`~reyn.interfaces.inline.textual_chat.
  chrome.DrawerRow`'s OWN `text` property (the class's single declared
  "the row as the pane renders it" — chrome.py's own docstring) actually
  reads via `self.<field>` — `command` is deliberately excluded (never
  referenced there; it is a slash-command identifier the pane renders
  elsewhere, not display prose). `tests/scripts/test_user_facing_lang_
  gate_6084.py`'s own render-property cross-check is what keeps this from
  silently falling behind DrawerRow's own future fields again: it derives
  the "actually rendered" kwarg set from `text`'s AST and fails if this
  spec and that derived set ever diverge, in EITHER direction.

Anywhere else in `interfaces/` a user-facing string might originate — a
different custom widget's kwarg not in this list, `App.notify(...)` (whose
argument is typically a function call, not a literal, per the investigation),
`interfaces/repl/` renderers beyond the one `OutboxMessage(...)` call the
investigation actually walked — is explicitly UNSCANNED. Extending the sink
list is a deliberate, separate decision (add the name to `_SINK_SPECS` below
and regenerate the baseline), never assumed by this docstring.

**Never claim this "eventually catches everything."** It catches a NEW
literal Japanese string landed directly at one of the five sink shapes
above. Nothing else.

## Detection basis: kana only, matching the investigation's own AST count

The investigation's own from-scratch AST scanner (its own disclosed method,
not this gate re-deriving it) used the kana range (`぀`-`ヿ`,
hiragana + katakana) to reach its committed 89-finding/28-file number, and
explicitly found that widening to CJK ideographs (`一`-`鿿`) added
only comment/docstring hits, never a string-literal hit at one of these
sinks. This gate matches that choice — kana only — so its own baseline count
is comparable to the investigation's, rather than diverging on a range
neither of us has re-justified.

## Indirection at the ARGUMENT is out of scope, not the sink

If the extracted argument node is not a plain string literal (`ast.Constant`
str) or an f-string (`ast.JoinedStr`, checked only in its literal segments;
a `{expr}` placeholder is not inspected) — e.g. a `Name`, `Attribute`,
`Subscript`, or another `Call` — this script skips that call site entirely.
It does NOT walk further to resolve what the name/lookup/call eventually
returns. That is the exact indirection class ruling 1 puts out of scope.

## Exception: a deliberate i18n pair, by STRUCTURE, never a name/file list

`web/routers/web_data.py`'s `_COPY_EN`/`_COPY_JA` pair is a ALREADY-DECIDED
i18n design (a locale-keyed `{"en": _COPY_EN, "ja": _COPY_JA}` table), not an
instance of "no decision was made" — ruling 3 requires this be excluded by a
MECHANICAL condition, never a hardcoded path or variable-name list (a list
means the next new intentional-i18n pair silently isn't recognised until a
human remembers to add it by hand).

The condition implemented here: **a module-level assignment target whose
name ends in `_EN` (or `_JA`) is exempt, together with everything nested
inside its value expression, if — and only if — the SAME module also has a
module-level assignment target with the identical prefix and the OTHER
suffix** (`_EN`⟷`_JA`). This is checked purely from each file's own AST
(`_collect_i18n_pair_names`) — no name is special-cased, no path is named;
any future `_FOO_EN = {...}` / `_FOO_JA = {...}` pair in ANY file under
scope is recognised the same way, automatically, the day it is written.

## Baseline semantics — same contract as `silent_except_ratchet.py`

The committed baseline is whatever THIS script's own AST logic measures
against the tree TODAY — not a hand-transcription of the investigation's 89.
The two are expected to be close (the investigation's own sink survey is
what this script's `_SINK_SPECS` encodes) but not necessarily identical: the
investigation's scanner and this one are independently written, and the
investigation's own AST tool explicitly did not attempt to check for the
i18n-pair exception at all (that exception was one of THIS gate's rulings).

CI: gate
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "user_facing_lang_gate_baseline.json"
_SCOPE = "src/reyn"

# Kana (hiragana + katakana) only — see module docstring's "Detection
# basis" section for why this deliberately does not widen to CJK
# ideographs.
_KANA_RE = re.compile(r"[぀-ヿ]")

# Sink name -> list of ("kwarg", name) | ("pos", index) argument specs.
# Each entry is a confirmed sink from #6084's investigation comment — see
# module docstring's Scope section. Extending this is a deliberate,
# separate decision (regenerate the baseline in the same PR).
_SINK_SPECS: "dict[str, list[tuple[str, object]]]" = {
    "OutboxMessage": [("kwarg", "text")],
    "reply": [("pos", 1)],
    "reply_error": [("pos", 1)],
    "add_argument": [("kwarg", "help")],
    "add_parser": [("kwarg", "help"), ("kwarg", "description")],
    "DrawerRow": [("kwarg", "label"), ("kwarg", "note"), ("kwarg", "state")],
}


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked `.py` file under `_SCOPE` (`src/reyn/`) — same
    population source (`git ls-files`) `silent_except_ratchet.py` uses,
    for the same reason: no hand-maintained exclusion list."""
    proc = subprocess.run(
        ["git", "ls-files", "--", f"{_SCOPE}/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def _call_target_name(node: ast.Call) -> "str | None":
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _extract_arg(node: ast.Call, spec: "tuple[str, object]") -> "ast.expr | None":
    kind, key = spec
    if kind == "kwarg":
        for kw in node.keywords:
            if kw.arg == key:
                return kw.value
        return None
    if kind == "pos":
        idx = key
        assert isinstance(idx, int)
        if len(node.args) > idx:
            return node.args[idx]
        return None
    return None


def _literal_text(node: ast.expr) -> "str | None":
    """The literal string content of *node*, or ``None`` if *node* is not a
    plain string literal or an f-string with only-literal segments checked.
    A `{expr}` placeholder inside an f-string is not inspected — only the
    literal (`ast.Constant`) segments of the `ast.JoinedStr` are joined."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts)
    return None


def _collect_i18n_pair_exempt_ids(tree: ast.Module) -> "set[int]":
    """`id()` of every AST node nested inside a module-level assignment
    whose target name ends in `_EN`/`_JA` AND has a same-prefix sibling
    with the other suffix, also at module level. See module docstring's
    "Exception" section — this is the structural condition, not a name or
    file list."""
    top_level_names: "set[str]" = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    top_level_names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top_level_names.add(node.target.id)

    def has_sibling(name: str) -> bool:
        if name.endswith("_EN"):
            return (name[: -len("_EN")] + "_JA") in top_level_names
        if name.endswith("_JA"):
            return (name[: -len("_JA")] + "_EN") in top_level_names
        return False

    exempt_ids: "set[int]" = set()
    for node in tree.body:
        targets: "list[ast.expr]" = []
        value: "ast.expr | None" = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target] if node.target else [], node.value
        if value is None:
            continue
        for tgt in targets:
            if isinstance(tgt, ast.Name) and has_sibling(tgt.id):
                for descendant in ast.walk(value):
                    exempt_ids.add(id(descendant))
                break
    return exempt_ids


def findings(path: Path, display: "str | None" = None) -> "list[str]":
    """`["display:lineno:sink_name", ...]` for every sink call site in
    *path* whose extracted argument is a direct string literal (or
    f-string literal segment) containing kana, and is not inside an
    i18n-pair exemption. *display* is the path string used in the key
    (defaults to `str(path)`) — `measured` passes the repo-relative form so
    baseline keys stay portable across checkouts. Raises on a parse
    failure — see `measured`'s fail-closed note; propagated the same way
    `silent_except_ratchet.py` propagates one, for the same reason (a
    silent under-count here would be the defect this gate exists to
    catch, recurring one layer up)."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    exempt_ids = _collect_i18n_pair_exempt_ids(tree)
    shown = display if display is not None else str(path)
    out: "list[str]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_target_name(node)
        if name is None or name not in _SINK_SPECS:
            continue
        for spec in _SINK_SPECS[name]:
            arg = _extract_arg(node, spec)
            if arg is None or id(arg) in exempt_ids:
                continue
            literal = _literal_text(arg)
            if literal is None:
                continue
            if _KANA_RE.search(literal):
                out.append(f"{shown}:{node.lineno}:{name}")
    return out


def sink_call_count(path: Path) -> int:
    """Count of ``(sink call, registered arg position)`` matches in *path*
    whose argument is PRESENT — regardless of whether it turns out to be a
    literal, or contains kana. #6084 hole ⑷ (lead-coder): a gate that
    prints only "0 findings" lets a reader believe no user-facing sink
    exists anywhere in scope — the true claim is narrower ("no sink CALL
    this gate can see carries kana"). This count names how many sink
    calls the scan actually looked at, so the output can say which."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_target_name(node)
        if name is None or name not in _SINK_SPECS:
            continue
        for spec in _SINK_SPECS[name]:
            if _extract_arg(node, spec) is not None:
                count += 1
    return count


def measured(root: Path = _ROOT) -> "tuple[set[str], int, int]":
    """`(finding_set, scanned_file_count, sink_call_count)`. A file that
    fails to parse is NOT caught here — it propagates to `main`, same
    fail-closed contract `silent_except_ratchet.py` documents (this scan's
    own under-count would be the exact defect this gate exists to
    prevent, one layer up)."""
    files = _iter_scan_files(root)
    all_findings: "set[str]" = set()
    total_sink_calls = 0
    for path in files:
        rel = str(path.relative_to(root))
        all_findings.update(findings(path, display=rel))
        total_sink_calls += sink_call_count(path)
    return (all_findings, len(files), total_sink_calls)


def load_baseline(path: Path = _BASELINE_PATH) -> "set[str]":
    return set(json.loads(path.read_text(encoding="utf-8")))


def write_baseline(finding_set: "set[str]", path: Path = _BASELINE_PATH) -> None:
    path.write_text(json.dumps(sorted(finding_set), indent=2) + "\n", encoding="utf-8")


def new_findings(measured_set: "set[str]", baseline_set: "set[str]") -> "set[str]":
    """The ratchet check: a measured finding absent from baseline is new
    debt. A baselined finding that disappears from `measured` (the file's
    literal was fixed, or the call site removed) is silently allowed to
    drop — the same silent-shrink contract every ratchet here shares."""
    return measured_set - baseline_set


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from the CURRENT measured findings "
            "instead of checking against it. Use for initial adoption, a "
            "real fix you want to lock in, or a deliberate reviewed new "
            "sink-literal (say why in the PR body) — no comment-based "
            "exception escape hatch exists."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        current, scanned, sink_calls = measured(_ROOT)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(
            f"user-facing-lang gate FAILED: could not scan the population "
            f"({exc}). This gate fails CLOSED on a scan error rather than "
            f"silently under-counting — fix the scan (or the file it "
            f"choked on), do not treat this as a pass.",
            file=sys.stderr,
        )
        return 1

    if scanned == 0:
        print(
            f"user-facing-lang gate FAILED: the scan found 0 files under "
            f"{_SCOPE}/ — this is a scanner failure, not a clean "
            "population; `git ls-files` returned nothing.",
            file=sys.stderr,
        )
        return 1

    if args.write_baseline:
        write_baseline(current, _BASELINE_PATH)
        print(
            f"Wrote {len(current)} user-facing-lang finding(s) to "
            f"{_BASELINE_PATH} — scanned {scanned} file(s) under {_SCOPE}/, "
            f"{sink_calls} sink call site(s)."
        )
        return 0

    baseline = load_baseline(_BASELINE_PATH)
    new = new_findings(current, baseline)

    if new:
        print("user-facing-lang gate FAILED:\n", file=sys.stderr)
        print(
            f"{len(new)} new sink call site(s) carry an undecided "
            f"(kana-containing) literal, not in the baseline "
            f"({_BASELINE_PATH.relative_to(_ROOT)}):",
            file=sys.stderr,
        )
        for f in sorted(new):
            print(f"  {f}", file=sys.stderr)
        print(
            "\nA literal string at one of this gate's confirmed sinks "
            "(OutboxMessage(text=), reply()/reply_error(), argparse "
            "help=/description=, DrawerRow(label=)) contains kana — see "
            "scripts/user_facing_lang_gate.py's own module docstring for "
            "exactly what this does and does not catch, and for the "
            "existing ~89 grandfathered findings' baseline. This gate does "
            "NOT enforce a target language; it only blocks a NEW site of "
            "this specific shape from landing unreviewed. If this is a "
            "deliberate, reviewed addition, say so in the PR body and run "
            "--write-baseline.",
            file=sys.stderr,
        )
        return 1

    print(
        f"user-facing-lang gate OK: scanned {sink_calls} sink call site(s) "
        f"across {scanned} file(s) under {_SCOPE}/, {len(current)} "
        f"finding(s) (all baselined). This is NOT \"no user-facing kana "
        f"anywhere\" — it is \"none of the {sink_calls} sink calls this "
        "gate can see, in this scope, carry one\"; see module docstring "
        "for exactly which sinks/scope that is."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
