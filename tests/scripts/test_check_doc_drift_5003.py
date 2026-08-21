"""Tier 1: scripts/check_doc_drift.py doc-drift discriminator contract.

Pins the invariant #5003 ratified: a PR that removes an identifier from
src/ is only flagged if some non-history docs/ file still names that
identifier AND this PR did not touch that doc file. The discriminator
asks the PR ("did you touch the doc"), never the doc ("are you a removal
record") — see the module docstring's own account of why the first form
(asking the doc) was rejected as an unmakeable natural-language judgment.

``check_doc_drift_pure`` is a pure function over already-resolved data
(gone identifiers, doc-files-by-identifier, touched files) — no
subprocess, no filesystem — mirroring
``check_pr_closing_intent.check_contradictions``. The diff-parsing helpers
(``find_removed_identifiers``, ``find_touched_files``) are also pure over
diff text. Only the ``git grep``-based resolvers
(``identifier_survives_in_src``, ``find_doc_files_containing``) touch the
filesystem, and are exercised separately against a real temp git repo.

Public surface only (no MagicMock, no private-state asserts).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys

from tests._support.paths import REPO_ROOT


def _load_module():
    repo_root = REPO_ROOT
    path = repo_root / "scripts" / "check_doc_drift.py"
    spec = importlib.util.spec_from_file_location("check_doc_drift", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_doc_drift"] = module
    spec.loader.exec_module(module)
    return module


m = _load_module()


# ---------------------------------------------------------------------------
# is_salient_identifier — the one tunable knob (purely syntactic)
# ---------------------------------------------------------------------------


def test_salient_identifier_passes_on_underscore():
    """Tier 1: snake_case (contains `_`) is salient regardless of length."""
    assert m.is_salient_identifier("go")  is False  # sanity: bare short word alone fails
    assert m.is_salient_identifier("go_x") is True


def test_salient_identifier_passes_on_dotted_form():
    """Tier 1: `module.symbol` shape is salient regardless of length."""
    assert m.is_salient_identifier("a.b") is True


def test_salient_identifier_passes_on_length_floor():
    """Tier 1: a bare word with no `_`/`.` still passes at/above the floor."""
    long_bare_word = "x" * m._MIN_IDENTIFIER_LENGTH
    short_bare_word = "x" * (m._MIN_IDENTIFIER_LENGTH - 1)
    assert m.is_salient_identifier(long_bare_word) is True
    assert m.is_salient_identifier(short_bare_word) is False


def test_salient_identifier_excludes_short_common_words():
    """Tier 1: the real motivating examples ("run", "Session") — the
    architect's own witness for why the floor exists at all."""
    assert m.is_salient_identifier("run") is False
    assert m.is_salient_identifier("Session") is False


# ---------------------------------------------------------------------------
# is_history_class_doc — directory-prefix exclusion, not semantic
# ---------------------------------------------------------------------------


def test_history_class_doc_excludes_decisions_dir():
    """Tier 1: docs/deep-dives/decisions/ is an exempt directory (exclusion 1)."""
    assert m.is_history_class_doc("docs/deep-dives/decisions/0027-foo.md") is True


def test_history_class_doc_excludes_journal_dir():
    """Tier 1: docs/deep-dives/journal/ is an exempt directory (exclusion 1)."""
    assert m.is_history_class_doc("docs/deep-dives/journal/2026-01-01-foo.md") is True


def test_history_class_doc_excludes_proposals_dir():
    """Tier 1: real incident (#5010 calibration, PR #4454) — a REAL
    removed identifier (`_force_close_wrap_up`) was still named in two
    `docs/deep-dives/proposals/` docs, both carrying their own `Status:
    cut, landed` field (the directory's own README: proposals are
    point-in-time design records, same rationale as decisions/)."""
    assert m.is_history_class_doc("docs/deep-dives/proposals/0045-foo.md") is True


def test_history_class_doc_does_not_exclude_ordinary_docs():
    """Tier 1: a doc outside all three exempt directories is not excluded."""
    assert m.is_history_class_doc("docs/reference/config/reyn-yaml.md") is False


# ---------------------------------------------------------------------------
# find_removed_identifiers / find_touched_files — pure diff parsing
# ---------------------------------------------------------------------------

_DIFF_REMOVES_MAYBE_COMPACT = """\
diff --git a/src/reyn/core/pipeline/executor.py b/src/reyn/core/pipeline/executor.py
index abc123..def456 100644
--- a/src/reyn/core/pipeline/executor.py
+++ b/src/reyn/core/pipeline/executor.py
@@ -10,7 +10,6 @@ class Executor:
     def run(self):
-        maybe_compact_messages(self.state)
         return self.state
"""


