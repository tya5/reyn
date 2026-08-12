"""Tier 1: reyn's own (host) code never imports ``reyn.api.safe.*`` (#4410).

``reyn.api.safe`` is the public import path safe-mode CodeAct python steps
get (the ``_python_allowlist`` grants only this prefix, default-deny
otherwise — see that module's own docstring). It exists to be imported by
UNTRUSTED step code running inside the CodeAct **subprocess** sandbox
(``core/kernel/codeact_runner.py``'s ``Popen`` is the real entry point;
``_codeact_harness.py``'s ``exec`` runs INSIDE that child process) — never
by the host process's own code.

The contract this gate protects: ``safe.http``'s ``_urlopen`` is a
SYNCHRONOUS ``urllib`` call. Inside the sandboxed subprocess this is fine —
a slow/hanging fetch only blocks that disposable child, never the host's
own event loop. If the HOST ever imported ``reyn.api.safe`` (directly or
transitively) and called into it, that same synchronous call would run on
whichever thread reached it — the TUI's own asyncio event loop, if that's
the caller — freezing animation and input exactly like #4395's litellm
tiktoken-fetch incident, for exactly the same reason (a synchronous
network/IO call executing where nothing can pre-empt it).

**Current measurement (architect, #4410): zero host-side imports of
``reyn.api.safe`` exist in ``src/reyn`` today.** That is not itself a
guarantee — it is a fact about today's code that the NEXT commit could
silently break (a new call site importing ``reyn.api.safe.http`` for a
quick fetch, say). This test turns that measurement into an enforced
invariant: Tier 1 (contract) — "the host does not import the safe-mode
surface" is a promise between two layers (host process / sandboxed
CodeAct subprocess) with a concrete consequence if broken (synchronous I/O
on the UI's event loop), not reyn trivia.

**Scope, and why**: this gate scans ``src/reyn`` only, not ``tests/``.
Tests legitimately import ``reyn.api.safe.*`` submodules directly to unit-
test them (they simulate untrusted-step CALLERS, which is exactly what the
allowlist is designed to permit) — scanning ``tests/`` would make the gate
fail on the very thing it exists to let happen elsewhere. The concern this
gate protects against is specifically the HOST's own production code
(``src/reyn``) reaching for the safe surface, not test code exercising it.

**Known limit** (record, per #4410's own two-tier framing — this closes
only the REYN-authored half): this gate can only ever see reyn's OWN
import statements. A third-party library reyn depends on that does
synchronous network/CPU work internally (the tiktoken cold-fetch that
actually caused #4395) is invisible to a grep/AST scan by construction —
that class is caught only by runtime detection (``REYN_STALL_TRACE``,
#4406), never by a static gate. This test closes the "reyn itself" half
of #4410's two-tier class, not the whole class.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _imports_reyn_api_safe(node: ast.AST) -> "str | None":
    """Return a short description if *node* imports ``reyn.api.safe`` (the
    package itself, or any of its submodules) in any form — ``import
    reyn.api.safe``, ``import reyn.api.safe.http``, ``from reyn.api.safe
    import http``, ``from reyn.api import safe``, ``from reyn.api.safe.http
    import get`` — else ``None``."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "reyn.api.safe" or alias.name.startswith("reyn.api.safe."):
                return f"import {alias.name}"
        return None
    if isinstance(node, ast.ImportFrom):
        if node.level:  # relative import — cannot reach reyn.api.safe from outside it
            return None
        module = node.module or ""
        if module == "reyn.api.safe" or module.startswith("reyn.api.safe."):
            return f"from {module} import ..."
        if module == "reyn.api":
            for alias in node.names:
                if alias.name == "safe":
                    return "from reyn.api import safe"
        return None
    return None


def test_host_code_never_imports_the_safe_mode_surface() -> None:
    """Tier 1: no file under src/reyn (outside reyn.api.safe itself) imports
    reyn.api.safe in any form. See module docstring for the contract, scope
    decision, and this gate's own limit."""
    root = _repo_root()
    src = root / "src" / "reyn"
    safe_dir = (src / "api" / "safe").resolve()

    offenders: list[str] = []
    for py in src.rglob("*.py"):
        resolved = py.resolve()
        if resolved == safe_dir or safe_dir in resolved.parents:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            found = _imports_reyn_api_safe(node)
            if found:
                offenders.append(f"{py.relative_to(root)}:{node.lineno} ({found})")

    assert not offenders, (
        "reyn's own (host) code must never import reyn.api.safe — that "
        "surface is for untrusted CodeAct step code running inside the "
        "sandboxed subprocess only. Importing it from the host risks a "
        "synchronous urllib call (safe.http) landing on whichever thread "
        "reaches it — the TUI's own event loop, if that's the caller — "
        f"freezing the UI (#4395's exact failure class). Offending sites: {offenders}"
    )


def test_the_scan_actually_finds_something_when_offered_a_real_violation() -> None:
    """Tier 1: positive guard — the AST detector recognizes a real
    violation, so the assertion above isn't vacuously green because
    _imports_reyn_api_safe never matches anything (mirrors
    test_network_egress_env_completeness_3075.py's own positive-twin
    pattern for its structural guards)."""
    samples = {
        "import reyn.api.safe": "import reyn.api.safe\n",
        "import reyn.api.safe.http": "import reyn.api.safe.http\n",
        "from reyn.api.safe import http": "from reyn.api.safe import http\n",
        "from reyn.api import safe": "from reyn.api import safe\n",
        "from reyn.api.safe.http import get": "from reyn.api.safe.http import get\n",
    }
    for label, source in samples.items():
        tree = ast.parse(source)
        matches = [
            found for node in ast.walk(tree)
            if (found := _imports_reyn_api_safe(node)) is not None
        ]
        assert matches, f"detector failed to recognize a real violation: {label!r}"

    # And the accept side: an unrelated import must NOT be flagged.
    tree = ast.parse("import reyn.api.op\nfrom reyn.runtime.session import Session\n")
    assert not any(_imports_reyn_api_safe(node) for node in ast.walk(tree)), (
        "detector false-positived on an unrelated import"
    )
