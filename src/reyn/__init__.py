"""reyn — Agent OS package root.

This ``__init__`` performs NO eager imports, so importing a *submodule* —
notably ``reyn.core.kernel._codeact_harness``, the CodeAct sandbox child-process
entry point — does not pull the agent / llm / httpx chain in through the package
root. (FP-0008 C4: keeping the harness cold-import path well under the
python-step timeout.)

It DOES set ``os.environ`` defaults (no import cost) before anything else
runs, because they must exist before litellm's own package init executes:
litellm's ``__init__.py`` calls ``get_model_cost_map(...)`` and (lazily,
on first Anthropic call) the beta-headers manager at *module import time*, each
doing a network fetch of a remote config unless the corresponding
``LITELLM_LOCAL_*`` env var is already set. Every ``import litellm`` in this
codebase is lazy (inside functions, in ``reyn.llm.*`` and friends) and none of
them precede a `reyn` submodule import, so this package root — which Python
guarantees runs before any ``reyn.*`` submodule is importable — is the
earliest point that is still guaranteed to run before the first
``import litellm`` on every startup path (CLI, tests, scripts).
"""
from __future__ import annotations

import importlib.util
import os
import shutil

# Silence litellm's startup network fetches (remote cost-table / remote beta-
# headers config) by defaulting to its bundled local snapshots. ``setdefault``
# (not a forced assignment) so an operator who explicitly wants the remote
# fetch (e.g. sets the var to a falsey value themselves) is respected — see
# reyn's no-uncustomizable-hardcodes rule. Must run before litellm is imported
# anywhere; see the module docstring above for why this is the right place.
# #3671: the earliest instant reyn's own code runs. Everything after it — the
# whole import tree, litellm included (1.75s of a 1.9s startup here) — is
# measurable; everything before it (interpreter start, site setup) is not, from
# inside this process. Captured HERE rather than in the timing module because
# that module is imported late, and a clock started then reports the import
# phase as 0.00s — measured, and the reason this line exists.
import time as _time

STARTUP_CLOCK_ORIGIN = _time.perf_counter()

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS", "True")


# #4395: point TIKTOKEN_CACHE_DIR at a reyn-owned, PERSISTENT directory —
# neither the OS temp dir (tiktoken's own fallback default, periodically
# cleared by the OS, so a network fetch can recur even after having worked
# once) nor litellm's own package-internal tokenizers dir (what an earlier
# version of this fix pointed at). litellm's ``default_encoding.py`` already
# points there itself at import time, and that turned out to be insufficient
# on its own for two independent reasons a live owner repro surfaced:
#   (a) IMPORT ORDER — that module only runs once something actually
#       imports ``litellm.litellm_core_utils.token_counter`` (or
#       ``_lazy_imports``); ``litellm_provider.py``'s own
#       ``tiktoken.get_encoding(...)`` call bypasses that chain entirely if
#       it's the first tiktoken-touching code in the process (e.g. an
#       embedding call before any chat completion ever runs
#       ``litellm.token_counter``).
#   (b) SELF-CORRUPTION — tiktoken's own cache-hit check is a sha256
#       comparison (tiktoken/load.py); a version mismatch between the
#       litellm-bundled blob and what the installed tiktoken expects makes
#       tiktoken DELETE the mismatched file and re-fetch — if that file
#       lives inside litellm's OWN package directory, tiktoken is mutating
#       an installed package's data files, and the deletion recurs on every
#       run (the redirect fixes this by never handing tiktoken a file
#       inside litellm's package to begin with — a fetch into reyn's own
#       directory, once successful, persists across restarts instead).
# This is NOT re-deciding litellm's own call (#3905's "don't take over a
# third party's responsibility") — litellm has already decided tiktoken
# should be fully offline by default; this only makes that decision
# survive both the import-order gap and the OS-temp-dir/package-mutation
# fragility of WHERE it points, for every same-process tiktoken caller, not
# just the ones that happen to import litellm's token-counting module
# first.
#
# TWO env vars, because they close two DIFFERENT holes — verified live,
# not assumed (an earlier draft of this comment claimed litellm's own
# import "force-assigns... so this is only a bridge", which was WRONG in
# the one case that matters most: the owner's actual crash happens INSIDE
# ``import litellm`` itself):
#   TIKTOKEN_CACHE_DIR (setdefault) — covers hole (a) above: a caller that
#     never imports litellm's token-counting module at all (direct
#     ``tiktoken.get_encoding(...)``, as ``litellm_provider.py`` does) only
#     ever sees this var; ``CUSTOM_TIKTOKEN_CACHE_DIR`` is never read by
#     anyone in that path.
#   CUSTOM_TIKTOKEN_CACHE_DIR (setdefault) — covers hole (b): once
#     ``litellm``'s own ``default_encoding.py`` DOES run (i.e. hole (a)
#     didn't apply), it OVERWRITES ``TIKTOKEN_CACHE_DIR`` with an
#     UNCONDITIONAL assignment (not ``setdefault`` — confirmed by reading
#     its source directly), ignoring whatever this file set moments
#     earlier. The one input it actually reads before deciding that value
#     is ``CUSTOM_TIKTOKEN_CACHE_DIR`` — set that instead, and litellm's
#     own import redirects itself (and even calls ``os.makedirs`` for us).
# ``setdefault`` on both — an operator who already set either var still
# wins; this is a default, not an override.
_tiktoken_cache_dir = os.path.join(
    os.path.expanduser("~"), ".reyn", "cache", "tiktoken",
)
try:
    os.makedirs(_tiktoken_cache_dir, exist_ok=True)