def test_find_removed_identifiers_finds_a_removed_salient_symbol():
    """Tier 1: a `-` line in src/ carrying a salient identifier not present
    on any `+` line of the same file is a removed identifier."""
    ids = m.find_removed_identifiers(_DIFF_REMOVES_MAYBE_COMPACT)
    assert "maybe_compact_messages" in ids


def test_find_removed_identifiers_ignores_non_src_paths():
    """Tier 1: the same removal, but outside src/, does not count — this
    check's whole premise is "removed from src/"."""
    diff = _DIFF_REMOVES_MAYBE_COMPACT.replace("src/reyn", "tests/reyn")
    ids = m.find_removed_identifiers(diff)
    assert "maybe_compact_messages" not in ids


def test_find_removed_identifiers_excludes_a_token_also_added_same_file():
    """Tier 1: a token present on both a `-` and a `+` line of the same
    file was relocated within the diff, not removed."""
    diff = """\
diff --git a/src/reyn/core/x.py b/src/reyn/core/x.py
--- a/src/reyn/core/x.py
+++ b/src/reyn/core/x.py
@@ -1,3 +1,3 @@
-    do_the_thing()
+    # moved below
+    do_the_thing()
"""
    ids = m.find_removed_identifiers(diff)
    assert "do_the_thing" not in ids


def test_find_removed_identifiers_ignores_words_inside_a_removed_comment():
    """Tier 1: real incident (#5003 calibration, PR #4981) — a prose word
    ("scaffolding") inside a removed Python comment line must not be
    extracted as a removed identifier; it names nothing in src/."""
    diff = """\
diff --git a/src/reyn/core/x.py b/src/reyn/core/x.py
--- a/src/reyn/core/x.py
+++ b/src/reyn/core/x.py
@@ -1,2 +1,1 @@
-        # working-attention scaffolding for the model
"""
    ids = m.find_removed_identifiers(diff)
    assert "scaffolding" not in ids


def test_find_removed_identifiers_still_finds_code_before_an_inline_comment():
    """Tier 1: an inline comment does not blind the scan to real code on
    the same removed line — only text from `#` onward is dropped."""
    diff = """\
diff --git a/src/reyn/core/x.py b/src/reyn/core/x.py
--- a/src/reyn/core/x.py
+++ b/src/reyn/core/x.py
@@ -1,2 +1,1 @@
-        maybe_compact_messages(state)  # scaffolding note
"""
    ids = m.find_removed_identifiers(diff)
    assert "maybe_compact_messages" in ids
    assert "scaffolding" not in ids


def test_find_removed_identifiers_ignores_words_inside_a_removed_docstring():
    """Tier 1: real incident (#5010 calibration, PR #4459) — a prose word
    ("normalises") inside a removed multi-line DOCSTRING (not a
    `#`-comment) must not be extracted as a removed identifier. The
    original `#`-only stripper (#5007) did not catch this class."""
    diff = """\
diff --git a/src/reyn/mcp/adapter.py b/src/reyn/mcp/adapter.py
--- a/src/reyn/mcp/adapter.py
+++ b/src/reyn/mcp/adapter.py
@@ -1,5 +1,1 @@
-    \"\"\"Handler bodies are written against the 2.0
-    shape (dict-style ``.get(...)``, snake_case), so :class:`_CtxAdapter`
-    normalises a 1.x pydantic ``Meta`` into a plain dict with the SAME
-    snake_case keys at adaptation time.
-    \"\"\"
"""
    ids = m.find_removed_identifiers(diff)
    assert "normalises" not in ids
    assert "_CtxAdapter" not in ids


def test_find_removed_identifiers_still_finds_code_inside_a_docstring_boundary_line():
    """Tier 1: the docstring stripper only drops TEXT inside the
    triple-quote span — real code on the opening/closing line itself
    (before the opening `\"\"\"` or after the closing one) still counts."""
    diff = """\
diff --git a/src/reyn/mcp/adapter.py b/src/reyn/mcp/adapter.py
--- a/src/reyn/mcp/adapter.py
+++ b/src/reyn/mcp/adapter.py
@@ -1,2 +1,1 @@
-    maybe_compact_messages(state)  \"\"\"trailing docstring-shaped text\"\"\"
"""
    ids = m.find_removed_identifiers(diff)
    assert "maybe_compact_messages" in ids


def test_find_removed_identifiers_excludes_non_salient_short_word():
    """Tier 1: a removed bare short word ("run") never enters the
    candidate set at all — filtered at the salience floor, not later."""
    diff = """\
diff --git a/src/reyn/core/x.py b/src/reyn/core/x.py
--- a/src/reyn/core/x.py
+++ b/src/reyn/core/x.py
@@ -1,2 +1,1 @@
-    run()
"""
    ids = m.find_removed_identifiers(diff)
    assert "run" not in ids


