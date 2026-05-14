# FP-0024: Router — セマンティックツール選択

**Status**: **partial-landed** — Component D (Anthropic `tool_search_tool` MCP integration) LANDED 2026-05-14 (commit `aa1b36f`) with threshold-based switch (default 30 MCP servers、 `mcp.search_threshold` で configurable)。 Components A/B/C (BM25 / search_hints / embedding) は現スケール (= ~30-50 skills、 invoke_skill enum bloat 未観測) で **YAGNI 判定 deferred**; skill catalog ≥ 100 or dogfood decision-fatigue 観測時に再開。
**Proposed**: 2026-05-13
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

ユーザー入力と `invoke_skill` 選択の間にセマンティック/ハイブリッド検索レイヤーを導入する。全スキル名を enum で LLM に渡す現在の方式を、BM25 または embedding 類似度による上位 K 件絞り込み→ LLM が選択、に変える。`invoke_skill.name` enum の O(N_skills) スキーマ肥大を解消し、曖昧な入力に対するルーティング精度を改善する。

---

## Motivation

### 現在のルーティングパス

```
ユーザー入力
  → LLM が invoke_skill.name enum（全 N スキル名）を受け取る
  → LLM が 1 つを選ぶ（または先に list_skills を呼ぶ）
```

現在（~100–200 スキル）は問題なく動作する。しかしカタログが大きくなると業界研究が 2 つの失敗パターンを指摘している。

**1. Schema enum の肥大化**
`invoke_skill.name` は全スキル名をツールスキーマに列挙する。N=1000 スキルでは enum だけで ~5k–8k トークンを消費する。スキルが追加されるたびにキャッシュがミスし、毎回この分のコストが発生する。
*参照: 平均 20 文字/スキル名 × 1000 = 20k 文字 ≈ 5k–8k トークンから推定。*

**2. 大規模 enum での決定疲れ**
Anthropic 研究: コンテキストに 30〜50 ツールを超えるとツール選択精度が有意に低下。OpenAI 公式ガイド: 「1 ターン開始時に 20 関数未満を目標にする」。
*参照: Anthropic "Advanced Tool Use" (2025); OpenAI Function Calling docs。*

### 研究が推奨するアプローチ

| 手法 | 精度改善 | レイテンシ | 本番準備状況 |
|---|---|---|---|
| BM25 キーワード検索 | ++ | 低 | GA（Anthropic tool_search_tool） |
| Embedding コサイン類似度 | + | 中 | プロトタイプ段階 |
| ハイブリッド（BM25 + embedding） | +++ | 中 | 研究で最良 |
| OATS（outcome-aware embedding） | +++ | 低 | 研究段階 |

Dynamic ReAct（arxiv 2509.20386）の重要な知見: "Search and Load" メタツールがツール読み込みを 50% 削減しつつ精度を維持または改善。

Tool2Vec（Red Hat, 2025）の重要な知見: description だけでなく「そのツールが答えられる典型的な質問」を embed すると Recall@5 が約 50% 相対向上。開発者の語彙とユーザーの語彙のギャップを埋める。

OATS（arxiv 2603.13426）の重要な知見: 事前計算済みの静的類似度ルックアップ（サービング時に GPU 不要）が NDCG@5 0.940 を達成（ベースライン 0.869）し、LLM ベース選択より 1000x 高速。

---

## Proposed implementation

4 つのコンポーネント。A → B → C → D の順で実装。

### Component A — BM25 スキル事前絞り込み（SMALL）

**内容**: LLM にスキルを渡す前に、ユーザーメッセージをクエリとして BM25 でスキル名+説明を検索し、上位 K 件（デフォルト K=5）だけを `invoke_skill.name` enum に含める。

**変更場所**: `src/reyn/chat/router_loop.py` — `build_tools()` 呼び出しの前。

```python
# ツール構築前: BM25 でスキルリストを絞り込む
if len(all_skills) > SKILL_SEARCH_THRESHOLD:  # 例: 20
    candidate_skills = bm25_skill_search(user_message, all_skills, top_k=5)
else:
    candidate_skills = all_skills

tools = build_tools(..., available_skills=candidate_skills, ...)
```

**BM25 インデックス**: セッション開始時に 1 回構築。スキルレジストリが変更されたら再構築。`src/reyn/chat/services/skill_search.py`（新規）に配置。

**フォールバック**: BM25 が 0 件（キーワード不一致）の場合は全 enum にフォールバック（既存の動作）。

**プロンプトへの影響**: `invoke_skill.name` enum が O(N_skills) から O(K) に縮小。ツールスキーマのキャッシュヒット率が向上。

### Component B — スキル説明のエンリッチメント / Tool2Vec（SMALL）

**内容**: 各スキルの検索用説明に「そのスキルが答えられる質問例」を追加する。軽量な LLM 呼び出しでオフライン生成し、`skill.search_hints` としてスキルレジストリに保存。

```yaml
# skill.md frontmatter（新しい任意フィールド）
search_hints:
  - "このコードをレビューして改善案を出して"
  - "PR を確認して問題点を教えて"
  - "このファイルのセキュリティを監査して"
```

`search_hints` がない場合は既存の `description` フィールドにフォールバック。

**影響**: BM25 と embedding 検索がよりリッチなテキストで動作し、Tool2Vec で示された開発者語彙/ユーザー語彙のギャップを解消。

**生成方法**: `reyn skill enrich <name>` CLI コマンド（任意）がワンショット LLM 呼び出しでヒントを生成し frontmatter に書き込む。スキル作者が手動で書くことも可能。

