"""
enrich_horses.py — Worker を使って全馬の過去走+血統を取得する (2-pass 最適化版)

Pass 1: /enrich-horse (pedigree_only) → 各馬の血統 + 過去レースID一覧を取得
Pass 2: 過去レースIDを重複除去 → /enrich-race で一括取得 (同じ過去レースは1回だけfetch)

使い方:
  python enrich_horses.py --input scraped_data.json [--delay 1.0]
"""

import json, os, sys, time, argparse
from urllib.request import urlopen, Request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

WORKER_BASE = os.environ.get("ENRICH_WORKER_URL", "https://equity-equine-worker.tachibanananana.workers.dev")
WORKER_ENRICH_HORSE = WORKER_BASE + "/enrich-horse"
WORKER_ENRICH_RACE = WORKER_BASE + "/enrich-race"


def call_worker(url: str, payload: dict, timeout: int = 30) -> dict:
    body = json.dumps(payload).encode()
    req = Request(url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": UA,
    }, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main():
    parser = argparse.ArgumentParser(description="Worker経由で馬データを取得 (2-pass optimized)")
    parser.add_argument("--input", required=True, help="scraped_data.json のパス")
    parser.add_argument("--delay", type=float, default=1.0, help="馬ごとの待機時間(秒) [default: 1.0]")
    parser.add_argument("--past-limit", type=int, default=5, help="過去走の最大取得数 [default: 5]")
    parser.add_argument("--race-ids", default=None, help="カンマ区切りのレースID (指定時はそのレースの馬のみ)")
    parser.add_argument("--dry-run", action="store_true", help="Worker を呼ばず馬ID一覧のみ表示")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        races = json.load(f)

    if not races:
        print("[INFO] No races in input")
        return

    # race-ids フィルタ
    race_ids_filter = set(args.race_ids.split(",")) if args.race_ids else None
    if race_ids_filter:
        races = [r for r in races if r.get("race_id") in race_ids_filter]
        print(f"[INFO] Filtered to {len(races)} races")

    # 全馬 ID を収集
    all_horse_ids = set()
    for race in races:
        for h in race.get("horses", []):
            hid = h.get("horse_id")
            if hid:
                all_horse_ids.add(hid)
                if "sire" not in h:
                    h["sire"] = ""
                if "damsire" not in h:
                    h["damsire"] = ""

    total_horses = len(all_horse_ids)
    print(f"[INFO] {total_horses} unique horses to enrich")

    if args.dry_run:
        for hid in sorted(all_horse_ids):
            print(f"  [DRY-RUN] {hid}")
        return

    # =====================================================================
    # Pass 1: pedigree_only → 血統 + 過去レースID一覧を取得
    # =====================================================================
    print("\n=== Pass 1: Pedigree + Past Race IDs ===")
    all_past_race_ids: set[str] = set()
    horse_to_races: dict[str, list[str]] = {}
    sire_count = 0
    damsire_count = 0

    for i, hid in enumerate(sorted(all_horse_ids)):
        if i > 0:
            time.sleep(args.delay)

        print(f"[{i+1}/{total_horses}] {hid} ...", end=" ", flush=True)
        try:
            result = call_worker(WORKER_ENRICH_HORSE, {
                "horse_id": hid,
                "past_limit": args.past_limit,
                "pedigree_only": True,
            })
            sire = result.get("sire", "")
            damsire = result.get("damsire", "")
            past_ids = result.get("past_race_ids", [])[:args.past_limit]

            if sire:
                sire_count += 1
            if damsire:
                damsire_count += 1

            horse_to_races[hid] = past_ids
            for prid in past_ids:
                all_past_race_ids.add(prid)

            # JSON に血統情報を反映
            for race in races:
                for h in race.get("horses", []):
                    if h.get("horse_id") == hid:
                        h["sire"] = sire
                        h["damsire"] = damsire
                        break

            print(f"✓ sire={sire[:12] if sire else '-'} "
                  f"damsire={damsire[:12] if damsire else '-'} "
                  f"pastIDs={len(past_ids)}")

        except Exception as e:
            print(f"✗ {e}")

    print(f"\n  Pedigree: {sire_count}/{total_horses} sire, {damsire_count}/{total_horses} damsire")
    print(f"  Unique past race IDs: {len(all_past_race_ids)} (vs {sum(len(v) for v in horse_to_races.values())} raw)")

    # =====================================================================
    # Pass 2: 重複除去した過去レースを一括取得
    # =====================================================================
    if all_past_race_ids:
        print(f"\n=== Pass 2: Past Race Details ({len(all_past_race_ids)} unique races) ===")
        total_saved = 0
        for i, prid in enumerate(sorted(all_past_race_ids)):
            if i > 0:
                time.sleep(0.5)  # レース間は短め

            # この過去レースに出ていた対象馬のIDを収集
            target_horses = [
                hid for hid, pr_list in horse_to_races.items() if prid in pr_list
            ]

            print(f"[{i+1}/{len(all_past_race_ids)}] {prid} ({len(target_horses)} horses) ...",
                  end=" ", flush=True)
            try:
                result = call_worker(WORKER_ENRICH_RACE, {
                    "race_id": prid,
                    "target_horse_ids": target_horses,
                })
                saved = result.get("horses_in_race", 0)
                total_saved += saved
                print(f"✓ {saved} saved")
            except Exception as e:
                print(f"✗ {e}")

        print(f"\n  Total past_results saved: {total_saved}")

    # JSON を上書き保存 (血統情報反映済み)
    with open(args.input, "w", encoding="utf-8") as f:
        json.dump(races, f, ensure_ascii=False, indent=2)

    print(f"\n=== Summary ===")
    print(f"  Horses: {total_horses}")
    print(f"  With sire: {sire_count}/{total_horses} ({100*sire_count//total_horses if total_horses else 0}%)")
    print(f"  With damsire: {damsire_count}/{total_horses}")
    print(f"  Updated: {args.input}")


if __name__ == "__main__":
    main()
