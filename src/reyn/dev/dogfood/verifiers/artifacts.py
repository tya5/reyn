"""Artifact presence verifier (FP-0036 Component C).

Each ArtifactAssertion matches against the workspace's artifact records.
``artifacts`` is a list of dicts shaped {"skill": str, "type": str, "data": dict, ...}.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from .types import VerifierResult

if TYPE_CHECKING:
    from reyn.dogfood.scenarios import ArtifactAssertion, ExpectedArtifacts


# ---------------------------------------------------------------------------
# Fingerprint helper
# ---------------------------------------------------------------------------


def _fingerprint(data: dict) -> str:
    """Compute SHA256 of normalised JSON content.

    Normalisation: json.dumps with sort_keys=True and compact separators.
    """
    normalised = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalised.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def _artifact_matches_filters(artifact: dict, assertion: "ArtifactAssertion") -> bool:
    """Return True if artifact passes the skill/type filters of assertion.

    None filter means "any" (= no restriction on that dimension).
    """
    if assertion.skill is not None and artifact.get("skill") != assertion.skill:
        return False
    if assertion.type is not None and artifact.get("type") != assertion.type:
        return False
    return True


def _check_assertion(
    assertion: "ArtifactAssertion",
    artifacts: list[dict],
) -> dict | None:
    """Return a failure dict if assertion is violated, else None."""
    candidates = [a for a in artifacts if _artifact_matches_filters(a, assertion)]

    if assertion.present:
        # At least one matching artifact required
        if not candidates:
            return {
                "check": "present",
                "skill": assertion.skill,
                "type": assertion.type,
                "reason": "no matching artifact found",
            }
        # Fingerprint check (when set): at least one candidate must match
        if assertion.fingerprint is not None:
            fp_matches = [
                a for a in candidates
                if _fingerprint(a.get("data", a)) == assertion.fingerprint
            ]
            if not fp_matches:
                computed = [_fingerprint(a.get("data", a)) for a in candidates]
                return {
                    "check": "fingerprint",
                    "skill": assertion.skill,
                    "type": assertion.type,
                    "expected_fingerprint": assertion.fingerprint,
                    "found_fingerprints": computed,
                    "reason": "no artifact matched the expected fingerprint",
                }
    else:
        # present=False: no matching artifact should exist
        if candidates:
            return {
                "check": "absent",
                "skill": assertion.skill,
                "type": assertion.type,
                "found_count": len(candidates),
                "reason": "artifact found but expected to be absent",
            }

    return None


# ---------------------------------------------------------------------------
# Public verifier
# ---------------------------------------------------------------------------


def verify_artifacts(
    expected: "ExpectedArtifacts | None",
    artifacts: list[dict],
) -> VerifierResult:
    """Score the workspace artifacts list against expected.

    For each ArtifactAssertion:
      - skill filter: match artifacts whose skill == assertion.skill (None = any)
      - type filter: match artifacts whose type == assertion.type (None = any)
      - present: True → at least one matching artifact required; False → none
      - fingerprint: SHA256 of normalised JSON content; when set, at least one
        matching artifact must have that fingerprint

    Returns the worst-case across assertions.

    Parameters
    ----------
    expected:
        The ExpectedArtifacts declared in the scenario. ``None`` → blocked.
    artifacts:
        List of artifact dicts from the workspace snapshot.

    Returns
    -------
    VerifierResult with outcome:
      verified — all assertions satisfied
      refuted  — any assertion violated (detail records each failure)
      blocked  — no expected provided
    """
    if expected is None:
        return VerifierResult(outcome="blocked", detail={"reason": "no expected artifacts declared"})

    failures: list[dict] = []
    for assertion in expected.items:
        failure = _check_assertion(assertion, artifacts)
        if failure is not None:
            failures.append(failure)

    if failures:
        return VerifierResult(outcome="refuted", detail={"failures": failures})

    return VerifierResult(
        outcome="verified",
        detail={"assertions_checked": len(expected.items)},
    )
