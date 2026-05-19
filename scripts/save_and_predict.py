"""
構造化済みデータを Turso DB に保存し、Cloudflare Workers の推論APIを呼び出すスクリプト。
Turso HTTP API を直接利用。
"""

import json
import os
import sys
import time
import argparse
import hashlib
import hmac
import requests

TABLES_DDL = """
CREATE TABLE IF NOT EXISTS races (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    distance INTEGER NOT NULL,
    track_condition TEXT NOT NULL DEFAULT '良',
    lap_times TEXT,
    result_confirmed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS horses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sire TEXT,
    damsire TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS past_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id TEXT NOT NULL REFERENCES horses(id),
    race_date TEXT NOT NULL,
    finish_time REAL,
    passage_rank TEXT,
    last_3furlong REAL,
    race_comment TEXT,
    structured_comment TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL REFERENCES races(id),
    horse_id TEXT NOT NULL REFERENCES horses(id),
    win_probability REAL NOT NULL,
    reasoning_logic TEXT,
    odds_at_prediction REAL,
    expected_value REAL,
    model_name TEXT NOT NULL,
    recommended INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS actual_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL REFERENCES races(id),
    horse_id TEXT NOT NULL REFERENCES horses(id),
    finish_order INTEGER,
    confirmed_odds REAL,
    hit INTEGER DEFAULT 0,
    brier_score REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class TursoDB:
    def __init__(self):
        self.db_url = os.environ.get("TURSO_DATABASE_URL", "")
        self.auth_token = os.environ.get("TURSO_AUTH_TOKEN", "")
        if not self.db_url or not self.auth_token:
            print("[ERROR] TURSO_DATABASE_URL or TURSO_AUTH_TOKEN not set")
            sys.exit(1)

        # libsql://host から https://host に変換
        base = self.db_url.replace("libsql://", "https://")
        self.api_url = f"{base}/v2/pipeline"

        self._execute_raw(TABLES_DDL)

    def _execute_raw(self, sql: str) -> dict:
        resp = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "Content-Type": "application/json",
            },
            json={"requests": [{"type": "execute", "stmt": {"sql": sql}}]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def execute_batch(self, statements: list[str]) -> None:
        requests_data = {
            "requests": [
                {"type": "execute", "stmt": {"sql": stmt}}
                for stmt in statements
            ]
        }
        resp = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "Content-Type": "application/json",
            },
            json=requests_data,
            timeout=30,
        )
        resp.raise_for_status()


def escape_sql(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def save_race(db: TursoDB, race_data: dict) -> None:
    race_id = race_data["race_id"]
    venue = race_data.get("venue", "?")
    date = race_data.get("date", "")
    distance = race_data.get("distance", 0)
    track_condition = race_data.get("track_condition", "良")

    stmts = []

    stmts.append(
        f"INSERT OR IGNORE INTO races (id, date, venue, distance, track_condition) "
        f"VALUES ({escape_sql(race_id)}, {escape_sql(date)}, {escape_sql(venue)}, "
        f"{distance}, {escape_sql(track_condition)})"
    )

    for h in race_data.get("horses", []):
        horse_id = h["horse_id"]
        horse_name = h["horse_name"]
        sire = h.get("sire", "")
        damsire = h.get("damsire", "")

        stmts.append(
            f"INSERT OR IGNORE INTO horses (id, name, sire, damsire) "
            f"VALUES ({escape_sql(horse_id)}, {escape_sql(horse_name)}, "
            f"{escape_sql(sire)}, {escape_sql(damsire)})"
        )

        for past in h.get("past_results", []):
            stmts.append(
                "INSERT OR IGNORE INTO past_results "
                "(horse_id, race_date, finish_time, passage_rank, last_3furlong, race_comment, structured_comment) "
                f"VALUES ({escape_sql(horse_id)}, {escape_sql(past.get('race_date', ''))}, "
                f"{past.get('finish_time') if past.get('finish_time') else 'NULL'}, "
                f"{escape_sql(past.get('passage_rank', ''))}, "
                f"{past.get('last_3furlong') if past.get('last_3furlong') else 'NULL'}, "
                f"{escape_sql(past.get('race_comment', ''))}, "
                f"{escape_sql(past.get('structured_comment', ''))})"
            )

    db.execute_batch(stmts)
    print(f"[DB] Saved race {race_id}")


def call_predict_api(race_id: str) -> bool:
    worker_url = os.environ.get("WORKER_PREDICT_URL")
    api_secret = os.environ.get("API_SECRET")
    if not worker_url or not api_secret:
        print("[ERROR] WORKER_PREDICT_URL or API_SECRET not set")
        return False

    sig = hmac.new(api_secret.encode(), race_id.encode(), hashlib.sha256).hexdigest()
    resp = requests.post(
        worker_url,
        json={"race_id": race_id},
        headers={
            "Content-Type": "application/json",
            "X-API-Signature": sig,
        },
        timeout=120,
    )

    if resp.status_code == 200:
        print(f"[API] Prediction triggered for {race_id}: {resp.json()}")
        return True
    else:
        print(f"[API] Failed to trigger prediction for {race_id}: {resp.status_code} {resp.text[:200]}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Turso保存 + Worker推論呼び出し")
    parser.add_argument("--input", required=True, help="structure_with_flash.py の出力JSON")
    parser.add_argument("--skip-predict", action="store_true", help="推論呼び出しをスキップ")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        races = json.load(f)

    db = TursoDB()

    for race in races:
        try:
            save_race(db, race)
        except Exception as e:
            print(f"[ERR] DB save failed for {race['race_id']}: {e}")
            continue

        if not args.skip_predict:
            try:
                call_predict_api(race["race_id"])
            except Exception as e:
                print(f"[ERR] Predict API call failed for {race['race_id']}: {e}")
            time.sleep(1)

    print("[DONE]")


if __name__ == "__main__":
    main()
