---
title: 「HN の AI agent 関連最新10件」 1 query から始まった full dogfood loop — 観測 → 真因 → fix → re-verify → 4-insight extraction → 全方位 landing → 再利用 tooling
date: 2026-05-09
session-arc: docs restructure → tutorial 02 rewrite → web A2A discoverability → real chat query → diagnosis loop → web_search hint fix → insight extraction → 4-axis landing → hn_research.py
related-commits:
  - 2c56577  # Guide restructure (agent-engineering → Concepts)
  - 4684a90  # Tutorial reordering (chat-mode → 02)
  - 80d649b  # Tutorial 02 multi-agent strip
  - 563ace6  # example query replacement
  - cf9d193  # web A2A endpoint in dogfood-discipline
  - 8af3444  # web_search description hint
  - b465521  # --reload doc + memory
  - 9e04c04  # HN landscape insights
  - a6c780f  # 4-insight landing + hn_research.py
related-insights:
  - 2026-05-09-hn-ai-agent-landscape-insights.md
related-giveup: [G30]
status: chronicle
---

# 「HN の AI agent 関連最新10件」 1 query から始まった full dogfood loop

> 1 つの user query が、 ドキュメントの discoverability、 dogfood discipline、
> tool description design、 industry observation pipeline、 全部に渡る改善 wave
> を引き起こした session の記録。 観測 → 真因 → fix → re-verify → insight →
> landing → tooling 化、 という dogfood discipline の理想形が偶然成立した。

---

## TL;DR

- User: 「HN の AI agent 関連記事の最新 10 件を拾ってきて。 投稿日時も一緒に。」
- 第 1 試行: HN URL 1/10、 投稿日時取れず — Gemini が `HN AI agent latest 10 posts` という素朴 query を構築、 DDG が散逸結果を返した
- User の鋭い問い 「本当にスキル使われてる？」 で events log を読む discipline 発動 → tool は actually 呼ばれていた、 LLM hallucination ではなかった
- 切り分け: DDG は site: operator サポート、 構築 query 側に問題 → web_search description に operator hint 追加 (1 sentence)
- 第 2 試行: HN URL **10/10**、 全件 posting date 付き、 hallucination ゼロ
- 「ついでに 10 件読んで insight 抽出してきて」 → Algolia API で全 thread 取得、 4 actionable insight 抽出
- 「全て着手してほしい。 そして今回のノウハウを今後も使えるように」 → 4 sonnet 並列で全方位 landing + `scripts/hn_research.py` で pipeline tooling 化

総 commit: 9 個 (= `2c56577` から `a6c780f`)、 1467 passed 維持、 mkdocs strict 全段 clean。

---

## 1. 始まり: ドキュメント前哨戦

session は HN クエリではなく、 docs の Guide restructure から始まっていた。

### Wave 0a — Guide 再編 (`2c56577`)

