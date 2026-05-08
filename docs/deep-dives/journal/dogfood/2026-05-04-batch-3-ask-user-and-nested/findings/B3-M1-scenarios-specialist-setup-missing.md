# B3-M1 [MED]: scenarios.md S1 に specialist agent 作成手順が抜けていた

> 一行で: 「specialist エージェントに...」 と user input する scenario なのに、
> specialist agent をどう作るかが scenarios.md に書いてなく、 実行 sonnet が
> setup 段階で詰まった。 scenario 設計の不備。

| Field | Value |
|---|---|
| Severity | MED |
| Status | **fixed** at `d8328b2` |
| Scenario | S1 (multi-agent re-confirm) |
| Found | 2026-05-04 |
| Raw observation | [B3-S1-observation.md](B3-S1-observation.md) |

---

## 観測

batch 3 S1 を実行 sonnet が走らせたところ、 `reyn topology show _default` で
specialist agent が居ないと判明。 user input は「specialist エージェントに
カレーレシピを聞いて」 だが、 specialist が存在しないと delegate_to_agent が
失敗する。 sonnet は手動で `reyn agent new specialist` を実行して continue
したが、 setup 手順が scenarios.md に明記されていない不備を report。

## つまり何が起きたか

scenarios.md の S1 Setup section は:

```bash
rm -rf .reyn/
reyn chat default --cui --no-restore
```

だけで、 `rm -rf .reyn/` で specialist が消えた後の **再作成手順が抜けて
いた**。 scenario author (= 私) は S1 を書いた時点で「specialist は既に
居る前提」 で書いたが、 batch 3 が「完全な fresh state から実行」 を
コンセプトにしていた以上、 setup 段階で必ず agent 作成が必要だった。

これは **scenario 設計時の暗黙前提**。 author が「自分の dev env では既に
存在する」 物を scenario 内で再現する手順を書き落とすと、 fresh runner で
実行不可能になる。 dogfood の「shadow しても見えないものを見る」 思想が
反映できていない、 author 視点と runner 視点のギャップ。

## 影響

- batch 3 S1 の実行 sonnet が setup 段階で停止、 finding 報告の中で別途
  setup 手順を recover した経緯あり
- batch 4 retest や batch 6 以降の同 scenario 再実行で同じ問題が出る risk
- scenario が「再現可能な実験記録」 として機能しなくなる (= dogfood の
  根本価値毀損)

## 修正 (`d8328b2`)

scenarios.md S1 の Setup section を 6 行追加 (= 2 行 → 6 行):

```bash
rm -rf .reyn/
reyn agent new specialist          # ← 追加 (specialist 作成)
reyn topology show _default        # ← 追加 (specialist が _default に居るか確認)
export OPENAI_API_KEY=dummy        # ← 追加 (LiteLLM proxy 用)
reyn chat default --cui --no-restore --output-language ja
```

加えて「`rm -rf .reyn/` で specialist が消えるので毎回再作成必要」 を注記。

## 後続 candidate

- 他 scenario の setup section も同様の暗黙前提が無いか audit
- scenario template に「fresh state から再現可能か?」 の self-check
  checklist を入れる (= author 視点で書き落とした暗黙前提を検出)
- `reyn topology show` を A1 step の output validation で必須化

## 教訓

1. **scenario 設計は author 視点 vs runner 視点で gap が出る**: author が
   既に持っている前提を scenario 内で再現可能にする責任は author 側
2. **fresh state 再現性が dogfood 価値の前提**: 「shadow しても見えない
   ものを見る」 のは fresh runner で再現できることが条件
3. **小さな MED でも cross-batch 影響大**: B3-M1 を batch 3 で fix しなかったら
   batch 4 retest / batch 5 retest 2 で同じ setup 詰まりが繰り返された
