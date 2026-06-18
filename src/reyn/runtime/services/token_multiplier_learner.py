"""TokenMultiplierLearner — per-(model, content_type) EMA-based token estimation.

PR-N6 (FP-0008): adaptive token estimation that learns from the gap between
client-side estimates and server-side actual prompt_tokens counts.

Design
------
- Per-(model, content_type) EMA with alpha=0.1 (slow-moving, stable).
- Cold-start defaults: text=1.05 (1.30 in chars/4 mode), image=1.20,
  audio=1.30, video=1.40, file=1.10.
- Persists to ``~/.reyn/learned_token_multipliers.json`` so learned
  multipliers carry across workspaces / sessions for the same user.
- Atomic write (temp + os.replace) to avoid partial-write corruption.
- Thread-safe via ``threading.Lock``.

Usage
-----
    learner = TokenMultiplierLearner()
    mult = learner.get_multiplier(model="gemini/gemini-2.5-flash-lite", content_type="text")
    estimate = int(raw_estimate * mult)
    # After the LLM call:
    learner.observe(model=..., content_type=..., estimate_tokens=estimate,
                    actual_tokens=response.usage.prompt_tokens)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

CONTENT_TYPES = ("text", "image", "audio", "video", "file")

# Cold-start defaults per content type.
# text chars4 mode: 1.30 (chars//4 tends to under-estimate; safety multiplier).
_DEFAULT_MULTIPLIERS: dict[str, float] = {
    "text":  1.05,
    "image": 1.20,
    "audio": 1.30,
    "video": 1.40,
    "file":  1.10,
}
_EMA_ALPHA = 0.1
_STORAGE_VERSION = 1


class TokenMultiplierLearner:
    """Per-(model, content_type) EMA-based multiplier learning.

    Persists to ``~/.reyn/learned_token_multipliers.json`` so the learned
    multipliers carry across workspaces / sessions for the same user.

    Parameters
    ----------
    storage_path:
        Path to the JSON persistence file.  Defaults to
        ``~/.reyn/learned_token_multipliers.json``.
    chars4_mode:
        When True, the cold-start default for ``text`` is 1.30 (chars//4
        tends to under-estimate more than litellm.token_counter).
    """

    def __init__(
        self,
        storage_path: Path | None = None,
        chars4_mode: bool = False,
    ) -> None:
        self._chars4_mode = chars4_mode
        self._lock = Lock()
        self._storage_path = (
            storage_path
            if storage_path is not None
            else Path.home() / ".reyn" / "learned_token_multipliers.json"
        )
        self._matrix: dict[tuple[str, str], dict] = {}
        self._load()

    def get_multiplier(self, model: str, content_type: str) -> float:
        """Return the current EMA multiplier for (model, content_type).

        Returns the cold-start default when no observations exist yet.
        """
        with self._lock:
            entry = self._matrix.get((model, content_type))
            if entry is None:
                return self._cold_start(content_type)
            return entry["ema"]

    def observe(
        self,
        model: str,
        content_type: str,
        estimate_tokens: int,
        actual_tokens: int,
    ) -> None:
        """Update the EMA multiplier from one (estimate, actual) observation.

        Parameters
        ----------
        model:
            LiteLLM model string.
        content_type:
            One of CONTENT_TYPES.
        estimate_tokens:
            Client-side token estimate (before multiplier application).
        actual_tokens:
            Server-side ``usage.prompt_tokens`` from the LLM API response.
        """
        if estimate_tokens <= 0 or actual_tokens <= 0:
            return  # skip degenerate observations
        gap_ratio = actual_tokens / estimate_tokens
        with self._lock:
            entry = self._matrix.get((model, content_type))
            if entry is None:
                old_ema = self._cold_start(content_type)
                new_ema = (1 - _EMA_ALPHA) * old_ema + _EMA_ALPHA * gap_ratio
                self._matrix[(model, content_type)] = {
                    "ema": new_ema,
                    "n_observations": 1,
                }
            else:
                new_ema = (1 - _EMA_ALPHA) * entry["ema"] + _EMA_ALPHA * gap_ratio
                self._matrix[(model, content_type)] = {
                    "ema": new_ema,
                    "n_observations": entry["n_observations"] + 1,
                }
            self._persist()

    def _cold_start(self, content_type: str) -> float:
        """Return the cold-start default multiplier for a content type."""
        if self._chars4_mode and content_type == "text":
            return 1.30
        return _DEFAULT_MULTIPLIERS.get(content_type, 1.10)

    def _load(self) -> None:
        """Load persisted matrix from storage_path (fail-silent on missing/corrupt)."""
        try:
            data = json.loads(self._storage_path.read_text(encoding="utf-8"))
            if data.get("version") != _STORAGE_VERSION:
                return  # ignore incompatible versions
            for k, v in data.get("multipliers", {}).items():
                model, content_type = k.split("|", 1)
                self._matrix[(model, content_type)] = v
        except (OSError, json.JSONDecodeError, ValueError):
            pass  # cold start on any load failure

    def _persist(self) -> None:
        """Atomically persist the matrix to storage_path (fail-silent on error)."""
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._storage_path.with_suffix(".tmp")
            payload = {
                "version": _STORAGE_VERSION,
                "multipliers": {
                    f"{m}|{c}": v
                    for (m, c), v in self._matrix.items()
                },
            }
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._storage_path)
        except OSError:
            pass  # persistence is best-effort


def detect_content_type(turn_content: object) -> str:
    """Return one of CONTENT_TYPES based on a turn's ``content`` field.

    Parameters
    ----------
    turn_content:
        The ``content`` value from a turn dict.  May be a str (text) or
        a list[dict] (multimodal parts).

    Returns
    -------
    str
        One of: ``"text"``, ``"image"``, ``"audio"``, ``"video"``, ``"file"``.
        Returns ``"text"`` for unknown shapes.
    """
    if isinstance(turn_content, str):
        return "text"
    if isinstance(turn_content, list):
        for part in turn_content:
            if not isinstance(part, dict):
                continue
            t = part.get("type")
            if t in ("image_url", "image_path", "image"):
                return "image"
            if t in ("input_audio", "audio_url", "audio"):
                return "audio"
            if t in ("video_url", "video_path", "video"):
                return "video"
            if t in ("file", "document"):
                return "file"
    return "text"


__all__ = [
    "CONTENT_TYPES",
    "TokenMultiplierLearner",
    "detect_content_type",
]
