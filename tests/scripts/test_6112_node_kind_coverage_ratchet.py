"""Tier 2: #6112 — the node-kind-coverage ratchet
(``scripts/node_kind_coverage_ratchet.py``).

Exercises the script's OWN logic functions against small, hand-built
fixture inputs — never a full live-corpus run against the real repo tree
(that is exactly what the gate script itself does, every CI run; this file
is about the EXTRACTION/CLASSIFICATION logic, not re-running the gate).

Real filesystem (``tmp_path``) and the real, installed ``tree-sitter``/
``tree-sitter-bash`` machinery throughout — no mocks: the functions under
test are thin wrappers over the filesystem and a real grammar, so faking
either would test nothing real. Never pins the exact SET of node kinds a
real command parses to (algorithm-level, forbidden by CLAUDE.md's testing
policy) — only the classification ARITHMETIC (allowed ∪ flagged must
cover the union, or the gate must say so).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.node_kind_coverage_ratchet import (
    _DELIBERATELY_NOT_ALLOWED_NODE_KINDS,
    classify_union,
    derive_live_corpus,
    derive_node_kind_union,
    extract_doc_command_lines,
    extract_script_command_lines,
    extract_workflow_command_lines,
    main,
)

# ── extraction: workflows ────────────────────────────────────────────────


def test_extract_workflow_command_lines_reads_run_steps(tmp_path: Path) -> None:
    """Tier 2: a `run:` step body's non-empty, non-comment lines are each
    their own extracted command — the exact shape #6110's own derivation
    used."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - run: |\n"
        "          echo hello\n"
        "          # a comment, skipped\n"
        "          sleep 5\n",
        encoding="utf-8",
    )
    lines = extract_workflow_command_lines(tmp_path)
    texts = [text for text, _source in lines]
    assert "echo hello" in texts
    assert "sleep 5" in texts
    assert not any(t.startswith("#") for t in texts)


def test_extract_workflow_command_lines_no_workflows_dir_returns_empty(tmp_path: Path) -> None:
    """Tier 2: a tree with no ``.github/workflows`` at all must not crash —
    an empty list, not an exception (a real, if unusual, repo shape)."""
    assert extract_workflow_command_lines(tmp_path) == []


# ── extraction: docs ─────────────────────────────────────────────────────


def test_extract_doc_command_lines_reads_fenced_shell_blocks(tmp_path: Path) -> None:
    """Tier 2: a fenced ```bash block's lines are extracted, a leading `$ `
    shell-prompt marker is stripped, and a non-shell fence is ignored."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# Title\n\n"
        "```bash\n"
        "$ git status\n"
        "head -40 out.txt\n"
        "```\n\n"
        "```python\n"
        "print('not shell')\n"
        "```\n",
        encoding="utf-8",
    )
    lines = extract_doc_command_lines(tmp_path)
    texts = [text for text, _source in lines]
    assert "git status" in texts
    assert "head -40 out.txt" in texts
    assert "print('not shell')" not in texts


def test_extract_doc_command_lines_no_docs_dir_returns_empty(tmp_path: Path) -> None:
    """Tier 2: no ``docs/`` directory at all is an empty list, not a crash."""
    assert extract_doc_command_lines(tmp_path) == []


# ── extraction: scripts ──────────────────────────────────────────────────


def test_extract_script_command_lines_finds_literal_subprocess_arg(tmp_path: Path) -> None:
    """Tier 2: a plain string-literal first argument to ``subprocess.run``
    is extracted as a command line."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "probe.py").write_text(
        "import subprocess\n"
        "subprocess.run('ps -ef', shell=True)\n",
        encoding="utf-8",
    )
    lines = extract_script_command_lines(tmp_path)
    assert ("ps -ef", "scripts/probe.py") in lines