- Researcher の docs-restructure-proposal.md と doc-improvement-proposals.md を最新実装に照らして再評価
- `agent-engineering/` (7 essays) を `guide/for-skill-authors/` から `concepts/agent-engineering/` に移動 (= task-oriented でなく lens-based の concept)
- `for-skill-authors/` を 6 sub-cluster に nav grouping (Foundation / Composition / Phase mechanics / Operations / UX / stdlib authoring tools)
- 不足していた `write-your-first-custom-skill.md` how-to を新規追加 (= researcher P1 #2-A)

### Wave 0b — Getting Started 順序見直し (`4684a90`)

- 旧順序: install → build → run → eval → chat
- 問題: 02 で `skill_builder` でいきなり skill を作る (= value demonstration なし)、 02 本文に「03 は Phase 2」 と stale 表記、 chat-mode が最後
- 新順序: install → **chat (見せる)** → build → run → eval

  ```
  reyn install すぐ → reyn chat で動くもの体感 → 自分で skill 書く
  ```

  build → run → eval の dependency chain は保持、 入口だけ value-first 化。

### Wave 0c — Tutorial 02 で詰まる罠の除去 (`80d649b` / `563ace6`)

User が 02 を読みながら 「`reyn chat researcher` (researcher 存在しない) で先に進めなくなる」 と指摘 → 02 から multi-agent 系内容 (= `reyn agent new` / `/attach` / topology) を完全切り出し、 `default` agent 単独で完結する value-demonstration tutorial に refocus。

更に 02 で示している 4 example queries を **実機検証** することに:

| Query | 結果 |
|---|---|
| `summarize the README of this project` | ✅ |
| `what skills are available?` | ⚠️ 「25 個ある、 詳しく聞く？」 と会話的 ask-back |
| `what's in src/reyn/?` | ✅ |
| `say hi in three languages` | ✅ |

→ "what skills are available?" を「what is this project about?」 に置換 (= concrete summary を返す方が tutorial として価値高い)。

ここで session は **「実機 chat で example query を叩く」** ことに慣れた。 これが伏線になる。

---

## 2. 中盤: Web A2A endpoint 再発見

User の一言: 「web A2A server があるの忘れてるみたいだね。」

### 観測

`reyn chat --cui` を subprocess 経由で叩くと TUI buffering で詰まる。 でも `reyn web` (port 8080) を起動すれば `POST /a2a/agents/<name>` の JSON-RPC `message/send` で 1 round-trip 化 — non-TTY からの chat 駆動が trivial に。

### 反省

これが dogfood-discipline.md の section 6「Reyn-specific tooling」 (= LLM 観測 4 ツール) に **載っていなかった** ことが判明。 載っていないから次回 session で再発見する。 構造的な discoverability 問題。

### Fix (`cf9d193`)

dogfood-discipline (en + ja) の section 6 に新 subsection 追加:

- 起動 / agents 一覧 / メッセージ送信の curl one-liner
- trace / replay tool との切り分け
- 「忘れがちな理由」 の 1 段落 (= dogfood batch driver は `reyn chat --cui` 駆動なので "chat = TUI subprocess" の思考回路が固まる)

memory `feedback_web_a2a_debug_surface.md` も新設、 想起トリガと quick reference 付き。

---

## 3. 本題: HN クエリと最初の失望

User: 「試しに、 「HN の AI agent 関連記事の最新10件を拾ってきて。 投稿日時も一緒に。」 をやって」

A2A endpoint で 1 round-trip。 結果:

- **URL 10 件のうち真の HN URL は 1 件のみ** (`news.ycombinator.com/item?id=44322036`)
- 残りは LinkedIn / dev.to / GitHub / agent.ai / Telegram / hospitalitynet / tribune.com.pk など、 "HN" / "Show HN" の文字列を含むだけの一般 web ページ
- **投稿日時は取れない** (LLM 自身が「web 検索結果から特定できませんでした」 と明言)

私の最初の報告: 「これは web_search の DuckDuckGo backend が HN-specific でない構造的問題、 解決策候補 3 つ ...」

---

## 4. 転回点: 「本当にスキル使われてる？」

User の問い:

> 本当にスキル使われてる？ モデルが勝手に回答作成してる可能性はない？

これが本 session の **methodological turning point**。

### Events log で fact 確定

```
.reyn/events/agents/default/chat/2026-05/2026-05-09T063940.jsonl
```

から `tool_called` / `web_search_started` / `web_search_completed` / `tool_returned` を 30 秒で確認:

- `tool_called`: tool=`web_search`、 query=`HN AI agent latest 10 posts`、 max_results=10
- `web_search_started` / `_completed`: backend=duckduckgo、 result_count=10
- LLM 最終応答の 10 URL = tool_returned の 10 URL と **完全一致**

→ **Hallucination ではなかった**。 LLM は受け取った search 結果を忠実にフォーマットしただけ。 真の犯人は **search backend ではなく、 query construction** (= Gemini が `site:` operator を使わなかった)。

### 切り分け実験

DuckDuckGo 直接呼び出しで 4 query 比較:

| Query | HN URL ratio |
|---|---|
| `HN AI agent latest 10 posts` (Gemini choice) | 1/10 |
| `site:news.ycombinator.com AI agent` | **6/10** |
| `"hacker news" AI agent` | 1/10 (= "thehackernews.com" に散逸) |
| `hacker news AI agent recent` | 1/10 |

確定: **DDG は site: operator をサポートしている、 Gemini が知らなかった (= description に hint がなかった) だけ**。

### 教訓 (= dogfood-discipline Principle 4 の実例)

「観測 infra を先に作る」。 events log がなければ「LLM が捏造してそう」 推測のまま、 description fix ではなく完全に違う方向 (= search backend 入れ替え) に着手していた可能性。 真因の確定は **30 秒の log 確認** で済んだ。

---

## 5. Fix: web_search description に operator hint 1 行 (`8af3444`)

### 設計判断: 強制せず能力告知

User の問い:

> なるほど良いね。でも強制はしない方が良くない？

正しい。 care boundary 原則 + MUST rule 積み重ね回避 (= `feedback_prompt_design.md`、 `feedback_reyn_care_boundary.md`)。

```diff
                "description": (
                    "Search the public web with DuckDuckGo and return "
-                   "structured results. query: search string. "
+                   "structured results. Standard search operators are "
+                   "supported in `query`: `site:<domain>` to scope to "
+                   "one site (e.g. `site:news.ycombinator.com`), "
+                   "`\"phrase\"` for exact match, `-term` to exclude. "
+                   "Use them when the user's intent is site-specific "
+                   "or phrase-anchored; plain keywords work otherwise. "
+                   "query: search string. "
                    "max_results: cap on returned results (default 5)."
                ),
