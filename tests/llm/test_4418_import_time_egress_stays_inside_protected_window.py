"""Tier 2: #4418 step ② — a genuine import-time fetch (forced cache miss)
actually happens INSIDE `_third_party_import_egress_honours_standard_env`'s
protected window, driven through the real `ensure_litellm_ready()` chokepoint
— not verified in isolation.

Distinct from what already existed before this file:
- `tests/security/test_network_egress_env_completeness_3075.py`'s tiktoken
  tests (`test_tiktoken_import_egress_injects_env_resolved_defaults_and_
  restores` etc.) call `_third_party_import_egress_honours_standard_env`
  directly and check ITS OWN behaviour in isolation (does the context
  manager patch/restore `Session.request` correctly, inject the right
  kwargs). They never drive a REAL `import litellm` through
  `ensure_litellm_ready()`, so they cannot catch a regression where the
  actual `import litellm` statement moves outside the `with` block, or a
  future third-party import-time fetch is added elsewhere and never gets
  wrapped in the first place.
- `tests/llm/test_4421_runtime_network_gate.py`'s "one turn, default
  config, zero real sockets" claim is a DIFFERENT, weaker assertion that
  happens to pass for a DIFFERENT reason: the bundled cache/local cost map
  are sufficient in the happy path, so nothing is fetched at all — that
  gate never exercises whether a fetch that DOES need to happen would
  actually be protected. (Traced directly, not assumed: #4421's own test
  script does `import litellm` itself, before `ensure_litellm_ready()` is
  ever called from the router loop — the exact ordering mistake this file
  exists to catch if it crept into PRODUCTION code. lead-coder review,
  2026-08-13: recorded against #4421 as a scope narrowing, not a defect —
  #4421 and this file are two separate claims, both needed.)

This file closes the actual gap: force a genuine tiktoken cache miss (same
technique `tests/llm/test_4422_litellm_import_failure_diagnosis.py`
already established — `TIKTOKEN_CACHE_DIR` pointed at an empty tmp dir),
drive `ensure_litellm_ready()` (never `import litellm` directly — that
would itself violate `litellm_bootstrap.py`'s own "ensure_litellm_ready()
is the ONE place" contract, the same discipline #4428's static gate
enforces outside tests/), and observe whether the resulting
`requests.Session.request` call(s) carry the env-resolved `verify`/
`timeout` defaults `_third_party_import_egress_honours_standard_env`
injects — proof the call happened INSIDE the protected window, not just
that some call happened somewhere.

Real subprocess, same reason `test_litellm_lazy_load.py` and
`test_4421_runtime_network_gate.py` both need one: `litellm` may already
be in `sys.modules` from an earlier test in this worker, making an
in-process forced cache-miss a no-op that proves nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run(src_root: str, script: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": src_root}
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )


@pytest.fixture
def _default_project(tmp_path: Path) -> Path:
    (tmp_path / "reyn.yaml").write_text(
        "llm:\n  models:\n    standard: fake/4418-gate\n", encoding="utf-8",
    )
    return tmp_path


def test_forced_cache_miss_fetch_carries_the_protected_windows_defaults(
    _default_project: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: decisive, not descriptive — a genuine tiktoken cache-miss
    fetch, forced the same way #4422's own tests force one, driven ONLY
    through `ensure_litellm_ready()` (never a bare `import litellm` in
    this script), must produce at least one recorded `requests.Session.
    request` call carrying the exact `timeout` value `_third_party_
    import_egress_honours_standard_env` injects — proof the real call
    happened inside the protected window. Zero recorded calls is a FAIL,
    not a pass: it would mean the forced cache miss never produced a
    fetch attempt at all, making the rest of the assertion vacuously true
    (the #3451-style "declared coverage, unexercised" hazard)."""
    tiktoken_cache_dir = _default_project / "empty_tiktoken_cache"
    tiktoken_cache_dir.mkdir()
    script = f"""
import requests.sessions

_calls = []
_original = requests.sessions.Session.request

def _recorder(self, method, url, **kwargs):
    _calls.append((method, url, kwargs))
    raise OSError("probe: blocked real request to " + repr(url))

requests.sessions.Session.request = _recorder

import os
# CUSTOM_TIKTOKEN_CACHE_DIR, not TIKTOKEN_CACHE_DIR: litellm's own
# litellm_core_utils/default_encoding.py OVERWRITES os.environ[
# "TIKTOKEN_CACHE_DIR"] with its bundled tokenizers dir at import time
# UNLESS CUSTOM_TIKTOKEN_CACHE_DIR is set -- verified directly (an
# earlier draft of this test set TIKTOKEN_CACHE_DIR and recorded ZERO
# calls, because litellm's own import silently overrode it back to the
# bundled, cache-hit path before tiktoken ever looked at it).
os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"] = {str(tiktoken_cache_dir)!r}

# `reyn` FIRST (production bootstrap order — see test_4421_runtime_
# network_gate.py's own note on why this matters) — but NEVER `import
# litellm` directly in this script. `ensure_litellm_ready()` is the ONE
# place that import statement should live (litellm_bootstrap.py's own
# module docstring) — this script honours that discipline itself, the
# exact thing it is testing reyn's own production code does too.
import reyn
from reyn.llm.litellm_bootstrap import (
    ensure_litellm_ready, _TIKTOKEN_IMPORT_TIMEOUT_SECONDS,
)

ensure_litellm_ready()

assert _calls, (
    "forced cache miss produced ZERO requests.Session.request calls -- "
    "the forcing setup itself is broken (TIKTOKEN_CACHE_DIR override "
    "not taking effect, or tiktoken's own fetch path changed), so this "
    "test cannot say anything about the protected window at all"
)

unprotected = [
    (method, url, kwargs) for method, url, kwargs in _calls
    if kwargs.get("timeout") != _TIKTOKEN_IMPORT_TIMEOUT_SECONDS
]
assert not unprotected, (
    "a real import-time fetch happened OUTSIDE "
    "_third_party_import_egress_honours_standard_env's protected window "
    "(no injected timeout={{}}): {{}}\\n"
    "Reyn-side remedy: check that `import litellm` still runs inside "
    "ensure_litellm_ready()'s `with (_litellm_import_logs_to_file(), "
    "_third_party_import_egress_honours_standard_env(events)):` block, "
    "and that no code path calls `import litellm` directly outside it "
    "(litellm_bootstrap.py's own module docstring: 'ensure_litellm_ready() "
    "is the ONE place' -- enforced statically by the #4428 AST gate "
    "outside tests/). If a NEW third-party import-time fetch is the "
    "cause, it needs the same protection this one already has, not a "
    "widened assertion here.".format(_TIKTOKEN_IMPORT_TIMEOUT_SECONDS, unprotected)
)
print("OK", len(_calls))
"""
    result = _run(out_of_process_reyn, script, cwd=_default_project)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


