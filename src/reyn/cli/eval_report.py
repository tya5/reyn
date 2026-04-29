"""
EvalReport: schema and writer for `reyn eval` result JSON.

Pulled out of cmd_eval so the JSON layout has one home and a future
`reyn eval report <path>` viewer subcommand can reuse it.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from reyn.pricing import TokenUsage


@dataclass
class EvalReport:
    spec_path: str
    app: str
    model: str
    cases: list[dict]
    total_tokens: TokenUsage = field(default_factory=TokenUsage)
    total_cost_usd: float = 0.0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def to_dict(self) -> dict:
        return {
            "spec_path":      self.spec_path,
            "app":            self.app,
            "model":          self.model,
            "timestamp":      self.timestamp,
            "total_tokens":   self.total_tokens.to_dict(),
            "total_cost_usd": self.total_cost_usd if self.total_cost_usd > 0 else None,
            "cases":          self.cases,
        }

    def write_to(self, state_dir: Path | str, skill_name: str) -> Path:
        eval_dir = Path(state_dir) / "evals"
        eval_dir.mkdir(parents=True, exist_ok=True)
        path = eval_dir / f"{self.timestamp}_{skill_name}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path
