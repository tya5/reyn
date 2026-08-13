"""Automatic purge of `.reyn/events` files (#4479).

Distinct from `retention.py` (ADR-0038's WAL/generation retention window for
reconstruct/rewind) — this module deletes closed audit-event `.jsonl` files
from disk; it has nothing to do with how far back a session can reconstruct.
Naming them both "retention" would be exactly the kind of conflation
CLAUDE.md's cross-cutting-band note bans for "event" — kept apart on
purpose. See `AuditEventsConfig`'s own docstring (config/infra.py) for the
config-facing name of each knob.

**Two independent axes, either fires deletion** — owner ruling (2026-08-13):
"規定は日数orサイズで削除" (age OR size, whichever is touched first — `and`
would mean disabling one axis silently disables the other too, which the
owner's own wording rules out).

- **age** (`max_age_days`, default 30): files whose filename start-date is
  older than `today - max_age_days` are purge targets. The default is
  practice, not measurement — architect's survey (#4479) found no local-CLI
  precedent measuring `.reyn/events`'s own growth rate; the closest
  comparable tool (Claude Code, another local-agent CLI) defaults its own
  `cleanupPeriodDays` to 30, and this borrows that number as a starting
  convention, not a derived one.
- **size** (`max_disk_usage_percent`, default 10): purge oldest-first once
  the events directory's own total size exceeds `max_disk_usage_percent`%
  of the filesystem's CURRENT free space. Relative, not absolute — an
  absolute byte ceiling would need reyn's own measured growth rate to set
  safely (unmeasured, per #4479), while a relative ceiling needs no such
  measurement to have a defensible default. 10% borrows systemd-journald's
  `SystemMaxUse` convention for the identical reason (its docs use the same
  device-relative-percentage shape) — again a borrowed convention, not a
  reyn-specific derivation.

**0 disables that axis** (owner ruling — explicit reversal of this same
config's earlier historical stance, which REJECTED 0 as a footgun; the
owner's ruling for THIS feature instead has each 0's meaning documented
plainly, which is what this docstring + `AuditEventsConfig`'s own docstring
do — Claude Code carries an open report of its OWN `cleanupPeriodDays` knob
being ambiguous about what 0 means, and this is the precedent being
deliberately not repeated).

The file-selection + deletion logic here is the ONE code path both `reyn
events purge` (`interfaces/cli/commands/events.py`) and the automatic
trigger (this module, called from `EventStore`) use — the CLI command
imports from here, never the reverse, so there is exactly one place that
decides "which files are purge targets" and one place that deletes them
(owner/lead-coder: "削除する主体を2つにしない")."""
from __future__ import annotations

import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

_FILENAME_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T(\d{6}))?")


def filename_start_date(name: str) -> date | None:
    """Parse a `.jsonl` filename's leading `YYYY-MM-DD` — `None` if the name
    doesn't carry a recognizable date (e.g. a manually dropped-in file)."""
    m = _FILENAME_TS_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def collect_dated_files(root: Path) -> list[tuple[Path, date | None]]:
    """Every `.jsonl` file under `root`, each paired with its parsed
    filename start-date (`None` if unparseable). Sorted oldest-first —
    `(date, path)`, with undated files sorted FIRST.

    **This ordering is "conservative" for the SIZE axis specifically, not
    a blanket policy** — the two axes read an undated file oppositely
    (lead-coder review, #4479): `select_by_age` (below) never selects one
    at all, since there is no date to compare against `before` — the
    conservative reading there is "don't guess, don't delete." Sorting it
    FIRST only matters to `select_by_disk_budget`, which purges oldest-first
    off THIS ordering — there, "don't guess, assume the worst" is the
    conservative reading: an unparseable filename (e.g. after a future
    naming-convention change) is treated as the OLDEST entry, so a
    size-budget purge reaches it before any dated file, rather than
    silently exempting whatever it doesn't recognize from the size axis
    the way the age axis structurally must exempt it."""
    out: list[tuple[Path, date | None]] = []
    for p in root.rglob("*.jsonl"):
        out.append((p, filename_start_date(p.name)))
    out.sort(key=lambda item: (item[1] or date.min, item[0]))
    return out


def select_by_age(
    files: list[tuple[Path, date | None]], *, before: date,
) -> list[Path]:
    """Files whose start-date is strictly before `before` — undated files
    are never selected by age (no date to compare; the size axis, if
    enabled, still covers them)."""
    return [p for p, d in files if d is not None and d < before]


def select_by_disk_budget(
    root: Path, files: list[tuple[Path, date | None]], *, max_disk_usage_percent: float,
) -> list[Path]:
    """Oldest-first purge targets until `root`'s own total `.jsonl` size is
    back under `max_disk_usage_percent`% of the filesystem's CURRENT free
    space (`shutil.disk_usage(root).free` — the space available if `root`
    did not already occupy any of it is not computed here; this reads the
    budget off the free bytes AS MEASURED, which already excludes what
    root currently occupies, matching journald's own SystemMaxUse framing:
    a ceiling on the journal's footprint relative to what's free, not a
    share of total capacity)."""
    if max_disk_usage_percent <= 0:
        return []
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError:
        return []  # can't measure — do nothing rather than guess
    budget_bytes = free_bytes * (max_disk_usage_percent / 100.0)
    sizes: list[tuple[Path, int]] = []
    total = 0
    for p, _d in files:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        sizes.append((p, sz))
        total += sz
    if total <= budget_bytes:
        return []
    targets: list[Path] = []
    for p, sz in sizes:  # already oldest-first
        if total <= budget_bytes:
            break
        targets.append(p)
        total -= sz
    return targets


def select_purge_targets(
    root: Path, *, max_age_days: int = 0, max_disk_usage_percent: float = 0.0,
) -> list[Path]:
    """The union of both axes' targets — either firing is enough (owner:
    "日数orサイズ"). `max_age_days <= 0` / `max_disk_usage_percent <= 0`
    disables that axis. Returns a de-duplicated, sorted list; empty when
    both axes are disabled or neither is currently over its threshold."""
    if not root.is_dir():
        return []
    files = collect_dated_files(root)
    targets: set[Path] = set()
    if max_age_days > 0:
        cutoff = date.today() - timedelta(days=max_age_days)
        targets.update(select_by_age(files, before=cutoff))
    if max_disk_usage_percent > 0:
        targets.update(select_by_disk_budget(
            root, files, max_disk_usage_percent=max_disk_usage_percent,
        ))
    return sorted(targets)


def purge_files(paths: list[Path]) -> int:
    """Delete every path in `paths`, best-effort (an already-gone or
    permission-denied file is skipped, not fatal — matches `reyn events
    purge`'s own established per-file `try/except OSError` shape). Returns
    the count actually deleted."""
    deleted = 0
    for p in paths:
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def apply_auto_purge(
    root: Path, *, max_age_days: int = 0, max_disk_usage_percent: float = 0.0,
) -> int:
    """Select + delete in one call — the automatic-trigger entry point
    (`EventStore`, off-loop via `DurabilityWorker.submit_nowait`). Returns
    the count deleted (0 when both axes are disabled, nothing is over
    threshold, or `root` doesn't exist yet)."""
    targets = select_purge_targets(
        root, max_age_days=max_age_days, max_disk_usage_percent=max_disk_usage_percent,
    )
    return purge_files(targets)
