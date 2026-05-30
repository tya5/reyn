---
type: concept
topic: architecture
audience: [human, agent]
---

# アーキテクチャ概要

```
User → Agent → Skill → OS → Phase → Workspace
                  ↘ Event（すべてを記録）
```

## レイヤー

### Agent

ユーザーの意図を解釈します。Skill を選択または生成します。Phase を直接実行することはありません。

実際には、今日の「Agent」は CLI とチャットルーターです。どちらも薄い実装で、ユーザーの入力を Skill へとルーティングします。

### Skill

マークダウンと YAML ファイルのディレクトリです。Phase グラフと最終出力スキーマを定義します。実行可能コードは含みません（オプションの Python プリプロセッサーステップはサンドボックス内で動作します）。

### Phase

再利用可能な処理ユニットです。`input` と instructions だけを宣言します。

### OS

ランタイムの実行エンジンです。制御フローの唯一のオーナーです。[principles.md](principles.md) の P3 と P7 を参照してください。

### Workspace

データのシングルソースオブトゥルースです。すべてのファイル、ツール出力、artifact がここに置かれます。Phase は Control IR を通じて読み書きします。

### Artifact

Phase 間でやりとりされる構造化データです。`artifacts/*.yaml` で宣言されたスキーマに対してバリデーションされます。

### Event

すべての状態変化は event を発行します。デバッグおよび（将来的には）チェックポイントのために再生可能です。

## ランタイムループ

各 Phase 訪問ごとに：

1. コンテキストフレームを構築する（instructions + input + candidate outputs + control ops）。
2. プリプロセッサーステップがあれば実行する（決定論的 — `reference/dsl/preprocessor.md`、Phase 2）。
3. LLM を呼び出す。
4. 受け取る：`next_phase | finish`、artifact、オプションの Control IR ops。
5. OS ルールと選択した遷移先のスキーマに対して出力をバリデーションする。
6. Control IR ops を実行する（ファイル操作、ask_user、サブスキル呼び出しなど）。
7. workspace を更新する。
8. event を発行する。
9. 遷移または終了する。

## なぜこの形なのか

レイヤリングから 3 つの特性が生まれます：

- **再生可能性。** すべての状態変化が event であり、OS が唯一のミューテーターであるため、保存された event ログだけで同じワークフローを決定論的に再生できます（各 Phase 内の LLM の確率性は除く）。
- **Skill の移植性。** OS は特定の skill について何も知らないため（P7）、新しい skill を追加しても OS のコードは変わりません。Skill は純粋なデータと LLM が読める instructions です。
- **制約された LLM の創造性。** LLM は OS が提供した固定の遷移候補セットから選ぶため（P4）、不変条件を壊す制御フローを創造することはできません。

## Phase 実行フロー

上のレイヤー図は各コンポーネントが *何であるか* を示しています。このセクションでは、1 回の Phase 呼び出し中に *何が起こるか* を示します — メンタルモデルを構築したい新しい貢献者、期待通りに動作しない Phase をデバッグしたい場合、1 回の Phase tick のコストを理解したい場合に役立ちます。

```
User        Agent          OS Runtime         LLM (LiteLLM)   Workspace       Events
 │            │                │                    │               │              │
 │──message──>│                │                    │               │              │
 │            │──invoke skill──>│                    │               │              │
 │            │          ┌─────┴──── Phase 訪問ごとに ──────────────────────────┐  │
 │            │          │     │                    │               │              │
 │            │          │     │──read artifacts────────────────────>│              │
 │            │          │     │<───────────────────────── context frame ─────────│  │
 │            │          │     │──────────────────────────────────────────────────── emit phase_started ──>│
 │            │          │     │                    │               │              │
 │            │          │     │──call(messages,────>│               │              │
 │            │          │     │    candidates, ops) │               │              │
 │            │          │     │<── {control,        │               │              │
 │            │          │     │     artifact,        │               │              │
 │            │          │     │     control_ir}      │               │              │
 │            │          │     │                    │               │              │
 │            │          │     ├── artifact をバリデーション（next-phase / final_output_schema 対比）
 │            │          │     │                    │               │              │
 │            │          │     │  ┌── バリデーション失敗時 ──────────────────────┐  │
 │            │          │     │  │──────────────────────────────────────────────── emit validation_error ─>│
 │            │          │     │  │──re-prompt─────>│               │              │
 │            │          │     │  └── (max_phase_retries 範囲内) ───────────────┘  │
 │            │          │     │                    │               │              │
 │            │          │     ├── Control IR op ごとに ───────────────────────┐  │
 │            │          │     │  ├── permission check                          │  │
 │            │          │     │  │──────────────────────────────────────────────── emit <op>_started ────>│
 │            │          │     │  │──dispatch + write result──────>│              │
 │            │          │     │  │──────────────────────────────────────────────── emit <op>_completed ──>│
 │            │          │     │  └────────────────────────────────────────────────────────────────────────│
 │            │          │     │──────────────────────────────────────────────────── emit phase_completed ─>│
 │            │          │     │                    │               │              │
 │            │          │     ├── control.type == transition ──────────────────┐  │
 │            │          │     │  └── Skill graph から次の Phase を選択して繰り返す ┘  │
 │            │          │     │                    │               │              │
 │            │          │     ├── control.type == finish ──────────────────────┐  │
 │            │          │     │  ├── final_output_schema でバリデーション        │  │
 │            │          │     │  │──────────────────────────────────────────────── emit skill_completed ──>│
 │            │          │     │  └────────────────────────────────────────────────────────────────────────│
 │            │          │     │                    │               │              │
 │            │          │     ├── control.type == abort ───────────────────────┐  │
 │            │          │     │  │──────────────────────────────────────────────── emit skill_aborted ───>│
 │            │          │     │  └────────────────────────────────────────────────────────────────────────│
 │            │          └─────┴────────────────────────────────────────────────┘  │
 │            │<───── final_output artifact ────────│               │              │
 │<─── reply ─│                │                    │               │              │
```

