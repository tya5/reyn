"""A persistent, environmental failure needs a COOLDOWN, not a permanent
give-up and not a retry on literally every call (#4395/#4398 — the same
class, closed here in ONE place instead of two independent
reimplementations).

Two distinct, real instances of this exact shape landed the same night:

- ``services/compaction/engine.py``'s ``estimate_tokens()`` (#4398): a
  ``litellm.token_counter`` CALL that keeps failing for an environmental
  reason (SSL egress blocked) was retried on every distinct text string
  (the result cache is keyed by text, so a new string is always a miss) —
  synchronously, from turn processing, blocking the UI each time.
- ``llm/litellm_bootstrap.py``'s ``ensure_litellm_ready()`` (#4395): a
  failed ``import litellm`` is REMOVED from ``sys.modules`` by Python
  itself (any module whose top-level code raises never gets cached), so
  the NEXT ``import litellm`` anywhere in the process starts completely
  from scratch and hits the exact same failure again — an IMPORT failure,
  not a call failure, but the same "persistent environmental failure,
  retried every single call" shape. PR-1 (#4413) closed the WITHIN-one-call
  double-attempt; this module backs PR-2's fix for the ACROSS-calls
  repeat (#4395 axis②) — a live owner repro caught the process still
  re-attempting, and re-hanging on, the same broken TLS handshake on
  every subsequent call after PR-1 alone landed.

Both need: a failure starts a cooldown window; any attempt inside it skips
straight to the fallback (or, for ``ensure_litellm_ready()``, returns
``None`` immediately — never a permanent give-up), no wait paid; a success
(during or after the cooldown) clears it; the window is temporary — the
underlying cause may clear (a proxy comes back up) and there is no restart
hook to notice that on its own, so the next attempt after the window
elapses always re-probes.

This module holds only the two PURE, STATE-FREE comparisons that shape
needs — ``time.monotonic()`` reads and arithmetic. Each call site still
owns its OWN cooldown-deadline variable (a plain module float, matching
the existing "no lock, unlocked check-then-set, worst case is a redundant
early/late re-probe, never a wrong count" reasoning both #4395 and #4398
already use) — sharing the STATE across unrelated failure classes (an
import failure and a token-counter-call failure) would be wrong; sharing
this tiny piece of ARITHMETIC is not.
"""
from __future__ import annotations

import time


def in_cooldown(cooldown_until: float) -> bool:
    """True while *cooldown_until* (an absolute ``time.monotonic()``
    deadline, or ``0.0``/negative for "no active cooldown") is still in the
    future. ``time.monotonic()`` — never wall-clock — so an NTP step or a
    sleep/resume cannot shorten OR lengthen the wait (#4398's own reasoning,
    reused verbatim here)."""
    return time.monotonic() < cooldown_until


def new_cooldown_deadline(seconds: float) -> float:
    """The absolute ``time.monotonic()`` deadline for a cooldown window of
    *seconds* starting now — call this on a fresh failure to arm/extend a
    call site's own cooldown-deadline variable."""
    return time.monotonic() + seconds