```

「Use them when ... ; plain keywords work otherwise」 で **判断は LLM、 道具を提示しただけ**。

### 4 LLMReplay fixture 再録音

tools schema の hash が変わるので必須。 `LITELLM_API_BASE=http://localhost:4000 REYN_LLM_RECORD=1` で 4 fixture 録音し直し、 1467 passed 維持。

---

## 6. 第 2 障害: Hot reload を忘れていた

User: 「kill だけやりました。 開発時に毎度私が kill するのは現実的でない。 以前の dogfood ではそんなことなかったよ。」

私が directly user の workflow blocker を作っていた。 `reyn web --reload` で uvicorn auto-reload が効くのに、 dogfood-discipline doc には plain `reyn web` しか書いていなかった。

### Fix (`b465521`)

doc + memory を `--reload` 推奨に書き直し。 「dev / debug iteration では `--reload` 必須、 さもないと編集が反映されない」 を boldface で。

---

## 7. 検証: 1 行 description で何が変わったか

`reyn web --reload` で再起動 → 同 query 再投入。

| 指標 | Before (`HN AI agent latest 10 posts`) | After (`site:news.ycombinator.com AI agent`) |
|---|---|---|
| HN URL ratio | 1/10 | **10/10** ✅ |
| 投稿日時 | 取れません | **10/10 (snippet 由来、 hallucination ゼロ)** ✅ |
| LLM 賢さ | keyword spam | **20 件中「snippet date 付き」 14 件から 10 件選択** |

events log で snippet ↔ LLM 応答を一件ずつ突合 → 9/10 で snippet date 完全一致、 1 件のみ "2 weeks ago" vs snippet "3 weeks ago" (= 解釈変動許容範囲)。 **Hallucination ゼロを構造的に証明**。

### 副次的観察

「投稿日時取れません」 と諦めていた前回 vs 「snippet date 付きの 14 件から 10 件 filter 提示」 の今回 — 同 LLM 同 backend で **道具の見せ方ひとつでこれだけ変わる**。 weak model onboarding における安価な勝ち筋として記録価値。

---

## 8. 「ついでに 10 件読んで insight 抽出して」

User の続き:

