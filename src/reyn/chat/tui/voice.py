"""Voice input via local Whisper for `reyn chat` TUI.

This module is **optional**. It is imported lazily by ``app.py`` only when the
user actually presses F2, so a base install (without the ``reyn[voice]``
extra) never pays the dependency cost and never crashes on import.

Public surface (kept tiny on purpose — ``app.py`` should not need to know
about the underlying audio pipeline):

  * :class:`VoiceInput` — owns one mic stream + one Whisper model.
    - ``available()``      → bool, deps importable
    - ``start_recording()`` → begin capturing into an in-memory numpy buffer
    - ``stop_recording()``  → coroutine, returns transcribed text
    - ``cancel()``          → discard the current buffer without transcribing

  * :class:`VoiceUnavailable` — raised by ``start_recording()`` when the
    optional extras are missing. Caller surfaces a friendly message.

Design notes:

  * ``faster-whisper`` is loaded once and cached in the instance — first call
    pays the model-download / load cost (seconds), subsequent calls are fast.
  * Audio capture uses a sounddevice ``InputStream`` whose callback runs on
    PortAudio's own thread. Chunks are appended to a ``list[np.ndarray]``;
    only the main thread reads it after stop.
  * Inference is dispatched to ``asyncio.to_thread`` so the Textual event
    loop never blocks. A ``small`` model on Apple Silicon transcribes 5 s of
    audio in ~1 s; ``medium`` ~3 s.

P7 / engine-design boundary: this module lives entirely under
``chat/tui/``. The OS layer (skills / phases / runtime) has no awareness of
voice input.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class VoiceUnavailable(RuntimeError):
    """Raised when the optional ``reyn[voice]`` deps cannot be imported."""


def _try_import() -> tuple[Any, Any] | None:
    """Return (sounddevice, numpy) or None if either is missing.

    Kept as a helper so ``available()`` and ``start_recording()`` agree on the
    import sites.
    """
    try:
        import numpy as _np
        import sounddevice as _sd
    except Exception as exc:
        logger.debug("voice deps unavailable: %s", exc)
        return None
    return _sd, _np


class VoiceInput:
    """One mic stream + one cached Whisper model for the lifetime of the TUI.

    Not thread-safe by design — all calls happen on the Textual main loop
    (the actual audio callback runs on PortAudio's thread but only appends
    to ``self._chunks``, which is read only after stop).
    """

    def __init__(
        self,
        *,
        model: str = "small",
        language: str | None = None,
        device: str = "auto",
        compute_type: str = "int8",
        sample_rate: int = 16000,
    ) -> None:
        self._model_name = model
        self._language = language
        self._device = device
        self._compute_type = compute_type
        self._sample_rate = sample_rate

        # Lazily-imported handles (None until first use)
        self._sd: Any = None
        self._np: Any = None
        self._whisper_model: Any = None

        # Recording state
        self._stream: Any = None
        self._chunks: list[Any] = []
        self._recording: bool = False

    # ── availability ─────────────────────────────────────────────────────────

    @staticmethod
    def available() -> bool:
        """True iff the optional extras (sounddevice + numpy) import cleanly.

        Whisper itself is not checked here — its import is deferred until
        the first transcription so that simply pressing F2 once doesn't
        block on a 1.5 GB model download.
        """
        return _try_import() is not None

    @property
    def is_recording(self) -> bool:
        return self._recording

    # ── recording control ───────────────────────────────────────────────────

    def start_recording(self) -> None:
        """Open the mic stream and begin appending chunks to the buffer.

        Raises :class:`VoiceUnavailable` when sounddevice / numpy are missing.
        Subsequent ``start_recording()`` calls while already recording are
        silently no-ops.
        """
        if self._recording:
            return
        deps = _try_import()
        if deps is None:
            raise VoiceUnavailable(
                "voice extras not installed; run: pip install \"reyn[voice]\""
            )
        self._sd, self._np = deps
        self._chunks = []

        def _callback(indata, frames, time_info, status) -> None:
            if status:
                logger.debug("sounddevice status: %s", status)
            # indata is float32 (n_frames, n_channels). Squeeze to mono and
            # copy because the buffer is reused by PortAudio after callback
            # returns.
            self._chunks.append(indata.copy())

        try:
            self._stream = self._sd.InputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
                callback=_callback,
            )
            self._stream.start()
            self._recording = True
        except Exception as exc:
            logger.warning("voice start_recording failed: %s", exc)
            self._stream = None
            self._chunks = []
            raise VoiceUnavailable(f"failed to open microphone: {exc}") from exc

    def cancel(self) -> None:
        """Stop the stream and drop the buffer without transcribing."""
        self._close_stream()
        self._chunks = []
        self._recording = False

    async def stop_recording(self) -> tuple[str, dict]:
        """Stop the stream, return ``(transcribed_text, diagnostics)``.

        The diagnostics dict is intentionally exposed so the TUI can show
        actionable hints when the result is empty:

          * ``duration_s``  — wall length of the captured audio
          * ``peak``        — max absolute sample amplitude (0.0–1.0)
          * ``rms``         — root-mean-square (rough loudness)
          * ``reason``      — ``"ok" | "no_audio" | "silent" | "error"``

        Errors during transcription are logged and surfaced as an empty
        string + ``reason="error"`` so the TUI never crashes.
        """
        diag: dict = {"duration_s": 0.0, "peak": 0.0, "rms": 0.0, "reason": "no_audio"}
        if not self._recording:
            return "", diag
        self._close_stream()
        self._recording = False

        if self._np is None or not self._chunks:
            return "", diag

        # Concatenate captured chunks into one (n_samples,) float32 array.
        try:
            audio = self._np.concatenate(self._chunks, axis=0).reshape(-1)
        except Exception as exc:
            logger.warning("voice concat failed: %s", exc)
            self._chunks = []
            diag["reason"] = "error"
            return "", diag
        self._chunks = []

        if audio.size == 0:
            return "", diag

        diag["duration_s"] = float(audio.size) / float(self._sample_rate)
        try:
            diag["peak"] = float(self._np.max(self._np.abs(audio)))
            diag["rms"] = float(self._np.sqrt(self._np.mean(audio.astype("float64") ** 2)))
        except Exception:
            pass

        # If the capture is essentially silence, skip the model call entirely
        # — Whisper hallucinates on pure-noise input.
        if diag["peak"] < 0.005:
            diag["reason"] = "silent"
            return "", diag

        # Run transcription off the Textual event loop.
        try:
            text = await asyncio.to_thread(self._transcribe_sync, audio)
        except Exception as exc:
            logger.warning("voice transcribe failed: %s", exc)
            diag["reason"] = "error"
            return "", diag

        diag["reason"] = "ok" if text else "silent"
        return text, diag

    # ── internals ───────────────────────────────────────────────────────────

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.debug("voice stream close: %s", exc)
            self._stream = None

    def _ensure_model(self) -> Any:
        """Lazy-load the faster-whisper model; cache for subsequent calls.

        First call may take seconds (model download + CTranslate2 init) so it
        always runs inside ``asyncio.to_thread`` via ``stop_recording``.
        """
        if self._whisper_model is not None:
            return self._whisper_model
        try:
            from faster_whisper import WhisperModel  # type: ignore[import]
        except Exception as exc:
            raise VoiceUnavailable(
                "faster-whisper not installed; run: pip install \"reyn[voice]\""
            ) from exc
        self._whisper_model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
        )
        return self._whisper_model

    def _transcribe_sync(self, audio) -> str:
        """Run Whisper. VAD is disabled by default — it's the #1 cause of
        spurious "no speech detected" results because the Silero threshold
        rejects quiet-but-real speech. We do our own peak gate up-front
        instead (see ``stop_recording``)."""
        model = self._ensure_model()
        segments, _info = model.transcribe(
            audio,
            language=self._language,
            beam_size=1,           # speed > marginal accuracy for dictation
            vad_filter=False,
        )
        return "".join(seg.text for seg in segments).strip()


__all__ = ["VoiceInput", "VoiceUnavailable"]
