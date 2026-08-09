"""Stacked-fixture detection (#3634).

A committed ``LLMReplay`` fixture "stacks" when the SAME logical call — the
same conversation-so-far, i.e. the same ``model`` + ``tool_choice`` + per-
message digest sequence — is recorded under more than one ``key``. This
happens when a tool's schema (its JSON schema, INCLUDING its ``description``
string — #3634 measured that a description-only edit already moves the key)
changes and the fixture is regenerated **in place**: :meth:`LLMReplay.flush`
used to only ever append, so the OLD entry (recorded against the old schema)
stayed on disk right alongside the NEW one, and the fixture then matched
BOTH schema generations — green regardless of which one the code actually
implements, which is worse than a stale fixture (a stale one goes RED and
gets noticed).

:func:`group_signature` is the grouping key two entries share when they are
the same logical call under different generations: everything the SHA-256
key hashes over EXCEPT ``tools`` — the one component a schema change is
*expected* to move, so grouping on it would hide the exact thing this module
exists to catch. It reads the per-component fingerprint (`key_components`,
#3473's ``replay_key_diff.fingerprint``) each entry recorded since #3473
carries. A pre-#3473 entry carries no fingerprint and cannot be grouped
precisely — see :func:`stacked_groups`'s docstring for how that gap is
handled (skipped, not guessed).

Two consumers share this one grouping rule rather than each re-deriving it:
:meth:`LLMReplay.flush` (record mode: drop an old entry from the SAME group
a session just re-recorded, so regenerating in place replaces instead of
appending — see ``reyn.dev.testing.replay``'s module docstring "Record
mode") and ``tests/test_replay_fixture_no_stacking_3634.py`` (the CI gate:
every committed fixture must hold zero multi-key groups).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def group_signature(key_components: dict[str, Any]) -> tuple[Any, ...]:
    """Return the (model, tool_choice, message-digest-sequence) signature.

    Two entries with the same signature are the SAME logical call — deliberately
    excluding ``tools``, the one component a schema change is expected to move.
    """
    return (
        key_components.get("model"),
        key_components.get("tool_choice"),
        tuple(key_components.get("messages") or []),
    )


def iter_completion_entries(fixture_path: Path) -> list[dict[str, Any]]:
    """Return every ``kind: completion`` entry in *fixture_path* (or legacy
    entries with no ``kind`` field, which default to ``"completion"`` — see
    ``LLMReplay._load``). Corrupt lines are skipped, mirroring ``_load``'s own
    silent-skip policy for a test-artifact file."""
    if not fixture_path.exists():
        return []
    entries = []
    for raw_line in fixture_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except Exception:
            continue
        if entry.get("kind", "completion") == "completion":
            entries.append(entry)
    return entries


def stacked_groups(fixture_path: Path) -> dict[tuple[Any, ...], list[str]]:
    """Return ``{group_signature: [keys]}`` for every group holding MORE THAN
    ONE distinct key among this fixture's completion entries.

    Only entries carrying a #3473 ``key_components`` fingerprint can be
    grouped precisely (the fingerprint is what lets two entries be recognised
    as the same call minus ``tools``) — an entry recorded before #3473 has no
    fingerprint and is silently excluded from grouping, not treated as its
    own singleton group. This means a pre-#3473 fixture that is ALSO stacked
    reports zero groups here, not a false "clean" verdict grounded in
    evidence — the caller (the CI gate, the sweep) must say "unmeasurable",
    never "measured clean", for such a file.
    """
    groups: dict[tuple[Any, ...], list[str]] = {}
    for entry in iter_completion_entries(fixture_path):
        components = entry.get("key_components")
        if not isinstance(components, dict):
            continue
        sig = group_signature(components)
        groups.setdefault(sig, []).append(entry.get("key"))
    return {sig: keys for sig, keys in groups.items() if len(set(keys)) > 1}


def has_fingerprinted_entries(fixture_path: Path) -> bool:
    """True if at least one completion entry carries a #3473 fingerprint —
    i.e. this fixture is precisely checkable by :func:`stacked_groups` at
    all. Lets a caller distinguish "measured clean" from "unmeasurable"."""
    return any(
        isinstance(entry.get("key_components"), dict)
        for entry in iter_completion_entries(fixture_path)
    )


# ── #3969: generalise stacking detection to every kind, not just completion ──
#
# #3634 fixed the completion-kind case (schema-drift regeneration stacking a
# NEW key-generation on top of the OLD one) and its own CI gate checked only
# that one kind. "environment" entries carry no ``key`` at all (they're keyed
# by ``name``, one snapshot per registered precondition), so #3634's key/
# group_signature machinery structurally could not see them — every
# re-recording session appended a fresh "environment" line without ever
# dropping the stale one, and the gate stayed green throughout because it
# never looked at that kind (#3969, found by tui-coder re-recording #3967's
# fixture). The fix generalises: every "kind" a fixture can hold gets an
# explicit stacking definition here, in ONE place, so a THIRD kind added
# later either gets its own entry or the gate below fails loud naming it —
# never a silent, unchecked pass-through the way "environment" was.


def all_entry_kinds(fixture_path: Path) -> set[str]:
    """Every distinct ``kind`` value present in *fixture_path* — absent
    ``kind`` defaults to ``"completion"``, matching ``LLMReplay._load``'s
    own policy. The population the CI gate below iterates, so a kind this
    module doesn't yet define a stacking check for is DETECTED (not
    silently skipped) the moment a fixture actually holds one."""
    if not fixture_path.exists():
        return set()
    kinds: set[str] = set()
    for raw_line in fixture_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except Exception:
            continue
        kinds.add(entry.get("kind", "completion"))
    return kinds


def iter_environment_entries(fixture_path: Path) -> list[dict[str, Any]]:
    """Return every ``kind: environment`` entry in *fixture_path*."""
    if not fixture_path.exists():
        return []
    entries = []
    for raw_line in fixture_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except Exception:
            continue
        if entry.get("kind") == "environment":
            entries.append(entry)
    return entries


def stacked_environment_names(fixture_path: Path) -> dict[str, int]:
    """Return ``{precondition name: count}`` for every environment
    precondition name appearing MORE THAN ONCE in *fixture_path*.

    An environment entry has no ``key`` (#3634's completion-only key/
    group_signature machinery does not apply), so its identity IS its
    ``name`` — a registered precondition contributes at most ONE snapshot
    per fixture; a second line with the same ``name`` is a stale
    generation from a prior re-recording, stacking exactly the way a
    completion entry used to before #3634 (#3969), regardless of whether
    the two lines' ``value`` happens to be byte-identical — a name
    appearing twice is itself the defect, not just a value mismatch.
    """
    counts: dict[str, int] = {}
    for entry in iter_environment_entries(fixture_path):
        name = entry.get("name")
        if name is not None:
            counts[name] = counts.get(name, 0) + 1
    return {name: count for name, count in counts.items() if count > 1}


#: kind -> the stacking-checker for that kind, returning a truthy "what's
#: stacked" structure (empty/falsy = clean). Every kind a committed fixture
#: can hold must appear either here or in :data:`_KINDS_WITH_NO_STACKING_CONCEPT`
#: below — see :mod:`tests.test_replay_fixture_no_stacking_3634` for the gate
#: that enforces this exhaustively (fails loud on an unrecognised kind rather
#: than silently passing it through).
STACKING_CHECKS: "dict[str, Any]" = {
    "completion": stacked_groups,
    "environment": stacked_environment_names,
}

#: kind -> "is this fixture precisely measurable for that kind's stacking
#: check" predicate. A kind absent from this dict is always measurable (the
#: ``environment`` case: identity is ``name``, no fingerprint prerequisite).
#: ``completion`` needs this because a pre-#3473 entry carries no
#: ``key_components`` fingerprint — see :func:`has_fingerprinted_entries`.
STACKING_MEASURABILITY: "dict[str, Any]" = {
    "completion": has_fingerprinted_entries,
}

#: Kinds with no schema-drift-style "stale generation" concept at all, so no
#: stacking checker applies — documented explicitly (not merely absent from
#: STACKING_CHECKS) so the gate can tell "deliberately exempt" from "nobody
#: has gotten to this yet". ``embedding``: a pure hash of model+input text,
#: with no schema component that can drift independently of the key itself —
#: an unchanged re-record produces the SAME key (already deduplicated by
#: LLMReplay.flush's exact-key-match rule), so there is no "same call, two
#: generations" case for this kind to stack.
_KINDS_WITH_NO_STACKING_CONCEPT: frozenset[str] = frozenset({"embedding"})