def test_extract_script_command_lines_finds_sh_dash_c_shape(tmp_path: Path) -> None:
    """Tier 2: the ``["sh", "-c", <cmd>]`` shape, where ``<cmd>`` is a
    same-module name assigned a literal — the exact shape #6110's own
    derivation needed for a real corpus hit."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "probe.py").write_text(
        "import subprocess\n"
        "cmd = 'ls / | wc -l'\n"
        "subprocess.run(['sh', '-c', cmd])\n",
        encoding="utf-8",
    )
    lines = extract_script_command_lines(tmp_path)
    assert ("ls / | wc -l", "scripts/probe.py") in lines


def test_extract_script_command_lines_finds_nested_string_constant_shape(
    tmp_path: Path,
) -> None:
    """Tier 2: the raw-text regex fallback — a `["sh", "-c", ...]` shape
    living INSIDE a string constant that itself holds literal Python
    source (the AST only ever sees the outer string). The exact real-repo
    shape #6110's own derivation found in
    ``scripts/sandbox_seccomp_x86_64_live_smoke.py``'s ``nested_pipe_code``."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "probe.py").write_text(
        "nested_pipe_code = (\n"
        "    \"import subprocess\\n\"\n"
        "    \"r = subprocess.run(['sh', '-c', 'ls / | wc -l'])\\n\"\n"
        ")\n",
        encoding="utf-8",
    )
    lines = extract_script_command_lines(tmp_path)
    texts = [text for text, _source in lines]
    assert "ls / | wc -l" in texts


def test_extract_script_command_lines_no_scripts_dir_returns_empty(tmp_path: Path) -> None:
    """Tier 2: no ``scripts/`` directory at all is an empty list, not a
    crash."""
    assert extract_script_command_lines(tmp_path) == []


# ── derive_live_corpus: union + dedup ─────────────────────────────────────


