"""
レース確定後に netkeiba から結果（着順、確定オッズ）を取得し、
Brier Score を計算して actual_results テーブルに保存する。

パイプライン:
  1. races テーブルから resultConfirmed=false のレースIDを取得
  2. netkeiba 結果ページから着順・確定オッズを取得
  3. predictions テーブルの勝率Pと結果を照合 → Brier Score 算出
  4. actual_results テーブルに保存、resultConfirmed=true に更新
"""

import json
import os
import re
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

NETKEIBA_RACE = "https://race.netkeiba.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9",
}

JST = timezone(timedelta(hours=9))


def get_turso_conn():
    db_url = os.environ.get("TURSO_DATABASE_URL")
    auth_token = os.environ.get("TURSO_AUTH_TOKEN")
    if not db_url or not auth_token:
        print("[ERROR] TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set")
        sys.exit(1)
    return db_url, auth_token


def turso_exec(db_url: str, auth_token: str, sql: str, args: list = None) -> Optional[dict]:
    url = db_url
    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://")

    pipeline_url = f"{url}/v2/pipeline"

    resp = requests.post(
        pipeline_url,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        },
        json={"requests": [{"type": "execute", "stmt": {"sql": sql, "args": args or []}}]},
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"  [WARN] Turso error {resp.status_code}: {resp.text[:200]}")
        return None

    return resp.json()


def turso_query(db_url: str, auth_token: str, sql: str, args: list = None) -> list[dict]:
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
        return []

    data = resp.json()
    rows = []
    results_list = data.get("results", [])
    if results_list and results_list[0].get("type") == "ok":
        response_data = results_list[0].get("response", {})
        result_rows = response_data.get("result", {}).get("rows", [])
        cols = [c["name"] for c in response_data.get("result", {}).get("cols", [])]
        for row in result_rows:
            rows.append(dict(zip(cols, row)))
    return rows


def scrape_result(race_id: str) -> dict:
    """netkeiba の結果ページから全馬の着順・確定オッズを取得"""
    url = f"{NETKEIBA_RACE}/race/result.html"
    params = {"race_id": race_id}
    html = ""
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        resp.encoding = resp.apparent_encoding or "EUC-JP"
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"  [WARN] Failed to fetch result {race_id}: {e}")
        return {"race_id": race_id, "horses": [], "error": str(e)}

    soup = BeautifulSoup(html, "html.parser")

    horses = []
    for row in soup.select("tr.HorseList"):
        # 馬ID
        name_el = row.select_one(".HorseInfo .HorseName a, .HorseInfo a")
        if not name_el:
            continue
        horse_href = name_el.get("href", "")
        m_id = re.search(r"horse/(\d+)", horse_href)
        horse_id = m_id.group(1) if m_id else "?"

        # 着順
        order_el = row.select_one(".Umaban, td:nth-child(1)")
        order_text = order_el.get_text(strip=True) if order_el else ""
        finish_order = None
        try:
            finish_order = int(re.sub(r"[^\d]", "", order_text))
        except:
            pass

        # 確定単勝オッズ
        odds_val = None
        for sel in [".Popular, .Tansho", ".Odds"]:
            odds_el = row.select_one(sel)
            if odds_el:
                odds_text = odds_el.get_text(strip=True)
                try:
                    odds_val = float(re.sub(r"[^\d.]", "", odds_text))
                    break
                except:
                    continue

        horses.append({
            "horse_id": horse_id,
            "finish_order": finish_order,
            "confirmed_odds": odds_val,
        })

    return {"race_id": race_id, "horses": horses}


def calculate_and_save_results(db_url: str, auth: str, race_id: str, results: list[dict]):
    """predictionsテーブルの勝率と結果からBrier Scoreを計算して保存"""
    for r in results:
        horse_id = r["horse_id"]
        finish_order = r["finish_order"]
        confirmed_odds = r.get("confirmed_odds")

        if finish_order is None:
            continue

        # predictions から勝率Pを取得
        pred_rows = turso_query(db_url, auth,
            "SELECT win_probability FROM predictions WHERE race_id = ? AND horse_id = ? ORDER BY created_at DESC LIMIT 1",
            [race_id, horse_id],
        )

        if not pred_rows:
            continue

        win_p = pred_rows[0]["win_probability"]
        hit = 1 if finish_order == 1 else 0
        brier = (win_p - hit) ** 2

        turso_exec(db_url, auth,
            "INSERT OR REPLACE INTO actual_results (race_id, horse_id, finish_order, confirmed_odds, hit, brier_score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [race_id, horse_id, finish_order, confirmed_odds, hit, brier],
        )

    # resultConfirmed を true に更新
    turso_exec(db_url, auth,
        "UPDATE races SET result_confirmed = 1 WHERE id = ?",
        [race_id],
    )


def main():
    parser = argparse.ArgumentParser(description="レース結果収集 + Brier Score計算")
    parser.add_argument("--date", required=True, help="対象日 YYYY-MM-DD (レース当日)")
    parser.add_argument("--race-id", help="特定レースIDのみ")
    args = parser.parse_args()

    db_url, auth = get_turso_conn()

    # 未確定のレースIDを取得
    if args.race_id:
        race_ids = [args.race_id]
    else:
        rows = turso_query(db_url, auth,
            "SELECT id FROM races WHERE result_confirmed = 0 AND date = ?",
            [args.date],
        )
        race_ids = [r["id"] for r in rows]

    if not race_ids:
        print(f"[INFO] No unconfirmed races found for {args.date}")
        return

    print(f"[INFO] Processing {len(race_ids)} races")

    for race_id in race_ids:
        print(f"[INFO] Fetching result for {race_id}")
        result = scrape_result(race_id)

        if result.get("error"):
            print(f"  [WARN] Skipping {race_id}: {result['error']}")
            continue

        horses = result.get("horses", [])
        if not horses:
            print(f"  [WARN] No results found for {race_id}")
            continue

        calculate_and_save_results(db_url, auth, race_id, horses)
        print(f"  [OK] {race_id}: saved {len(horses)} results")
        time.sleep(1.0)

    print("[DONE]")


if __name__ == "__main__":
    main()
