#!/usr/bin/env python3
"""Operator-facing diagnostic for #4395 (tiktoken reaching
openaipublic.blob.core.windows.net over the network instead of hitting a
local cache).

Run: ``python scripts/diag_tiktoken_cache.py``

Prints ONLY version numbers and True/False flags — never a path, a
directory listing, or file content — so the output is safe to paste
verbatim into an issue/chat from an environment where nothing else can
leave (the reason this script exists at all: #4395's owner-side repro
couldn't run a one-off diagnostic command that shows paths).

#4422: the judgment itself (locate the cache dir, resolve the expected
cl100k blob hash, compare) now lives in :mod:`reyn._tiktoken_diag`, shared
with ``reyn.llm.litellm_bootstrap``'s own runtime failure-message —
this script is a thin print wrapper over :func:`reyn._tiktoken_diag.diagnose`,
not a second copy of the algorithm.

**Behavior change from the pre-#4422 version**: this script now imports
``reyn`` (to reach ``reyn._tiktoken_diag``), so it observes the SAME
environment ``ensure_litellm_ready`` itself sees — including
``reyn/__init__.py``'s own ``TIKTOKEN_CACHE_DIR``/``CUSTOM_TIKTOKEN_CACHE_DIR``
redirect, which runs unconditionally before any ``reyn.*`` code, this
script included, can execute at all. That is deliberate: "why did reyn's
own litellm import just fail" (the #4422 use case) needs reyn's OWN view,
not tiktoken's un-redirected default. An operator who specifically wants
tiktoken's RAW pre-reyn default (bypassing the redirect entirely) needs a
plain interpreter that never imports ``reyn`` — this script is no longer
that; run the four ``reyn._tiktoken_diag`` functions by hand from such an
interpreter instead if that isolated reading is what's needed.

This is a read-only probe: it locates the currently-effective cache
directory and inspects a candidate file already inside it (if present) —
it never fetches anything, never writes anything.

No test file for this script (operator tool, not a reyn invariant — see
CLAUDE.md's test-review question 1: this fits no Tier). The judgment
ALGORITHM it now delegates to is exercised for real by
``tests/llm/test_4422_litellm_import_failure_diagnosis.py`` (the runtime
consumer this script shares it with); if the underlying CACHE MECHANISM
itself needs a test, that belongs on ``reyn/__init__.py``'s own
TIKTOKEN_CACHE_DIR default, not here.

CI: manual -- run by an operator diagnosing a tiktoken cache issue, per its own docstring
"""
from __future__ import annotations

# #3024: verify the in-process `reyn` this bare-python script is about
# to import (module-level or lazily, anywhere below) resolves THIS
# checkout, not whichever tree the ambient venv's editable install
# happens to point at. Exits loudly (never a silent wrong-tree run) on
# a mismatch -- see verify_env_identity.py's own guard_bare_script_or_exit
# docstring. Population + presence are enforced mechanically by
# check_scripts_import_identity_guard.py, so this call cannot be dropped
# without the gate catching it.
from verify_env_identity import guard_bare_script_or_exit

guard_bare_script_or_exit()

import sys


def main() -> int:
    from reyn._tiktoken_diag import diagnose

    d = diagnose()
    print("litellm:", d.litellm_version)
    print("tiktoken:", d.tiktoken_version)
    print("custom_cache_dir_set:", d.custom_cache_dir_set)

    if d.bundled_file_exists is None:
        print("bundled_file_exists: (unknown — could not read tiktoken_ext source)")
        print("sha256_matches: (unknown — could not read tiktoken_ext source)")
        return 0

    print("bundled_file_exists:", d.bundled_file_exists)
    if d.sha256_matches is None:
        print("sha256_matches: (not applicable — file does not exist)")
    else:
        print("sha256_matches:", d.sha256_matches)
    return 0


if __name__ == "__main__":
    sys.exit(main())
