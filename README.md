# EquityEquine — 競馬予想評価システム

netkeiba 無料データをスクレイピングし、DeepSeek V4 Flash で勝率を推論、的中率を Brier Score で評価するサーバーレス競馬予想システム。

**ダッシュボード**: https://master.equity-equine.pages.dev

## 技術スタック

| 層 | 技術 |
|----|------|
| AI 推論 | DeepSeek V4 Flash (`deepseek/deepseek-v4-flash`) via [OpenRouter](https://openrouter.ai/) |
| データベース | [Turso](https://turso.tech/) (libSQL) + raw SQL (HTTP API) |
| API Worker | [Hono](https://hono.dev/) on Cloudflare Workers |
| フロントエンド | Cloudflare Pages (static HTML + Chart.js) |
| スクリプト | Python 3.12+ (requests, openai lib, beautifulsoup4) |
| スケジューラ | GitHub Actions (cron: 毎日9時推論 / 20時結果収集) |
| データソース | [race.netkeiba.com](https://race.netkeiba.com/) + [db.sp.netkeiba.com](https://db.sp.netkeiba.com/) (SP版) |

## データベーススキーマ

```
races
├── id (TEXT PRIMARY KEY, 例: 202605020801)
├── date, venue, distance, track_condition, lap_times (JSON)
└── result_confirmed (INT)

horses
├── id (TEXT PRIMARY KEY, netkeiba horse_id)
└── name, sire, damsire

past_results
├── horse_id (FK → horses.id)
├── race_date, finish_time, passage_rank, last_3furlong
├── race_comment, structured_comment
└── INDEX: idx_past_results_horse (horse_id, race_date)

predictions
├── race_id, horse_id (FK)
├── model_name, win_probability, odds_at_prediction
├── expected_value, recommended (EV > 1.25 なら 1)
├── reasoning_logic, created_at
└── INDEX: idx_predictions_race (race_id, horse_id, model_name)

actual_results
├── race_id, horse_id (FK)
├── finish_order, confirmed_odds, hit (1着=1), brier_score
└── INDEX: idx_actual_results_race (race_id, horse_id)
```

## システム構成 (Worker 方式)

```
race.netkeiba.com ───┐
                     │ scrape (shutuba.html → 出走表 + オッズ)
                     ▼
        scripts/scrape_entries.py (日付リスト→レース一覧→出走表)
                     │
        scripts/enrich_horses.py (Worker /enrich-horse + /enrich-race 呼出)
                     │
db.sp.netkeiba.com ──┼── Worker /enrich-horse → 血統 (父/母父)
                     │         /enrich-race  → 過去走 (タイム/通過/上り3F)
                     ▼
               Turso DB
                     │
        scripts/save_and_predict.py (エントリ保存)
        scripts/predict_v4flash.py (V4 Flash 推論)
                     │
              Worker /save-predictions
                     │
              Worker /dashboard/*
                     │
          Cloudflare Pages (ダッシュボード)
```

## Worker エンドポイント一覧

| メソッド | パス | 用途 |
|----------|------|------|
| POST | `/save-and-predict` | スクレイピングデータ保存 |
| POST | `/save-predictions` | 推論結果の一括保存 (dedup: race+model単位) |
| POST | `/results-collect` | 結果ページスクレイピング + ラップタイム + Brier Score |
| POST | `/enrich-horse` | SP版から1頭の血統+過去走取得 → DB保存 |
| POST | `/enrich-race` | SP版から1過去レース全馬の詳細を一括取得 → DB保存 |
| GET | `/dashboard/races` | レース一覧 |
| GET | `/dashboard/stats` | 集計 (総レース数/予測数/推奨馬数/ROI) |
| GET | `/dashboard/recommended` | 予測一覧 (レース選択/ソート機能付き) |
| GET | `/dashboard/brier` | Brier Score 時系列 |
| GET | `/dashboard/roi` | ROI 時系列 |
| POST | `/admin/reset-race` | レースの予測・実績をリセット |
| POST | `/admin/cleanup-dupes` | 重複行のクリーンアップ |
| POST | `/admin/sync-odds` | actual_results → predictions オッズ同期 |
| POST | `/admin/query` | 生 SQL デバッグ |
| GET | `/health` | ヘルスチェック |

### `/enrich-horse` 詳細

- 入力: `{"horse_id": "2023106113", "past_limit": 5, "pedigree_only": false}`
- SP版 `db.sp.netkeiba.com` から EUC-JP デコード + Chrome UA で fetch
- 血統: `/horse/{id}/` プロフィールページから `父`/`母父` ラベル抽出 (英数字ID対応)
- 過去走: `/horse/result/{id}/` → レースID一覧 → `/race/{id}/` → タイム/通過/上り3F
- `pedigree_only=true` 時は血統のみ取得 (過去走fetch スキップ)

### `/enrich-race` 詳細

- 入力: `{"race_id": "202605020801"}`
- SP版 `/race/{id}/` を1回fetchし、全馬のタイム/通過/上り3F/着順を抽出
- `past_results` に一括保存 (`ON CONFLICT DO NOTHING` で重複防止)
- 複数馬が同じ過去レースを共有する場合の重複fetchを除去 (2-pass enrich で使用)

## 予測ロジック (predict_v4flash.py)

### 入力データ
- **レース基本情報**: 競馬場、距離、馬場状態、ラップタイム（結果確定後）
- **出走情報**: 枠番、騎手、斤量、馬体重
- **馬基本情報**: 馬名、父馬、母父馬
- **過去走データ**: 直近5走のタイム、通過順位、上り3F、着順
- **不利要素検出**:
  - 出遅れ: 直近走の位置取りが同馬の通常平均より15ポジション以上後退
  - 掛かり: 通常より極端な先行 + 失速5ポジション以上
- **ペース情報**: ラップタイムからペース種別 (H=ハイ, M=平均, S=スロー) と区間ラップを注入

### 出力
- 全馬の勝率 (合計 ≒ 100%)
- 期待値 (EV = 勝率 × オッズ)
- 推奨馬判定 (EV > 1.25)
- 推論理由 (reasoning、日本語)

## データ収集範囲

| 取得可 | 取得不可/限界 |
|--------|-------------|
| レース基本情報 (場/距離/馬場) | 厩舎コメント (netkeiba Premium 有料) |
| 出走馬 (馬名/ID) | レース短評 (無料枠では空) |
| 血統 (父/母父) — SP版プロフィールから100%取得 | 出走前オッズ (未来レースは shutuba でマスク `---.-`) |
| 過去走 (タイム/通過/上り3F) — SP版 `/race/{id}/` から | |
| 出走情報 (枠番/騎手/斤量/馬体重) | |
| 確定オッズ (過去レースは result.html から取得) | |
| 着順 | |
| ラップタイム | |
| V4 Flash 勝率推論 (日本語) | |

## GitHub Actions ワークフロー

### predict.yml (毎日 9:00 JST)
1. `scrape_entries.py` — race.netkeiba.com から出走表スクレイピング (~15秒)
2. `enrich_horses.py` — Worker `/enrich-horse` + `/enrich-race` で SP版血統+過去走取得 (~90秒)
3. `save_and_predict.py --skip-predict` — エントリを Turso に保存
4. `predict_v4flash.py` — V4 Flash で推論し結果を Worker `/save-predictions` に送信

ワークフロー手動実行時は `date` (対象日)、`race_ids` (特定レースのみ) の指定が可能。
所要時間: 1レースあたり ~2分 (旧SP直接方式 30分超から 15倍高速化)。

### results.yml (毎日 20:00 JST)
1. Worker `/results-collect` を curl で叩いて netkeiba 結果を収集
2. Brier Score 計算 + ラップタイム保存

## ローカル実行

```bash
# 1. 環境変数
export OPENROUTER_API_KEY="sk-or-..."
export TURSO_DATABASE_URL="libsql://..."
export TURSO_AUTH_TOKEN="..."
export WORKER_SAVE_PREDICTIONS_URL="https://<worker>/save-predictions"
export ENRICH_WORKER_URL="https://<worker>"
export API_SECRET="..."

# 2. エントリスクレイピング (race.netkeiba.com のみ、高速)
python scripts/scrape_entries.py --date 2026-05-17

# 3. SP版 血統+過去走 取得 (Worker 経由)
python scripts/enrich_horses.py --input scraped_data.json

# 4. DB 保存
python scripts/save_and_predict.py --skip-predict --input scraped_data.json

# 5. 推論
python scripts/predict_v4flash.py --input scraped_data.json

# または GitHub Actions の workflow_dispatch で実行
```

## 評価指標

- **Brier Score**: 予測確率のキャリブレーション評価（0 に近いほど良い）
  - `Brier = (P_win - outcome)²` の平均
  - 現在の平均: ~0.062（ランダム均等割り ~0.059 と近いが、的中馬確率 14.3% vs ランダム 7.9% で 1.8x 改善）
- **ROI (回収率)**: 推奨馬 (EV > 1.25) 全頭に均等 100 円賭けた場合の回収率 (%)
  - 的中馬が高オッズになるほど高い
- **最高確率馬的中率**: 58%（24レース中14レース） — JRA 1番人気勝率 ~33% より高い
- **推奨馬に的中馬が含まれた率**: 42%

## 最近の修正

| 日付 | コミット | 内容 |
|------|----------|------|
| 2026-05-22 | `6b29800` | scrape_entries: race_ids が日付リスト外の場合の直接 shutuba フォールバック |
| 2026-05-22 | `137391d` | DB index 追加 (3テーブル計4件) + dashboard auto-refresh 完全停止 (Turso 読み取り超過対策) |
| 2026-05-22 | `5f9f2a7` | enrich 最適化: 2-pass dedup + 血統英数字ID対応 + test_sp_connect.py 削除 |
| 2026-05-22 | `6e5e337` | pedigree 抽出: 血統表→馬プロフィールページ方式 + 英数字ID対応 (sire coverage 100%) |
| 2026-05-22 | `b3bd20f` | Worker POST /enrich-horse 新設 (SP版 per-horse スクレイピング) |
| 2026-05-22 | `272a3ff` | scrape_race.py SP書き換え (db.sp.netkeiba.com 全置換、並行fetch) |
| 2026-05-21 | `bb0b085` | fetch_html に指数バックオフリトライ追加 (400/429/502/503/504) |
| 2026-05-21 | `148c399` | sleep 間隔を倍に (horse 0.5→1.0s, race 1.0→2.0s) |
| 2026-05-21 | `c488e9c` | odds セレクタ修正: `.Popular` (人気順位) → `td.Txt_R.Popular` |

## 未完了・制限事項

1. **厩舎コメントが空**: netkeiba 無料枠ではコメントが取得不可。netkeiba Premium (¥550/月) で取得可能
2. **Worker 30 秒制限**: 16 頭の推論が収まらないため、Python（GH Actions）側で推論を実行
3. **DB 保存未対応の新フィールド**: 枠番/騎手/斤量/馬体重は scraped_data.json 経由でプロンプト注入済みだが、DB テーブルには保存されていない
4. **タイム標準化**: 異なる距離/馬場のタイム比較のための標準化ロジックは未実装（検討中）
5. **予測精度の経時分析**: データ蓄積後の回帰分析・モデル改善は未着手
6. **未来レースのオッズ**: レース2-3日前まで shutuba.html の odds が `---.-` にマスクされる
7. **Turso 無料枠超過対策済み**: dashboard 30s auto-refresh 削除、index 追加、exist check 削除 → 推定使用量 540M行/月→3M行/月 (無料枠 1Bの0.3%)