def test_falsify_a_fetch_outside_the_window_is_actually_caught(
    _default_project: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: falsify-verify companion — a `requests.Session.request` call
    that genuinely happens OUTSIDE the protected window (simulated here by
    calling the tiktoken-shaped bare request directly, with no
    `_third_party_import_egress_honours_standard_env` active at all) is
    NOT silently accepted by the assertion shape above. Without this, a
    change that weakened the ``!=`` comparison (e.g. to always pass) would
    go unnoticed — the green in the main test would be indistinguishable
    from a broken assertion that can never fire."""
    script = """
import requests.sessions

_calls = []
def _recorder(self, method, url, **kwargs):
    _calls.append((method, url, kwargs))
    raise OSError("probe: blocked")
requests.sessions.Session.request = _recorder

# A bare call with NO timeout injected -- exactly tiktoken's own shape,
# made with NO protection window active (simulating a regression where
# the real call escaped `_third_party_import_egress_honours_standard_env`).
try:
    requests.get("https://openaipublic.blob.core.windows.net/fake-probe")
except OSError:
    pass

from reyn.llm.litellm_bootstrap import _TIKTOKEN_IMPORT_TIMEOUT_SECONDS

unprotected = [
    (method, url, kwargs) for method, url, kwargs in _calls
    if kwargs.get("timeout") != _TIKTOKEN_IMPORT_TIMEOUT_SECONDS
]
assert unprotected, (
    "an unprotected call MUST be flagged by the same comparison the main "
    "test uses, or that test's green is meaningless"
)
print("RED-AS-EXPECTED")
"""
    result = _run(out_of_process_reyn, script, cwd=_default_project)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "RED-AS-EXPECTED" in result.stdout
