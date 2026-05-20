"""Tier 2: ``_extract_skill_input_hint`` falls back to stdlib shared
artifacts when the entry-phase input references an artifact that lives
outside the skill directory.

Pinned invariants:

- A skill whose entry phase declares ``input: user_message`` (= the
  most common shared input artifact) resolves ``input_schema``
  populated from ``src/reyn/stdlib/artifacts/user_message.yaml``,
  not from the skill-local ``artifacts/`` directory (which does
  not contain ``user_message.yaml``).
- ``input_fields`` lists ``["text"]`` for skills using
  ``user_message`` — derived from the stdlib artifact's
  ``schema.properties``.
- ``enumerate_available_skills`` exposes ``input_schema`` for
  ``word_stats_demo`` (= regression guard against the B46 W2 S6
  failure where the missing input_schema caused the hot-list
  alias to advertise empty parameters → LLM called with ``{}``).
- A skill whose artifact lives only in skill-local ``artifacts/``
  still resolves correctly (= regression guard, fallback must not
  override skill-local).
- A skill referencing an artifact that exists in neither location
  returns ``input_schema``-absent (= empty hint, best-effort
  contract preserved).

testing.ja.md compliance:
- No mocks. Real ``_extract_skill_input_hint`` called against the
  real skill tree (= stdlib paths included).
- Tier 2: pins the OS contract that ``input_schema`` resolution
  follows the documented fallback chain.
- No private-state assertions; only the public return dict.

Behavior reference:
- ``feedback_iterative_replay_patch_disambiguation`` — verified via
  trace-patch-replay (B46 W2 S6 request_id
  cf834800-79fe-481e-9176-91bae0aed3a4): baseline 9/10 empty args
  with the unfixed wrapper; patched (= input_schema surfaced)
  10/10 correct text arg passed.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from reyn.chat.session import (
    _extract_skill_input_hint,
    enumerate_available_skills,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_STDLIB_SKILLS = _REPO_ROOT / "src" / "reyn" / "stdlib" / "skills"
_STDLIB_ARTIFACTS = _REPO_ROOT / "src" / "reyn" / "stdlib" / "artifacts"


# ---------------------------------------------------------------------------
# Shared-artifact fallback — the headline B46 fix
# ---------------------------------------------------------------------------


def test_word_stats_demo_resolves_input_schema_from_stdlib_user_message():
    """Tier 2 (B46-fix): ``word_stats_demo`` declares
    ``input: user_message`` in its entry phase and has no local
    ``artifacts/user_message.yaml``. After the fix, the hint must
    resolve ``input_schema`` from the stdlib shared artifact at
    ``src/reyn/stdlib/artifacts/user_message.yaml``."""
    hint = _extract_skill_input_hint(
        _STDLIB_SKILLS / "word_stats_demo", "review",
    )
    assert hint.get("input_artifact") == "user_message"
    assert hint.get("input_fields") == ["text"], (
        f"Expected input_fields=['text'] from shared user_message "
        f"artifact. Got: {hint.get('input_fields')!r}"
    )
    assert "input_schema" in hint, (
        f"input_schema must be populated from stdlib fallback. "
        f"Full hint: {hint!r}"
    )
    schema = hint["input_schema"]
    assert schema["type"] == "object"
    assert "text" in schema["properties"]
    assert schema["properties"]["text"]["type"] == "string"


def test_stdlib_shared_user_message_artifact_exists_and_declares_text():
    """Tier 2: regression guard — the stdlib shared artifact file
    that the fallback relies on must exist and declare the ``text``
    property. If someone deletes / restructures the shared artifact,
    the fallback silently regresses to ``input_schema``-absent. Pin
    the file's structural contract here so a future contributor
    catches it at test time."""
    art_path = _STDLIB_ARTIFACTS / "user_message.yaml"
    assert art_path.exists(), (
        f"Shared stdlib artifact missing: {art_path}. The "
        f"_extract_skill_input_hint fallback depends on this file."
    )
    art_data = yaml.safe_load(art_path.read_text(encoding="utf-8"))
    schema = art_data["schema"]
    assert "text" in schema["properties"]


def test_enumerate_surfaces_input_schema_for_word_stats_demo():
    """Tier 2 (B46-fix end-to-end): ``enumerate_available_skills``
    must surface ``input_schema`` on the ``word_stats_demo`` entry.
    This is the data the hot-list alias builder reads to construct
    ``skill__word_stats_demo`` wrapper parameters — without
    input_schema present here, the wrapper falls back to
    ``properties: {}, additionalProperties: true`` and the LLM
    calls the skill with ``{}`` (= B46 W2 S6 R verdict)."""
    skills = enumerate_available_skills({"skill_router", "chat_compactor"})
    word_stats = next(
        (s for s in skills if s.get("name") == "word_stats_demo"), None,
    )
    assert word_stats is not None, "word_stats_demo missing from catalogue"
    assert "input_schema" in word_stats, (
        f"word_stats_demo entry must include input_schema for the "
        f"hot-list alias builder. Entry keys: {list(word_stats.keys())}"
    )
    assert word_stats["input_fields"] == ["text"]


# ---------------------------------------------------------------------------
# Regression guards — skill-local still wins, missing-everywhere returns empty
# ---------------------------------------------------------------------------


def test_skill_local_artifact_still_wins_over_stdlib_fallback(tmp_path):
    """Tier 2: when a skill has its own artifact in the local
    ``artifacts/`` directory, the fallback must NOT override it.
    Builds a synthetic skill tree in ``tmp_path`` whose
    ``artifacts/user_message.yaml`` declares a custom property
    (``custom_field``) and verifies the local schema wins."""
    skill_dir = tmp_path / "synthetic_skill"
    (skill_dir / "phases").mkdir(parents=True)
    (skill_dir / "artifacts").mkdir()
    (skill_dir / "phases" / "entry.md").write_text(
        "---\ntype: phase\nname: entry\ninput: user_message\n---\n",
        encoding="utf-8",
    )
    (skill_dir / "artifacts" / "user_message.yaml").write_text(
        "schema:\n"
        "  type: object\n"
        "  properties:\n"
        "    custom_field:\n"
        "      type: string\n"
        "      description: synthetic-only property\n",
        encoding="utf-8",
    )
    hint = _extract_skill_input_hint(skill_dir, "entry")
    assert hint["input_fields"] == ["custom_field"], (
        f"Skill-local artifact must win over stdlib fallback. "
        f"Got input_fields={hint.get('input_fields')!r}"
    )


def test_unknown_artifact_returns_empty_hint(tmp_path):
    """Tier 2: when the entry phase references an artifact that
    exists in neither the skill-local ``artifacts/`` nor the stdlib
    shared ``artifacts/``, the hint must remain best-effort — return
    the artifact-name string but no ``input_schema``. The catalogue
    enumeration must not break."""
    skill_dir = tmp_path / "mystery_skill"
    (skill_dir / "phases").mkdir(parents=True)
    (skill_dir / "artifacts").mkdir()
    (skill_dir / "phases" / "entry.md").write_text(
        "---\ntype: phase\nname: entry\n"
        "input: definitely_not_a_real_artifact_x9z7\n---\n",
        encoding="utf-8",
    )
    hint = _extract_skill_input_hint(skill_dir, "entry")
    # Artifact name still surfaces (= LLM can see it in input_artifact)
    assert hint.get("input_artifact") == "definitely_not_a_real_artifact_x9z7"
    # But no schema (= can't fabricate one we don't have)
    assert "input_schema" not in hint
    assert hint.get("input_fields") == []
