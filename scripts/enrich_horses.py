"""
enrich_horses.py — Worker /enrich-horse を使って全馬の過去走+血統を取得する

scraped_data.json から馬ID一覧を抽出し、Worker に 1 頭ずつ送信。
Worker は SP(db.sp.netkeiba.com) からデータを取得し Turso DB に直接保存する。

レート制限:
  - Worker 呼出し間隔: 1.5 秒 (--delay で変更可)
  - Worker 内部でも SP への fetch は逐次実行

使い方:
  python enrich_horses.py --input scraped_data.json [--delay 2.0]
"""

import json, os, sys, time, argparse
from urllib.request import urlopen, Request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

WORKER_ENRICH = os.environ.get("ENRICH_WORKER_URL", "https://equity-equine-worker.tachibanananana.workers.dev") + "/enrich-horse"


def enrich_horse(horse_id: str, past_limit: int = 5) -> dict:
    body = json.dumps({"horse_id": horse_id, "past_limit": past_limit}).encode()
    req = Request(WORKER_ENRICH, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": UA,
    }, method="POST")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    parser = argparse.ArgumentParser(description="Worker経由で馬データを取得")
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
                # Worker の結果を JSON にも反映させるための準備
                if "sire" not in h:
                    h["sire"] = ""
                if "damsire" not in h:
                    h["damsire"] = ""

    total = len(all_horse_ids)
    print(f"[INFO] {total} unique horses to enrich")

    if args.dry_run:
        for hid in sorted(all_horse_ids):
            print(f"  [DRY-RUN] {hid}")
        return

    # 馬ごとに Worker を呼出し
    sire_count = 0
    damsire_count = 0
    past_total = 0

    for i, hid in enumerate(sorted(all_horse_ids)):
        if i > 0:
            time.sleep(args.delay)

        print(f"[{i+1}/{total}] {hid} ...", end=" ", flush=True)
        try:
            result = enrich_horse(hid, past_limit=args.past_limit)
            sire = result.get("sire", "")
            damsire = result.get("damsire", "")
            saved = result.get("saved_past_results", 0)
            past_total += saved

            if sire:
                sire_count += 1
            if damsire:
                damsire_count += 1

            # JSON 内の該当馬に血統情報を反映
            for race in races:
                for h in race.get("horses", []):
                    if h.get("horse_id") == hid:
                        h["sire"] = sire
                        h["damsire"] = damsire
                        break

            print(f"✓ sire={sire[:12] if sire else '-'} "
                  f"damsire={damsire[:12] if damsire else '-'} "
                  f"past={saved}saved")

        except Exception as e:
            print(f"✗ {e}")

    # JSON を上書き保存 (血統情報反映済み)
    with open(args.input, "w", encoding="utf-8") as f:
        json.dump(races, f, ensure_ascii=False, indent=2)

    print(f"\n=== Summary ===")
    print(f"  Horses: {total}")
    print(f"  With sire: {sire_count}/{total} ({100*sire_count//total if total else 0}%)")
    print(f"  With damsire: {damsire_count}/{total}")
    print(f"  Past results saved: {past_total}")
    print(f"  Updated: {args.input}")


if __name__ == "__main__":
    main()
