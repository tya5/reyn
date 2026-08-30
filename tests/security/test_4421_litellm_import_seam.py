"""Tier 1: reyn's own (host) code never imports ``litellm`` directly outside
the ONE designated seam (#4421).

``litellm_bootstrap.py``'s ``ensure_litellm_ready()`` is the sole chokepoint
that should ever execute ``import litellm`` (or any ``from litellm...``
statement) — every other file reads the module it RETURNS. This gate turns
that convention into an enforced invariant, closing the class #4395/#4421
found (not the individual sites): "litellm-touching code can (a) bypass the
chokepoint — ignore the cooldown/warming-thread machinery entirely, or (b)
grab a genuinely incomplete module — Python places a module into
``sys.modules`` at the START of import, before its top-level code finishes,
so a second, independent import statement racing the dedicated background
warming thread (#4417) can observe a partially-initialized module (the
owner's own live repro: ``AttributeError: module 'litellm' has no attribute
'exceptions'``)."

**Order note (#4421's own 3-step plan):** this gate is step 3, landed only
AFTER every production call site was migrated to route through the seam
(#4413/#4417/#4420/#4423/#4425) — landing it first would fail red on
sites nobody had touched yet. The full census confirming zero remaining
production offenders was re-run immediately before writing this gate, not
assumed from an earlier count (#4415's own classification went stale once
mid-arc, from #4417 landing after it was written — re-measured here
against the code this gate actually walks, not inherited from a prior
count).

**Scope, and why**: this gate scans ``src/reyn`` only, not ``tests/`` —
mirrors #4410's own ``reyn.api.safe`` gate precedent
(``test_4410_host_never_imports_safe_surface.py``). Tests legitimately
import ``litellm`` directly to construct real exception/response instances
to test against (the pattern this same arc found breaks in 2 test files
when it ISN'T paired with an explicit ``ensure_litellm_ready()``
precondition — see ``verification-hazards.md`` §22) — scanning ``tests/``
would fail on exactly the thing those tests are supposed to do. The
concern this gate protects against is specifically the HOST's own
production code reaching for litellm outside the one place equipped to
handle a broken/still-warming environment.

**Excluded within ``src/reyn``:**
- ``litellm_bootstrap.py`` itself — the seam.
- ``dev/testing/`` — LLMReplay (``replay.py``) and the sibling-coverage
  probe (``network_gate.py``) both need their OWN direct litellm touch to
  monkeypatch ``litellm.acompletion``/``litellm.aembedding`` at the exact
  boundary reyn's own source code calls (that IS their job — a test-replay
  seam is not a production litellm-touching one, #4415's own "dev-only, 6
  sites" bucket). Scanning them would fail this gate on infrastructure
  built to intercept litellm, not to bypass it.
- ``llm/_litellm_compat_patches.py`` (#5603, lead-coder's own seam-gate
  review) — this exemption is FILE-scoped (this gate has no per-function
  granularity), and covers 3 functions with 2 DIFFERENT reasons, both
  disclosed here rather than assumed by proximity:
  - ``apply_all()`` (and the ``apply_stream_chunk_recovery``/
    ``apply_overflow_diagnosis`` it calls) is invoked from EXACTLY ONE
    place: inside ``ensure_litellm_ready()``'s own success path, AFTER
    ``import litellm`` has already completed inside that SAME chokepoint
    — never called from anywhere else. So these functions' own litellm
    imports execute only at a point where litellm is provably already
    warm, the same relationship ``litellm_bootstrap.py``'s own internal
    helpers already have to ``ensure_litellm_ready()`` — a second part
    of the seam's own body, not a bypass of it.
  - ``report_applied_state()`` carries NO such structural guarantee (its
    own docstring says so explicitly — the caller must warm litellm
    first, this function does not) because it is a general-purpose
    READER any future caller may reach for. It is safe anyway for a
    DIFFERENT reason: every litellm touch inside it is wrapped in its
    own ``try/except Exception`` that degrades to a "could not measure"
    string rather than raising — called cold (litellm never
    successfully imported), it cannot crash the caller, it can only
    under-report. This is the intentional design this function's own
    docstring already states ("this function does not [call
    ensure_litellm_ready] — matching this module's own 'no side effect'
    contract"), not an oversight papered over by the exemption.
  This is narrower than ``litellm_bootstrap.py``'s own exemption (that
  whole file is presumed trusted, #4450's own disclosed limit above) —
  it covers exactly these 3 functions' own reasoning, stated here so the
  next person adding a 4th function to this file has to ask which of the
  two shapes above (or neither) it fits, rather than inheriting the
  exemption by simply landing in an already-excluded file.

**Known limit** (mirrors #4410's own framing): this gate can only ever see
reyn's OWN import statements — a third-party library that touches litellm
internally, or code that reaches litellm via ``sys.modules`` lookup /
``getattr`` rather than a literal ``import``/``from`` statement, is
invisible to an AST scan by construction. Architect's own #4415 recount
found exactly this gap once already (an attribute-access path an earlier
count had not measured). This gate closes the "a literal import statement
outside the seam" half of the class, not the whole class.

**#4421's own follow-up, #4450: the excluded files' insides are not
watched either.** This gate treats ``litellm_bootstrap.py`` as trusted
once it's on the exclusion list — it never inspects what that seam file's
own body does. #4450 found a real bug entirely INSIDE that excluded file
(``_capture_client_cache_baseline()``'s own unconditional second
``import litellm``, reopening the exact race #4419 closed, on the
first-import-failure path) that this gate could not have caught: the
statement triggering it lives in the one file this gate is built to
ignore. The scope this docstring already states above ("this gate scans
``src/reyn`` only, not ``tests/``" / the third-party-touch limit) is
still accurate — it just doesn't cover this angle, which #4450 made
concrete rather than hypothetical.
"""
from __future__ import annotations

