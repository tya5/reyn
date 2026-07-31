"""rekey_fixtures.py — automate LLMReplay fixture rekey after system-prompt changes.

Detects MissingFixture errors by patching LLMReplay._replay, captures the new
SHA-256 keys, then appends the most-recent existing entry under each new key
(additive — never deletes existing entries).

Usage:
    python scripts/rekey_fixtures.py [--test-pattern GLOB] [--dry-run]

Options:
    --test-pattern  pytest nodeids/glob (default: tests — the whole suite; pass a
                    narrower pattern when you know which tests replay)
    --dry-run       print what would change; do not write files

Exit codes:
    0  the scan ran and either found nothing to do or did it
    1  the scan ran but could not be trusted — see below
    2  usage error (argparse)

#3568: this tool patches a PRIVATE method of another module, so it is exposed to
signature drift, and its failure mode was to report the drift as good news. The
pre-#3473 patch took ``(self, key, model, messages)``; ``_replay`` had since
grown ``observed`` and ``request``, so every patched call raised ``TypeError``,
no ``MISSING_KEY`` line was ever printed, and the tool announced "No missing keys
found — all fixtures up to date." That is "the instrument is dead" reported in
the affirmative. Two mechanisms close it, and the FIRST is the one that matters:

1. **Compatibility is verified before the scan** (:func:`verify_replay_signature`
   against ``REQUIRED_REPLAY_PARAMS``) — in the SUBPROCESS, so it inspects the
   very ``reyn`` the scan measures (the #2103 wrong-import-target hazard), and
   pytest is never started when it fails. The patch body itself forwards
   ``*args``/``**kwargs`` and binds by name, but a forgiving signature ALONE
   would only fail differently and silently: a renamed or reordered parameter
   still passes ``*args`` and then reads the wrong value. The name-and-order
   check is the mechanism; the forwarding is the convenience.
2. **"0 missing keys" and "the patch never fired" are different states, and only
   the first is success.** The patcher reports its call count; a scan that never
   invoked ``_replay`` (bad pattern, collection error, a fixture wired some other
   way) is reported as an error with a non-zero exit, never as "up to date".
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATTERN = "tests"

# The exact parameter list (names AND order) of ``LLMReplay._replay`` that the
# patcher below is written against. It binds the call by NAME, so a rename or a
# reorder is as fatal as an added parameter — hence the whole tuple is compared,
# not its length. Update this in the same change that changes ``_replay``.
REQUIRED_REPLAY_PARAMS = ("self", "key", "model", "messages", "observed", "request")

# Emitted by the patcher subprocess; parsed by the parent.
MARKER_MISSING = "MISSING_KEY="
MARKER_PATCH_ERROR = "REKEY_PATCH_ERROR="
MARKER_CALLS = "REKEY_REPLAY_CALLS="
# Subprocess exit code for "the patch is incompatible, pytest was never started".
EXIT_PATCH_INCOMPATIBLE = 97


def verify_replay_signature(replay_cls: Any) -> str | None:
    """Return ``None`` when ``replay_cls._replay`` matches
    :data:`REQUIRED_REPLAY_PARAMS`, else a human-readable description of the
    incompatibility.

    This is the drift gate. It is deliberately a pure function over a class so
    the same comparison can be made from the patcher subprocess (against the
    ``reyn`` actually under test) and from a test (against the real
    ``LLMReplay``)."""
    actual = tuple(inspect.signature(replay_cls._replay).parameters)
    if actual == REQUIRED_REPLAY_PARAMS:
        return None
    return _describe_incompatibility(list(REQUIRED_REPLAY_PARAMS), list(actual))


def _describe_incompatibility(expected: list[str], actual: list[str]) -> str:
    missing = [p for p in expected if p not in actual]
    added = [p for p in actual if p not in expected]
    detail = []
    if missing:
        detail.append(f"parameters the patch reads but _replay no longer has: {missing}")
    if added:
        detail.append(f"parameters _replay grew that the patch does not read: {added}")
    if not detail:
        detail.append("same parameter names in a DIFFERENT order")
    return (
        "LLMReplay._replay signature is incompatible with this script's patch.\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        "  " + "\n  ".join(detail) + "\n"
        "Nothing was scanned. Update REQUIRED_REPLAY_PARAMS and the patcher in "
        "scripts/rekey_fixtures.py to match, then re-run."
    )


@dataclass
class ScanResult:
    """What one patched-pytest subprocess actually observed.

    ``replay_calls is None`` means the patcher never got to report — it is NOT
    the same as ``0``, and neither is the same as "no missing keys"."""

    missing: list[dict] = field(default_factory=list)
    replay_calls: int | None = None
    patch_error: str | None = None
    returncode: int = 0
    output: str = ""


# ── Step 1: capture new keys by temporarily patching LLMReplay._replay ────────


def _capture_new_keys(test_pattern: str) -> ScanResult:
    """Re-run pytest with LLMReplay._replay patched to emit the missing key.

    Each MissingFixture raises after printing a ``MISSING_KEY=<json>`` line. We
    capture stdout/stderr and parse those lines, plus the patcher's own
    self-report (``REKEY_PATCH_ERROR=`` / ``REKEY_REPLAY_CALLS=``) that says
    whether the instrument was alive at all.
    """
    import subprocess
    import textwrap

    # Inject a sitecustomize-style pth or use PYTHONPATH + sitecustomize.
    # Simpler: pass a conftest plugin via --co ... actually the cleanest is
    # a small wrapper script that installs the patch before importing pytest.

    patcher_code = textwrap.dedent(f"""\
        import atexit
        import inspect
        import json
        import sys
        from reyn.dev.testing.replay import LLMReplay

        REQUIRED = {tuple(REQUIRED_REPLAY_PARAMS)!r}

        _orig_replay = LLMReplay._replay
        _sig = inspect.signature(_orig_replay)
        _actual = tuple(_sig.parameters)
        if _actual != REQUIRED:
            # Refuse to run. A patch that cannot bind produces zero MISSING_KEY
            # lines, which is indistinguishable from "everything is fine" once
            # pytest's own failure output is discarded by --tb=no (#3568).
            print(
                {MARKER_PATCH_ERROR!r} + json.dumps({{"actual": list(_actual)}}),
                flush=True,
            )
            sys.exit({EXIT_PATCH_INCOMPATIBLE})

        _calls = [0]

        def _patched_replay(self, *args, **kwargs):
            # Forward-compatible call shape, but read the arguments BY NAME via
            # the real signature — positional forwarding alone would happily
            # hand a renamed/reordered parameter to the wrong reader.
            _calls[0] += 1
            bound = _sig.bind(self, *args, **kwargs)
            bound.apply_defaults()
            arg = bound.arguments
            key = arg["key"]
            messages = arg["messages"]
            if key not in self._records:
                preview = self._prompt_preview(messages)
                # #3473: carry the live environment imprint and the per-component
                # fingerprint of THIS request, so the rekeyed entry has the same
                # shape a real record-mode entry has. Without them a replayed
                # entry is "precondition unverifiable" on any machine whose
                # environment is non-empty, and a future miss cannot be attributed.
                try:
                    from reyn.dev.testing.replay_key_diff import fingerprint
                    request = arg["request"]
                    components = fingerprint(
                        request.model, request.messages, request.tools,
                        request.tool_choice,
                    )
                except Exception:
                    components = None
                observed = arg["observed"]
                # Emit a machine-readable JSON line BEFORE raising. JSON keeps it
                # single-line + delimiter-safe: a preview or path may contain
                # newlines or '|' (the old f-string |-split form truncated those).
                print(
                    {MARKER_MISSING!r} + json.dumps({{
                        "new_key": key,
                        "fixture_path": str(self.fixture_path),
                        "prompt_preview": preview[:200],
                        "preconditions": observed if isinstance(observed, dict) else None,
                        "key_components": components,
                    }}),
                    flush=True,
                )
            return _orig_replay(*bound.args, **bound.kwargs)

        LLMReplay._replay = _patched_replay
        atexit.register(
            lambda: print({MARKER_CALLS!r} + str(_calls[0]), flush=True)
        )

        import pytest
        sys.exit(pytest.main(sys.argv[1:]))
    """)

    patcher_path = REPO_ROOT / "tmp" / "_rekey_patcher.py"
    patcher_path.parent.mkdir(exist_ok=True)
    patcher_path.write_text(patcher_code, encoding="utf-8")

    try:
        cmd = [
            sys.executable, str(patcher_path),
            test_pattern,
            # `-s` (= --capture=no) is MANDATORY: without it pytest captures the
            # patcher's MISSING_KEY print per-test and `--tb=no -q` never surfaces
            # it, so the scan finds nothing and silently no-ops (#2024 bug 1).
            "-s", "--tb=no", "--no-header", "-q",
        ]
        # #2103: PYTHONPATH the subprocess to THIS worktree's src so the patcher
        # imports the worktree's ``reyn`` (= the code under test, with the local
        # changes that cause the rot) and NOT a global editable-install pointing at a
        # DIFFERENT checkout. Without this the subprocess imports the install's reyn →
        # no local change → no rot → "finds no missing keys" silently no-ops (the
        # #1092-class wrong-import-target footgun — the scan must measure the SAME tree
        # pytest-from-rootdir does).
        env = {**os.environ}
        _src = REPO_ROOT / "src"
        if _src.is_dir():
            env["PYTHONPATH"] = (
                f"{_src}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(_src)
            )
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
        )
        output = result.stdout + result.stderr
        return ScanResult(
            missing=_parse_missing_keys(output),
            replay_calls=parse_replay_calls(output),
            patch_error=parse_patch_error(output),
            returncode=result.returncode,
            output=output,
        )
    finally:
        patcher_path.unlink(missing_ok=True)


def parse_patch_error(output: str) -> str | None:
    """Return the rendered incompatibility report if the patcher refused to run."""
    for line in output.splitlines():
        idx = line.find(MARKER_PATCH_ERROR)
        if idx < 0:
            continue
        try:
            rec = json.loads(line[idx + len(MARKER_PATCH_ERROR):])
        except json.JSONDecodeError:
            continue
        return _describe_incompatibility(
            list(REQUIRED_REPLAY_PARAMS), list(rec.get("actual", [])),
        )
    return None


def parse_replay_calls(output: str) -> int | None:
    """Return how many times the patched ``_replay`` ran, or ``None`` if the
    patcher never reported (it did not reach exit — the scan measured nothing)."""
    found: int | None = None
    for line in output.splitlines():
        idx = line.find(MARKER_CALLS)
        if idx < 0:
            continue
        try:
            found = int(line[idx + len(MARKER_CALLS):].strip())
        except ValueError:
            continue
    return found


def diagnose_scan(result: ScanResult) -> str | None:
    """Return ``None`` when the scan can be believed, else why it cannot.

    #3568: this is the separation between "0 anomalies" and "0 observations".
    Only the former may be reported as success."""
    if result.patch_error:
        return result.patch_error
    if result.replay_calls is None:
        return (
            "The patcher never reported its call count — it did not reach exit, "
            "so the scan measured nothing.\n"
            f"pytest subprocess exit code: {result.returncode}\n"
            "This is NOT 'all fixtures up to date'. Re-run the pattern under "
            "pytest directly to see what failed."
        )
    if result.replay_calls == 0:
        return (
            "The patch never fired: LLMReplay._replay was called 0 times by this "
            "pattern.\n"
            f"pytest subprocess exit code: {result.returncode}\n"
            "This is NOT 'all fixtures up to date' — nothing was observed. Check "
            "that --test-pattern selects tests that actually replay."
        )
    return None


def _parse_missing_keys(output: str) -> list[dict]:
    """Parse ``MISSING_KEY=<json>`` lines from the patcher subprocess output.

    Each line is ``MISSING_KEY=`` followed by a JSON object
    ``{new_key, fixture_path, prompt_preview, preconditions, key_components}``
    (the last two added by #3568 so the written entry can have a record-mode
    shape). JSON-encoding makes the line newline-/delimiter-safe (a preview or
    path may contain ``|`` or a newline — the old ``|``-split + raw f-string form
    truncated the preview at the first newline, #2024 bug 1). The marker is
    matched anywhere in the line (pytest may prefix it), and results are deduped
    by ``(new_key, fixture_path)``."""
    marker = MARKER_MISSING
    captured: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in output.splitlines():
        idx = line.find(marker)
        if idx < 0:
            continue
        try:
            rec = json.loads(line[idx + len(marker):])
        except json.JSONDecodeError:
            continue
        new_key = rec.get("new_key")
        fixture_path = rec.get("fixture_path")
        if not new_key or not fixture_path:
            continue
        dedup = (new_key, fixture_path)
        if dedup in seen:
            continue
        seen.add(dedup)
        preconditions = rec.get("preconditions")
        components = rec.get("key_components")
        captured.append({
            "new_key": new_key,
            "fixture_path": Path(fixture_path),
            "prompt_preview": rec.get("prompt_preview", ""),
            # #3568/#3473: absent on a line from an older patcher — kept as None
            # so the writer can fall back rather than write an empty imprint,
            # which means something else (see LLMReplay._check_preconditions).
            "preconditions": preconditions if isinstance(preconditions, dict) else None,
            "key_components": components if isinstance(components, dict) else None,
        })
    return captured


# ── Step 3: load fixture, find latest entry, append under new key ──────────────


def _load_entries(fixture_path: Path) -> list[dict]:
    if not fixture_path.exists():
        return []
    entries = []
    for line in fixture_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


def _rekey_fixture(
    fixture_path: Path,
    new_key: str,
    prompt_preview: str,
    dry_run: bool,
    preconditions: dict | None = None,
    key_components: dict | None = None,
) -> bool:
    """Append a new entry for ``new_key`` reusing the response of the EXISTING
    entry whose ``prompt_preview`` matches.

    A re-key happens when a system-prompt change shifts the SHA key of an
    otherwise-identical request. The fixture's ``prompt_preview`` is the last
    message's content, which is stable across SP changes — so the new key's
    captured preview matches the original entry's preview exactly, and that
    entry's response is the correct one to reuse.

    Reusing the LAST entry unconditionally (the old behavior) corrupts
    multi-round fixtures: every re-keyed round would get the final round's
    response (#2024 bug 2). On an ambiguous match (several entries share a
    preview) the most-recent match is reused (tie-break); on NO match the rekey
    is skipped with a warning — never write an unjustified response.

    #3568/#3473: the written entry must have the same SHAPE a record-mode entry
    has, not just a key and a response. ``preconditions`` (the live environment
    imprint observed on the missing call) and ``key_components`` (that call's
    per-component fingerprint) come from the patcher; ``kind`` follows the
    source entry. Omitting ``preconditions`` makes the entry "precondition
    unverifiable" on any machine whose environment is non-empty, and omitting
    ``key_components`` leaves a future miss with no attribution. The fingerprint
    is NOT copied from the source entry when the patcher did not supply one — a
    stale fingerprint would attribute the next miss to the wrong component,
    which is worse than having none.

    Returns True if a rekey was performed (or would be in dry-run).
    """
    entries = _load_entries(fixture_path)
    if not entries:
        print(f"  [WARN] {fixture_path} is empty or missing — skip", file=sys.stderr)
        return False

    if any(e.get("key") == new_key for e in entries):
        print(f"  [SKIP] key already present: {new_key[:16]}... in {fixture_path.name}")
        return False

    # Preview-match: the existing entry(ies) for the same logical request. The
    # last among matches is the most-recent recording (tie-break for duplicates).
    matches = [e for e in entries if e.get("prompt_preview", "") == prompt_preview]
    if not matches:
        print(
            f"  [WARN] no prompt_preview match for {new_key[:16]}... in "
            f"{fixture_path.name} — skip (manual rekey needed; not reusing an "
            f"unrelated response)",
            file=sys.stderr,
        )
        return False
    source = matches[-1]

    new_entry: dict[str, Any] = {
        "key": new_key,
        "kind": source.get("kind", "completion"),
        "model": source.get("model", ""),
        "prompt_preview": prompt_preview or source.get("prompt_preview", ""),
    }
    imprint = preconditions if preconditions is not None else source.get("preconditions")
    if isinstance(imprint, dict):
        new_entry["preconditions"] = imprint
    if isinstance(key_components, dict):
        new_entry["key_components"] = key_components
    new_entry["response"] = source["response"]

    if dry_run:
        print(
            f"  [DRY-RUN] {fixture_path.name}\n"
            f"    matched key: {source['key'][:16]}...\n"
            f"    new key: {new_key[:16]}..."
        )
        return True

    with fixture_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(new_entry, ensure_ascii=False) + "\n")
    print(f"  [REKEY] {fixture_path.name}: +{new_key[:16]}... (matched {source['key'][:16]}...)")
    return True


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-pattern",
        default=DEFAULT_PATTERN,
        help="pytest nodeids or glob (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change; do not write files",
    )
    args = parser.parse_args()

    print(f"Scanning: {args.test_pattern}")
    result = _capture_new_keys(args.test_pattern)

    # #3568: believe the scan only after it has said it observed something.
    problem = diagnose_scan(result)
    if problem is not None:
        print(f"[ERROR] {problem}", file=sys.stderr)
        return 1

    missing = result.missing
    if not missing:
        print(
            "No missing keys found — all fixtures up to date "
            f"({result.replay_calls} replay call(s) observed)."
        )
        return 0

    print(f"Found {len(missing)} missing key(s):")
    changed = 0
    for item in missing:
        print(f"  fixture: {item['fixture_path'].name}, key: {item['new_key'][:16]}...")
        ok = _rekey_fixture(
            fixture_path=item["fixture_path"],
            new_key=item["new_key"],
            prompt_preview=item["prompt_preview"],
            dry_run=args.dry_run,
            preconditions=item.get("preconditions"),
            key_components=item.get("key_components"),
        )
        if ok:
            changed += 1

    if args.dry_run:
        print(f"\n[DRY-RUN] Would rekey {changed} entry/entries. No files changed.")
    else:
        print(f"\nRekeyed {changed} entry/entries.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
