---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn skill]
---

# `reyn skill`

Manage version history and roll back skill definitions. Reads snapshots
created by the `skill_improver` finalize step (FP-0006 Component B), stored
under `.reyn/skill-versions/<name>/`.

## Synopsis

```
reyn skill versions <SKILL_NAME>
reyn skill rollback <SKILL_NAME> [--to vN]
```

## Subcommands

### `reyn skill versions`

List saved version snapshots for a skill.

```
reyn skill versions <SKILL_NAME>
```

#### Positional arguments

| Name | Description |
|------|-------------|
| `SKILL_NAME` | Name of the skill to inspect. Resolved via the standard lookup order (project → local → stdlib). |

#### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success — versions listed, or no versions saved yet (graceful). |

#### Output

```
my_skill version history:
  v1  2026-05-01 10:00
  v2  2026-05-05 14:30
  v3  2026-05-09 09:15  -> current
```

If no snapshot directory exists for the skill:

```
No versions saved for skill 'my_skill'.
```

---

### `reyn skill rollback`

Restore a skill to a previous saved version.

```
reyn skill rollback <SKILL_NAME> [--to vN]
```

#### Positional arguments

| Name | Description |
|------|-------------|
| `SKILL_NAME` | Name of the skill to roll back. |

#### Options

| Flag | Description |
|------|-------------|
| `--to vN` | Target version (e.g. `v2`). When omitted, defaults to the version immediately before the current one (current − 1). |

#### Behavior

1. Reads `.reyn/skill-versions/<name>/current` for the current version number.
2. Determines target version from `--to` or defaults to `current - 1`.
3. Verifies `.reyn/skill-versions/<name>/<target>.md` exists.
4. Atomically overwrites the skill's `skill.md` with the snapshot content.
5. Updates `.reyn/skill-versions/<name>/current` to the restored version.
6. Prints a confirmation line to stdout.

#### Stdlib restriction

Rolling back a stdlib skill is refused. Stdlib skills are ship-bundled and
must remain immutable. To customise a stdlib skill, copy it to
`reyn/project/<name>/` first, then roll back the project copy.

#### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Rollback succeeded. |
| `1` | Refused — target is a stdlib skill. |
| `2` | Error — skill not found, no versions saved, or target version file missing. |

#### Output

```
Rolled back 'my_skill' from v3 to v2.
skill.md content restored from .reyn/skill-versions/my_skill/v2.md.
```

## Examples

List all saved snapshots:

```bash
reyn skill versions my_skill
```

Check a skill that has no saved snapshots yet (exits 0):

```bash
reyn skill versions new_skill
# No versions saved for skill 'new_skill'.
```

Roll back to the previous version (current − 1):

```bash
reyn skill rollback my_skill
```

Roll back to a specific version:

```bash
reyn skill rollback my_skill --to v1
```

> **P6 audit gap:** `reyn skill rollback` does not currently emit a
> `skill_rolled_back` event — no active EventStore exists in the standalone
> CLI context. The rollback is confirmed via a printed audit line instead.
> Tracked for a follow-up PR. See [Reference: events — `skill_rolled_back`](../runtime/events.md#skill-management).

## Snapshot directory layout

```
.reyn/skill-versions/
  my_skill/
    v1.md      # snapshot at first save
    v2.md      # snapshot after first improvement
    v3.md      # snapshot after second improvement
    current    # plain-text file containing "3"
```

Snapshots are written by `skill_improver` (FP-0006 Component B). This command
reads them; it never creates new snapshots.

## See also

- `reyn skills` — list and inspect all installed skills
- [Reference: stdlib/skill_improver](../stdlib/skill_improver.md) — creates snapshots
- [Proposal: FP-0006 skill self-improvement](../../deep-dives/proposals/0006-skill-self-improvement.md) — Component B (versioning) and Component E (CLI)
- [Reference: events — `skill_rolled_back`](../runtime/events.md#skill-management) — planned P6 event payload
