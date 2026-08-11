"""Tier 2: OS invariant — #3830's embedding-side secret-scrub boundary guard.

`litellm.aembedding` may be called ONLY inside `LiteLLMEmbeddingProvider.
_aembedding_bounded` (`src/reyn/data/embedding/litellm_provider.py`) — the
one place reyn scrubs a litellm-constructed exception
(`reyn.llm.secret_scrub.scrub_exception_in_place`) before it can reach any
consumer (a retry-loop log line, a caller's own error handling, a sink
nobody has written yet). A future `litellm.aembedding` call site placed
anywhere else would bypass that scrub entirely and reopen #3830's exact
class on the embedding side — a raw provider exception (which may carry a
credential the provider echoed back, e.g. a 401 quoting the rejected key)
reaching a consumer unscrubbed.

Same shape as `tests/repo/test_cost_chokepoint_ast_guard_1190.py`
(`litellm.acompletion` confined to `recorded_acompletion`) — a DIFFERENT
justification (secret-scrub boundary integrity, not cost-observability),
so a separate guard, not a fold into that file. Per lead-coder's #3830
ruling: the guard mechanism should be extended to `aembedding` in this
same PR rather than left as an unenforced convention.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _is_litellm_aembedding_call(node: ast.AST) -> bool:
    """True for a ``litellm.aembedding(...)`` CALL — an attribute
    *reference* (not a call) is not a bypass, same exclusion as the
    acompletion guard's own docstring."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "aembedding"
        and isinstance(func.value, ast.Name)
        and func.value.id == "litellm"
    )


def _aembedding_bounded_span(provider_py: Path) -> tuple[int, int]:
    """Return the (start, end) line span of ``_aembedding_bounded``."""
    tree = ast.parse(provider_py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "_aembedding_bounded"
        ):
            return node.lineno, (node.end_lineno or node.lineno)
    raise AssertionError(
        "_aembedding_bounded not found in litellm_provider.py — secret-scrub "
        "boundary moved?"
    )


def test_litellm_aembedding_only_inside_aembedding_bounded() -> None:
    """Tier 2: the single embedding secret-scrub boundary is the only
    `litellm.aembedding` caller."""
    root = _repo_root()
    src = root / "src" / "reyn"
    provider_py = (src / "data" / "embedding" / "litellm_provider.py").resolve()
    start, end = _aembedding_bounded_span(provider_py)

    offenders: list[str] = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _is_litellm_aembedding_call(node):
                continue
            inside = py.resolve() == provider_py and start <= node.lineno <= end
            if not inside:
                offenders.append(f"{py.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "litellm.aembedding called outside _aembedding_bounded (bypasses the "
        "#3830 secret-scrub boundary — a litellm-constructed exception "
        "reaching this call site's caller unscrubbed). Route the call through "
        "reyn.data.embedding.litellm_provider.LiteLLMEmbeddingProvider."
        "_aembedding_bounded, or call reyn.llm.secret_scrub."
        "scrub_exception_in_place(exc, kwargs) in the new site's own except "
        f"block. Offending sites: {offenders}"
    )
