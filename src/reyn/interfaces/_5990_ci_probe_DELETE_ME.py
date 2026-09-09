"""TEMPORARY CI-wiring probe for #5990 (PR #6012) -- DELETE before merge.

Exists only to force a real `pull_request` run of the new
`silent-except-ratchet-gate.yml` workflow (paths: src/reyn/interfaces/**)
so its red/green transition can be observed directly, not just asserted.
Removed in the same PR before it is marked ready.
"""


def probe_silent() -> None:
    try:
        risky()
    except Exception:
        pass


def risky() -> None: ...
