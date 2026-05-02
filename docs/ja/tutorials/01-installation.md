---
type: tutorial
topic: getting-started
audience: [human]
---

# 01 — インストール

5 分以内に Reyn をインストールして動かします。

## 前提条件

- Python 3.11+
- LiteLLM 互換のモデルエンドポイント（OpenAI、Google AI 経由の Gemini、Anthropic、または LiteLLM Proxy のようなローカルプロキシ）

## インストール

```bash
git clone https://github.com/<org>/reyn.git
cd reyn
python -m venv venv
source venv/bin/activate
pip install -e '.[dev]'
```

`reyn` CLI が PATH に追加されます。

## モデルを設定する

Reyn は `reyn.yaml` からモデルを選択します。デフォルトは LiteLLM プロキシ経由の Gemini です。別のプロバイダーを使用するには、`models` マップを編集します:

```yaml
# reyn.yaml
model: standard
models:
  light:    openai/gpt-4o-mini
  standard: openai/gpt-4o
  strong:   anthropic/claude-3-5-sonnet-20241022
```

対応する API キーをエクスポートします:

```bash
export OPENAI_API_KEY=sk-...
# または
export ANTHROPIC_API_KEY=sk-ant-...
```

!!! warning "API キーは絶対にコミットしない"
    キーは環境変数にのみ保存します。`reyn.yaml` はチェックインします。プロキシ URL は `reyn.local.yaml` や `~/.reyn/config.yaml`（gitignored）に書きます。

## プロジェクトを初期化する

作業ディレクトリで:

```bash
reyn init
```

これで `reyn.yaml` と `.reyn/config.yaml` が存在しない場合に作成されます。

## 確認する

```bash
reyn skills          # stdlib + project + local の Skill を一覧表示
reyn run text_summarizer "reyn is a workflow OS for LLMs."
```

2 番目のコマンドがサマリーを生成してクリーンに終了すれば、[02 — はじめての Skill](02-your-first-skill.md) に進む準備ができています。

## トラブルシューティング

- **`reyn: command not found`** — venv がアクティブになっていません。`source venv/bin/activate` を実行してください。
- **`AuthenticationError`** — API キーの環境変数が設定されていないか、`reyn.yaml` のモデルと一致していません。
- **Proxy connection refused** — LiteLLM プロキシを起動するか、`reyn.local.yaml` から `api_base` を削除してください。
