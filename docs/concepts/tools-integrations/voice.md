---
type: concept
topic: [voice, speech-to-text, chat]
audience: [human, agent]
---

# Voice input

Speech-to-text for the `reyn chat` inline CUI, powered by `faster-whisper`.
Revived (#4187) as a reimplementation against the current CUI — the retired
Textual TUI this was originally built for (`src/reyn/chat/tui/`) was deleted
wholesale when `reyn chat` moved to the inline CUI, so the STT core (record →
transcribe) is ported near-verbatim from that history but the key binding and
composer wiring are new.

## What it does

Press **F2** to start recording; press **F2** again to stop and transcribe.
The transcribed text is inserted at the composer's cursor, ahead of any
existing draft — it is never auto-submitted, so the operator reviews (and can
edit) before pressing Enter. **Esc** while recording cancels it without
transcribing. A recording left open past `voice.max_duration_s` (default
300 s) auto-stops and transcribes on its own, the same as a manual F2 press,
so a forgotten open mic does not run forever.

F2, not the retired TUI's Ctrl+R: #4187 measured Ctrl+R as colliding with
reverse-history-search, a terminal-wide convention users bring to any
text-input surface (the Composer is a multi-line text area) — F2, the
retired TUI's own alias, carries no such conflict.

## Backend

`faster-whisper` (`Systran/faster-whisper-<model>` on HuggingFace) provides
local, offline transcription via CTranslate2. The model downloads on first
use and is cached in the HuggingFace hub cache — the FIRST F2-stop after
launch pays that cost, not the press that starts recording. Audio is
captured at 16 kHz mono via `sounddevice` (PortAudio). Inference runs in
`asyncio.to_thread` so the Textual event loop never blocks.

## Enabling

```bash
pip install "reyn[voice]"
```

The base install never imports `sounddevice` or `faster-whisper`. Without the
extras, pressing F2 shows a friendly in-conversation message naming the
extra to install, rather than doing nothing or crashing.

## Configuration (`reyn.yaml`)

```yaml
voice:
  enabled: true           # false = hard-disable F2 even if deps are installed
  model: small             # tiny | base | small | medium | large-v3
  language: "ja"           # ISO 639-1 code; "" or null = Whisper auto-detect
  device: cpu               # cpu | cuda (no Metal backend; explicit cpu avoids Mac issues)
  compute_type: int8        # int8 | float16 | float32
  cpu_threads: 4             # pin to 4 to avoid OpenMP deadlock on Apple Silicon
  num_workers: 1              # parallel transcribe streams — 1 keeps memory/threads low
  sample_rate: 16000            # Whisper expects 16 kHz mono — leave this alone
  max_duration_s: 300.0        # auto-stop+transcribe a forgotten recording after this long
```

## Language detection

Default is `language: "ja"` (Reyn's Japanese-enterprise focus). Short clips
misidentify as other languages at non-trivial rates when auto-detect is on.
Set `language: ""` or `language: null` to opt back into Whisper auto-detection.

## Limitations

- **No Metal backend.** `faster-whisper` does not support Apple Metal/MPS;
  `device: cpu` is correct on Mac.
- **Model download on first use.** `small` is ~460 MB.
- **Silence gate.** Audio with peak amplitude below 0.005 is skipped entirely
  (never reaches the model) — Whisper hallucinates on pure noise. Textual's
  own VAD filter is disabled for the same reason (it rejects quiet-but-real
  speech more often than it rejects noise).
- **No dictate-and-send, no debug-WAV dump.** The retired TUI's later
  additions (Enter-while-recording stops+submits in one keystroke; a
  `REYN_VOICE_DEBUG` env var that saves captured audio to disk) were not
  ported — #4187 ported the STT core and rebuilt only the interface glue the
  current CUI needs. Revisit if wanted; nothing here blocks adding them.

## See also

- `src/reyn/interfaces/inline/textual_chat/voice.py`
- `src/reyn/config/media.py` — `VoiceConfig`