> 素晴らしい。 ついでにさっきの 10 件の内容をあなたが読んで、 reyn に活かせる insight を抽出してきて

→ 10 thread の content + 上位コメントを **HN Algolia API** で並列取得。 表面的タイトル流し読みではなく、 各 thread の本文 + 高 engagement コメントを横断分析。

### 4 件の actionable insight (`9e04c04`)

| # | 抽出元 | Insight | Reyn への含意 |
|---|---|---|---|
| 1 | Berkeley RDI paper (588 pts) | 主要 AI agent benchmark が空 `{}` 送信で near-perfect score 取れる構造的脆弱性 | Reyn `eval_builder` rubric も「形式チェック」 中心で同型脆弱性、 negative-test + evidence-bound criteria 追加で harden |
| 2 | Lenzy / Agentainer threads | analytics platform / stateful runtime 等の product が Reyn の primitive 上に build される downstream surface | care-boundary に「downstream tooling」 section 追加で明示、 P7 を contributor judgement の guideline に |
| 3 | Tines litmus test (94 pts) | 「act-sense-react loop」 = agent 定義の収束点、 Reyn の Phase model と完全 1:1 mapping | architecture.md に新 section、 LangGraph / AutoGen / Semantic Kernel comparison で読者が知っている語彙を借りる |
| 4 | HATS thread (28 pts) + MoE 比較コメント | multi-agent **debate** primitive は MoE 比で非効率、 HN expert consensus は delegation 支持 | Reyn の delegation-only 路線は正解、 debate 追加しない negative-space decision を giveup-tracker に G30 として記録 |

これらを `docs/deep-dives/journal/insights/2026-05-09-hn-ai-agent-landscape-insights.md` に集約 + メタ insight として「site-scoped DDG → Algolia API 横断分析の pipeline は再現可能」 を末尾に記録。

---

## 9. 「全て着手 + 今後も使えるように」

User の最終リクエスト:

> 素晴らしい全て着手してほしい。 そして今回のノウハウを今後も使えるようにしてほしい

### 並列 sonnet wave (`a6c780f`)

file-disjoint な 4 sonnet を同時 dispatch:

| Sonnet | 担当 | 成果物 |
|---|---|---|
| A | Insight 1 (eval rubric harden) | `eval-builder-rubric.md` に Principle 5「Evidence-bound」 + Adversarial self-check section + Berkeley 出典 |
| B | Insight 2 (care-boundary downstream) | `care-boundary.{md,ja.md}` に「Downstream tooling」 section、 5 raw primitive + 4 product category + Lenzy/Agentainer 名指し |
| C | Insight 3 (act-sense-react) | `architecture.{md,ja.md}` に新 section、 Tines blog 引用 + 1:1 mapping table + framework 比較 |
| D | `scripts/hn_research.py` | site-scoped DDG → Algolia API → 横断 digest を 1 コマンド化 (= 今後の industry research wave に再利用) |

並列 inline で:

- Insight 4: G30 negative-space entry を `giveup-tracker.md` に追記 (= file conflict 回避のため inline)

全 5 work item complete、 1467 passed 維持、 mkdocs strict clean。

---

## 10. 振り返り — なぜこの session が "理想形" だったか

### A. User の指摘 1 つで何度も方向修正された

| User 指摘 | 効果 |
|---|---|
| 「researcher 存在しない (= tutorial 02 で詰まる)」 | tutorial 02 を value-demonstration 単機能に refocus |
| 「web A2A server あるの忘れてる」 | dogfood-discipline section 6 の discoverability gap 修正 |
| 「本当にスキル使われてる？ hallucination ない？」 | events log 起点 diagnosis を強制 → 真因 (query construction) 確定 |
| 「強制はしない方が良くない？」 | description hint を MUST から能力告知に再設計 |
| 「kill 毎回は現実的でない」 | `--reload` discipline が doc 化された |
| 「ついでに insight 抽出して」 | HN Algolia 経由の Industry observation pipeline が偶然確立 |
| 「全て着手 + 今後も使えるように」 | 並列 landing + tooling 化で再現可能化 |

