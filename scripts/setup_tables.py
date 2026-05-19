"""
Turso DB のテーブル初期化スクリプト。
初回実行時に schema 定義に従ってテーブルを作成する。
"""
import os
import sys
import requests


def turso_exec(db_url: str, auth_token: str, sql: str, args: list = None) -> dict | None:
    url = db_url
    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://")
    resp = requests.post(
        f"{url}/v2/pipeline",
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        },
        json={"requests": [{"type": "execute", "stmt": {"sql": sql, "args": args or []}}]},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"[ERROR] {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)
    return resp.json()


CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS races (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    distance INTEGER NOT NULL,
    track_condition TEXT NOT NULL,
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


def main():
    db_url = os.environ.get("TURSO_DATABASE_URL")
    auth_token = os.environ.get("TURSO_AUTH_TOKEN")
    if not db_url or not auth_token:
        print("[ERROR] TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set")
        sys.exit(1)

    for stmt in CREATE_TABLES_SQL.strip().split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        print(f"[INFO] {stmt[:50]}...")
        turso_exec(db_url, auth_token, stmt)

    print("[DONE] Tables created")


if __name__ == "__main__":
    main()
