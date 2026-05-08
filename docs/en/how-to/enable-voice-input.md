---
type: how-to
topic: tui
audience: [human]
applies_to: [reyn chat]
---

# Enable voice input in `reyn chat`

**Goal:** Dictate into the TUI input bar instead of typing, using local Whisper.

Voice input is shipped as an optional extra. The feature stays off by default
so the base install has zero extra dependencies.

## 1. Install the `voice` extra

```bash
pip install "reyn[voice]"
```

This pulls three pure-Python packages:

- `sounddevice` — microphone capture (PortAudio binding)
- `numpy` — audio buffer
- `faster-whisper` — local Whisper inference (CTranslate2 backend)

## 2. Install the system audio library

`sounddevice` needs PortAudio on the OS layer. pip cannot install this for you.

| OS | Command |
|---|---|
| macOS | `brew install portaudio` |
| Ubuntu / Debian | `sudo apt install libportaudio2 libsndfile1` |
| Fedora / RHEL | `sudo dnf install portaudio` |
| Windows | nothing — bundled in the wheel |

Verify:

```bash
python -c "import sounddevice; print(sounddevice.query_devices())"
```

You should see your microphone listed.

## 3. Grant microphone permission (macOS)

The first time you press the record key, macOS prompts your terminal app
(iTerm2 / Terminal / Ghostty / Wezterm) to access the microphone. Approve it.

If you missed the prompt: **System Settings → Privacy & Security → Microphone**
→ enable your terminal.

## 4. Configure the model (optional)

Edit `~/.reyn/config.yaml` (or your project's `reyn.yaml` / `reyn.local.yaml`):

```yaml
voice:
  enabled: true        # set false to hard-disable F2 even if deps installed
  model: small         # tiny | base | small | medium | large-v3
  language: ja         # ISO code, or omit for auto-detect
  device: auto         # auto | cpu | cuda | metal
  compute_type: int8   # int8 (fast) | float16 | float32
```

Defaults: `small` / auto-detect language / auto device / int8.

The model is downloaded on first use and cached under `~/.cache/huggingface/`.
Approximate sizes: `tiny` 75MB, `base` 140MB, `small` 460MB, `medium` 1.5GB.

## 5. Use it

Inside `reyn chat`:

| Key | Action |
|---|---|
| `Ctrl+R` | Start recording — `🔴 recording…` appears in the conversation pane |
| `Ctrl+R` (again) | Stop recording, transcribe, append the result to the input bar |
| `Esc` while recording | Cancel without transcribing |
| `F2` | Alias for `Ctrl+R` (see note below) |

The transcribed text lands in the input bar but **is not sent**. Review, edit
if needed, then `Enter` to submit.

!!! note "Why Ctrl+R, not F2?"
    `F2` is intercepted by some terminals and by macOS itself (default
    "F1, F2 = brightness keys"). If you want `F2` to work on macOS, enable
    **System Settings → Keyboard → "Use F1, F2, etc. keys as standard
    function keys"**, otherwise hold `Fn` while pressing `F2`. `Ctrl+R` has
    no such friction and is the recommended binding.

## Troubleshooting

### "PortAudioError: Error querying device"
PortAudio not installed at the OS layer. Re-run step 2.

### "ModuleNotFoundError: No module named 'sounddevice'"
The extra was not installed. Run `pip install "reyn[voice]"`.

### First transcription takes 30+ seconds
Model is being downloaded. Subsequent transcriptions use the cache.

### Transcription is wrong on technical terms
Whisper struggles with proper nouns, file paths, and code symbols. Edit the
input bar before sending, or pin a larger model:

```yaml
voice:
  model: medium
```

### CPU usage is high during recording
Recording itself is cheap. Inference runs only after you stop. If you have a
GPU or Apple Silicon, set `device: metal` (Apple) or `device: cuda` (NVIDIA).

### I want to disable voice input
`pip uninstall sounddevice faster-whisper` — the `voice` config block is
ignored if the modules are missing.

## Privacy note

`faster-whisper` runs **fully on-device**. No audio is sent to any server. If
you later switch to the OpenAI Whisper API for accuracy, that uploads audio
to OpenAI — opt-in only, never the default.

## See also

- [Customize TUI key bindings](customize-tui-keys.md) *(planned)*
- [`reyn chat` reference](../reference/chat-cli.md) *(planned)*
