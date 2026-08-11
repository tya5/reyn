---
type: concept
topic: [voice, speech-to-text, chat]
audience: [human, agent]
---

# 音声入力

`reyn chat` のインライン CUI 向けの音声テキスト変換。`faster-whisper` で動作します。
#4187 で復活（再実装） — この機能がもともと実装されていた Textual TUI
（`src/reyn/chat/tui/`）は、`reyn chat` がインライン CUI に移行した際に丸ごと
削除されたため、STT コア（録音 → 変換）はその履歴からほぼそのまま移植しました
が、キーバインドと入力欄への結線は現行 CUI 向けに新規実装です。

## 機能概要

**F2** を押すと録音開始、もう一度 **F2** を押すと停止・変換します。
変換されたテキストはコンポーザのカーソル位置（既存の下書きより前）に挿入され、
自動送信はされません — オペレーターが確認・編集してから Enter を押します。
録音中に **Esc** を押すと、変換せずにキャンセルします。`voice.max_duration_s`
（デフォルト 300 秒）を超えて開いたままの録音は、F2 を手動で押した場合と同様に
自動的に停止・変換されるため、マイクの消し忘れが延々と続くことはありません。

Ctrl+R ではなく F2: #4187 の測定により、Ctrl+R は逆方向履歴検索
（reverse-history-search）というターミナル全体の慣習と衝突することが分かり
ました（コンポーザは複数行のテキスト入力欄であり、この慣習をユーザーが持ち込
む対象そのものです）。F2 は退役した TUI 自身のエイリアスでもあり、この衝突が
ありません。

## バックエンド

`faster-whisper`（HuggingFace の `Systran/faster-whisper-<model>`）が
CTranslate2 経由でローカル・オフライン変換を提供します。モデルは初回使用時に
ダウンロードされ、HuggingFace ハブキャッシュにキャッシュされます — 起動後、
最初の「F2 で停止」がこのコストを払います（録音を開始した押下ではありません）。
音声は `sounddevice`（PortAudio）経由で 16 kHz モノラルでキャプチャされます。
推論は `asyncio.to_thread` で実行されるため、Textual のイベントループは
ブロックされません。

## 有効化

```bash
pip install "reyn[voice]"
```

ベースインストールでは `sounddevice` や `faster-whisper` はインポートされません。
extras なしで F2 を押すと、何も起きない・クラッシュするのではなく、会話ペイン
にインストールすべき extra 名を明記したメッセージが表示されます。

## 設定（`reyn.yaml`）

```yaml
voice:
  enabled: true              # false = deps がインストールされていても F2 を無効化
  model: small                # tiny | base | small | medium | large-v3
  language: "ja"               # ISO 639-1 コード。"" または null = Whisper 自動検出
  device: cpu                   # cpu | cuda（Metal バックエンドなし。Mac では cpu を明示）
  compute_type: int8             # int8 | float16 | float32
  cpu_threads: 4                  # Apple Silicon での OpenMP デッドロック回避のため 4 に固定
  num_workers: 1                    # 並列変換ストリーム数 — 1 でメモリ/スレッドを抑制
  sample_rate: 16000                  # Whisper は 16 kHz モノラルを期待 — 変更しないこと
  max_duration_s: 300.0               # 消し忘れた録音をこの秒数後に自動停止・変換
```

## 言語検出

デフォルトは `language: "ja"`（Reyn の日本語エンタープライズ向け用途）。自動検出
を有効にすると、短いクリップは無視できない確率で他の言語と誤認識されます。
Whisper の自動検出に戻すには `language: ""` または `language: null` を
設定してください。

## 制限事項

- **Metal バックエンドなし.** `faster-whisper` は Apple Metal / MPS を
  サポートしていません。Mac では `device: cpu` が正しい設定です。
- **初回使用時のモデルダウンロード.** `small` は約 460 MB です。
- **無音ゲート.** ピーク振幅が 0.005 未満の音声は完全にスキップされます
  （モデルには一切渡りません）— Whisper は純粋なノイズで幻覚を起こすためです。
  Textual 自身の VAD フィルタも同じ理由で無効化されています（静かだが実際の
  発話をノイズより高い頻度で棄却してしまうため）。
- **ディクテーション送信・デバッグモードなし.** 退役した TUI の後発コミットで
  追加された機能（録音中の Enter で停止・変換・送信を 1 回のキー操作で行う機能、
  キャプチャ音声をディスクに保存する `REYN_VOICE_DEBUG` 環境変数）は移植して
  いません — #4187 では STT コアのみを移植し、インタフェース結線は現行 CUI
  向けに必要な分だけ新規実装しました。欲しければ後日追加できます — ここには
  それを妨げるものはありません。

## 参照

- `src/reyn/interfaces/inline/textual_chat/voice.py`
- `src/reyn/config/media.py` — `VoiceConfig`
