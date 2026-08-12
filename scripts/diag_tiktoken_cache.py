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

Distinguishes the 3 ways tiktoken's own cache-hit check
(``tiktoken/load.py``) can miss, matching #4395's own A/B/C:

  A. bundled_file_exists=False  -> the cache file was never written at all
     (a fresh install, or something cleared the cache dir).
  B. sha256_matches=False (bundled_file_exists=True) -> the cached file's
     content doesn't match what THIS installed tiktoken expects — a
     version mismatch between whatever wrote the cache and the tiktoken
     now running. tiktoken deletes a mismatched file and re-fetches over
     the network on every call until this is resolved.
  C. custom_cache_dir_set=True -> operator/litellm set
     CUSTOM_TIKTOKEN_CACHE_DIR themselves; if that path doesn't already
     have a valid cache, this script's other flags describe THAT
     directory, not tiktoken's default.

This is a read-only probe: it locates the currently-effective cache
directory and inspects a candidate file already inside it (if present) —
it never fetches anything, never writes anything, and never imports
``litellm``\'s or ``reyn``\'s own package init (so it reports what tiktoken
would ACTUALLY see, unaffected by reyn's own #4395 TIKTOKEN_CACHE_DIR fix
in ``reyn/__init__.py`` — run with ``-c`` or from a plain interpreter
outside this repo's own import chain for that isolated read; run normally
to see the value reyn itself would set).

No test file for this script (operator tool, not a reyn invariant — see
CLAUDE.md's test-review question 1: this fits no Tier). If the underlying
CACHE MECHANISM itself needs a test, that belongs on ``reyn/__init__.py``'s
own TIKTOKEN_CACHE_DIR default, not here.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import inspect
import os
import re
import sys
import tempfile


def _installed_version(dist_name: str) -> str:
    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return "(not installed)"


def _effective_cache_dir() -> "tuple[str, bool]":
    """Mirrors tiktoken/load.py's own resolution order — returns
    (cache_dir, came_from_env). Never printed; used only to locate the
    candidate file for the hash check below."""
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        return os.environ["TIKTOKEN_CACHE_DIR"], True
    if "DATA_GYM_CACHE_DIR" in os.environ:
        return os.environ["DATA_GYM_CACHE_DIR"], True
    return os.path.join(tempfile.gettempdir(), "data-gym-cache"), False


def _cl100k_expectation() -> "tuple[str, str] | None":
    """(blobpath, expected_hash) for cl100k_base, read from the INSTALLED
    tiktoken_ext package's own source text — never hardcoded here, so this
    stays correct across a tiktoken version bump without editing this
    script. Static source read only (inspect.getsource), never calls the
    function — calling it would itself fetch/read the real cache file,
    defeating the point of a side-effect-free probe."""
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


def main() -> int:
    print("litellm:", _installed_version("litellm"))
    print("tiktoken:", _installed_version("tiktoken"))
    print(
        "custom_cache_dir_set:",
        bool(os.getenv("CUSTOM_TIKTOKEN_CACHE_DIR")),
    )

    expectation = _cl100k_expectation()
    if expectation is None:
        print("bundled_file_exists: (unknown — could not read tiktoken_ext source)")
        print("sha256_matches: (unknown — could not read tiktoken_ext source)")
        return 0

    blobpath, expected_hash = expectation
    cache_dir, _ = _effective_cache_dir()
    cache_key = hashlib.sha1(blobpath.encode()).hexdigest()
    cache_path = os.path.join(cache_dir, cache_key)

    exists = os.path.exists(cache_path)
    print("bundled_file_exists:", exists)

    if not exists:
        print("sha256_matches: (not applicable — file does not exist)")
        return 0

    with open(cache_path, "rb", buffering=0) as f:
        data = f.read()
    actual_hash = hashlib.sha256(data).hexdigest()
    print("sha256_matches:", actual_hash == expected_hash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