### Component C — Embedding ベースの事前絞り込み（MEDIUM）

**内容**: BM25（Component A）をベクトル類似度検索で補完または置き換える。各スキルの `description + search_hints` をオフラインで embed し、クエリ時にユーザーメッセージを embed してコサイン類似度で上位 K 件を返す。

**変更場所**: `src/reyn/chat/services/skill_search.py` — `SkillSearchIndex` クラス（3 バックエンド）:
- `BM25Backend`（Component A）
- `EmbeddingBackend`（Component C）
- `HybridBackend`（BM25 + embedding、RRF fusion）

**Embedding モデル**: `reyn.yaml` で設定可能:

```yaml
skill_search:
  backend: hybrid          # bm25 | embedding | hybrid
  embedding_model: local   # local (sentence-transformers) | api (openai/anthropic)
  top_k: 5
```

デフォルト `backend: bm25`（embedding モデル不要）。`embedding` または `hybrid` はオペレーターが明示的にオプトイン。

**インデックスライフサイクル**:
- セッション開始時に 1 回構築。`.reyn/skill-index/` に保存（gitignore 対象）
- スキルファイル変更時に再構築（ファイルウォッチャーまたは `reyn skill reindex`）
- スキルファイルのハッシュ比較で鮮度を管理

### Component D — Anthropic tool_search_tool 統合（SMALL）

**内容**: MCP ツールが 30+ ある大規模デプロイでは、全 MCP ツールスキーマを先頭から渡す代わりに Anthropic の `tool_search_tool`（2025-11 GA）と `defer_loading: true` を使用。

**変更場所**: `src/reyn/chat/router_tools.py` — `build_tools()`。

```python
if mcp_tool_count > MCP_SEARCH_THRESHOLD:  # 例: 30
    # 検索メタツールのみ含める。個別ツールはオンデマンドで読み込む
    tools.append(build_mcp_search_tool(mcp_servers))
else:
    # 既存の動作: 全 MCP ツールをインライン
    tools.extend(build_mcp_tools(mcp_servers))
```

**影響**: 大規模 MCP デプロイでコンテキストが O(N_mcp_tools) から O(1)（検索ツールのみ + 検索結果 K=3–5）に削減。Spring AI の実験: Anthropic バックエンドで 63–64% のトークン削減。

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/chat/router_loop.py` | `build_tools()` 前に `available_skills` を絞り込む |
| `src/reyn/chat/router_tools.py` | 絞り込み後スキルリストを渡す；Component D の MCP 検索 |
| `src/reyn/chat/services/skill_search.py` | 新規: `SkillSearchIndex`（BM25 + embedding バックエンド） |
| `src/reyn/config.py` | 新規: `SkillSearchConfig`（backend、top_k、embedding_model） |
| `src/reyn/cli/skill.py` | 新サブコマンド: `reyn skill enrich`（Component B） |

---

## Dependencies

- Component A は依存なし。単独でリリース可能。
- Component B は依存なし。単独でリリース可能（A/C を強化）。
- Component C は Component A に依存（BM25 バックエンドを置き換え）。
- Component D は依存なし。単独でリリース可能。

すべてのコンポーネントは加算的でオプトイン方式。デフォルト設定は既存の動作を保持（検索なし、フル enum——現在と同じ）。

---

## Cost estimate

| コンポーネント | タスク | コスト |
|---|---|---|
| A | BM25 事前絞り込み + `SkillSearchIndex`（BM25 バックエンド） | SMALL |
| B | `search_hints` frontmatter フィールド + `reyn skill enrich` CLI | SMALL |
| C | Embedding バックエンド + ハイブリッド + `.reyn/skill-index/` ライフサイクル | MEDIUM |
| D | Anthropic tool_search_tool MCP 統合 | SMALL |
| Config + docs | `SkillSearchConfig` + reyn.yaml docs | SMALL |
| **合計** | | **MEDIUM** |

A + B は C より先にリリースでき、測定可能な改善を届けられる。C が最大の投資だが Tool2Vec レベルの recall 向上を実現する。

---

## Verification

1. **Component A**: 50+ スキルかつ BM25 有効の状態で、ツールスキーマの `invoke_skill.name` enum が 1 ターンに K 件以下であることを確認。既存のスキルルーティング dogfood でリグレッションがないことを確認。
2. **Component B**: `reyn skill enrich review` が `skill.md` frontmatter に `search_hints:` を書き込む。BM25 と embedding 検索がヒントをインデックスに使用する。
3. **Component C**: 50 スキルを embed し、「PR をレビューして」クエリで top-5 に `code_review` スキルが含まれることを確認。Recall@5 ≥ 80%。
4. **Component D**: MCP ツール 40+ の環境で LLM に送るツールスキーマが検索メタツールのみを含む（40 件すべてではない）ことを確認。ツール検索呼び出し後に正しい MCP ツールが読み込まれることを確認。
5. **トークン削減**: Component A の前後で `input_tokens`（prompt cache miss）を計測。大規模カタログで 30–50% の削減を期待。

---

## Related

- FP-0023 (`0023-router-sp-quick-wins.ja.md`) — 先行する速攻改善 FP
- Dynamic ReAct（arxiv 2509.20386）— "Search and Load" パターン
- Tool2Vec（Red Hat, 2025）— usage-driven embedding
- OATS（arxiv 2603.13426）— outcome-aware ツール選択
- Anthropic tool_search_tool docs — `defer_loading` + BM25/regex バックエンド
- langgraph-bigtool — LangChain の 50+ ツールパターン
- Spring AI `ToolSearchToolCallAdvisor` — Anthropic で 63% トークン削減
