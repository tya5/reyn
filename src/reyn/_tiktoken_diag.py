"""Shared tiktoken-cache diagnosis core (#4399 origin, #4422 extraction).

The judgment logic here originally lived entirely inside
``scripts/diag_tiktoken_cache.py`` (#4399), a standalone operator CLI. #4422
needs the SAME judgment inside a runtime failure message
(``reyn.llm.litellm_bootstrap.ensure_litellm_ready``'s warn-once log line) —
"don't re-derive it, reuse it" only holds if there is ONE function both
callers share, so this module is that extraction: the script now imports
:func:`diagnose` from here instead of owning a second copy that could drift.

**Why this couldn't just stay `scripts/`-only and be imported from
`src/reyn/...`**: ``scripts/`` ships in a repo checkout, not in an installed
(non-editable) ``reyn`` package — a runtime import from ``src/reyn/`` reaching
into ``scripts/`` would work in this repo's own dev venv and break for every
other install. A ``src/reyn/`` module is the only shape both callers can
legally import.

**Why this doesn't reintroduce the script's own documented "isolated read"
concern**: by the time ANY ``reyn.*`` code can run at all — including this
module — ``reyn/__init__.py``'s own ``TIKTOKEN_CACHE_DIR``/
``CUSTOM_TIKTOKEN_CACHE_DIR`` ``setdefault`` calls have ALREADY executed
(Python runs a package's ``__init__.py`` before any of its submodules); there
is no "import reyn without triggering it" mode available to runtime code in
the first place. The script's diagnosis, once it imports this module, sees
the SAME env `ensure_litellm_ready` itself sees — which is the useful view
for "why did reyn's own litellm import just fail", the actual #4422 use case.
An operator who specifically wants tiktoken's RAW pre-reyn default (bypassing
reyn's redirect entirely) still can, by not invoking this script and instead
running the resolution by hand in a plain interpreter that never imports
`reyn` at all — worth restating explicitly here since the script's own
docstring used to promise a mode this refactor changes.

Read-only, side-effect-free: locates the currently-effective tiktoken cache
directory and inspects a candidate file already inside it, if present — never
fetches, never writes.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import inspect
import os
import re
import tempfile
from dataclasses import dataclass


def installed_version(dist_name: str) -> str:
    """The installed version of *dist_name*, or ``"(not installed)"``."""
    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return "(not installed)"


def effective_cache_dir() -> "tuple[str, bool]":
    """Mirrors ``tiktoken/load.py``'s own resolution order — returns
    ``(cache_dir, came_from_env)``. Used only to locate the candidate file
    for the hash check in :func:`diagnose`."""
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        return os.environ["TIKTOKEN_CACHE_DIR"], True
    if "DATA_GYM_CACHE_DIR" in os.environ:
        return os.environ["DATA_GYM_CACHE_DIR"], True
    return os.path.join(tempfile.gettempdir(), "data-gym-cache"), False


def cl100k_expectation() -> "tuple[str, str] | None":
    """``(blobpath, expected_hash)`` for ``cl100k_base``, read from the
    INSTALLED ``tiktoken_ext`` package's own source text — never hardcoded
    here, so this stays correct across a tiktoken version bump without
    editing this module. Static source read only (``inspect.getsource``),
    never calls the function — calling it would itself fetch/read the real
    cache file, defeating the point of a side-effect-free probe."""
    try:
        from tiktoken_ext.openai_public import cl100k_base
    except ImportError:
        return None
    src = inspect.getsource(cl100k_base)
    hash_match = re.search(r'expected_hash="([0-9a-f]+)"', src)
    url_match = re.search(r'"(https://[^"]+)"', src)
    if not hash_match or not url_match:
        return None
    return url_match.group(1), hash_match.group(1)


@dataclass(frozen=True)
class TiktokenCacheDiagnosis:
    """The 3 flags #4395/#4399 distinguish, plus the two version strings.

    ``bundled_file_exists`` / ``sha256_matches`` are ``None`` when unknown
    (``cl100k_expectation()`` could not read the installed ``tiktoken_ext``
    source) or not applicable (``sha256_matches`` when the file does not
    exist at all) — a caller must not treat ``None`` as ``False``, the same
    "unknown is not failure" distinction the original script's prints made
    ("(unknown — ...)" / "(not applicable — ...)" as text, not a boolean).
    """

    litellm_version: str
    tiktoken_version: str
    custom_cache_dir_set: bool
    bundled_file_exists: "bool | None"
    sha256_matches: "bool | None"


def diagnose() -> TiktokenCacheDiagnosis:
    """Run the #4395/#4399 judgment once and return it as data.

    Distinguishes the 3 ways tiktoken's own cache-hit check
    (``tiktoken/load.py``) can miss:

      A. ``bundled_file_exists=False`` — the cache file was never written at
         all (a fresh install, or something cleared the cache dir).
      B. ``sha256_matches=False`` (``bundled_file_exists=True``) — the cached
         file's content doesn't match what THIS installed tiktoken expects —
         a version mismatch between whatever wrote the cache and the
         tiktoken now running. tiktoken deletes a mismatched file and
         re-fetches over the network on every call until this is resolved.
      C. ``custom_cache_dir_set=True`` — operator/litellm set
         ``CUSTOM_TIKTOKEN_CACHE_DIR`` themselves; if that path doesn't
         already have a valid cache, the other two flags describe THAT
         directory, not tiktoken's default.
    """
    litellm_version = installed_version("litellm")
    tiktoken_version = installed_version("tiktoken")
    custom_cache_dir_set = bool(os.getenv("CUSTOM_TIKTOKEN_CACHE_DIR"))

    expectation = cl100k_expectation()
    if expectation is None:
        return TiktokenCacheDiagnosis(
            litellm_version, tiktoken_version, custom_cache_dir_set, None, None,
        )

    blobpath, expected_hash = expectation
    cache_dir, _ = effective_cache_dir()
    cache_key = hashlib.sha1(blobpath.encode()).hexdigest()
    cache_path = os.path.join(cache_dir, cache_key)

    if not os.path.exists(cache_path):
        return TiktokenCacheDiagnosis(
            litellm_version, tiktoken_version, custom_cache_dir_set, False, None,
        )

    with open(cache_path, "rb", buffering=0) as f:
        data = f.read()
    actual_hash = hashlib.sha256(data).hexdigest()
    return TiktokenCacheDiagnosis(
        litellm_version, tiktoken_version, custom_cache_dir_set,
        True, actual_hash == expected_hash,
    )


__all__ = ["TiktokenCacheDiagnosis", "diagnose"]
