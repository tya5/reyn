"""Tier 2: Docs tab lang preference toggle — en preferred + ja fallback.

Each doc concept appears once in the Docs tab. Default: en preferred,
ja fallback when .md absent. ``g`` key (docs tab only) toggles to
ja preferred (en fallback).

Fixture layout (created per-test via tmp_path):
  docs/
    concepts/
      glossary.md           # both en and ja present
      glossary.ja.md
      plan-mode.md          # en only
      universal-catalog.ja.md  # ja only
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


def _make_docs_tree(tmp_path: Path) -> Path:
    """Create a minimal docs/ tree and return the project root (= tmp_path)."""
    concepts = tmp_path / "docs" / "concepts"
    concepts.mkdir(parents=True)
    (concepts / "glossary.md").write_text("# glossary (en)\n", encoding="utf-8")
    (concepts / "glossary.ja.md").write_text("# glossary (ja)\n", encoding="utf-8")
    (concepts / "plan-mode.md").write_text("# plan-mode (en)\n", encoding="utf-8")
    (concepts / "universal-catalog.ja.md").write_text(
        "# universal-catalog (ja)\n", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: build_docs_index lang="ja" — prefer ja, en fallback, no duplicates
# ---------------------------------------------------------------------------


def test_build_docs_index_ja_lang_resolves_correctly(tmp_path: Path) -> None:
    """Tier 2: lang=ja → glossary→.ja.md, plan-mode→.md (fallback), no duplicates."""
    root = _make_docs_tree(tmp_path)
    from reyn.chat.tui.widgets.right_panel.docs_tab import build_docs_index

    groups, ordered = build_docs_index(root, lang="ja")

    assert ordered, "build_docs_index must return at least one file"
    stems = [p.stem for p in ordered]
    # glossary.ja.md is preferred (ja present).
    assert "glossary.ja" in stems, f"glossary.ja.md must be chosen; got stems={stems}"
    # plan-mode has no .ja.md — should fall back to en.
    assert "plan-mode" in stems, f"plan-mode.md must appear; got stems={stems}"
    # universal-catalog has no .md — should use .ja.md.
    assert "universal-catalog.ja" in stems, (
        f"universal-catalog.ja.md must appear; got stems={stems}"
    )

    # Strict no-duplicate check: each base stem appears at most once.
    from reyn.chat.tui.widgets.right_panel.docs_tab import _base_stem
    base_stems = [_base_stem(p) for p in ordered]
    assert len(base_stems) == len(set(base_stems)), (
        f"Duplicate base stems found: {base_stems}"
    )


# ---------------------------------------------------------------------------
# Test 2: build_docs_index lang="en" — prefer en, ja fallback, no duplicates
# ---------------------------------------------------------------------------


def test_build_docs_index_en_lang_resolves_correctly(tmp_path: Path) -> None:
    """Tier 2: lang=en → glossary→.md, plan-mode→.md, universal-catalog→.ja.md (fallback)."""
    root = _make_docs_tree(tmp_path)
    from reyn.chat.tui.widgets.right_panel.docs_tab import build_docs_index

    groups, ordered = build_docs_index(root, lang="en")

    assert ordered, "build_docs_index must return at least one file"
    stems = [p.stem for p in ordered]
    # glossary.md preferred (en present).
    assert "glossary" in stems, f"glossary.md must be chosen; got stems={stems}"
    # glossary.ja must NOT be chosen.
    assert "glossary.ja" not in stems, (
        f"glossary.ja.md must NOT be chosen (en preferred); got stems={stems}"
    )
    # plan-mode has no .ja.md — still en.
    assert "plan-mode" in stems, f"plan-mode.md must appear; got stems={stems}"
    # universal-catalog has no .md — fall back to ja.
    assert "universal-catalog.ja" in stems, (
        f"universal-catalog.ja.md must appear (ja fallback); got stems={stems}"
    )

    # Strict no-duplicate check.
    from reyn.chat.tui.widgets.right_panel.docs_tab import _base_stem
    base_stems = [_base_stem(p) for p in ordered]
    assert len(base_stems) == len(set(base_stems)), (
        f"Duplicate base stems found: {base_stems}"
    )


# ---------------------------------------------------------------------------
# Test 3: render_docs lang="ja" output contains "lang: ja" + "(en fallback)" hint
# ---------------------------------------------------------------------------


def test_render_docs_ja_lang_header(tmp_path: Path) -> None:
    """Tier 2: render_docs(lang='ja') header shows 'lang: ja' and 'en fallback'."""
    root = _make_docs_tree(tmp_path)
    from reyn.chat.tui.widgets.right_panel.docs_tab import (
        build_docs_index,
        render_docs,
    )

    groups, ordered = build_docs_index(root, lang="ja")
    rendered = render_docs(root, 0, groups, lang="ja")

    # The markup contains Rich color tags interleaved with the text, e.g.:
    #   "[#aaaaaa]  lang: [/][#C8553D]ja[/][#555555]  (en fallback)..."
    # We check for the literal substrings that appear in the markup regardless
    # of tag boundaries.
    assert "lang: " in rendered, (
        f"render_docs(lang='ja') must contain 'lang: ' prefix; got:\n{rendered}"
    )
    assert "]ja[/" in rendered, (
        f"render_docs(lang='ja') must contain tagged 'ja' value; got:\n{rendered}"
    )
    assert "(en fallback)" in rendered, (
        f"render_docs(lang='ja') must contain '(en fallback)'; got:\n{rendered}"
    )
    # After the MissingStyle fix, [g] is escaped as \[g] in the markup string.
    assert "\\[g] to toggle" in rendered, (
        f"render_docs must contain escaped '\\[g] to toggle' hint; got:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Test 4: render_docs lang="ja" shows (en) suffix for plan-mode (fallback row)
# ---------------------------------------------------------------------------


def test_render_docs_ja_lang_fallback_en_suffix(tmp_path: Path) -> None:
    """Tier 2: render_docs(lang='ja') shows (en) suffix for en-only doc."""
    root = _make_docs_tree(tmp_path)
    from reyn.chat.tui.widgets.right_panel.docs_tab import (
        build_docs_index,
        render_docs,
    )

    groups, ordered = build_docs_index(root, lang="ja")
    rendered = render_docs(root, 0, groups, lang="ja")

    # plan-mode has no .ja.md → resolved to .md (en) → must show (en) suffix.
    assert "(en)" in rendered, (
        f"render_docs(lang='ja') must show (en) suffix for plan-mode fallback; "
        f"got:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Test 5: render_docs lang="ja" — preferred match (glossary.ja) has NO (en) suffix
# ---------------------------------------------------------------------------


def test_render_docs_ja_lang_no_en_suffix_for_preferred_match(tmp_path: Path) -> None:
    """Tier 2: render_docs(lang='ja') glossary row has no (en) suffix (ja matched)."""
    root = _make_docs_tree(tmp_path)
    from reyn.chat.tui.widgets.right_panel.docs_tab import (
        build_docs_index,
        render_docs,
    )

    groups, ordered = build_docs_index(root, lang="ja")

    # Find the cursor index for the glossary file.
    glossary_idx = next(
        (i for i, p in enumerate(ordered) if p.stem == "glossary.ja"), None
    )
    assert glossary_idx is not None, "glossary.ja.md must be in the ordered list"

    rendered = render_docs(root, glossary_idx, groups, lang="ja")

    # The glossary row is the preferred match — it must not carry an (en) suffix.
    # Strategy: check that the line containing "glossary" does not contain "(en)".
    # We split on newlines and inspect the glossary line specifically.
    glossary_lines = [ln for ln in rendered.splitlines() if "glossary" in ln.lower()]
    assert glossary_lines, "glossary must appear in the rendered output"
    for ln in glossary_lines:
        assert "(en)" not in ln, (
            f"glossary line (preferred ja match) must not contain '(en)'; "
            f"got line: {ln!r}"
        )


# ---------------------------------------------------------------------------
# Test 6: RightPanel 'g' on docs tab toggles _docs_lang ja → en → ja
# ---------------------------------------------------------------------------


def test_right_panel_g_key_on_docs_tab_toggles_lang() -> None:
    """Tier 2: pressing 'g' on docs tab cycles _docs_lang ja → en → ja."""
    from reyn.chat.tui.widgets.right_panel import RightPanel

    panel = RightPanel.__new__(RightPanel)
    panel._panel_type = "docs"
    panel._docs_lang = "ja"
    invalidated: list[bool] = []
    panel._invalidate = lambda: invalidated.append(True)
    panel._flash_status = lambda *a, **kw: None  # stub

    class _Event:
        key = "g"
        prevented = False

        def prevent_default(self):
            self.prevented = True

        def stop(self):
            pass

    ev = _Event()
    panel.on_key(ev)
    assert panel._docs_lang == "en", (
        f"First 'g' press on docs tab must toggle ja → en; got {panel._docs_lang!r}"
    )
    assert invalidated == [True]
    assert ev.prevented is True

    # Second press: en → ja.
    ev2 = _Event()
    panel.on_key(ev2)
    assert panel._docs_lang == "ja", (
        f"Second 'g' press must toggle en → ja; got {panel._docs_lang!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: RightPanel 'g' on memory tab does NOT change _docs_lang (scope guard)
# ---------------------------------------------------------------------------


def test_right_panel_g_key_on_memory_tab_does_not_toggle_lang() -> None:
    """Tier 2: pressing 'g' on a non-docs tab must NOT change _docs_lang."""
    from reyn.chat.tui.widgets.right_panel import RightPanel

    panel = RightPanel.__new__(RightPanel)
    panel._panel_type = "memory"
    panel._docs_lang = "ja"
    invalidated: list[bool] = []
    panel._invalidate = lambda: invalidated.append(True)
    panel._flash_status = lambda *a, **kw: None

    class _Event:
        key = "g"
        prevented = False

        def prevent_default(self):
            self.prevented = True

        def stop(self):
            pass

    ev = _Event()
    panel.on_key(ev)
    assert panel._docs_lang == "ja", (
        f"'g' on memory tab must NOT change _docs_lang; got {panel._docs_lang!r}"
    )
    # _invalidate must NOT have been called (no state change occurred).
    assert invalidated == [], (
        f"'g' on memory tab must not trigger _invalidate; got {invalidated}"
    )


# ---------------------------------------------------------------------------
# Test 8: docs_filter interaction — lang collapse runs before substring filter
# ---------------------------------------------------------------------------


def test_build_docs_index_filter_applied_after_lang_collapse(tmp_path: Path) -> None:
    """Tier 2: docs_filter substring is applied after lang-collapse (no duplicates leak)."""
    root = _make_docs_tree(tmp_path)
    from reyn.chat.tui.widgets.right_panel.docs_tab import build_docs_index

    # Filter to "glossary" only, ja preferred.
    groups, ordered = build_docs_index(root, docs_filter="glossary", lang="ja")

    (only,) = ordered
    assert only.stem == "glossary.ja", (
        f"The single result must be glossary.ja.md; got {only.stem!r}"
    )

    # Same with lang=en.
    groups_en, ordered_en = build_docs_index(root, docs_filter="glossary", lang="en")
    (only_en,) = ordered_en
    assert only_en.stem == "glossary", (
        f"The single result must be glossary.md; got {only_en.stem!r}"
    )


# ---------------------------------------------------------------------------
# Test 9: render_docs lang footer markup is parseable by Rich (no MissingStyle)
# ---------------------------------------------------------------------------


def test_docs_lang_toggle_hint_no_missing_style(tmp_path: Path) -> None:
    """Tier 2b: docs tab fallback hint markup is escaped (no MissingStyle on [g]).

    Regression guard for dogfood report 2026-05-24:
    `MissingStyle: Failed to get style 'g'` raised when opening Docs tab
    because `[g] to toggle` in the lang fallback footer was interpreted as
    a Rich style tag. Fixed by escaping to `\\[g]`.
    """
    from rich.text import Text

    from reyn.chat.tui.widgets.right_panel.docs_tab import (
        build_docs_index,
        render_docs,
    )

    root = _make_docs_tree(tmp_path)
    groups, _ = build_docs_index(root, lang="en")

    # Both lang variants must render without raising MissingStyle.
    for lang in ("en", "ja"):
        groups_l, _ = build_docs_index(root, lang=lang)
        markup = render_docs(root, 0, groups_l, lang=lang)
        # Rich.Text.from_markup raises MissingStyle if any tag is an invalid
        # style (e.g. [g]).  This must not raise.
        try:
            Text.from_markup(markup)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"render_docs(lang={lang!r}) produced markup that Rich cannot parse "
                f"(MissingStyle regression): {exc}\n\nmarkup:\n{markup}"
            ) from exc
