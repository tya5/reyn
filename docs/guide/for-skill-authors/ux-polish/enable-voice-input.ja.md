---
type: how-to
topic: tui
audience: [human]
applies_to: [reyn chat]
---

# `reyn chat` で音声入力を有効にする

**目的:** TUI の入力バーをタイピングではなくマイクで吹き込めるようにする (ローカル Whisper)。

音声入力は optional extra として提供。デフォルトは OFF なので、本体インストールには
追加依存ゼロ。

## 1. `voice` extra をインストール

```bash
pip install "reyn[voice]"
```

入る pure-Python パッケージは 3 つだけ:

- `sounddevice` — マイク capture (PortAudio binding)
- `numpy` — 音声 buffer
- `faster-whisper` — ローカル Whisper 推論 (CTranslate2 backend)

## 2. システム側の音声ライブラリを入れる

`sounddevice` は OS layer で PortAudio が必要。 pip では入らない。

| OS | コマンド |
|---|---|
| macOS | `brew install portaudio` |
| Ubuntu / Debian | `sudo apt install libportaudio2 libsndfile1` |
| Fedora / RHEL | `sudo dnf install portaudio` |
| Windows | 不要 (wheel に同梱) |

確認:

```bash
python -c "import sounddevice; print(sounddevice.query_devices())"
```

マイクが list されれば OK。

## 3. マイク権限を許可 (macOS)

最初に録音キーを押したとき、 macOS が terminal app (iTerm2 / Terminal / Ghostty /
Wezterm) のマイクアクセス許可を聞いてくる。 許可する。

聞き逃した場合: **システム設定 → プライバシーとセキュリティ → マイク** →
使っている terminal を有効化。

## 4. モデルを設定 (任意)

`~/.reyn/config.yaml` (または project の `reyn.yaml` / `reyn.local.yaml`) を編集:

```yaml
voice:
  enabled: true        # false で deps があっても F2 を無効化
  model: small         # tiny | base | small | medium | large-v3
  language: ja         # ISO コード、 省略で auto-detect
  device: auto         # auto | cpu | cuda | metal
  compute_type: int8   # int8 (速い) | float16 | float32
```

デフォルトは `small` / 言語自動判定 / device 自動 / int8。

モデルは初回使用時に DL され `~/.cache/huggingface/` にキャッシュされる。
サイズ目安: `tiny` 75MB、 `base` 140MB、 `small` 460MB、 `medium` 1.5GB。

## 5. 使う

`reyn chat` 起動中:

| キー | 動作 |
|---|---|
| `Ctrl+R` | 録音開始 — conv pane に `🔴 recording…` が出る |
| `Ctrl+R` (もう一度) | 録音停止 → 転写 → 入力バー末尾に追記 (確認・編集可能) |
| 録音中に `Enter` | 録音停止 → 転写 → **そのまま即送信** (編集ステップなし) |
| 録音中に `Esc` | 転写せずキャンセル |
| `F2` | `Ctrl+R` の alias (注意点は下記) |

転写結果は入力バーに入るだけで **送信はされない**。 内容確認・必要なら編集してから
`Enter` で送る。

!!! note "なぜ `Ctrl+R` が primary か"
    `F2` は terminal によっては吸われ、 macOS は default で「F1/F2 = 輝度キー」 に
    割当てている。 macOS で `F2` を使いたい場合は **システム設定 → キーボード →
    「F1、 F2 などのキーを標準のファンクションキーとして使用」** を有効化、
    または `Fn` 押しながら `F2`。 `Ctrl+R` ならこの面倒なし。

## トラブルシュート

### "PortAudioError: Error querying device"
OS layer の PortAudio が入っていない。 手順 2 をやり直す。

### "ModuleNotFoundError: No module named 'sounddevice'"
extra が入っていない。 `pip install "reyn[voice]"` を実行。

### 初回転写が 30 秒以上かかる
モデル DL 中。 2 回目以降はキャッシュから即起動。

### 専門用語の認識が悪い
Whisper は固有名詞・ファイルパス・コード記号が苦手。 送信前に入力バーで編集する、
または大きめモデルに変える:

```yaml
voice:
  model: medium
```

### 録音中の CPU 使用率が高い
録音自体は軽い。 推論は録音停止後のみ走る。 GPU / Apple Silicon があるなら
`device: metal` (Apple) または `device: cuda` (NVIDIA) を指定。

### 音声入力を無効化したい
`pip uninstall sounddevice faster-whisper`。 module がなければ `voice` 設定 block
は無視される。

## プライバシーについて

`faster-whisper` は **完全にデバイス内** で動く。 音声はどのサーバにも送られない。
精度向上のため OpenAI Whisper API に切り替える option は今後追加予定だが、
opt-in のみでデフォルトにはしない。

## 関連

- [`reyn chat` リファレンス](../../../reference/cli/chat.md)
- TUI キーバインドのカスタマイズ *(doc 予定、 当面は `src/reyn/chat/tui/app.py` の `Ctrl+R` を直接編集)*
