---
type: concept
topic: [voice, speech-to-text, chat]
audience: [human, agent]
---

# 音声入力

`reyn chat` TUI 向けの音声テキスト変換。`faster-whisper` で動作します。

## 機能概要

**Ctrl+R** を押すと録音開始、もう一度 **Ctrl+R** を押すと停止します。
変換されたテキストは入力バーに挿入されます。録音中に **Enter** を押すと、停止・変換・送信を 1 回のキー操作で行えます（ディクテーション送信）。音声レイヤーは `chat/tui/` 内に完結しており、OS とスキルレイヤーはこれを認識しません（P7）。

## バックエンド

`faster-whisper`（HuggingFace の `Systran/faster-whisper-<model>`）が CTranslate2 経由でローカル・オフライン変換を提供します。モデルは初回使用時にダウンロードされ、HuggingFace ハブキャッシュにキャッシュされます。音声は `sounddevice`（PortAudio）経由で 16 kHz モノラルでキャプチャされます。推論は `asyncio.to_thread` で実行されるため、Textual のイベントループはブロックされません。Apple Silicon 上の `small` モデルはロード後、5 秒の音声を約 1 秒で変換します。

## 有効化

```bash
pip install "reyn[voice]"
```

ベースインストールでは `sounddevice` や `faster-whisper` はインポートされません。extras なしで Ctrl+R を押すと、クラッシュではなく TUI 内にわかりやすいエラーが表示されます。

## 設定（`reyn.yaml`）

```yaml
voice:
  enabled: true           # false = deps がインストールされていても Ctrl+R を無効化
  model: small            # tiny | base | small | medium | large-v3
  language: "ja"          # ISO 639-1 コード。"" または null = Whisper 自動検出
  device: cpu             # cpu | cuda（Metal バックエンドなし。Mac では cpu を明示）
  compute_type: int8      # int8 | float16 | float32
  cpu_threads: 4          # Apple Silicon での OpenMP デッドロック回避のため 4 に固定
  max_duration_s: 300.0   # 5 分後に自動キャンセル（メモリ消費の暴走防止）
```

## 言語検出

デフォルトは `language: "ja"`（Reyn の日本語エンタープライズ向け用途）。自動検出を有効にすると、短いクリップは無視できない確率で他の言語と誤認識されます。Whisper の自動検出に戻すには `language: ""` または `language: null` を設定してください。

## 制限事項

- **Metal バックエンドなし.** `faster-whisper` は Apple Metal / MPS をサポートしていません。Mac では `device: cpu` が正しい設定です。
- **初回使用時のモデルダウンロード.** `small` は約 460 MB です。TUI は録音開始と同時にバックグラウンドでモデルをプリロードするため、2 回目の Ctrl+R 呼び出しがロードコストを支払います（1 回目ではありません）。
- **無音ゲート.** ピーク振幅が 0.005 未満の音声は完全にスキップされます。Whisper は純粋なノイズで幻覚を起こすためです。`vad_filter` は無効化されています。
- **タイムアウト.** 変換は 120 秒でタイムアウト。マイクのオープン / クローズは 5 秒でタイムアウトします。
- **デバッグモード.** `REYN_VOICE_DEBUG=1` を設定すると、キャプチャした WAV ファイルを `/tmp/reyn-voice-debug-<ts>.wav` に保存し、ログを `/tmp/reyn-voice.log` に出力します。

## 参照

- `src/reyn/interfaces/tui/voice.py`
- `src/reyn/config/media.py` — `VoiceConfig`