except OSError:
    pass  # unwritable $HOME — fall through, tiktoken's own default still applies
else:
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", _tiktoken_cache_dir)
    os.environ.setdefault("CUSTOM_TIKTOKEN_CACHE_DIR", _tiktoken_cache_dir)

    # Redirecting WHERE the cache lives (above) still leaves the directory
    # EMPTY on a first run — and tiktoken's own fetch call
    # (``requests.get(blobpath)`` in tiktoken/load.py) is called with NO
    # timeout, so under a proxy/firewall that accepts a connection but
    # never answers, that first run hangs forever, not just slowly —
    # observed live: "Loading..." never resolves, no exception, no log
    # growth, because the hang is INSIDE ``import litellm`` on the startup
    # path, before reyn's own event loop or logging is even running. Since
    # reyn already ships litellm's own bundled tiktoken blobs (this is
    # exactly what ``default_encoding.py`` would copy-from if IT ran
    # first), seed reyn's cache directory from them now — same-machine
    # file copy, zero network, so a version-COMPATIBLE combo (the common
    # case) needs no fetch even on a cold cache. ``find_spec`` again (no
    # ``import litellm`` — see the "no eager imports" module docstring).
    # Copy only ONCE (never overwrite an existing destination file) — a
    # file already there might be a real, freshly-fetched answer for a
    # DIFFERENT tiktoken/litellm version pairing than what shipped in
    # litellm's own bundle; this seed step must never regress that.
    #
    # LIMIT, stated plainly rather than silently: this does NOT help a
    # version-INCOMPATIBLE combo (#4395's failure mode B — the bundled
    # blob's sha256 doesn't match what the installed tiktoken expects).
    # tiktoken still detects the mismatch on first use and deletes the
    # seeded file to re-fetch — offline capability is not recoverable for
    # that combination without a real network path at least once; seeding
    # only removes the network round-trip for the compatible case, and
    # (per the redirect above) confines a genuine mismatch's self-deletion
    # to reyn's own directory instead of litellm's installed package.
    #
    # Skip entirely if an operator's own value won the setdefault above
    # (their choice, not reyn's directory, is what actually gets read) —
    # seeding a directory nothing will ever consult is pure waste.
    if (
        os.environ.get("TIKTOKEN_CACHE_DIR") == _tiktoken_cache_dir
        and os.environ.get("CUSTOM_TIKTOKEN_CACHE_DIR") == _tiktoken_cache_dir
    ):
        _litellm_spec = importlib.util.find_spec("litellm")
    else:
        _litellm_spec = None
    if _litellm_spec is not None and _litellm_spec.submodule_search_locations:
        _litellm_tokenizers_dir = os.path.join(
            next(iter(_litellm_spec.submodule_search_locations)),
            "litellm_core_utils",
            "tokenizers",
        )
        try:
            _bundled_names = os.listdir(_litellm_tokenizers_dir)
        except OSError:
            _bundled_names = []
        for _name in _bundled_names:
            # tiktoken's own cache-key naming: sha1(blobpath).hexdigest() —
            # a 40-char lowercase hex string. Filters out this directory's
            # non-cache-blob contents (__init__.py, __pycache__/,
            # anthropic_tokenizer.json — litellm's own Anthropic tokenizer
            # metadata, unrelated to tiktoken's cache scheme) without
            # hardcoding which specific encodings litellm bundles.
            if len(_name) != 40 or not all(c in "0123456789abcdef" for c in _name):
                continue
            _dest = os.path.join(_tiktoken_cache_dir, _name)
            if os.path.exists(_dest):
                continue
            try:
                shutil.copy2(os.path.join(_litellm_tokenizers_dir, _name), _dest)
            except OSError:
                pass  # best-effort — tiktoken's own fetch-and-cache is still correct, just slower
