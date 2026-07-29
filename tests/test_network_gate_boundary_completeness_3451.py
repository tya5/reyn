"""Tier 2: OS invariant — reyn.dev.testing.network_gate.LLM_NETWORK_BOUNDARY_ATTRS
is AST-derived, not hand-guessed (#3451, mirroring #3437's SSoT +
bidirectional-gate shape).

Two directions, both required:

- **declared ⊆ real**: every name in the SSoT tuple is a real coroutine
  attribute on the `litellm` module (catches a stale/renamed entry).
- **real ⊆ declared**: every `litellm.<attr>(` call site in `src/reyn` where
  `attr` resolves to a coroutine function on `litellm` is in the SSoT tuple
  (catches reyn's own code reaching a NEW litellm async surface — e.g. a
  future `litellm.atranscription(...)` call site — that the #3451 gate does
  not wrap yet; that PR fails THIS test until it patches the gate too, same
  PR, not a follow-up).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from reyn.dev.testing.network_gate import LLM_NETWORK_BOUNDARY_ATTRS


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _litellm_call_sites_in_src() -> dict[str, list[str]]:
    """Return {attr_name: [file:lineno, ...]} for every ``litellm.<attr>(...)``
    CALL site under ``src/reyn`` (attribute *references*, e.g. LLMReplay's own
    ``litellm.acompletion = self._handle`` monkeypatch assignment, are not
    call sites and are excluded — same distinction #1190's guard makes)."""
    root = _repo_root()
    src = root / "src" / "reyn"
    sites: dict[str, list[str]] = {}
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "litellm"
            ):
                continue
            sites.setdefault(func.attr, []).append(
                f"{py.relative_to(root)}:{node.lineno}"
            )
    return sites


def test_declared_boundary_attrs_are_real_litellm_coroutines() -> None:
    """Tier 2: declared ⊆ real — every SSoT name resolves, on the real
    `litellm` module, to an actual coroutine function (not a stale/renamed
    attribute the gate would silently fail to wrap)."""
    import litellm

    for attr in LLM_NETWORK_BOUNDARY_ATTRS:
        obj = getattr(litellm, attr, None)
        assert obj is not None, (
            f"LLM_NETWORK_BOUNDARY_ATTRS declares {attr!r}, which does not "
            "exist on the litellm module — stale/renamed entry."
        )
        assert inspect.iscoroutinefunction(obj), (
            f"LLM_NETWORK_BOUNDARY_ATTRS declares {attr!r}, which exists on "
            "litellm but is not a coroutine function — the gate wraps it as "
            "one (`async def _gate(...)`), so this would break at call time."
        )


def test_every_litellm_coroutine_call_site_in_src_is_declared() -> None:
    """Tier 2: real ⊆ declared — every `litellm.<attr>(` call site in
    src/reyn where `attr` is a coroutine function on litellm is in the SSoT
    tuple. RED the moment reyn's own code starts calling a new litellm async
    surface (e.g. `litellm.atranscription(...)`) without teaching the #3451
    gate about it too — the exact silent-reopening #3445 measured."""
    import litellm

    sites = _litellm_call_sites_in_src()
    undeclared: dict[str, list[str]] = {}
    for attr, locations in sites.items():
        obj = getattr(litellm, attr, None)
        if obj is None or not inspect.iscoroutinefunction(obj):
            continue  # not a network-boundary coroutine (e.g. ModelResponse, token_counter)
        if attr not in LLM_NETWORK_BOUNDARY_ATTRS:
            undeclared[attr] = locations

    assert not undeclared, (
        "src/reyn calls a litellm async coroutine attribute NOT covered by "
        "reyn.dev.testing.network_gate.LLM_NETWORK_BOUNDARY_ATTRS — the "
        "#3451 gate (and LLMReplay, see src/reyn/dev/testing/replay.py) "
        f"would silently miss it: {undeclared}"
    )
