"""Voice input via local Whisper for `reyn chat` TUI.

Debug mode: set ``REYN_VOICE_DEBUG=1`` (any truthy value) before launching
``reyn chat`` and every captured buffer will be saved to ``/tmp/reyn-voice-
debug-<ts>.wav`` before transcription. Useful when the mic looks healthy
(peak > 0.05) but Whisper still returns empty — playing the WAV back
confirms the audio is intelligible, narrowing the bug to model / config.


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
import os
import threading
import time as _time
import wave
from typing import Any

# IMPORTANT: these env vars must be set BEFORE faster-whisper / huggingface_hub /
# tqdm import, because they're read at import time, not at call time. We do it at
# module level so they're in place by the time `_ensure_model` invokes WhisperModel.
#
# Why we need them: Textual replaces sys.stdout/stderr with custom stream objects
# whose .fileno() returns -1 (no real OS file descriptor). When huggingface_hub
# (faster-whisper's downloader) tries to render a tqdm progress bar, tqdm spawns
# a helper subprocess via _posixsubprocess.fork_exec(...). That call validates
# every fd in fds_to_keep is ≥ 0 — and our wrapped streams' -1 explodes with the
# observed `ValueError('bad value(s) in fds_to_keep')`.
#
# Setting these three is belt-and-braces: each lib reads a different env var.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("DISABLE_TQDM", "1")
os.environ.setdefault("TQDM_DISABLE", "1")

logger = logging.getLogger(__name__)

# Optional sink for REYN_VOICE_DEBUG — write directly to a file so the
# user doesn't need to redirect stderr (which can interfere with the
# Textual rendering pipeline). Path is fixed so `tail -f` works.
_DEBUG_LOG_PATH = "/tmp/reyn-voice.log"


def _vlog(msg: str) -> None:
    """Append one line to the voice debug log when REYN_VOICE_DEBUG is set."""
    if not os.environ.get("REYN_VOICE_DEBUG"):
        return
    try:
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{_time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


class _real_stdio_during_load:  # noqa: N801 — context-manager naming intentional
    """Context manager: swap sys.stdout/stderr to /dev/null during model load.

    Textual replaces ``sys.stdout`` / ``sys.stderr`` with stream objects whose
    ``.fileno()`` returns -1. When faster-whisper / huggingface_hub / tokenizers
    transitively spawn a subprocess (progress bar helper, tokenizer warm-up,
    telemetry probe), Python's ``_posixsubprocess.fork_exec`` validates every
    fd in ``fds_to_keep`` and rejects -1 with::

        ValueError("bad value(s) in fds_to_keep")

    Swapping in real ``open(os.devnull, "w")`` handles for the duration of the
    load gives any such subprocess a valid fileno to inherit. The OS-level
    fds 0/1/2 (which Textual *doesn't* touch — it only wraps the Python
    objects) stay untouched, so the terminal's actual rendering pipeline is
    unaffected.

    Best-effort: swallows its own setup errors so the caller still attempts
    the load even if /dev/null can't be opened (some sandbox environments).
    """

    def __enter__(self) -> "_real_stdio_during_load":
        import sys
        self._saved_stdout = sys.stdout
        self._saved_stderr = sys.stderr
        self._devnull: Any = None
        try:
            self._devnull = open(os.devnull, "w")
            sys.stdout = self._devnull
            sys.stderr = self._devnull
        except Exception as exc:
            _vlog(f"stdio swap setup failed ({exc!r}) — continuing without it")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import sys
        sys.stdout = self._saved_stdout
        sys.stderr = self._saved_stderr
        if self._devnull is not None:
            try:
                self._devnull.close()
            except Exception:
                pass


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
        device: str = "cpu",
        compute_type: str = "int8",
        sample_rate: int = 16000,
        cpu_threads: int = 4,
        num_workers: int = 1,
        max_duration_s: float = 300.0,
    ) -> None:
        self._model_name = model
        self._language = language
        self._device = device
        self._compute_type = compute_type
        self._sample_rate = sample_rate
        self._cpu_threads = cpu_threads
        self._num_workers = num_workers
        self._max_duration_s = max_duration_s

        # Lazily-imported handles (None until first use)
        self._sd: Any = None
        self._np: Any = None
        self._whisper_model: Any = None
        # Serialise WhisperModel construction across worker threads. Without
        # this, the BG `preload_model()` thread and the foreground transcribe
        # thread can both observe `self._whisper_model is None` and BOTH call
        # WhisperModel(...) concurrently — racing on the model-cache files
        # and (empirically, 2026-05-09) deadlocking inside CTranslate2.
        self._model_lock = threading.Lock()

        # Recording state
        self._stream: Any = None
        self._chunks: list[Any] = []
        self._recording: bool = False
        # Wall-clock start of the current recording, for the
        # ``max_duration_s`` safeguard. ``0.0`` when not recording.
        self._recording_started_at: float = 0.0

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

    @property
    def recording_elapsed_s(self) -> float:
        """Seconds since the current recording started (0 when not recording)."""
        if not self._recording or self._recording_started_at <= 0.0:
            return 0.0
        return _time.monotonic() - self._recording_started_at

    @property
    def max_duration_s(self) -> float:
        """Configured cap on recording length (0 = no cap)."""
        return self._max_duration_s

    # ── recording control ───────────────────────────────────────────────────

    async def start_recording(self) -> None:
        """Open the mic stream and begin appending chunks to the buffer.

        Async because the underlying ``sd.InputStream(...)`` constructor +
        ``.start()`` are synchronous and **can block** on macOS when the
        audio subsystem is contended (= other apps holding the input
        device, AirPods reconnecting, CoreAudio HAL grumpy). Inline
        calls froze the event loop and produced the user-reported
        "Ctrl+R では時計止まったまま" symptom. We run the open in a
        worker thread with a 5-second timeout and translate any failure
        / timeout into a ``VoiceUnavailable`` exception that the TUI
        already knows how to surface.

        Raises :class:`VoiceUnavailable` when sounddevice / numpy are
        missing OR the audio device cannot be opened in time. Subsequent
        ``start_recording()`` calls while already recording are silently
        no-ops.
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
        _vlog("start_recording: opening InputStream")
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._open_stream_sync),
                timeout=5.0,
            )
        except asyncio.TimeoutError as exc:
            _vlog("start_recording: InputStream open timed out (5s)")
            self._stream = None
            self._chunks = []
            raise VoiceUnavailable(
                "microphone open timed out (5s) — another app may be "
                "holding the device; check System Settings → Sound → Input"
            ) from exc
        except Exception as exc:
            _vlog(f"start_recording: InputStream open failed: {exc!r}")
            self._stream = None
            self._chunks = []
            raise VoiceUnavailable(f"failed to open microphone: {exc}") from exc
        self._recording = True
        self._recording_started_at = _time.monotonic()
        _vlog("start_recording: InputStream live")

    def _open_stream_sync(self) -> None:
        """Synchronous body of the InputStream open. Runs in a worker thread."""
        def _callback(indata, frames, time_info, status) -> None:
            if status:
                logger.debug("sounddevice status: %s", status)
            # indata is float32 (n_frames, n_channels). Squeeze to mono and
            # copy because the buffer is reused by PortAudio after callback
            # returns.
            self._chunks.append(indata.copy())

        try:
            stream = self._sd.InputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
                callback=_callback,
            )
            stream.start()
            self._stream = stream
        except Exception:
            # Make sure we don't leak a half-opened stream into self.
            self._stream = None
            raise

    def cancel(self) -> None:
        """Stop the stream and drop the buffer without transcribing."""
        self._close_stream()
        self._chunks = []
        self._recording = False
        self._recording_started_at = 0.0

    @property
    def model_loaded(self) -> bool:
        """True iff the Whisper model has already been loaded into memory."""
        return self._whisper_model is not None

    async def preload_model(self) -> None:
        """Force-load the Whisper model in a worker thread.

        Called from the TUI right after the user starts recording, so the
        slow first-time model load (= 460 MB download for `small`, then
        CTranslate2 init) overlaps with the user actually speaking. By the
        time they press Ctrl+R the second time, the model is hot and the
        transcribe call returns in ~1 s instead of ~30 s+.

        Idempotent — repeat calls are no-ops once the model is cached.
        """
        if self._whisper_model is not None:
            return
        try:
            await asyncio.to_thread(self._ensure_model)
        except Exception as exc:
            logger.warning("voice preload_model failed: %s", exc)

    async def stop_recording(self) -> tuple[str, dict]:
        """Stop the stream, return ``(transcribed_text, diagnostics)``.

        The diagnostics dict is intentionally exposed so the TUI can show
        actionable hints when the result is empty:

          * ``duration_s``  — wall length of the captured audio
          * ``peak``        — max absolute sample amplitude (0.0–1.0)
          * ``rms``         — root-mean-square (rough loudness)
          * ``reason``      — ``"ok" | "no_audio" | "silent" | "error" | "timeout"``

        Errors during transcription are logged and surfaced as an empty
        string + ``reason="error"`` so the TUI never crashes.
        """
        diag: dict = {"duration_s": 0.0, "peak": 0.0, "rms": 0.0, "reason": "no_audio"}
        if not self._recording:
            return "", diag

        # PortAudio's ``InputStream.stop()`` / ``close()`` can block for
        # multiple seconds on macOS when the audio subsystem is grumpy
        # (= other apps holding the input device, AirPods reconnecting,
        # CoreAudio HAL contention). Calling them inline freezes the
        # entire event loop. Run the cleanup in a worker thread with a
        # 5-second timeout; if it doesn't complete we orphan the stream
        # and continue with the chunks we already have — the next
        # ``start_recording`` will create a fresh stream.
        _vlog("stop_recording: closing audio stream")
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._close_stream),
                timeout=5.0,
            )
            _vlog("stop_recording: stream closed cleanly")
        except asyncio.TimeoutError:
            _vlog("stop_recording: stream close timed out (5s) — orphaning stream")
            self._stream = None
        except Exception as exc:
            _vlog(f"stop_recording: stream close raised {exc!r} — continuing")
            self._stream = None

        self._recording = False
        self._recording_started_at = 0.0

        if self._np is None or not self._chunks:
            _vlog("stop_recording: no chunks to process")
            return "", diag

        # Concatenate captured chunks into one (n_samples,) float32 array.
        _vlog(f"stop_recording: concatenating {len(self._chunks)} chunks")
        try:
            audio = self._np.concatenate(self._chunks, axis=0).reshape(-1)
        except Exception as exc:
            logger.warning("voice concat failed: %s", exc)
            _vlog(f"stop_recording: concat failed: {exc!r}")
            self._chunks = []
            diag["reason"] = "error"
            return "", diag
        self._chunks = []

        if audio.size == 0:
            _vlog("stop_recording: audio.size=0")
            return "", diag

        diag["duration_s"] = float(audio.size) / float(self._sample_rate)
        try:
            diag["peak"] = float(self._np.max(self._np.abs(audio)))
            diag["rms"] = float(self._np.sqrt(self._np.mean(audio.astype("float64") ** 2)))
        except Exception:
            pass
        _vlog(
            f"stop_recording: audio prepared duration={diag['duration_s']:.2f}s "
            f"peak={diag['peak']:.3f}"
        )

        # Optional debug-dump: save the captured buffer to /tmp/ as a 16-bit
        # PCM WAV file so the user can play it back and confirm the audio
        # is intelligible. Gated by ``REYN_VOICE_DEBUG`` so it never runs
        # for normal users. Path is logged at WARNING so it surfaces in
        # `reyn chat` stderr without touching the conv pane.
        if os.environ.get("REYN_VOICE_DEBUG"):
            try:
                ts = int(_time.time())
                wav_path = f"/tmp/reyn-voice-debug-{ts}.wav"
                pcm = (audio.clip(-1.0, 1.0) * 32767.0).astype("int16")
                with wave.open(wav_path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(self._sample_rate)
                    w.writeframes(pcm.tobytes())
                diag["wav_path"] = wav_path
                _vlog(
                    f"WAV saved: {diag['duration_s']:.2f}s / "
                    f"peak={diag['peak']:.3f} → {wav_path}"
                )
            except Exception as exc:
                logger.warning("voice debug WAV dump failed: %s", exc)

        # If the capture is essentially silence, skip the model call entirely
        # — Whisper hallucinates on pure-noise input.
        if diag["peak"] < 0.005:
            diag["reason"] = "silent"
            return "", diag

        # Run transcription off the Textual event loop. Wrap in a timeout
        # so a hung worker (network stall, CTranslate2 deadlock, partial
        # model file, etc.) surfaces as a clean error instead of leaving
        # the TUI frozen on "⏳ transcribing…".
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self._transcribe_sync, audio),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            logger.warning("voice transcribe timed out after 120s")
            diag["reason"] = "timeout"
            _vlog("transcribe timed out (120s)")
            return "", diag
        except Exception as exc:
            logger.warning("voice transcribe failed: %s", exc)
            diag["reason"] = "error"
            return "", diag

        diag["reason"] = "ok" if text else "silent"
        return text, diag

    # ── internals ───────────────────────────────────────────────────────────

    def _close_stream(self) -> None:
        """Tear down the active sounddevice InputStream.

        Uses ``abort()`` (= immediate, drops in-flight frames) instead of
        ``stop()`` (= waits for the callback to drain). The drain on macOS
        sometimes takes seconds when CoreAudio HAL is contended, and we'd
        rather lose a few trailing milliseconds of audio than block the
        event loop. ``close()`` after abort is fast.
        """
        if self._stream is None:
            return
        try:
            self._stream.abort()
        except Exception as exc:
            _vlog(f"_close_stream: abort raised {exc!r}")
        try:
            self._stream.close()
        except Exception as exc:
            _vlog(f"_close_stream: close raised {exc!r}")
        self._stream = None

    def _model_cache_dir(self) -> "Path | None":
        """Best-effort discovery of the HuggingFace cache dir for our model.

        Used by the auto-recovery path in ``_ensure_model`` to wipe a
        partially-downloaded model on construction failure. Returns
        ``None`` when the dir is unidentifiable (= we won't try to clean).
        """
        from pathlib import Path
        try:
            from huggingface_hub.constants import HF_HUB_CACHE  # type: ignore[import]
            cache_root = Path(HF_HUB_CACHE)
        except Exception:
            cache_root = Path.home() / ".cache" / "huggingface" / "hub"
        if not cache_root.is_dir():
            return None
        # faster-whisper canonical repos: ``Systran/faster-whisper-<size>``.
        # HF Hub flattens repo names into ``models--Systran--faster-whisper-<size>``.
        candidate = cache_root / f"models--Systran--faster-whisper-{self._model_name}"
        return candidate if candidate.exists() else None

    def _clear_model_cache(self) -> None:
        """Remove the cached model dir so the next load triggers a fresh download.

        No-op when the cache dir isn't found. Errors are logged and swallowed
        so a failed cleanup never propagates as a TUI exception — worst
        case the user retries manually.
        """
        target = self._model_cache_dir()
        if target is None:
            _vlog("cache cleanup: no cache dir to clean")
            return
        try:
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            _vlog(f"cache cleanup: removed {target}")
        except Exception as exc:
            _vlog(f"cache cleanup failed: {exc}")

    def _ensure_model(self) -> Any:
        """Lazy-load the faster-whisper model; cache for subsequent calls.

        First call may take seconds (model download + CTranslate2 init) so it
        always runs inside ``asyncio.to_thread`` via ``stop_recording``
        — or, when pre-warm is enabled, via ``preload_model``.

        Thread-safe: ``self._model_lock`` serialises the constructor so the
        BG pre-warm and a foreground transcribe call can't race on the
        cache files / inflate two CTranslate2 contexts at once.

        Self-healing: a cancelled / interrupted download leaves a partial
        file in the HF cache that breaks the next load with an opaque
        error or hang. We catch the load failure, wipe the cache dir, and
        retry exactly once with a forced fresh download.
        """
        # Fast path: already loaded, no lock needed (single pointer read).
        if self._whisper_model is not None:
            return self._whisper_model
        with self._model_lock:
            # Re-check inside the lock (double-checked locking).
            if self._whisper_model is not None:
                return self._whisper_model
            try:
                from faster_whisper import WhisperModel  # type: ignore[import]
            except Exception as exc:
                raise VoiceUnavailable(
                    "faster-whisper not installed; run: pip install \"reyn[voice]\""
                ) from exc
            self._whisper_model = self._construct_whisper_model(WhisperModel)
            return self._whisper_model

    def _construct_whisper_model(self, WhisperModel) -> Any:
        """Build a WhisperModel with one cache-reset retry on failure.

        Caller must hold ``self._model_lock``.
        """
        # Defensive disable of progress bars — handles the case where
        # huggingface_hub / tqdm were already imported before our module-
        # level env-var setdefaults could take effect.
        try:
            from huggingface_hub.utils import disable_progress_bars  # type: ignore[import]
            disable_progress_bars()
        except Exception:
            pass
        try:
            from tqdm import tqdm  # type: ignore[import]
            tqdm.disable = True  # type: ignore[attr-defined]
        except Exception:
            pass

        _vlog(
            f"loading WhisperModel({self._model_name!r}, "
            f"device={self._device!r}, compute_type={self._compute_type!r}, "
            f"cpu_threads={self._cpu_threads}, num_workers={self._num_workers})"
        )
        with _real_stdio_during_load():
            try:
                model = WhisperModel(
                    self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                    cpu_threads=self._cpu_threads,
                    num_workers=self._num_workers,
                )
                _vlog("WhisperModel loaded")
                return model
            except Exception as exc:
                _vlog(
                    f"WhisperModel load failed ({exc!r}) — clearing cache and retrying"
                )
                self._clear_model_cache()
                try:
                    model = WhisperModel(
                        self._model_name,
                        device=self._device,
                        compute_type=self._compute_type,
                        cpu_threads=self._cpu_threads,
                        num_workers=self._num_workers,
                    )
                    _vlog("WhisperModel loaded after cache reset")
                    return model
                except Exception as exc2:
                    _vlog(f"WhisperModel load failed even after cache reset: {exc2!r}")
                    raise

    def _transcribe_sync(self, audio) -> str:  # noqa: C901
        # Debug-mode visibility into exactly what the in-memory path sees.
        # Mirror what the standalone script reports so any divergence is
        # obvious in the log.
        if os.environ.get("REYN_VOICE_DEBUG"):
            try:
                peak = (
                    float(self._np.max(self._np.abs(audio)))
                    if self._np is not None else -1.0
                )
                rms = (
                    float(self._np.sqrt(self._np.mean(audio.astype("float64") ** 2)))
                    if self._np is not None else -1.0
                )
                _vlog(
                    f"pre-transcribe: shape={getattr(audio, 'shape', '?')} "
                    f"dtype={getattr(audio, 'dtype', '?')} "
                    f"peak={peak:.4f} rms={rms:.4f} "
                    f"model={self._model_name} lang={self._language} "
                    f"compute={self._compute_type}"
                )
            except Exception as exc:
                _vlog(f"pre-transcribe log failed: {exc}")
        return self._transcribe_real(audio)

    def _transcribe_real(self, audio) -> str:
        """Run Whisper on a numpy float32 mono buffer.

        We deliberately do NOT normalise the audio amplitude here.
        Empirically, rescaling the buffer to peak ≈ 0.95 before transcribe
        produced empty results on real mic input (= the dogfood log dated
        2026-05-09: same WAV transcribed fine standalone with the same
        params, but the in-memory normalised version returned ""). Whisper
        is tolerant of a wide range of speech amplitudes; better to let
        it see the raw signal.

        Settings:

        * ``vad_filter=False`` — Silero VAD is the #1 cause of false
          "no speech" on quiet-but-real input. We do our own peak gate
          upstream in ``stop_recording`` (peak < 0.005 → skip the model
          call entirely).
        * ``no_speech_threshold=0.3`` (vs default 0.6) — accept quieter
          speech. The upstream peak gate keeps spurious transcriptions
          from pure noise.
        * ``log_prob_threshold=-1.5`` (vs -1.0) — accept lower-confidence
          decoding.
        * ``temperature=0.0`` + ``condition_on_previous_text=False`` —
          one-shot dictation: deterministic single pass, no streaming
          context across calls.
        """
        model = self._ensure_model()
        with _real_stdio_during_load():
            segments, info = model.transcribe(
                audio,
                language=self._language,
                beam_size=1,
                vad_filter=False,
                temperature=0.0,
                no_speech_threshold=0.3,
                log_prob_threshold=-1.5,
                condition_on_previous_text=False,
            )
            # Materialise the segment generator INSIDE the stdio swap —
            # generator iteration runs the actual decode, which is where
            # any tokenizer subprocess would spawn.
            seg_list = list(segments)
        text = "".join(s.text for s in seg_list).strip()
        if os.environ.get("REYN_VOICE_DEBUG"):
            try:
                _vlog(
                    f"post-transcribe: lang={getattr(info, 'language', '?')} "
                    f"prob={float(getattr(info, 'language_probability', 0.0)):.2f} "
                    f"n_segments={len(seg_list)} text={text!r}"
                )
            except Exception as exc:
                _vlog(f"post-transcribe log failed: {exc}")
        return text


__all__ = ["VoiceInput", "VoiceUnavailable"]
