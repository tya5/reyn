"""Voice input via local Whisper for `reyn chat`'s inline CUI (#4187 revival).

Reimplementation, not restoration — the retired Textual TUI this shipped
against (``chat/tui/``, deleted alongside the god-package split, #4187 issue
body's own measurement) is gone, so the interface glue below is new. The STT
core (record → concatenate → gate on silence → transcribe) is ported near
verbatim from the retired ``chat/tui/voice.py`` (``git show
ca42d1d0d:src/reyn/chat/tui/voice.py``) — that half was already
interface-agnostic and #4187's own pre-measurement confirmed the
``faster-whisper`` API it calls (``WhisperModel.__init__`` /
``.transcribe()``) is unchanged in the currently-pinned range (measured
directly against ``faster-whisper==1.2.1``, the newest release under this
repo's open ``>=1.0.0`` pin).

Public surface (mirrors :mod:`reyn.interfaces.inline.textual_chat.text_effect`'s
optional-extra shape on purpose — two sibling opt-in features, one convention):

  * :func:`available` — bool, deps importable (``find_spec``, not a try/import
    — this is asked on every key press, and importing the library to answer
    "is it here" would pay the import on a press that's about to say "no").
  * :func:`unavailable_message` — what to tell the operator, naming the extra.
  * :class:`VoiceInput` — owns one mic stream + one cached Whisper model.
  * :class:`VoiceUnavailable` — raised by ``start_recording()`` / model load
    when the optional extras are missing or the mic can't be opened.

Design notes (unchanged from the retired module):

  * ``faster-whisper`` is loaded once and cached on the instance — first call
    pays the model-download / load cost (seconds), subsequent calls are fast.
  * Audio capture uses a sounddevice ``InputStream`` whose callback runs on
    PortAudio's own thread. Chunks are appended to a ``list[np.ndarray]``;
    only the main thread reads it after stop (not thread-safe by design —
    every OTHER call happens on the Textual main loop).
  * Inference is dispatched to ``asyncio.to_thread`` so the Textual event
    loop never blocks.
  * VAD is disabled by default — it was the #1 cause of spurious "no speech
    detected" results (the Silero threshold rejects quiet-but-real speech);
    a peak-amplitude gate does the same job without that failure mode.

This module is TTY-only ``textual_chat`` package territory but does NOT
itself import ``textual`` — ``sounddevice``/``faster-whisper`` are imported
lazily inside functions so a base install (no ``reyn[voice]`` extra) never
pays their cost and the module itself stays importable for the
``available()`` check alone.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: An EXTRA rather than a core dependency (same rationale as #3796's
#: ``effects`` extra, ``text_effect.py``): not everyone who installs reyn
#: should carry an audio-capture + local-STT stack for an opt-in dictation
#: key. The message names the extra, never the raw packages — an operator
#: told to install a package directly gets a working key and never learns
#: the extra exists, which is the extra failing at the one job it has.
_DEPS = ("sounddevice", "faster_whisper")
_EXTRA = "voice"


class VoiceUnavailable(RuntimeError):
    """Raised when the optional ``reyn[voice]`` deps cannot be imported, or
    the microphone cannot be opened."""


def available() -> bool:
    """Whether both optional deps (``sounddevice``, ``faster-whisper``) are
    importable.

    Checked through ``find_spec`` rather than a try/import, mirroring
    :func:`reyn.interfaces.inline.textual_chat.text_effect.available` — this
    is asked on the key press, and importing either library to answer "is it
    here" would pay the import on a press that is about to say "no".
    """
    import importlib.util

    return all(importlib.util.find_spec(dep) is not None for dep in _DEPS)


def unavailable_message() -> str:
    """What to tell the operator when a dep is absent.

    Names the install, because "unavailable" without a remedy is a dead end
    — and this is the one failure mode of the feature that is not a bug.
    """
    return (
        f"voice input needs the optional '{_EXTRA}' extra — "
        f"pip install 'reyn[{_EXTRA}]'"
    )


def _try_import() -> tuple[Any, Any] | None:
    """Return (sounddevice, numpy) or None if either is missing.

    Kept as a helper so :func:`available` (via ``find_spec``, no import paid)
    and :meth:`VoiceInput.start_recording` (which needs the real modules)
    agree on the import sites.
    """
    try:
        import numpy as _np
        import sounddevice as _sd
    except Exception as exc:
        logger.debug("voice deps unavailable: %s", exc)
        return None
    return _sd, _np


class VoiceInput:
    """One mic stream + one cached Whisper model for the lifetime of the app.

    Not thread-safe by design — all calls happen on the Textual main loop
    (the actual audio callback runs on PortAudio's thread but only appends
    to ``self._chunks``, which is read only after stop).
    """

    def __init__(
        self,
        *,
        model: str = "small",
        language: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        sample_rate: int = 16000,
        cpu_threads: int = 4,
        num_workers: int = 1,
    ) -> None:
        self._model_name = model
        self._language = language
        self._device = device
        self._compute_type = compute_type
        self._sample_rate = sample_rate
        self._cpu_threads = cpu_threads
        self._num_workers = num_workers

        # Lazily-imported handles (None until first use)
        self._sd: Any = None
        self._np: Any = None
        self._whisper_model: Any = None

        # Recording state
        self._stream: Any = None
        self._chunks: list[Any] = []
        self._recording: bool = False

    # ── availability ─────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._recording

    # ── recording control ───────────────────────────────────────────────

    def start_recording(self) -> None:
        """Open the mic stream and begin appending chunks to the buffer.

        Raises :class:`VoiceUnavailable` when sounddevice / numpy are missing
        or the microphone cannot be opened. Subsequent ``start_recording()``
        calls while already recording are silently no-ops.
        """
        if self._recording:
            return
        deps = _try_import()
        if deps is None:
            raise VoiceUnavailable(unavailable_message())
        self._sd, self._np = deps
        self._chunks = []

        def _callback(indata, frames, time_info, status) -> None:
            if status:
                logger.debug("sounddevice status: %s", status)
            # indata is float32 (n_frames, n_channels). Copied because the
            # buffer is reused by PortAudio after the callback returns.
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

        The diagnostics dict is intentionally exposed so the caller can show
        actionable hints when the result is empty:

          * ``duration_s``  — wall length of the captured audio
          * ``peak``        — max absolute sample amplitude (0.0-1.0)
          * ``rms``         — root-mean-square (rough loudness)
          * ``reason``      — ``"ok" | "no_audio" | "silent" | "error"``

        Errors during transcription are logged and surfaced as an empty
        string + ``reason="error"`` so the caller never crashes on them.
        """
        diag: dict = {"duration_s": 0.0, "peak": 0.0, "rms": 0.0, "reason": "no_audio"}
        if not self._recording:
            return "", diag
        self._close_stream()
        self._recording = False

        if self._np is None or not self._chunks:
            return "", diag

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

        # Essentially silence — skip the model call entirely; Whisper
        # hallucinates on pure-noise input.
        if diag["peak"] < 0.005:
            diag["reason"] = "silent"
            return "", diag

        try:
            text = await asyncio.to_thread(self._transcribe_sync, audio)
        except Exception as exc:
            logger.warning("voice transcribe failed: %s", exc)
            diag["reason"] = "error"
            return "", diag

        diag["reason"] = "ok" if text else "silent"
        return text, diag

    # ── internals ──────────────────────────────────────────────────────

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
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except Exception as exc:
            raise VoiceUnavailable(unavailable_message()) from exc
        self._whisper_model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
            num_workers=self._num_workers,
        )
        return self._whisper_model

    def _transcribe_sync(self, audio) -> str:
        """Run Whisper. VAD stays off — see the module docstring."""
        model = self._ensure_model()
        segments, _info = model.transcribe(
            audio,
            language=self._language,
            beam_size=1,           # speed > marginal accuracy for dictation
            vad_filter=False,
        )
        return "".join(seg.text for seg in segments).strip()


__all__ = ["VoiceInput", "VoiceUnavailable", "available", "unavailable_message"]
