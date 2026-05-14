"""SkillSearchIndex — BM25-based skill pre-filter (FP-0024 Component A).

Indexes skill name + description, supports top-K search by user query.
Per-session index, rebuilt when skill registry changes.

Future: embedding backend (Component C, hybrid). BM25 stands alone for now.

BM25 implementation: Robertson-Sparck-Jones formula, k1=1.5, b=0.75.
Tokenisation: whitespace + lowercase + punctuation-strip (ASCII).
No external deps — avoids adding rank-bm25 to pyproject.toml at this scale.
"""
from __future__ import annotations

import math
import re
import string
from dataclasses import dataclass


@dataclass
class SkillCandidate:
    name: str
    description: str
    score: float


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip ASCII punctuation, split on whitespace."""
    table = str.maketrans("", "", string.punctuation)
    return text.lower().translate(table).split()


class BM25Backend:
    """Pure-Python BM25 (Robertson-Sparck-Jones, k1=1.5, b=0.75).

    For Reyn's scale (~30-50 skills currently, future 1000+), pure
    Python is fine; no need for sklearn or external packages.
    """

    _k1: float = 1.5
    _b: float = 0.75

    def __init__(self, skills: list[dict]) -> None:
        """Index skills from [{name, description, ...}] dicts.

        Tokenises ``name`` + space + ``description`` as the document corpus.
        """
        self._skills: list[dict] = skills
        self._corpus: list[list[str]] = [
            _tokenize(
                (s.get("name") or "") + " " + (s.get("description") or "")
            )
            for s in skills
        ]
        n = len(self._corpus)
        self._avgdl: float = (
            sum(len(doc) for doc in self._corpus) / n if n else 0.0
        )
        # Document frequency (df[term] = number of docs containing term)
        self._df: dict[str, int] = {}
        for doc in self._corpus:
            for term in set(doc):
                self._df[term] = self._df.get(term, 0) + 1
        self._n: int = n

    def search(self, query: str, top_k: int = 5) -> list[SkillCandidate]:
        """Return up to top_k SkillCandidate instances ranked by BM25 score.

        Returns an empty list when the corpus is empty or no document scores
        above 0.  The caller is responsible for fall-through on empty results.
        """
        if not self._n:
            return []

        q_terms = _tokenize(query)
        if not q_terms:
            return []

        scores: list[float] = [0.0] * self._n
        k1, b, n = self._k1, self._b, self._n
        avgdl = self._avgdl

        for term in q_terms:
            df_t = self._df.get(term, 0)
            if df_t == 0:
                continue
            # IDF (Robertson-Jones smooth form with +0.5 smoothing)
            idf = math.log((n - df_t + 0.5) / (df_t + 0.5) + 1.0)
            for i, doc in enumerate(self._corpus):
                tf = doc.count(term)
                if tf == 0:
                    continue
                dl = len(doc)
                denom = tf + k1 * (1 - b + b * dl / avgdl) if avgdl else tf + k1
                scores[i] += idf * (tf * (k1 + 1)) / denom

        # Pair with skills, sort descending, return top_k non-zero
        ranked = sorted(
            (
                (scores[i], self._skills[i])
                for i in range(self._n)
                if scores[i] > 0.0
            ),
            key=lambda x: x[0],
            reverse=True,
        )
        return [
            SkillCandidate(
                name=skill.get("name", ""),
                description=skill.get("description", ""),
                score=score,
            )
            for score, skill in ranked[:top_k]
        ]
