#!/usr/bin/env python3
"""test_enrich.py — Worker /enrich-horse 検証スクリプト

SP版 netkeiba (db.sp.netkeiba.com) から馬の過去走+血統を Worker 経由で取得し、
scraped_data.json 形式に近いファイルを生成する (predict_v4flash.py 互換)。

使い方:
  python scripts/test_enrich.py --date 2026-05-17  # 全レース
  python scripts/test_enrich.py --race 202605020801  # 特定レースのみ
"""

import argparse, json, os, sys, time
from urllib.request import urlopen, Request

WORKER = os.environ.get(
    "ENRICH_WORKER_URL",
    "https://equity-equine-worker.tachibanananana.workers.dev",
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": UA,
}


def post_json(url, payload):
    req = Request(url, data=json.dumps(payload).encode(), headers=HEADERS, method="POST")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    parser = argparse.ArgumentParser(description="Test Worker /enrich-horse")
    parser.add_argument("--date", help="Target date (YYYY-MM-DD)")
    parser.add_argument("--race", help="Single race_id")
    parser.add_argument("--limit", type=int, default=5, help="Max past races per horse")
    args = parser.parse_args()

    if not args.date and not args.race:
        parser.error("--date or --race required")

    # 1. レース一覧取得 (簡易: 直接指定 or scrape_race から)
    if args.race:
        race_ids = [args.race]
    else:
        # scrape_race.py の date list だけ使う
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/scrape_race.py", f"--date={args.date}"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            print(f"scrape_race.py failed:\n{result.stderr}")
            sys.exit(1)
        # scraped_data.json から race_id 一覧を抽出
        with open("scraped_data.json") as f:
            data = json.load(f)
        race_ids = [r["race_id"] for r in data.get("races", [])]
        print(f"Found {len(race_ids)} races for {args.date}")

    # 2. 各レースの出走馬IDを取得
    all_horse_ids = set()
    race_horses: dict[str, list[str]] = {}

    for rid in race_ids:
        # Worker のダッシュボードAPIで馬一覧を取得
        try:
            url = f"{WORKER}/dashboard/recommended?race_id={rid}"
            req = Request(url, headers={"Accept": "application/json", "User-Agent": UA})
            with urlopen(req, timeout=10) as resp:
                items = json.loads(resp.read().decode()).get("items", [])

            horse_ids = list(set(item["horseId"] for item in items))
            race_horses[rid] = horse_ids
            all_horse_ids.update(horse_ids)
            print(f"  {rid}: {len(horse_ids)} horses")
        except Exception as e:
            print(f"  {rid}: skip (no data yet or error: {e})")
            continue

    print(f"\nTotal unique horses: {len(all_horse_ids)}")

    # 3. 各馬を enrich
    enriched: dict[str, dict] = {}
    for i, hid in enumerate(sorted(all_horse_ids)):
        print(f"[{i+1}/{len(all_horse_ids)}] {hid} ...", end=" ", flush=True)
        try:
            result = post_json(f"{WORKER}/enrich-horse", {"horse_id": hid, "past_limit": args.limit})
            enriched[hid] = {
                "sire": result.get("sire", ""),
                "damsire": result.get("damsire", ""),
                "past_race_ids": result.get("past_race_ids", []),
                "saved": result.get("saved_past_results", 0),
            }
            print(f"✓ sire={result['sire']}, damsire={result['damsire']}, past={result['saved_past_results']} saved")
        except Exception as e:
            print(f"✗ {e}")
        time.sleep(0.3)  # Worker 負荷軽減

    # 4. サマリー
    total_saved = sum(e["saved"] for e in enriched.values())
    sire_count = sum(1 for e in enriched.values() if e["sire"])
    damsire_count = sum(1 for e in enriched.values() if e["damsire"])
    print(f"\n=== Summary ===")
    print(f"  Horses processed: {len(enriched)}")
    print(f"  With sire: {sire_count}")
    print(f"  With damsire: {damsire_count}")
    print(f"  Past results saved: {total_saved}")


if __name__ == "__main__":
    main()