import ast

from tests._support.paths import REPO_ROOT


def _imports_litellm(node: ast.AST) -> "str | None":
    """Return a short description if *node* imports ``litellm`` (the
    package itself, or any of its submodules) in any form — ``import
    litellm``, ``import litellm.llms.custom_httpx.http_handler``, ``from
    litellm import X``, ``from litellm.X import Y`` — else ``None``.
    Relative imports (``node.level`` set) cannot reach an absolute
    top-level package and are correctly never flagged."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "litellm" or alias.name.startswith("litellm."):
                return f"import {alias.name}"
        return None
    if isinstance(node, ast.ImportFrom):
        if node.level:  # relative import — cannot reach litellm from outside it
            return None
        module = node.module or ""
        if module == "litellm" or module.startswith("litellm."):
            return f"from {module} import ..."
        return None
    return None


def _seam_scan_root_and_exclusions():
    root = REPO_ROOT
    src = root / "src" / "reyn"
    seam_file = (src / "llm" / "litellm_bootstrap.py").resolve()
    dev_testing_dir = (src / "dev" / "testing").resolve()
    # #5603 — see module docstring's own "Excluded within src/reyn" entry
    # for the exact reasoning (2 different shapes, one per function, both
    # disclosed there rather than inherited by merely landing in this file).
    compat_patches_file = (src / "llm" / "_litellm_compat_patches.py").resolve()
    return root, src, seam_file, dev_testing_dir, compat_patches_file


def test_no_litellm_import_outside_the_seam() -> None:
    """Tier 1: no file under src/reyn (outside litellm_bootstrap.py,
    dev/testing/, and _litellm_compat_patches.py) imports litellm in any
    form. See module docstring for the contract, scope decision, and
    this gate's own limit."""
    root, src, seam_file, dev_testing_dir, compat_patches_file = _seam_scan_root_and_exclusions()

    offenders: list[str] = []
    for py in src.rglob("*.py"):
        resolved = py.resolve()
        if resolved == seam_file:
            continue
        if resolved == compat_patches_file:
            continue
        if resolved == dev_testing_dir or dev_testing_dir in resolved.parents:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            found = _imports_litellm(node)
            if found:
                offenders.append(f"{py.relative_to(root)}:{node.lineno} ({found})")

    assert not offenders, (
        "reyn's own (host) code must never import litellm outside "
        "litellm_bootstrap.py — that is the sole chokepoint equipped to "
        "handle a not-yet-warm or persistently-broken environment "
        "(cooldown, the dedicated background warming thread, the "
        "log-routing/egress setup). A second, independent import "
        "statement can bypass all of that, or — since #4417's warming "
        "thread landed — grab a genuinely incomplete module mid-import "
        "(#4395's owner-observed 'litellm has no attribute exceptions'). "
        f"Route through ensure_litellm_ready() instead. Offending sites: {offenders}"
    )


