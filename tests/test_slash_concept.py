"""Tier 2: /concept slash — inline glossary lookup contract.

Pins:
  1. Bare ``/concept`` (no args) returns intro + glossary path reference.
  2. Exact match: ``/concept skill`` returns the skill definition.
  3. Case-insensitive: ``/concept SKILL`` returns the same definition.
  4. Partial/fuzzy match: ``/concept skil`` (typo) → "no entry" + did-you-mean.
  5. No match at all: ``/concept zxzxzx`` → "no entry" + no suggestions.
  6. Missing glossary file: defensive reply, no crash.

Tests use a small fixture glossary string so they don't break on every
real glossary edit.  Parser helpers are tested directly; the command is
smoke-tested via the @slash registry to verify end-to-end registration.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Import the module so the @slash decorator fires and registers the command.
import reyn.slash.concept  # noqa: F401  (side-effect: registers /concept)
from reyn.slash.concept import _lookup, _parse_glossary

# ── fixture ────────────────────────────────────────────────────────────────

# Minimal glossary in the real three-column format to keep tests independent
# of the live glossary.md content.
_FIXTURE_TEXT = """\
## Core

| English | 日本語 | Definition |
|---------|--------|------------|
| Skill | スキル | A directory defining a phase graph and final output schema. |
| Phase | フェーズ | A reusable processing unit declaring only its input and instructions. |
| Workspace | ワークスペース | The shared store for files and artifacts. |
| Artifact | アーティファクト | Structured data passed between phases. |

## DSL files

| File | Purpose |
|------|---------|
| `skill.md` | Skill declaration: entry, graph, final_output, permissions. |
"""

_FIXTURE_GLOSSARY = _parse_glossary(_FIXTURE_TEXT)


# ── _parse_glossary helper ─────────────────────────────────────────────────


def test_parse_three_column_table() -> None:
    """Tier 2: parser extracts term→definition from three-column table rows."""
    assert "skill" in _FIXTURE_GLOSSARY
    assert "phase" in _FIXTURE_GLOSSARY
    assert "workspace" in _FIXTURE_GLOSSARY
    assert "artifact" in _FIXTURE_GLOSSARY


def test_parse_two_column_table() -> None:
    """Tier 2: parser extracts term→definition from two-column (File/Purpose) tables."""
    # skill.md is in the two-column DSL table
    assert "skill.md" in _FIXTURE_GLOSSARY
    assert "Skill declaration" in _FIXTURE_GLOSSARY["skill.md"]


def test_parse_skips_header_and_separator_rows() -> None:
    """Tier 2: header rows ("English", "File") and separator rows are not in output."""
    assert "english" not in _FIXTURE_GLOSSARY
    assert "file" not in _FIXTURE_GLOSSARY
    # Separator cell would look like "---"
    assert "---" not in _FIXTURE_GLOSSARY


# ── _lookup helper ─────────────────────────────────────────────────────────


def test_lookup_exact_match() -> None:
    """Tier 2: exact match returns the definition and no suggestions."""
    defn, suggestions = _lookup("skill", _FIXTURE_GLOSSARY)
    assert defn is not None
    assert "phase graph" in defn
    assert suggestions == []


def test_lookup_case_insensitive() -> None:
    """Tier 2: lookup is case-insensitive — SKILL resolves same as skill."""
    defn_lower, _ = _lookup("skill", _FIXTURE_GLOSSARY)
    defn_upper, _ = _lookup("SKILL", _FIXTURE_GLOSSARY)
    assert defn_lower == defn_upper
    assert defn_lower is not None


def test_lookup_fuzzy_miss_returns_suggestions() -> None:
    """Tier 2: typo 'skil' → no exact match + close-match suggestions."""
    defn, suggestions = _lookup("skil", _FIXTURE_GLOSSARY)
    assert defn is None
    # 'skill' should appear in the suggestions list for a one-char typo
    assert "skill" in suggestions


def test_lookup_no_match_no_suggestions() -> None:
    """Tier 2: completely unrelated term → no definition, no suggestions."""
    defn, suggestions = _lookup("zxzxzx", _FIXTURE_GLOSSARY)
    assert defn is None
    assert suggestions == []


# ── /concept command — smoke tests via the live REGISTRY ──────────────────


class _FakeSession:
    """Minimal session stand-in: collects outbox messages without networking."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def _put_outbox(self, msg: object) -> None:
        self.messages.append({"kind": msg.kind, "text": msg.text})


