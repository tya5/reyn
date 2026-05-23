---
type: concept
topic: [voice, speech-to-text, chat]
audience: [human, agent]
---

# Voice input

Speech-to-text for the `reyn chat` TUI, powered by `faster-whisper`.

## What it does

Press **Ctrl+R** to start recording; press **Ctrl+R** again to stop.
The transcribed text is inserted into the input bar. While recording,
pressing **Enter** stops, transcribes, and submits in one keystroke
(= dictate-and-send). The voice layer lives entirely in `chat/tui/` —
the OS and skill layers have no awareness of it (P7).

## Backend

`faster-whisper` (`Systran/faster-whisper-<model>` on HuggingFace) provides
local, offline transcription via CTranslate2. The model downloads on first
use and is cached in the HuggingFace hub cache. Audio is captured at 16 kHz
mono via `sounddevice` (PortAudio). Inference runs in `asyncio.to_thread` so
the Textual event loop never blocks. A `small` model on Apple Silicon
transcribes 5 s of audio in ~1 s once loaded.

## Enabling

```bash
pip install "reyn[voice]"
```

The base install never imports `sounddevice` or `faster-whisper`. Without the
extras, pressing Ctrl+R shows a friendly in-TUI error rather than crashing.

## Configuration (`reyn.yaml`)

```yaml
voice:
  enabled: true           # false = hard-disable Ctrl+R even if deps are installed
  model: small            # tiny | base | small | medium | large-v3
  language: "ja"          # ISO 639-1 code; "" or null = Whisper auto-detect
  device: cpu             # cpu | cuda (no Metal backend; explicit cpu avoids Mac issues)
  compute_type: int8      # int8 | float16 | float32
  cpu_threads: 4          # pin to 4 to avoid OpenMP deadlock on Apple Silicon
  max_duration_s: 300.0   # auto-cancel after 5 min to prevent runaway memory
```

## Language detection

Default is `language: "ja"` (Reyn's Japanese-enterprise focus). Short clips
misidentify as other languages at non-trivial rates when auto-detect is on.
Set `language: ""` or `language: null` to opt back into Whisper auto-detection.

## Limitations

- **No Metal backend.** `faster-whisper` does not support Apple Metal/MPS;
  `device: cpu` is correct on Mac.
- **Model download on first use.** `small` is ~460 MB. The TUI pre-loads the
  model in the background as soon as recording begins, so the second Ctrl+R
  invocation pays the load cost, not the first.
- **Silence gate.** Audio with peak amplitude below 0.005 is skipped entirely;
  Whisper halluccinates on pure noise. `vad_filter` is disabled.
- **Timeouts.** Transcription times out after 120 s; mic open/close after 5 s.
- **Debug mode.** `REYN_VOICE_DEBUG=1` saves captured WAV files to
  `/tmp/reyn-voice-debug-<ts>.wav` and writes a log to `/tmp/reyn-voice.log`.

## See also

- `src/reyn/chat/tui/voice.py`
- `src/reyn/config.py` — `VoiceConfig`
