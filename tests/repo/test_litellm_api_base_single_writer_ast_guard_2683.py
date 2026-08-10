"""Tier 2: OS invariant — #2683 single-writer AST guard for LITELLM_API_BASE.

The ``LITELLM_API_BASE`` env var routes every LLM/embedding request to the
LiteLLM proxy (readers: ``reyn.llm.llm.proxy_kwargs`` and the embedding
``data/embedding/litellm_provider._proxy_kwargs`` mirror). #2682 established
``config/loader.py::load_config`` as the *single canonical writer* because it is
the one universal chokepoint every LLM entry passes before its first LLM call.
#2683 removed the two redundant per-entry inline copies
(``interfaces/cli/invocation_context.py`` + ``interfaces/web/deps.py``) and adds
this guard so the "per-entry hand-wiring" class that caused #2682 cannot recur:
a NEW ``environ["LITELLM_API_BASE"] = ...`` (or ``setdefault`` / ``update`` /
``os.putenv``) anywhere else in ``src/reyn/`` fails here at PR time.

The guard classifies by *operation*, not string presence: it flags only WRITE
positions and leaves reads (``.get`` / ``in`` / subscript-read) alone. It matches
on the attribute name ``environ`` being written (plus module-level ``putenv``) and
resolves ``from os import environ [as Y]`` aliases, so the canonical write —
authored through the ``import os as _os_for_mcp`` alias
(``_os_for_mcp.environ.setdefault(...)``) — is correctly attributed to
``loader.py`` regardless of the alias.

Static residual (accepted, not a silent gap): a dynamic-key write
(``environ[var] = ...`` where ``var`` is a runtime string) is not statically
catchable — same limitation as the #1190 cost-chokepoint guard's dynamic
dispatch. Every current write uses a string-literal key, so the static guard is
complete for the present tree.
"""
from __future__ import annotations

import ast
from pathlib import Path

_KEY = "LITELLM_API_BASE"
_CANONICAL_REL = "src/reyn/config/loader.py"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _environ_alias_names(tree: ast.AST) -> set[str]:
    """Names bound to ``os.environ`` via ``from os import environ [as Y]``.

    ``import os as X`` needs no entry here: ``X.environ`` is an ``Attribute``
    whose ``attr`` is ``environ``, which ``_is_environ_expr`` matches directly.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    names.add(alias.asname or alias.name)
    return names


def _putenv_names(tree: ast.AST) -> set[str]:
    """Names bound to ``os.putenv`` via ``from os import putenv [as Z]``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "putenv":
                    names.add(alias.asname or alias.name)
    return names


def _is_environ_expr(node: ast.AST, alias_names: set[str]) -> bool:
    """True if ``node`` refers to an ``os.environ`` mapping.

    - ``ast.Attribute`` with ``attr == "environ"`` — covers ``os.environ``,
      ``_os_for_mcp.environ`` (the canonical alias), any ``import os as X``.
    - ``ast.Name`` bound by ``from os import environ [as Y]``.
    """
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    return isinstance(node, ast.Name) and node.id in alias_names


def _slice_is_key(sl: ast.AST) -> bool:
    return isinstance(sl, ast.Constant) and sl.value == _KEY


def _arg0_is_key(args: list[ast.expr]) -> bool:
    return bool(args) and isinstance(args[0], ast.Constant) and args[0].value == _KEY


def _dict_or_kwargs_has_key(call: ast.Call) -> bool:
    """True if an ``environ.update(...)`` call sets the literal key.

    Covers ``update({"LITELLM_API_BASE": ...})`` (dict literal) and
    ``update(LITELLM_API_BASE=...)`` (keyword — the string is a valid identifier).
    """
    for a in call.args:
        if isinstance(a, ast.Dict):
            for k in a.keys:
                if isinstance(k, ast.Constant) and k.value == _KEY:
                    return True
    for kw in call.keywords:
        if kw.arg == _KEY:
            return True
    return False


def _is_putenv_call(call: ast.Call, putenv_names: set[str]) -> bool:
    """True for ``os.putenv("LITELLM_API_BASE", ...)`` / aliased ``putenv(...)``.

    ``putenv`` writes the real environment bypassing ``os.environ`` — flag it
    belt-and-suspenders even though no current site uses it.
    """
    func = call.func
    is_putenv = (isinstance(func, ast.Attribute) and func.attr == "putenv") or (
        isinstance(func, ast.Name) and func.id in putenv_names
    )
    return is_putenv and _arg0_is_key(call.args)


def _write_sites(py: Path, root: Path) -> list[str]:
    """Return ``rel:lineno`` for every LITELLM_API_BASE *write* in ``py``."""
    tree = ast.parse(py.read_text(encoding="utf-8"))
    alias_names = _environ_alias_names(tree)
    putenv_names = _putenv_names(tree)
    rel = py.resolve().relative_to(root).as_posix()
    sites: list[str] = []

    for node in ast.walk(tree):
        # 1) subscript-assign: <environ>["LITELLM_API_BASE"] = / += / : T =
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            if (
                isinstance(t, ast.Subscript)
                and _is_environ_expr(t.value, alias_names)
                and _slice_is_key(t.slice)
            ):
                sites.append(f"{rel}:{node.lineno}")

        # 2) mutating method call on environ: setdefault / pop / update
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            fn = node.func
            if _is_environ_expr(fn.value, alias_names):
                if fn.attr in ("setdefault", "pop") and _arg0_is_key(node.args):
                    sites.append(f"{rel}:{node.lineno}")
                elif fn.attr == "update" and _dict_or_kwargs_has_key(node):
                    sites.append(f"{rel}:{node.lineno}")

        # 3) module-level putenv with the literal key
        if isinstance(node, ast.Call) and _is_putenv_call(node, putenv_names):
            sites.append(f"{rel}:{node.lineno}")

    return sites


def test_litellm_api_base_has_exactly_one_writer() -> None:
    """Tier 2: the only code writing LITELLM_API_BASE lives in loader.py."""
    root = _repo_root()
    src = root / "src" / "reyn"

    all_sites: list[str] = []
    for py in src.rglob("*.py"):
        all_sites.extend(_write_sites(py, root))

    write_files = {site.rsplit(":", 1)[0] for site in all_sites}

    # The collector must find the canonical writer — else the guard is a
    # false-green (an empty collector would trivially pass the set equality).
    assert _CANONICAL_REL in write_files, (
        "single-writer guard found NO LITELLM_API_BASE write in "
        f"{_CANONICAL_REL} — the canonical writer moved or the write-detector "
        f"regressed (its alias/operation classification). Collected: {all_sites}"
    )

    # And it must be the ONLY file writing it. A second writer anywhere else
    # (the #2682 per-entry hand-wiring class) grows this set and fails here.
    assert write_files == {_CANONICAL_REL}, (
        "LITELLM_API_BASE written outside the single canonical writer "
        f"({_CANONICAL_REL}::load_config). This re-opens the per-entry proxy "
        "hand-wiring class (#2682): a per-entry copy silently drifts from the "
        "chokepoint. Route the export through load_config() instead. "
        f"Offending write sites: {sorted(all_sites)}"
    )