def test_find_touched_files_lists_every_changed_path():
    """Tier 1: every file path present as a `diff --git` header in the
    diff text is returned, regardless of src/ vs docs/."""
    diff = """\
diff --git a/docs/reference/config/reyn-yaml.md b/docs/reference/config/reyn-yaml.md
--- a/docs/reference/config/reyn-yaml.md
+++ b/docs/reference/config/reyn-yaml.md
@@ -1 +1 @@
-old
+new
diff --git a/src/reyn/core/x.py b/src/reyn/core/x.py
--- a/src/reyn/core/x.py
+++ b/src/reyn/core/x.py
@@ -1 +1 @@
-old
+new
"""
    touched = m.find_touched_files(diff)
    assert touched == {"docs/reference/config/reyn-yaml.md", "src/reyn/core/x.py"}


# ---------------------------------------------------------------------------
# check_doc_drift_pure — the discriminator itself, over resolved fixtures
# ---------------------------------------------------------------------------


def test_pure_flags_when_doc_file_untouched():
    """Tier 1: boundary witness ① (architect-required) — a gone identifier
    named in an untouched doc file is flagged."""
    findings = m.check_doc_drift_pure(
        gone_identifiers={"maybe_compact_messages"},
        doc_files_by_identifier={"maybe_compact_messages": {"docs/reference/pipeline.md"}},
        touched_files=set(),
    )
    assert [(f.identifier, f.doc_path) for f in findings] == [
        ("maybe_compact_messages", "docs/reference/pipeline.md"),
    ]


def test_pure_passes_when_the_same_pr_touches_the_doc():
    """Tier 1: boundary witness ② (architect-required) — the SAME PR that
    removes the identifier also edits the doc that named it (e.g. to write
    a removal record) → no finding. Without this witness, "always flags"
    would still pass witness ①."""
    findings = m.check_doc_drift_pure(
        gone_identifiers={"maybe_compact_messages"},
        doc_files_by_identifier={"maybe_compact_messages": {"docs/reference/pipeline.md"}},
        touched_files={"docs/reference/pipeline.md"},
    )
    assert findings == []


def test_pure_is_silent_when_no_doc_names_the_identifier():
    """Tier 1: a gone identifier with no entry in doc_files_by_identifier
    produces no finding — nothing to flag against."""
    findings = m.check_doc_drift_pure(
        gone_identifiers={"some_internal_helper"},
        doc_files_by_identifier={},
        touched_files=set(),
    )
    assert findings == []


def test_pure_only_flags_the_untouched_doc_among_several():
    """Tier 1: an identifier named in two docs, one touched one not — only
    the untouched one is flagged."""
    findings = m.check_doc_drift_pure(
        gone_identifiers={"foo_bar_baz"},
        doc_files_by_identifier={"foo_bar_baz": {"docs/a.md", "docs/b.md"}},
        touched_files={"docs/a.md"},
    )
    assert [(f.identifier, f.doc_path) for f in findings] == [("foo_bar_baz", "docs/b.md")]


# ---------------------------------------------------------------------------
# git-grep-backed resolvers — exercised against a real temp git repo
# ---------------------------------------------------------------------------


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def test_identifier_survives_in_src_true_when_present(tmp_path):
    """Tier 1: git-grep resolver finds a symbol still present in src/."""
    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("def foo_bar_baz(): pass\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    assert m.identifier_survives_in_src("foo_bar_baz", repo) is True


def test_identifier_survives_in_src_false_when_absent(tmp_path):
    """Tier 1: git-grep resolver finds nothing when the symbol is gone."""
    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("def other_name(): pass\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    assert m.identifier_survives_in_src("foo_bar_baz", repo) is False


def test_find_doc_files_containing_excludes_history_class_dir(tmp_path):
    """Tier 1: the git-grep resolver itself applies exclusion 1 — a
    journal entry naming the identifier is not returned."""
    repo = _init_repo(tmp_path)
    (repo / "docs" / "reference").mkdir(parents=True)
    (repo / "docs" / "deep-dives" / "journal").mkdir(parents=True)
    (repo / "docs" / "reference" / "pipeline.md").write_text("uses maybe_compact_messages\n")
    (repo / "docs" / "deep-dives" / "journal" / "2026-01-01.md").write_text(
        "maybe_compact_messages was removed in #4978\n",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    found = m.find_doc_files_containing("maybe_compact_messages", repo)
    assert found == {"docs/reference/pipeline.md"}