def test_derive_live_corpus_dedups_across_sources(tmp_path: Path) -> None:
    """Tier 2: the same command text appearing in two sources collapses to
    one corpus entry, keeping the FIRST source seen (workflows before docs
    before scripts, #6110's own source order)."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  j:\n    steps:\n      - run: echo hi\n", encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "g.md").write_text("```bash\necho hi\n```\n", encoding="utf-8")

    corpus = derive_live_corpus(tmp_path)
    assert ("echo hi", ".github/workflows/ci.yml") in corpus
    # The docs/-sourced duplicate must not survive dedup alongside it — if
    # it did, this entry would ALSO be present (the real regression this
    # test exists to catch: the same command counted twice, once per
    # source, rather than collapsed to the first source seen).
    assert ("echo hi", "docs/g.md") not in corpus


def test_derive_live_corpus_empty_tree_is_empty(tmp_path: Path) -> None:
    """Tier 2: a tree with none of the 3 source directories produces an
    empty corpus — :func:`main`'s own ZERO-commands gate-failure branch is
    what turns this into a red CI run, not this function."""
    assert derive_live_corpus(tmp_path) == []


# ── derive_node_kind_union / classify_union ──────────────────────────────


def test_derive_node_kind_union_excludes_parse_error_trees() -> None:
    """Tier 2: a command that fails to parse contributes NOTHING to the
    union — #6110's own exclusion for markdown prose / un-substituted
    ``${{ }}`` living inside a fenced block."""
    union = derive_node_kind_union(["echo hi", "this is not $( valid shell"])
    # "this is not $( valid shell" has an unterminated subshell -- a real
    # tree-sitter-bash parse error -- so it must not poison the union with
    # node kinds a human never intended to classify.
    assert "word" in union  # from "echo hi", a real, successfully-parsed command
    assert "ERROR" not in union


def test_classify_union_splits_allowed_flagged_and_unclassified() -> None:
    """Tier 2: the core arithmetic — a kind in neither declared set is
    unclassified, independent of whatever real populations
    ``_ALLOWED_NODE_KINDS``/``_DELIBERATELY_NOT_ALLOWED_NODE_KINDS``
    happen to hold today (this test does not pin either set's exact
    membership, only the set arithmetic over them)."""
    from reyn.security.exec_plan import _ALLOWED_NODE_KINDS

    one_allowed = next(iter(_ALLOWED_NODE_KINDS))
    one_flagged = next(iter(_DELIBERATELY_NOT_ALLOWED_NODE_KINDS))
    union = frozenset({one_allowed, one_flagged, "definitely_not_a_real_node_kind_6112"})

    allowed_hit, flagged_hit, unclassified = classify_union(union)

    assert allowed_hit == {one_allowed}
    assert flagged_hit == {one_flagged}
    assert unclassified == {"definitely_not_a_real_node_kind_6112"}


def test_classify_union_every_kind_classified_means_empty_unclassified() -> None:
    """Tier 2: the gate's PASS condition — a union drawn entirely from the
    two declared sets classifies with an empty third element."""
    from reyn.security.exec_plan import _ALLOWED_NODE_KINDS

    union = frozenset(set(list(_ALLOWED_NODE_KINDS)[:2]) | set(list(_DELIBERATELY_NOT_ALLOWED_NODE_KINDS)[:2]))
    _allowed_hit, _flagged_hit, unclassified = classify_union(union)
    assert unclassified == frozenset()


def test_every_deliberately_not_allowed_kind_carries_a_reason() -> None:
    """Tier 2: every entry in ``_DELIBERATELY_NOT_ALLOWED_NODE_KINDS`` is a
    non-empty STRING reason, not a bare name — the whole point of #6112
    (lead-coder's instruction: "理由の無い flag は、次の人が「なぜ足さない
    のか」で止まります")."""
    assert len(_DELIBERATELY_NOT_ALLOWED_NODE_KINDS) > 0
    for kind, reason in _DELIBERATELY_NOT_ALLOWED_NODE_KINDS.items():
        assert isinstance(reason, str) and reason.strip(), (
            f"{kind!r} has no real reason string"
        )


def test_allowed_and_deliberately_not_allowed_never_overlap() -> None:
    """Tier 2: the two sets this gate splits the union across must be
    disjoint — a kind in both would make ``classify_union``'s own
    arithmetic (allowed_hit / flagged_hit) double-count it, silently
    papering over a real classification conflict."""
    from reyn.security.exec_plan import _ALLOWED_NODE_KINDS

    overlap = _ALLOWED_NODE_KINDS & frozenset(_DELIBERATELY_NOT_ALLOWED_NODE_KINDS)
    assert overlap == frozenset()


# ── main(): end-to-end against a real, tiny fixture tree ─────────────────


def _make_fixture_tree(tmp_path: Path, *, extra_workflow_line: "str | None" = None) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    lines = ["echo hi"]
    if extra_workflow_line is not None:
        lines.append(extra_workflow_line)
    body = "\n          ".join(lines)
    (workflows / "ci.yml").write_text(
        f"jobs:\n  j:\n    steps:\n      - run: |\n          {body}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_main_passes_on_a_tree_with_only_known_kinds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: a fixture tree whose only command parses entirely within
    ``_ALLOWED_NODE_KINDS`` (``echo hi`` — program/command/command_name/
    word) exits 0."""
    import scripts.node_kind_coverage_ratchet as gate

    _make_fixture_tree(tmp_path)
    monkeypatch.setattr(gate, "_ROOT", tmp_path)

    assert main([]) == 0


def test_main_fails_on_a_tree_with_an_unclassified_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: a fixture tree whose only command parses to a node kind NOT
    in either declared set — here, a heredoc, via ``<<<`` (herestring
    redirect IS flagged, so instead this drives a construct unlikely to
    ever be in either population: a genuinely exotic, real bash construct
    this repo's own allowlist has never seen, a case statement, whose
    ``case_statement``/``case_item`` node kinds are neither allowed nor
    flagged here) — exits nonzero."""
    import scripts.node_kind_coverage_ratchet as gate

    _make_fixture_tree(tmp_path, extra_workflow_line='case "$x" in a) echo y;; esac')
    monkeypatch.setattr(gate, "_ROOT", tmp_path)

    assert main([]) != 0


def test_main_fails_on_zero_extracted_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: the #6112 spec's own item 4 — a live derivation that finds
    ZERO commands is a gate FAILURE, not a silent pass (guards against the
    extraction itself silently breaking)."""
    import scripts.node_kind_coverage_ratchet as gate

    monkeypatch.setattr(gate, "_ROOT", tmp_path)  # empty tmp_path: no .github/, docs/, scripts/

    assert main([]) != 0
