"""TEMPORARY CI-wiring probe for #5990 (PR #6012) -- DELETE before merge.

Exists only to force a real `pull_request` run of the new
`silent-except-ratchet-gate.yml` workflow (paths: src/reyn/interfaces/**)
so its red/green transition can be observed directly, not just asserted.
Removed in the same PR before it is marked ready.

Stage 2 (false-reject direction): the except below now carries a visible
marker (a re-raise) -- CI must go GREEN, confirming the gate does not
flag a genuinely reported except just because this file exists at all.
"""
import logging

logger = logging.getLogger(__name__)


def probe_silent() -> None:
    try:
        risky()
    except Exception:
        logger.error("risky() failed")


def risky() -> None: ...