**図のレンダリングについて:** 上の図は ASCII アートを使用しています。このドキュメントのビルドでは `pymdownx.superfences` が有効ですが、Mermaid 用の `custom_fences` は設定されていないためです。同じフローの Mermaid 版はプロジェクトのウェブサイトアーキテクチャページで確認できます。

### ステップごとの説明

1. **Context build (P5)** — OS は Phase が input として宣言したものだけを Workspace から読み込みます。それ以外のチャネルを通じて Phase 間でデータが漏れることはありません。

2. **LLM 呼び出し** — OS がプロンプト（instructions + input artifact + `candidate_outputs` + `available_control_ops`）を組み立て、LLM を呼び出します。デフォルトは 1 回; バリデーション失敗時は `max_phase_retries` の範囲内で再試行します。

3. **出力バリデーション (P4)** — LLM のレスポンスに含まれる artifact は、選択した遷移先のスキーマと一致しなければなりません: transition の場合は `next_phase.input_schema`、finish の場合は `skill.final_output_schema`。OS は Skill graph に存在しない Phase 名を reject します。

4. **再プロンプトループ** — バリデーションが失敗した場合、OS は `validation_error` を emit して再プロンプトします。ループは `max_phase_retries` で上限が設けられており、リトライ上限を超えると Phase がクラッシュではなく失敗します。

5. **Control IR 実行 (P3 + permissions)** — OS は `control_ir` 内の各 op を順番にディスパッチします。すべての op はディスパッチ前に permission ゲートを通ります。拒否時は `permission_denied` を emit して構造化された拒否結果を返します。LLM が abort を決定しない限り Phase は中断しません。

6. **Workspace への書き込み (P5)** — データを生成する op（ファイル読み込み、web フェッチ、MCP 呼び出しなど）はすべて、次の op が実行される前に Workspace へ結果を書き込みます。op 間でメモリ上の結果は信頼されません。

7. **Event の emit (P6)** — すべての状態変化が event になります: `phase_started`、`phase_completed`、`validation_error`、`<op>_started`、`<op>_completed`、`skill_completed`、`skill_aborted`。OS は LLM の推論内容には関心を持たず、遷移がバリデーションされ記録されたかどうかだけを確認します。

8. **Transition または finish** — `transition` の場合、OS は Skill graph から次の Phase を選択して新しい Phase 訪問を開始します。`finish` の場合、`skill.final_output_schema` に対して最終 artifact をバリデーションし、`skill_completed` を emit して artifact を呼び出し元に返します。

### act-sense-react との対応

上の図の Phase 訪問ループの各イテレーションが、1 回の完全な act-sense-react サイクルです。**act** は `control_ir` の実行（OS がディスパッチする LLM の決定）です。**sense** は次の訪問の冒頭で Workspace と Events から行われる context frame の組み立てです。**re-act** は更新されたコンテキストを使った次の LLM 呼び出しです。このシーケンス図は、以下の act-sense-react フレーミングが要約している内容を操作的に示します — ループを LLM の挙動の中に暗黙的に埋め込むのではなく、明示的かつ OS が所有するものとして定義する構造的な契約です。

## act-sense-react レンズから見た Reyn

