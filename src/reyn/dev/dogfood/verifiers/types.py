from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

OutcomeBand = Literal["verified", "inconclusive", "refuted", "blocked"]


@dataclass
class VerifierResult:
    """Outcome of one verifier (reply / events / artifacts).

    outcome: 4-band band
    detail: structured machine-readable explanation (= shown in reports)
    """
    outcome: OutcomeBand
    detail: dict = field(default_factory=dict)
