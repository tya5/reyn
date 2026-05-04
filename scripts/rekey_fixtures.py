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
        import sys
        from pathlib import Path
        from reyn.testing.replay import LLMReplay, MissingFixture

        _orig_replay = LLMReplay._replay

        def _patched_replay(self, key, model, messages):
            if key not in self._records:
                preview = self._prompt_preview(messages)
                # Emit machine-readable line BEFORE raising
                print(
                    f"MISSING_KEY={key}|{self.fixture_path}|{preview[:200]}",
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
            "-v", "--tb=no", "--no-header", "-q",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(REPO_ROOT)
        )
        output = result.stdout + result.stderr

        captured: list[dict] = []
        seen: set[tuple] = set()
        for line in output.splitlines():
            if not line.startswith("MISSING_KEY="):
                continue
            rest = line[len("MISSING_KEY="):]
            parts = rest.split("|", 2)
            if len(parts) < 2:
                continue
            new_key = parts[0]
            fixture_path = Path(parts[1])
            prompt_preview = parts[2] if len(parts) > 2 else ""
            dedup = (new_key, str(fixture_path))
            if dedup not in seen:
                seen.add(dedup)
                captured.append({
                    "new_key": new_key,
                    "fixture_path": fixture_path,
                    "prompt_preview": prompt_preview,
                })
        return captured
    finally:
        patcher_path.unlink(missing_ok=True)


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
    """Append a new entry for new_key reusing the last entry's response.

    Returns True if a rekey was performed (or would be in dry-run).
    """
    entries = _load_entries(fixture_path)
    if not entries:
        print(f"  [WARN] {fixture_path} is empty or missing — skip", file=sys.stderr)
        return False

    if any(e.get("key") == new_key for e in entries):
        print(f"  [SKIP] key already present: {new_key[:16]}... in {fixture_path.name}")
        return False

    # Use the last entry's response (most recent recorded state)
    last_entry = entries[-1]
    new_entry = {
        "key": new_key,
        "model": last_entry.get("model", ""),
        "prompt_preview": prompt_preview or last_entry.get("prompt_preview", ""),
        "response": last_entry["response"],
    }

    if dry_run:
        old_key = last_entry["key"]
        print(
            f"  [DRY-RUN] {fixture_path.name}\n"
            f"    old key: {old_key[:16]}...\n"
            f"    new key: {new_key[:16]}..."
        )
        return True

    with fixture_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(new_entry, ensure_ascii=False) + "\n")
    print(f"  [REKEY] {fixture_path.name}: +{new_key[:16]}...")
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
