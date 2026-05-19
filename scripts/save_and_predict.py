"""
構造化済みデータを Turso DB に保存し、Cloudflare Workers の推論 API を呼び出す。

テーブル保存順序:
  1. horses   (馬の基本情報 + 血統)
  2. races    (レース基本情報)
  3. past_results (過去走データ + V4 Flash 構造化コメント)
  4. Workers predict API 呼び出し → predictions テーブルに保存
"""

import json
import os
import sys
import time
import argparse
import hashlib
import hmac

import requests


def get_turso_conn():
    """Turso HTTP API を使ってクエリを実行するヘルパー"""
    db_url = os.environ.get("TURSO_DATABASE_URL")
    auth_token = os.environ.get("TURSO_AUTH_TOKEN")
    if not db_url or not auth_token:
        print("[ERROR] TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set")
        sys.exit(1)
    return db_url, auth_token


def turso_exec(db_url: str, auth_token: str, sql: str, params: list = None) -> dict:
    """Turso HTTP API で SQL を実行"""
    # Turso HTTP API エンドポイント
    # 形式: https://[hostname]/v2/pipeline または libsql://...
    # libsql:// から https:// に変換
    url = db_url
    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://")

    # pipeline エンドポイント
    pipeline_url = f"{url}/v2/pipeline"

    statements = [{"q": sql}]
    if params:
        statements[0]["params"] = params

    resp = requests.post(
        pipeline_url,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        },
        json={"requests": [{"type": "execute", "stmt": {"sql": sql, "args": params or []}}]},
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"  [WARN] Turso error {resp.status_code}: {resp.text[:200]}")
        return None

    return resp.json()


def save_to_turso(races: list[dict]):
    """スクレイピングデータを全テーブルに保存"""
    db_url, auth = get_turso_conn()

    for race in races:
        race_id = race["race_id"]
        race_date = race.get("date", "")
        venue = race.get("venue", race_id[4:6])
        distance = race.get("distance", 0)
        track_condition = race.get("track_condition", "良")
        lap_times = json.dumps(race.get("lap_times", {})) if race.get("lap_times") else None

        # races テーブル (INSERT OR REPLACE)
        turso_exec(db_url, auth,
            "INSERT OR REPLACE INTO races (id, date, venue, distance, track_condition, lap_times) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [race_id, race_date, venue, distance, track_condition, lap_times],
        )

        for h in race.get("horses", []):
            horse_id = h["horse_id"]
            horse_name = h["horse_name"]
            sire = h.get("sire", "")
            damsire = h.get("damsire", "")

            # horses テーブル
            turso_exec(db_url, auth,
                "INSERT OR REPLACE INTO horses (id, name, sire, damsire) VALUES (?, ?, ?, ?)",
                [horse_id, horse_name, sire, damsire],
            )

            # past_results テーブル
            for pi, past in enumerate(h.get("past_results", [])):
                structured = past.get("structured_comment")
                turso_exec(db_url, auth,
                    "INSERT INTO past_results (horse_id, race_date, finish_time, passage_rank, last_3furlong, race_comment, structured_comment) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        horse_id,
                        past.get("race_date", ""),
                        past.get("finish_time"),
                        past.get("passage_rank"),
                        past.get("last_3furlong"),
                        past.get("race_comment", ""),
                        structured if isinstance(structured, str) else json.dumps(structured, ensure_ascii=False) if structured else None,
                    ],
                )

            time.sleep(0.1)

        print(f"  [OK] Saved race {race_id} ({len(race.get('horses', []))} horses)")

    print(f"[DONE] Turso DB saved {len(races)} races")


def trigger_prediction(races: list[dict]):
    """Workers predict API を呼び出す。レースごとの馬ID・オッズ情報を含める"""
    worker_url = os.environ.get("WORKER_PREDICT_URL")
    api_secret = os.environ.get("API_SECRET")
    if not worker_url or not api_secret:
        print("[ERROR] WORKER_PREDICT_URL and API_SECRET must be set")
        sys.exit(1)

    # 新しいリクエスト形式: { "races": [{ "race_id": "...", "horses": [{"horse_id": "...", "odds": 1.5}] }] }
    race_entries = []
    for race in races:
        race_id = race.get("race_id")
        if not race_id:
            continue
        horses = []
        for h in race.get("horses", []):
            horses.append({
                "horse_id": h["horse_id"],
                "odds": h.get("odds", 0),
            })
        race_entries.append({"race_id": race_id, "horses": horses})

    body = json.dumps({"races": race_entries}).encode()
    signature = hmac.new(
        api_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    resp = requests.post(
        worker_url,
        headers={
            "Content-Type": "application/json",
            "X-API-Signature": signature,
        },
        data=body,
        timeout=120,
    )

    if resp.status_code == 200:
        print(f"[OK] Prediction triggered for {len(race_entries)} races: {resp.text[:300]}")
    else:
        print(f"[ERROR] Prediction API returned {resp.status_code}: {resp.text[:300]}")


def main():
    parser = argparse.ArgumentParser(description="Turso保存 + 推論API呼び出し")
    parser.add_argument("--input", required=True, help="structure_with_flash.py の出力JSON")
    parser.add_argument("--skip-predict", action="store_true", help="推論APIを呼ばず保存のみ")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        races = json.load(f)

    print(f"[INFO] Saving {len(races)} races to Turso")
    save_to_turso(races)

    if not args.skip_predict:
        if races:
            timestamp = races[0].get("date", "unknown")
            print(f"[INFO] Triggering prediction for {len(races)} races (date: {timestamp})")
            trigger_prediction(races)


if __name__ == "__main__":
    main()