agent コミュニティでは、「agent とは何か」の実用的な定義として、**act → sense → re-act のフィードバックループ**という概念への収束が見られます — システムが世界に影響を与え、その影響を感知し、追加のアクションを選択できることが最低要件だというものです。この framing は Tines のブログ記事 ["What, exactly, is an 'AI Agent'? Here's a litmus test"](https://www.tines.com/blog/a-litmus-test-for-ai-agents/) と、それに続く HN ディスカッションで複数のコメンターが独立して同じループの定式化に辿り着いたことで広く知られるようになりました。

Reyn はこのループを名目上ではなく **構造的に** 実装しています。ループの各ステップは具体的な primitive に対応します：

| ループのステップ | Reyn の primitive |
|-----------------|-------------------|
| **act** | Phase が `control_ir` を出力 — OS がディスパッチする LLM の決定 |
| **sense** | Workspace と Events を次の Phase の context frame が読み込む |
| **re-act** | LLM が新しい context で次の transition と artifact を生成する |
| **ループの閉性** | Skill graph の `transitions` と finish condition |

この対応が構造的であることが、ループが暗黙的な他のフレームワークとの違いです。多くの agent システムでは、「sensing」とは LLM がたまたま読んだもの、「acting」とは LLM がたまたま呼び出したツール、ループはLLM が続けると決めたから閉じる — という具合になっています。Reyn は各ステップを明示的かつ OS が所有するものとして定義します：

- Workspace が唯一の sensing チャネル — LLM が見るのは OS が context frame に組み込んだものだけです。
- Events が唯一の audit 記録 — すべての sense-act サイクルに再生可能なトレースが残ります ([events.md](events.md))。
- Control IR が唯一の acting 語彙 — LLM は宣言された op セット外の新しい操作を作れません。
- Skill graph が唯一の re-act パス — LLM は OS が検証した transition の中から選択し、実行中に新しいエッジを追加できません ([principles.md](principles.md#p3-os-controls-execution))。

これが [P3 (OS controls execution)](principles.md#p3-os-controls-execution) をループの framing で具体化したものです: OS がループ構造を所有し、LLM がその中で決定を行います。

LangGraph・AutoGen・Semantic Kernel といった他の agent フレームワークに慣れている読者にとって、この対応は直接的な置き換えマッピングを提供します。それらのシステムがループをプログラム可能な surface として公開しているのに対し、Reyn はそれを検証済みの runtime contract としてエンコードします。LLM の役割はどのシステムでも同じ (次のステップを決定すること) ですが、ループの境界がコードによって強制されるか、慣習に委ねられるかが異なります。

## カーネルランタイムレイヤー（FP-0020）

`OSRuntime` は 4 つの垂直レイヤーを束ねる薄い配線レイヤーとして実装されており、
各レイヤーがスキル実行の 1 つの深さを担当する：

| レイヤー | モジュール | 責務 |
|---|---|---|
| 1（上位） | `run_orchestrator.py` *（予定、Component D）* | フェーズ順序 + 遷移 + ロールバック + ライフサイクル |
| 2 | `phase_executor.py` | 1 フェーズの act/decide ループ + リトライ |
| 3 | `llm_call_recorder.py` | LLM 呼び出し 1 回 + WAL 記録 + バジェット強制 |
| state | `run_state.py` | レイヤー 1-3 を横断するミュータブルな run スコープ状態 |
| types | `runtime_types.py` | 例外型 + ヘルパー（リーフ、カーネル依存なし） |

`OSRuntime.__init__` がこれらのレイヤー（state → recorder → executor →
orchestrator）を配線し、`OSRuntime.run()` がオーケストレーターに委譲する。

ChatSession も同様に `chat/services/` 配下のサービスに分解されている：

- `compaction_controller.py`
- `skill_runner.py`
- `budget_gateway.py`、`chain_manager.py`、`intervention_registry.py`、
  `memory_service.py`、`router_host_adapter.py`、`snapshot_journal.py`
- `a2a_handler.py`、`intervention_handler.py`、`auto_resume_handler.py`

## 参考

- [principles.md](principles.md) — 8 つの制約
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — 責務境界
- [workspace.md](workspace.md) — Workspace の詳細
- [events.md](events.md) — event の完全な分類
- [Reference: control-ir](../reference/runtime/control-ir.md) — Control IR op のセマンティクス
- [Reference: llm-output-contract](../reference/runtime/llm-output-contract.md) — LLM JSON の形式
- [Reference: events](../reference/runtime/events.md) — event 型
- [Agent engineering — 7 つのレンズ](agent-engineering/index.md) — 外部エンジニアリングの視点から見た同じアーキテクチャ
