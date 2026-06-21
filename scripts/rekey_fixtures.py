"""rekey_fixtures.py — automate LLMReplay fixture rekey after system-prompt changes.

Detects MissingFixture errors by patching LLMReplay._replay, captures the new
SHA-256 keys, then appends the most-recent existing entry under each new key
(additive — never deletes existing entries).

Usage:
    python scripts/rekey_fixtures.py [--test-pattern GLOB] [--dry-run]

Options:
    --test-pattern  pytest nodeids/glob (default: tests/test_replay_skill_router.py)
    --dry-run       print what would change; do not write files
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATTERN = "tests/test_replay_skill_router.py"


# ── Step 1: capture new keys by temporarily patching LLMReplay._replay ────────


def _capture_new_keys(test_pattern: str) -> list[dict]:
    """Re-run pytest with LLMReplay._replay patched to emit the missing key.

    Each MissingFixture raises after printing:  MISSING_KEY=<hash>|<fixture>|<preview>
    We capture stdout/stderr and parse those lines.
    """
    import subprocess
    import textwrap

    # Inject a sitecustomize-style pth or use PYTHONPATH + sitecustomize.
    # Simpler: pass a conftest plugin via --co ... actually the cleanest is
    # a small wrapper script that installs the patch before importing pytest.

    patcher_code = textwrap.dedent("""\
        import json
        import sys
        from pathlib import Path
        from reyn.dev.testing.replay import LLMReplay, MissingFixture

        _orig_replay = LLMReplay._replay

        def _patched_replay(self, key, model, messages):
            if key not in self._records:
                preview = self._prompt_preview(messages)
                # Emit a machine-readable JSON line BEFORE raising. JSON keeps it
                # single-line + delimiter-safe: a preview or path may contain
                # newlines or '|' (the old f-string |-split form truncated those).
                print(
                    "MISSING_KEY=" + json.dumps({
                        "new_key": key,
                        "fixture_path": str(self.fixture_path),
                        "prompt_preview": preview[:200],
                    }),
                    flush=True,
                )
            return _orig_replay(self, key, model, messages)

        LLMReplay._replay = _patched_replay

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
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(REPO_ROOT)
        )
        return _parse_missing_keys(result.stdout + result.stderr)
    finally:
        patcher_path.unlink(missing_ok=True)


def _parse_missing_keys(output: str) -> list[dict]:
    """Parse ``MISSING_KEY=<json>`` lines from the patcher subprocess output.

    Each line is ``MISSING_KEY=`` followed by a JSON object
    ``{new_key, fixture_path, prompt_preview}``. JSON-encoding makes the line
    newline-/delimiter-safe (a preview or path may contain ``|`` or a newline —
    the old ``|``-split + raw f-string form truncated the preview at the first
    newline, #2024 bug 1). The marker is matched anywhere in the line (pytest may
    prefix it), and results are deduped by ``(new_key, fixture_path)``."""
    marker = "MISSING_KEY="
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
        captured.append({
            "new_key": new_key,
            "fixture_path": Path(fixture_path),
            "prompt_preview": rec.get("prompt_preview", ""),
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

    new_entry = {
        "key": new_key,
        "model": source.get("model", ""),
        "prompt_preview": prompt_preview or source.get("prompt_preview", ""),
        "response": source["response"],
    }

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
    missing = _capture_new_keys(args.test_pattern)

    if not missing:
        print("No missing keys found — all fixtures up to date.")
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
