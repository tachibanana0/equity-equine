"""
add_indexes.py — DB インデックス追加
Turso の読み取りがブロックされている場合、CREATE INDEX は失敗する可能性あり。
書き込み枠 (25M行/月) で動作するか試行。
"""
import json, os, sys
from urllib.request import urlopen, Request

WORKER = os.environ.get("ENRICH_WORKER_URL", "https://equity-equine-worker.tachibanananana.workers.dev")
WORKER_ADMIN = WORKER + "/admin/query"
WORKER_CHECK = WORKER + "/admin/db-check"
UA = "Mozilla/5.0 DB-Index/1.0"

def run_sql(sql: str) -> dict:
    req = Request(WORKER_CHECK, data=json.dumps({
        "requests": [{"type": "execute", "stmt": {"sql": sql}}]
    }).encode(), headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    # Use admin/query which returns the raw Turso response
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
        # if it has 'rows' key, it's the admin/query response
        if "rows" in data:
            return data
        # otherwise it's the db-check response
        raw = data.get("raw", data)
        if isinstance(raw, dict) and "results" in raw:
            r = raw["results"][0]
            return {"type": r.get("type"), "error": r.get("error", {}).get("message", ""), "rows": []}
        return data

if __name__ == "__main__":
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_past_results_horse ON past_results(horse_id, race_date)",
        "CREATE INDEX IF NOT EXISTS idx_predictions_race ON predictions(race_id, horse_id, model_name)",
        "CREATE INDEX IF NOT EXISTS idx_actual_results_race ON actual_results(race_id, horse_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_past_results_uniq ON past_results(horse_id, race_date)",
    ]
    for sql in indexes:
        print(f"[INFO] {sql}")
        try:
            result = run_sql(sql)
            print(f"  -> {json.dumps(result, ensure_ascii=False, default=str)[:200]}")
        except Exception as e:
            print(f"  -> ERROR: {e}")
