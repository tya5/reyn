"""Tier 1: scripts/check_retired_config_keys_denylist.py's detection contract.

Real filesystem fixtures for every regex/scan test (a real `tmp_path` tree,
real `git init`/`git add`) — no mocks, the whole point is these are pure
functions over real text/files, same discipline as
`test_check_tests_path_literal_reference_4065.py`.
"""
from __future__ import annotations

import subprocess

from reyn.config.config_schema import _REMOVED_CONFIG_KEYS, _RENAMED_CONFIG_KEYS
from scripts.check_retired_config_keys_denylist import (
    _ROOT,
    offending_lines,
)


def _git_repo(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)


def _write(tmp_path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _add(tmp_path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)


# ── the load-bearing positive case ──────────────────────────────────────────


def test_a_column_zero_retired_key_is_offending(tmp_path) -> None:
    """Tier 1: `models:` at YAML top level (column 0) in a docs/ file is the
    gate's whole reason to exist — #4322/#4323's own measured shape."""
    _write(tmp_path, "docs/guide/example.md", "models:\n  standard: foo\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    offenders = offending_lines(tmp_path)
    assert [(str(p.relative_to(tmp_path)), n, k) for p, n, k, _ in offenders] == [
        ("docs/guide/example.md", 1, "models"),
    ]


# ── the two deliberate false-negative shapes (by design, see module docstring) ──


def test_an_indented_nested_key_sharing_a_retired_name_is_not_offending(tmp_path) -> None:
    """Tier 1: `llm:\\n  models:` is the CURRENT correct shape — column-0
    anchoring must not flag the indented `models:` nested under `llm:`."""
    _write(tmp_path, "docs/guide/example.md", "llm:\n  models:\n    standard: foo\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    assert offending_lines(tmp_path) == []


def test_a_dotted_mention_is_not_offending(tmp_path) -> None:
    """Tier 1: `web.fetch.max_download_bytes` names a leaf, it does not
    claim the old top-level `web:` shape — must not be flagged."""
    _write(tmp_path, "docs/guide/example.md", "set `web.fetch.max_download_bytes` to tune it\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    assert offending_lines(tmp_path) == []


# ── exclusion classes ───────────────────────────────────────────────────────


def test_an_untracked_file_is_not_scanned(tmp_path) -> None:
    """Tier 1: population is `git ls-files` (tracked content) — an
    untracked file must not contribute offenders."""
    _write(tmp_path, "docs/guide/example.md", "models:\n  standard: foo\n")
    _git_repo(tmp_path)
    # never `git add`-ed.
    assert offending_lines(tmp_path) == []


def test_a_non_docs_non_example_file_is_not_scanned(tmp_path) -> None:
    """Tier 1: the scan population is `docs/**` plus `*.example` — a
    retired key sitting in an unrelated tracked file (e.g. `src/`) is out
    of this gate's population entirely."""
    _write(tmp_path, "src/reyn/notes.md", "models:\n  standard: foo\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    assert offending_lines(tmp_path) == []


def test_spec_directory_is_excluded_even_though_tracked(tmp_path) -> None:
    """Tier 1: docs/deep-dives/spec/ records a design AS PROPOSED — a
    retired key there is a legitimate historical fact, not drift."""
    _write(tmp_path, "docs/deep-dives/spec/design/old.md", "models:\n  standard: foo\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    assert offending_lines(tmp_path) == []


def test_decisions_adr_directory_is_excluded_even_though_tracked(tmp_path) -> None:
    """Tier 1: docs/deep-dives/decisions/ (ADRs) are immutable-by-policy
    records of a past decision — same reasoning as spec/."""
    _write(tmp_path, "docs/deep-dives/decisions/0001-old.md", "models:\n  standard: foo\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    assert offending_lines(tmp_path) == []


def test_proposals_directory_is_excluded_even_though_tracked(tmp_path) -> None:
    """Tier 1: docs/deep-dives/proposals/ (FP-NNNN docs) show a schema
    shape AS PROPOSED, sometimes predating the field's eventual real name —
    same reasoning as spec/, discovered by a real pre-flight scan (#4327),
    not named in the original brief but structurally identical."""
    _write(tmp_path, "docs/deep-dives/proposals/0099-old.md", "agent:\n  id: x\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    assert offending_lines(tmp_path) == []


def test_journal_directory_is_excluded_even_though_tracked(tmp_path) -> None:
    """Tier 1: docs/deep-dives/journal/ entries quote the EXACT config used
    for a dated run — the same "records what happened" class
    `check_tests_path_literal_reference.py` carves out for CHANGELOG.md."""
    _write(tmp_path, "docs/deep-dives/journal/dogfood/run.md", "models:\n  standard: foo\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    assert offending_lines(tmp_path) == []


def test_a_known_different_schema_file_excludes_only_the_colliding_key(tmp_path) -> None:
    """Tier 1: docs/reference/builtin-models.md's fenced blocks are the
    model-CATALOG entry's own field shape (`{model, max_completion_tokens}`),
    never a `reyn.yaml` example — a real pre-flight scan (#4327) found this
    exact file colliding on `model:` even after column-0 anchoring."""
    _write(
        tmp_path, "docs/reference/builtin-models.md",
        "model: anthropic/claude-3-7-sonnet\nmax_completion_tokens: 8192\n",
    )
    _git_repo(tmp_path)
    _add(tmp_path)
    assert offending_lines(tmp_path) == []


def test_a_known_different_schema_file_still_catches_other_retired_keys(tmp_path) -> None:
    """Tier 1: lead-coder's #4332 review block — the exclusion is FILE x KEY,
    not whole-file. `builtin-models.md` colliding on `model:` must not also
    blind the gate to a genuine `models:` drift in the SAME file — exactly
    the shape #4322 had to fix there the same night this gate was written."""
    _write(
        tmp_path, "docs/reference/builtin-models.md",
        "model: anthropic/claude-3-7-sonnet\nmodels:\n  standard: foo\n",
    )
    _git_repo(tmp_path)
    _add(tmp_path)
    offenders = offending_lines(tmp_path)
    assert [(str(p.relative_to(tmp_path)), n, k) for p, n, k, _ in offenders] == [
        ("docs/reference/builtin-models.md", 2, "models"),
    ]


# ── single source: the denylist is read from _RENAMED_CONFIG_KEYS, not hand-duplicated ──


def test_denylist_keys_are_exactly_the_renamed_config_keys_registry(tmp_path) -> None:
    """Tier 1: the gate's denylist must be READ from
    `reyn.config.config_schema._RENAMED_CONFIG_KEYS`, not a hand-copied
    literal — a key registered there and nowhere else in this test file
    (so this assertion can't pass by both sides transcribing the same
    literal) must still be detected."""
    assert _RENAMED_CONFIG_KEYS, "the registry must be non-empty for this test to mean anything"
    any_retired_key = next(iter(_RENAMED_CONFIG_KEYS))
    _write(tmp_path, "docs/guide/example.md", f"{any_retired_key}: x\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    offenders = offending_lines(tmp_path)
    assert [k for _, _, k, _ in offenders] == [any_retired_key]


def test_denylist_also_includes_the_removed_config_keys_registry(tmp_path) -> None:
    """Tier 1: #4375 — a key registered ONLY in `_REMOVED_CONFIG_KEYS`
    (deleted, no successor — never in `_RENAMED_CONFIG_KEYS`) must still be
    detected. This is the gate's own #4373 witness: `shell_allowed` /
    `skill_search` were retired keys the OLD rename-only denylist could
    never have caught, by construction, regardless of scan scope — because
    they were never a rename at all."""
    assert _REMOVED_CONFIG_KEYS, "the registry must be non-empty for this test to mean anything"
    any_removed_key = next(iter(_REMOVED_CONFIG_KEYS))
    assert any_removed_key not in _RENAMED_CONFIG_KEYS, (
        "test precondition: the two registries must be disjoint, or this "
        "test can't tell which one actually caught the key"
    )
    _write(tmp_path, "docs/guide/example.md", f"{any_removed_key}: x\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    offenders = offending_lines(tmp_path)
    assert [k for _, _, k, _ in offenders] == [any_removed_key]


# ── #4375 ruling ②: `.example` comment-line scanning ────────────────────────


def test_a_commented_out_top_level_retired_key_in_dot_example_is_offending(
    tmp_path,
) -> None:
    """Tier 1: #4375 — a retired key commented out at column 0 (`# key:`,
    ONE leading space) in a `.example` file is offending. This is the
    #4392 real-crash shape: a stale commented-out example is invisible to
    every unknown-key check while commented, and only bites the operator
    the moment they uncomment it to actually use the block."""
    any_retired_key = next(iter(_RENAMED_CONFIG_KEYS))
    _write(tmp_path, "reyn.local.yaml.example", f"# {any_retired_key}: x\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    offenders = offending_lines(tmp_path)
    assert [k for _, _, k, _ in offenders] == [any_retired_key]


def test_a_commented_out_nested_key_in_dot_example_is_not_offending(tmp_path) -> None:
    """Tier 1: #4375 — a retired key commented out at NESTED position
    (`#   key:`, 2+ leading spaces after `#`, mirroring the YAML indent it
    would have if uncommented) in a `.example` file is NOT offending —
    same "indented nested key sharing a retired name" exclusion the bare
    (non-`.example`) case already has, applied to the comment-prefixed
    form too. This is the false positive a naive `#\\s*` (unbounded
    whitespace) pattern would reintroduce: `reyn.local.yaml.example`'s own
    real convention marks a NESTED commented key with extra leading
    spaces (`# llm:` then `#   model:` for `llm.model`)."""
    any_retired_key = next(iter(_RENAMED_CONFIG_KEYS))
    _write(
        tmp_path, "reyn.local.yaml.example",
        f"# llm:\n#   {any_retired_key}: x\n",
    )
    _git_repo(tmp_path)
    _add(tmp_path)
    offenders = offending_lines(tmp_path)
    assert offenders == []


def test_a_commented_out_retired_key_in_docs_is_not_offending(tmp_path) -> None:
    """Tier 1: #4375 ruling ② — the comment-prefix form applies ONLY to
    `.example` files. A `docs/**` file's `# key:` stays invisible (prose
    scope, unchanged from before this PR) — a doc explaining a rename in
    prose (`` # models: moved to llm.models ``) must not itself trip the
    gate (#3559's "the disciplined party pays" shape)."""
    any_retired_key = next(iter(_RENAMED_CONFIG_KEYS))
    _write(tmp_path, "docs/guide/example.md", f"# {any_retired_key}: x\n")
    _git_repo(tmp_path)
    _add(tmp_path)
    offenders = offending_lines(tmp_path)
    assert offenders == []


# ── the real committed tree ─────────────────────────────────────────────────


def test_the_real_repo_tree_has_zero_offenders() -> None:
    """Tier 1: the load-bearing witness — running the real scan against the
    real repo tree, right now, must find nothing. This is not a ratchet
    (see module docstring "Not a ratchet") — any hit fails outright."""
    assert offending_lines(_ROOT) == []
