"""Tier 2: #4422 — an ``import litellm`` failure's warn-once log line names
a CAUSE and a remedy, instead of just "it's broken."

Owner + lead-coder spent real hours tracing a real environment's failed
``import litellm`` back to a missing/mismatched tiktoken cache file (#4395,
#4399) before this existed — the fix is making reyn say that in one line so
the next operator (owner's own question: "everyone else who isn't me, what
are THEY supposed to do?") doesn't need the same multi-hour trace.

Two collaborators, both real, no mocks:

- :mod:`reyn._tiktoken_diag` — the shared judgment #4399's own
  ``scripts/diag_tiktoken_cache.py`` used to own alone; ``diagnose()`` is
  exercised here against REAL files on disk (a real cache dir under
  ``tmp_path``, a real copy of litellm's own bundled tokenizer blob — the
  same seeding mechanism ``reyn/__init__.py`` itself uses), not a fake
  return value, for the two branches that can be produced without a real
  network fetch.
- ``reyn.llm.litellm_bootstrap._diagnose_import_failure_for_log`` — the
  message-selection logic. Its 3 outcome branches are driven via a
  monkeypatched ``reyn._tiktoken_diag.diagnose`` returning a real
  ``TiktokenCacheDiagnosis`` instance (the module's own public return
  type, not an invented shape) — legitimate because the branch SELECTION
  is reyn's own logic under test, while the underlying file-system read
  ``diagnose()`` performs is already covered by the ``_tiktoken_diag``
  tests above; and one end-to-end test (real ``import litellm`` failure,
  forced via ``builtins.__import__`` patching — the same technique
  ``test_4395_litellm_import_not_recached_on_failure.py`` already
  established) proving the diagnosis is ACTUALLY appended to the real
  warn-once log line, not just callable in isolation.

lead-coder's review correction (owner's own question forced it): a MISSING
cache file must never be asserted as "tiktoken deleted this" — absence has
more than one cause, and only a directly-read sha256 MISMATCH is a
present-tense, confirmed fact. The missing-file branch's own test asserts
the message does NOT claim deletion happened.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib.util
import logging
import os

import pytest

import reyn.llm.litellm_bootstrap as lb_mod
from reyn._tiktoken_diag import TiktokenCacheDiagnosis, cl100k_expectation, diagnose
from reyn.llm.litellm_bootstrap import _diagnose_import_failure_for_log, ensure_litellm_ready

pytestmark = pytest.mark.skipif(
    cl100k_expectation() is None,
    reason="tiktoken_ext.openai_public.cl100k_base not importable in this environment",
)


def _real_bundled_blob_path() -> "str | None":
    """A REAL bundled cl100k cache blob shipped inside the installed
    litellm package (the same file ``reyn/__init__.py``'s own seeding step
    copies from), or ``None`` if litellm isn't installed / ships none —
    the only way to produce a file with a genuinely CORRECT sha256 without
    a real network fetch (the hash's preimage isn't otherwise derivable)."""
    spec = importlib.util.find_spec("litellm")
    if spec is None or not spec.submodule_search_locations:
        return None
    tokenizers_dir = os.path.join(
        next(iter(spec.submodule_search_locations)), "litellm_core_utils", "tokenizers",
    )
    expectation = cl100k_expectation()
    if expectation is None:
        return None
    blobpath, _ = expectation
    cache_key = hashlib.sha1(blobpath.encode()).hexdigest()
    candidate = os.path.join(tokenizers_dir, cache_key)
    return candidate if os.path.isfile(candidate) else None


# ── reyn._tiktoken_diag.diagnose(): real filesystem, no network ────────────


def test_diagnose_reports_missing_cache_file(tmp_path, monkeypatch):
    """Tier 2: an empty cache dir -> bundled_file_exists=False, sha256_matches
    is None (not applicable — nothing to hash)."""
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CUSTOM_TIKTOKEN_CACHE_DIR", raising=False)

    d = diagnose()

    assert d.bundled_file_exists is False
    assert d.sha256_matches is None


def test_diagnose_reports_a_matching_cache_file(tmp_path, monkeypatch):
    """Tier 2: a real, correctly-named, correctly-hashed cache file (copied
    from litellm's own bundle — real bytes, real sha256, no fetch) ->
    bundled_file_exists=True, sha256_matches=True."""
    blob = _real_bundled_blob_path()
    if blob is None:
        pytest.skip("no real bundled cl100k blob available in this environment")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CUSTOM_TIKTOKEN_CACHE_DIR", raising=False)
    expectation = cl100k_expectation()
    assert expectation is not None
    blobpath, _ = expectation
    cache_key = hashlib.sha1(blobpath.encode()).hexdigest()
    (tmp_path / cache_key).write_bytes(open(blob, "rb").read())

    d = diagnose()

    assert d.bundled_file_exists is True
    assert d.sha256_matches is True


def test_diagnose_reports_a_mismatched_cache_file(tmp_path, monkeypatch):
    """Tier 2: a file present at the right name but WRONG content (the
    #4395 failure-mode B shape — a real, direct mismatch reading) ->
    bundled_file_exists=True, sha256_matches=False."""
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CUSTOM_TIKTOKEN_CACHE_DIR", raising=False)
    expectation = cl100k_expectation()
    assert expectation is not None
    blobpath, _ = expectation
    cache_key = hashlib.sha1(blobpath.encode()).hexdigest()
    (tmp_path / cache_key).write_bytes(b"not the real blob content")

    d = diagnose()

    assert d.bundled_file_exists is True
    assert d.sha256_matches is False


def test_diagnose_reports_unknown_when_the_expectation_is_unreadable(monkeypatch):
    """Tier 2: accept-side — when the installed tiktoken_ext source can't be
    read (a real branch reyn's own code takes, not a third-party promise:
    diagnose() must not crash or fabricate a verdict when its OWN
    precondition fails), both flags come back None (unknown), never a
    fabricated True/False."""
    monkeypatch.setattr("reyn._tiktoken_diag.cl100k_expectation", lambda: None)

    d = diagnose()

    assert d.bundled_file_exists is None
    assert d.sha256_matches is None


# ── litellm_bootstrap._diagnose_import_failure_for_log(): message shape ────


def _diagnosis(**overrides) -> TiktokenCacheDiagnosis:
    base = dict(
        litellm_version="1.0.0", tiktoken_version="0.9.0",
        custom_cache_dir_set=False, bundled_file_exists=None, sha256_matches=None,
    )
    base.update(overrides)
    return TiktokenCacheDiagnosis(**base)


def test_missing_file_message_never_asserts_deletion_as_fact(monkeypatch):
    """Tier 2: lead-coder's review correction — a MISSING file is a
    present-tense "not found," never an asserted-as-fact "was deleted."
    Absence has more than one cause; only a direct mismatch READING (the
    next test) is a confirmed fact."""
    monkeypatch.setattr(
        "reyn._tiktoken_diag.diagnose",
        lambda: _diagnosis(bundled_file_exists=False, sha256_matches=None),
    )

    message = _diagnose_import_failure_for_log()

    assert "not found" in message
    assert "was deleted" not in message and "has been deleted" not in message, (
        f"must not assert deletion as a confirmed fact for a merely-absent file: {message!r}"
    )
    assert "--force-reinstall" in message, "must offer the remedy that also self-diagnoses"


def test_mismatch_message_states_the_confirmed_present_tense_fact(monkeypatch):
    """Tier 2: a DIRECT sha256 mismatch reading IS a confirmed, present-tense
    fact (the file was just read) — this branch may state it as such,
    unlike the missing-file branch above."""
    monkeypatch.setattr(
        "reyn._tiktoken_diag.diagnose",
        lambda: _diagnosis(bundled_file_exists=True, sha256_matches=False),
    )

    message = _diagnose_import_failure_for_log()

    assert "mismatch" in message.lower()
    assert "--force-reinstall" in message


def test_no_cache_signal_message_falls_back_to_network_cert_remedy(monkeypatch):
    """Tier 2: matching (or unknown) cache state carries no diagnostic
    signal of its own — the message falls back to the #4418 network/cert
    remedy rather than claiming a cause it has no evidence for."""
    monkeypatch.setattr(
        "reyn._tiktoken_diag.diagnose",
        lambda: _diagnosis(bundled_file_exists=True, sha256_matches=True),
    )

    message = _diagnose_import_failure_for_log()

    assert "SSL_VERIFY" in message


def test_a_broken_diagnosis_read_never_raises_past_the_warn(monkeypatch):
    """Tier 2: this augments a WARN — a broken diagnostic must not turn one
    warning into a startup-time exception."""
    def _raising():
        raise RuntimeError("simulated diagnosis failure")

    monkeypatch.setattr("reyn._tiktoken_diag.diagnose", _raising)

    message = _diagnose_import_failure_for_log()  # must not raise

    assert "SSL_VERIFY" in message


# ── end-to-end: the diagnosis actually reaches the real warn-once line ─────


@pytest.fixture(autouse=True)
def _clean_litellm_bootstrap_state():
    """Same hygiene as test_4395_litellm_import_not_recached_on_failure.py's
    own fixture — this module's readiness/warn state is process-global."""
    original_ready = lb_mod._litellm_ready
    original_cooldown_until = lb_mod._litellm_import_cooldown_until
    lb_mod._litellm_ready = False
    lb_mod._ready_registry.pop("ready", None)
    lb_mod._litellm_import_failure_warned = False
    lb_mod._litellm_import_cooldown_until = 0.0
    yield
    lb_mod._litellm_ready = original_ready
    lb_mod._ready_registry.pop("ready", None)
    lb_mod._litellm_import_failure_warned = False
    lb_mod._litellm_import_cooldown_until = original_cooldown_until


def test_a_real_failed_import_carries_the_diagnosis_in_its_log_line(monkeypatch, caplog):
    """Tier 2: end-to-end wiring — a REAL forced ``import litellm`` failure
    (``builtins.__import__`` patched, the same technique #4395's own test
    file established — not a mock of any reyn object) produces a warn-once
    log line that carries diagnosis content beyond the original plain
    "import litellm failed" sentence, proving the two are actually wired
    together in production code, not just independently callable."""
    real_import = builtins.__import__

    def _failing_import(name, *args, **kwargs):
        if name == "litellm":
            raise RuntimeError("simulated persistent litellm import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _failing_import)
    monkeypatch.setattr(
        "reyn._tiktoken_diag.diagnose",
        lambda: _diagnosis(bundled_file_exists=False, sha256_matches=None),
    )

    with caplog.at_level(logging.WARNING, logger=lb_mod.__name__):
        result = ensure_litellm_ready()

    assert result is None
    warning_messages = [
        r.getMessage() for r in caplog.records if "import litellm failed" in r.getMessage()
    ]
    (single_warning,) = warning_messages
    assert "not found" in single_warning, (
        f"the diagnosis text must reach the real log line, not just be callable "
        f"in isolation: {single_warning!r}"
    )