async def _run_concept(args: str, *, glossary_path: Path | None = None) -> list[dict]:
    """Invoke the registered /concept handler; returns collected messages.

    When ``glossary_path`` is provided the module's ``_default_glossary_path``
    is monkey-patched so the command reads a test-controlled file rather than
    the real glossary.
    """
    import reyn.slash.concept as _mod
    from reyn.slash import REGISTRY

    session = _FakeSession()
    cmd = REGISTRY.get("concept")
    assert cmd is not None, "/concept must be registered"

    if glossary_path is not None:
        original = _mod._default_glossary_path
        _mod._default_glossary_path = lambda: glossary_path
        try:
            await cmd.handler(session, args)
        finally:
            _mod._default_glossary_path = original
    else:
        await cmd.handler(session, args)

    return session.messages


# ── helpers to make async tests runnable ──────────────────────────────────


def _run(coro):  # type: ignore[no-untyped-def]
    import asyncio
    return asyncio.run(coro)


def _write_fixture_glossary(tmp_path: Path) -> Path:
    p = tmp_path / "glossary.md"
    p.write_text(_FIXTURE_TEXT, encoding="utf-8")
    return p


# ── test 1: bare /concept ──────────────────────────────────────────────────


def test_bare_concept_returns_intro_and_path(tmp_path: Path) -> None:
    """Tier 2: bare /concept (no args) shows intro + glossary path hint."""
    gpath = _write_fixture_glossary(tmp_path)
    msgs = _run(_run_concept("", glossary_path=gpath))
    assert len(msgs) >= 1
    text = msgs[0]["text"]
    assert "glossary" in text.lower()
    # path reference must be present
    assert "Ctrl+B" in text or "guide/for-skill-authors/glossary" in text


# ── test 2: exact match ────────────────────────────────────────────────────


def test_concept_exact_match_returns_definition(tmp_path: Path) -> None:
    """Tier 2: /concept skill returns the skill definition."""
    gpath = _write_fixture_glossary(tmp_path)
    msgs = _run(_run_concept("skill", glossary_path=gpath))
    assert len(msgs) >= 1
    text = msgs[0]["text"]
    assert "skill" in text.lower()
    assert "phase graph" in text


# ── test 3: case-insensitive ───────────────────────────────────────────────


def test_concept_case_insensitive(tmp_path: Path) -> None:
    """Tier 2: /concept SKILL and /concept skill return the same definition text.

    The reply prefix includes the term as typed (= case is echoed back), but
    the definition portion must be identical — confirming case-insensitive lookup.
    """
    gpath = _write_fixture_glossary(tmp_path)
    msgs_lower = _run(_run_concept("skill", glossary_path=gpath))
    msgs_upper = _run(_run_concept("SKILL", glossary_path=gpath))
    # Both must return a hit (not an error)
    assert msgs_lower[0]["kind"] != "error"
    assert msgs_upper[0]["kind"] != "error"
    # The definition portion — everything after the first ": " — must match.
    def _definition(text: str) -> str:
        _, _, defn = text.partition(": ")
        return defn
    assert _definition(msgs_lower[0]["text"]) == _definition(msgs_upper[0]["text"])
    assert "phase graph" in msgs_lower[0]["text"]
    assert "phase graph" in msgs_upper[0]["text"]


# ── test 4: fuzzy / did-you-mean ──────────────────────────────────────────


def test_concept_fuzzy_miss_shows_did_you_mean(tmp_path: Path) -> None:
    """Tier 2: /concept skil (typo) → "no entry" + did-you-mean suggestion."""
    gpath = _write_fixture_glossary(tmp_path)
    msgs = _run(_run_concept("skil", glossary_path=gpath))
    assert len(msgs) >= 1
    text = msgs[0]["text"]
    assert "no glossary entry" in text
    assert "skill" in text  # suggestion


# ── test 5: no match, no suggestions ──────────────────────────────────────


def test_concept_no_match_no_suggestions(tmp_path: Path) -> None:
    """Tier 2: /concept zxzxzx → "no entry" with no suggestions."""
    gpath = _write_fixture_glossary(tmp_path)
    msgs = _run(_run_concept("zxzxzx", glossary_path=gpath))
    assert len(msgs) >= 1
    text = msgs[0]["text"]
    assert "no glossary entry" in text
    # No "did you mean" when there are zero close matches
    assert "did you mean" not in text


# ── test 6: missing glossary file ─────────────────────────────────────────


def test_concept_missing_glossary_returns_error(tmp_path: Path) -> None:
    """Tier 2: when glossary.md is missing, reply is a graceful error, no crash."""
    nonexistent = tmp_path / "no_such_glossary.md"
    msgs = _run(_run_concept("skill", glossary_path=nonexistent))
    assert len(msgs) >= 1
    msg = msgs[0]
    assert msg["kind"] == "error"
    # Error text should point to the expected path
    assert "glossary" in msg["text"].lower()


# ── /concept registered in REGISTRY ───────────────────────────────────────


def test_concept_in_registry() -> None:
    """Tier 2: /concept is present in the REGISTRY after import."""
    from reyn.slash import REGISTRY
    cmd = REGISTRY.get("concept")
    assert cmd is not None
    assert cmd.name == "concept"
