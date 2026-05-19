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
| スクリプト | Python 3.11+ (requests, openai lib, beautifulsoup4) |
| スケジューラ | GitHub Actions (cron: 毎日9時推論 / 20時結果収集) |
| データソース | [netkeiba.com](https://race.netkeiba.com/) 無料枠 |

## データベーススキーマ

```
races
├── id (TEXT PRIMARY KEY, 例: 202605020801)
├── date, venue, distance, track_condition, lap_times (JSON)
└── result_confirmed (INT)

horses
├── id (TEXT PRIMARY KEY, netkeiba horse_id)
├── name, sire, damsire
└── race_id (FK → races.id)

past_results
├── horse_id, race_id (FK)
├── date, venue, distance, track_condition
├── finish_order, finish_time, passage_rank, last_3furlong
└── stable_comment (無料枠では空)

predictions
├── race_id, horse_id (FK)
├── model_name, win_probability, odds_at_prediction
├── expected_value, recommended (EV > 1.25 なら 1)
├── reasoning_logic, prompt_text
└── created_at

actual_results
├── race_id, horse_id (FK)
├── finish_order, confirmed_odds, hit (1着=1)
└── brier_score
```

## システム構成

```
netkeiba.com ────┐
                 │ scrape
                 ▼
    scripts/scrape_race.py (4層スクレイピング)
                 │
    scripts/save_and_predict.py (Turso 保存)
                 │
    scripts/predict_v4flash.py (V4 Flash 推論)
                 │
                 ▼
  Cloudflare Worker (/save-predictions)
                 │
                 ▼
            Turso DB
                 │
                 ▼
  Cloudflare Worker (/dashboard/*)
                 │
                 ▼
  Cloudflare Pages (ダッシュボード)
```

## Worker エンドポイント一覧

| メソッド | パス | 用途 |
|----------|------|------|
| POST | `/save-and-predict` | スクレイピングデータ保存 |
| POST | `/save-predictions` | 推論結果の一括保存 |
| POST | `/predict-now` | テスト用・単レース推論 |
| POST | `/results-collect` | 結果ページスクレイピング + ラップタイム保存 |
| GET | `/dashboard/races` | レース一覧 |
| GET | `/dashboard/stats` | 集計 (総レース数/予測数/推奨馬数/ROI) |
| GET | `/dashboard/recommended` | 予測一覧 (?race_id= で絞込可能) |
| GET | `/dashboard/brier` | Brier Score 時系列 |
| GET | `/dashboard/roi` | ROI 時系列 |
| POST | `/admin/reset-race` | レースの予測・実績をリセット |
| POST | `/admin/cleanup-dupes` | 重複行のクリーンアップ |
| POST | `/admin/query` | 生 SQL デバッグ |
| GET | `/health` | ヘルスチェック |

## 予測ロジック (predict_v4flash.py)

### 入力データ
- **レース基本情報**: 競馬場、距離、馬場状態、ラップタイム（結果確定後）
- **馬基本情報**: 馬名、父馬、母父馬
- **過去走データ**: 直近5走のタイム、通過順位、上り3F、着順
- **不利要素検出**: 出遅れ（直近走の位置取りが通常より15ポジション以上後退）、掛かり（通常より極端な先行 + 失速5ポジション以上）
- **ペース情報**: ラップタイムからペース種別 (H=ハイ, M=平均, S=スロー) と区間ラップを注入

### 出力
- 全馬の勝率 (合計 ≒ 100%)
- 期待値 (EV = 勝率 × オッズ)
- 推奨馬判定 (EV > 1.25)
- 推論過程 (reasoning)

## データ収集範囲

| 取得可 | 取得不可/限界 |
|--------|-------------|
| レース基本情報 (場/距離/馬場) | 厩舎コメント (netkeiba Premium 有料) |
| 出走馬 (馬名/ID) | レース短評 (無料枠では空) |
| 血統 (父/母父) | 出走前オッズ (過去レース出馬表ではマスク) |
| 過去走 (タイム/通過/上り3F) | |
| 確定オッズ | |
| 着順 | |
| ラップタイム | |
| V4 Flash 勝率推論 | |

## GitHub Actions ワークフロー

### predict.yml (毎日 9:00 JST)
1. `scrape_race.py` — netkeiba からレースデータをスクレイピング
2. `save_and_predict.py --skip-predict` — Turso にデータ保存
3. `predict_v4flash.py` — V4 Flash で推論し結果を Worker に送信

### results.yml (毎日 20:00 JST)
1. Worker `/results-collect` を叩いて netkeiba 結果を収集
2. Brier Score 計算 + ラップタイム保存

## ローカル実行

```bash
# 1. 環境変数
export OPENROUTER_API_KEY="sk-or-..."
export TURSO_DATABASE_URL="libsql://..."
export TURSO_AUTH_TOKEN="..."
export WORKER_SAVE_PREDICTIONS_URL="https://<worker>/save-predictions"
export API_SECRET="..."

# 2. スクレイピング
python scripts/scrape_race.py --date 2026-05-17

# 3. DB 保存
python scripts/save_and_predict.py --skip-predict --input scraped_data.json

# 4. 推論
python scripts/predict_v4flash.py --input scraped_data.json

# または GitHub Actions の workflow_dispatch で実行
```

## 評価指標

- **Brier Score**: 予測確率のキャリブレーション評価（0 に近いほど良い）
  - `Brier = (P_win - outcome)²` の平均
  - 現在の平均: ~0.046-0.057 (良好)
- **ROI (回収率)**: 推奨馬 (EV > 1.25) 全頭に均等 100 円賭けた場合の回収率 (%)
  - 的中馬が高オッズになるほど高い
  - 現在: 0% (的中馬が低オッズで非推奨のため)

## 未完了・制限事項

1. **厩舎コメントが空**: netkeiba 無料枠ではコメントが取得不可。netkeiba Premium (¥550/月) で取得可能
2. **AI モデル混在**: 初期に gemini/gpt-4o-mini で生成された予測が DB に残存
3. **Worker 30 秒制限**: 16 頭の推論が収まらないため、Python 側で推論を実行
4. **レースフィルター未実装**: 全レースが一度に読み込まれる（データ量が増えると要対応）
5. **枠番・斤量未取得**: netkeiba 出馬表から取得可能だが未実装
6. **予測精度の経時分析**: データ蓄積後の回帰分析・モデル改善は未着手