def test_the_scan_actually_finds_something_when_offered_a_real_violation() -> None:
    """Tier 1: positive guard — the AST detector recognizes a real
    violation, so the assertion above isn't vacuously green because
    _imports_litellm never matches anything (mirrors #4410's own
    positive-twin pattern for its structural guard)."""
    samples = {
        "import litellm": "import litellm\n",
        "import litellm.llms.custom_httpx.http_handler": "import litellm.llms.custom_httpx.http_handler\n",
        "from litellm import exceptions": "from litellm import exceptions\n",
        "from litellm.exceptions import AuthenticationError": "from litellm.exceptions import AuthenticationError\n",
        "from litellm.utils import supports_native_streaming": "from litellm.utils import supports_native_streaming\n",
    }
    for label, source in samples.items():
        tree = ast.parse(source)
        matches = [
            found for node in ast.walk(tree)
            if (found := _imports_litellm(node)) is not None
        ]
        assert matches, f"detector failed to recognize a real violation: {label!r}"

    # And the accept side: an unrelated import must NOT be flagged, and
    # neither must a relative import (cannot reach litellm from inside a
    # package anyway).
    tree = ast.parse(
        "import reyn.llm.pricing\n"
        "from reyn.runtime.session import Session\n"
        "from . import litellm_bootstrap\n"
    )
    assert not any(_imports_litellm(node) for node in ast.walk(tree)), (
        "detector false-positived on an unrelated or relative import"
    )


def test_the_seam_file_itself_is_excluded_not_silently_unreachable() -> None:
    """Tier 1: accept-side for the exclusion itself — litellm_bootstrap.py
    genuinely DOES import litellm (that's its whole job); this confirms
    the scan's exclusion list is doing real work (skipping a file that
    would otherwise fail the gate), not merely pointing at a path that
    happens not to exist or not to import litellm at all."""
    _root, _src, seam_file, _dev_testing_dir, _compat_patches_file = (
        _seam_scan_root_and_exclusions()
    )
    assert seam_file.is_file(), "the seam file path itself must exist"
    tree = ast.parse(seam_file.read_text(encoding="utf-8"))
    found_any = any(_imports_litellm(node) for node in ast.walk(tree))
    assert found_any, (
        "litellm_bootstrap.py no longer contains any litellm import — the "
        "exclusion in the main gate test is not actually exercised; if "
        "this fires, either the seam moved or the detector itself broke"
    )


def test_the_compat_patches_file_is_excluded_not_silently_unreachable() -> None:
    """Tier 1: #5603 accept-side for the ``_litellm_compat_patches.py``
    exclusion — same shape as the seam-file's own twin above: this file
    genuinely DOES import litellm (that's the whole point of
    ``apply_stream_chunk_recovery``/``apply_overflow_diagnosis``/
    ``report_applied_state``); this confirms the exclusion is doing real
    work, not pointing at a path that happens not to exist or not to
    import litellm at all."""
    _root, _src, _seam_file, _dev_testing_dir, compat_patches_file = (
        _seam_scan_root_and_exclusions()
    )
    assert compat_patches_file.is_file(), "the compat-patches file path itself must exist"
    tree = ast.parse(compat_patches_file.read_text(encoding="utf-8"))
    found_any = any(_imports_litellm(node) for node in ast.walk(tree))
    assert found_any, (
        "_litellm_compat_patches.py no longer contains any litellm import "
        "— the exclusion in the main gate test is not actually exercised; "
        "if this fires, either the file moved/changed or the detector "
        "itself broke"
    )