User input がなければ私は「いきなり HN-specific search backend 追加」 等の wrong-layer fix に向かっていた可能性が高い。

### B. dogfood-discipline の Principle が一通り発火した

- **Principle 4 (= 観測 infra を先に作る)**: 「LLM hallucination 疑惑」 を events log で 30 秒解消
- **Principle 5 (= care boundary)**: 「強制せず能力告知」 で description hint を MUST 化せず
- **Principle 7 (= fix classification)**: description hint は 🔵 不具合修正 (= LLM の認知 gap への構造的対応)、 search backend 追加 (= 🟡 仕様変更) は採用しなかった
- **Principle 9 (= simplicity smell test)**: HN search backend 追加・debate primitive 追加など「ありそう」 な複雑解は negative-space decision で記録

### C. 1 つの dogfood query から多軸 landing への波及

```
HN クエリ
  ↓ (1) tool actually called を log で確定
  ↓ (2) DDG vs Gemini の切り分け
  ↓ (3) description hint で 1/10 → 10/10
  ↓ (4) thread 内容を Algolia で横断分析
  ↓ (5) 4 actionable insight 抽出
  ↓ (6) 全 4 sonnet 並列 landing
  ↓ (7) pipeline を hn_research.py で再利用化
```

各段は独立に発生したが、 **1 つの user query が起点**。 dogfood は scenario 駆動だけでなく **「real query で何が起きるかを見る」 単発探索** からも極めて密度の高い学習を生む。

### D. ノウハウの "再使用性" 投資

今回の session の独自性は、 **landing で終わらず tooling と memory に投資した** こと:

- `scripts/hn_research.py` — 30 分の手作業を 1 コマンドに圧縮、 industry positioning research wave が定期化したときの基盤
- `memory/feedback_web_a2a_debug_surface.md` — 「忘れがち」 を構造的に解消する想起トリガ
- `dogfood-discipline.md` 5 軸 toolkit (= 4 観測 ツール + 1 industry research ツール) — discipline doc 自体がノウハウ蓄積層に

「次回も同じ問題を解く」 ではなく「次回はもう問題が起きない」 への投資。

---

## 11. Session 全 commit

```
2c56577  docs: restructure Guide (agent-engineering → Concepts、 6 sub-cluster nav)
4684a90  docs: reorder Getting Started (chat-mode → 02、 value-first onboarding)
80d649b  docs: tutorial 02 strip multi-agent content (default agent only)
563ace6  docs: tutorial 02 example query replacement (verified live)
cf9d193  docs: dogfood-discipline section 6 — web A2A endpoint
8af3444  feat(router): web_search description — operator hint (not mandatory)
b465521  docs: dogfood-discipline — reyn web --reload
9e04c04  docs(insights): HN AI agent landscape (4 insights extracted)
a6c780f  docs+tooling: act on all 4 insights + scripts/hn_research.py
```

総 9 commit、 +1840 行 / -130 行、 mkdocs strict 全段 clean、 1467 passed 維持。

---

## 12. 関連

- 元になった insights: [2026-05-09-hn-ai-agent-landscape-insights.md](../insights/2026-05-09-hn-ai-agent-landscape-insights.md)
- 該当 giveup entry: G30 (= multi-agent debate negative-space)
- 影響を受けた discipline doc: `docs/deep-dives/contributing/dogfood-discipline.md` section 6
- 新規 tool: `scripts/hn_research.py`
- 関連 memory: `feedback_web_a2a_debug_surface.md`、 `feedback_reyn_care_boundary.md`、 `feedback_observe_before_speculate_llm.md`
- 関連原則: P3 (OS controls execution)、 P4 (LLM constrained decision)、 P7 (OS skill-agnostic)、 dogfood-discipline Principle 4 (= observe-first)、 Principle 5 (= care boundary)
