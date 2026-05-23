"""Safe-mode postprocessor step for index_docs (= the `extract_and_split`
path enumeration phase).

Split out from ``chunkers.py`` so that ``extract_and_split`` can actually
run under ``mode: safe``. The companion ``chunkers.py`` module
legitimately imports ``os`` / ``pathlib`` for its remaining unsafe-mode
steps (``write_chunks_with_lock``, ``apply_strategy``), and the safe-mode
AST validator walks all imports in the module via ``ast.walk(tree)`` — so
a single-file layout forces every safe-mode step to inherit those
unsafe imports. Following the audit's own Wave 2 pattern (= ``aggregate``
→ ``aggregate_pure``), this module hosts only the safe-mode step and the
exact set of imports the safe-mode allowlist admits.

R-PURE-MODE-REDEFINE audit (2026-05-15) signed off on ``glob.glob`` as a
restricted ambient source for path-list-only enumeration. See
``docs/deep-dives/audits/2026-05-15-pure-mode-stdlib-audit.md`` and
``_python_allowlist.py`` for the contract. FP-0042 Phase 2.1 (2026-05-22)
migrated the two preprocessor steps to their own
``chunkers_preproc_safe.py`` for the same reason.
"""
from __future__ import annotations

import glob as _glob_mod


def extract_and_split(artifact: dict) -> list:
    """Postprocessor python step (mode: safe): glob enum — enumerates source files.

    Receives the LLM's finish artifact (= chunk_strategy). Enumerates files
    matching the path glob and returns an ordered list of source file paths.
    Does NOT read file content — content read is deferred to the unsafe step
    ``write_chunks_with_lock``.

    Glob ownership rationale (R-PURE-MODE audit, 2026-05-15): ``glob.glob``
    exposes filesystem path state (= list of paths matching the pattern) but
    never reads file content. The audit endorsed this as a restricted
    ambient source — see ``docs/deep-dives/audits/2026-05-15-pure-mode-
    stdlib-audit.md`` for the full reasoning.

    No ``os.path.isfile`` filter: keeping the safe-mode allowlist narrow
    means this module imports only ``glob``. ``glob.glob`` does not
    distinguish files from directories, but for typed-extension patterns
    (``**/*.md``, ``**/*.py``, …) directory matches are exotic. If one
    does sneak through, the downstream ``write_chunks_with_lock`` (=
    mode: unsafe) fails explicitly at content read time, which is
    preferable to a silent drop.

    Returns a list of source-file path dicts placed at ``data.chunk_list``:
        [{"source_path": str}, ...]
    """
    data = artifact.get("data") or {}
    path = str(data.get("path") or "")
    if not path:
        return []

    matches = _glob_mod.glob(path, recursive=True)
    return [{"source_path": fp} for fp in sorted(matches)]
