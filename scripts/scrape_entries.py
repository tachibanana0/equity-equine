"""
scrape_entries.py — netkeiba 出走表だけ取得する軽量スクレイパー

過去走・血統は取得しない。それらは Worker /enrich-horse 側で SP(db.sp.netkeiba.com) から取得する。
このスクリプトは race.netkeiba.com のみを使うため GH Actions から高速に動作する。

パイプライン:
  1. 日付リスト (race.netkeiba.com)
  2. レース一覧 (race.netkeiba.com)
  3. 出走表 (shutuba.html: 馬名/枠番/騎手/斤量/馬体重/出走前オッズ)
  4. 過去レース: 確定オッズ (result.html)

出力: scraped_data.json (predict_v4flash.py / save_and_predict.py 互換形式)
"""

import os, re, json, time, sys, argparse
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

NETKEIBA_RACE = "https://race.netkeiba.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9",
}

VENUE_MAP = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟",
    "05": "東京", "06": "中山", "07": "中京", "08": "京都",
    "09": "阪神", "10": "小倉",
}

def fetch_html(url: str, params: Optional[dict] = None, retries: int = 3, encoding: str = "EUC-JP") -> str:
    backoff = 5.0
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if resp.status_code in (400, 429):
                raise Exception(f"Blocked ({resp.status_code}) from {url[:80]}")
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or encoding
            return resp.text
        except (requests.HTTPError, Exception) as e:
            if attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                print(f"  [RETRY] {e}, waiting {wait:.0f}s (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            raise

def fetch_date_list(date_ymd: str) -> list[dict]:
    d = date_ymd.replace("-", "")
    url = f"{NETKEIBA_RACE}/top/race_list_get_date_list.html"
    html = fetch_html(url, {"kaisai_date": d, "encoding": "UTF-8"}, encoding="UTF-8")
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for li in soup.select("li[date]"):
        li_date = li.get("date", "")
        li_group = li.get("group", "")
        if li_date and li_group:
            entries.append({"date": li_date, "group": li_group})
    return entries

def fetch_race_list_sub(date_entry: dict) -> list[dict]:
    url = f"{NETKEIBA_RACE}/top/race_list_sub.html"
    html = fetch_html(url, {"kaisai_date": date_entry["date"], "current_group": date_entry["group"]}, encoding="UTF-8")
    soup = BeautifulSoup(html, "html.parser")
    races = []
    for a in soup.select("a[href*='race/result.html']"):
        href = a.get("href", "")
        rid_match = re.search(r"race_id=(\d{12})", href)
        if not rid_match:
            continue
        race_id = rid_match.group(1)
        row = a.find_parent("tr") or a.find_parent("li") or a.parent
        text = a.get_text(" ", strip=True)
        venue_code = race_id[4:6]
        venue = VENUE_MAP.get(venue_code, venue_code)
        dist_match = re.search(r"(\d+)m", text)
        distance = int(dist_match.group(1)) if dist_match else 0
        condition = "良"
        if "稍" in text: condition = "稍重"
        elif "重" in text: condition = "重"
        elif "不" in text: condition = "不良"
        elif "ダ" in text:
            pass
        races.append({
            "race_id": race_id,
            "date": f"{race_id[0:4]}-{race_id[4:6]}-{race_id[6:8]}",
            "venue": venue,
            "distance": distance,
            "track_condition": condition,
        })
    return races

def parse_shutuba(race_id: str) -> list[dict]:
    """出走表から馬エントリを取得 (過去走・血統は含まない)"""
    url = f"{NETKEIBA_RACE}/race/shutuba.html"
    html = fetch_html(url, {"race_id": race_id})
    soup = BeautifulSoup(html, "html.parser")

    horses = []
    for tr in soup.select("tr.HorseList"):
        cells = tr.select("td")
        if len(cells) < 8:
            continue

        name_link = tr.select_one(".HorseInfo .HorseName a, a[href*='/horse/']")
        if not name_link:
            continue
        href = name_link.get("href", "")
        hid_match = re.search(r"(\d{10,12})", href)
        horse_id = hid_match.group(1) if hid_match else ""
        horse_name = name_link.get_text(strip=True)

        # 枠番: class="Waku1" 等
        gate_cell = tr.select_one("td[class*='Waku']")
        gate = int(gate_cell.get_text(strip=True)) if gate_cell else 0

        # 騎手
        jockey_link = tr.select_one("td.Jockey a, a[href*='/jockey/']")
        jockey = jockey_link.get_text(strip=True) if jockey_link else ""

        # 斤量
        handicap = 0.0
        for td in cells:
            txt = td.get_text(strip=True)
            if re.match(r"^\d{2,3}(\.\d)?$", txt) and 40 <= float(txt) <= 65:
                handicap = float(txt)
                break

        # 馬体重: odds と同じ td にある場合もあるが、td.Weight や数値パターンで判別
        weight_str = ""
        for td in cells:
            txt = td.get_text(strip=True)
            if re.search(r"\d{3}\(\+?-\d+\)", txt):
                weight_str = txt
                break

        # オッズ: td.Txt_R 内の数値
        odds_val = 0.0
        for td in cells:
            if "Txt_R" in (td.get("class") or []):
                t = td.get_text(strip=True)
                try:
                    o = float(t)
                    if o >= 1.0:
                        odds_val = o
                        break
                except ValueError:
                    pass

        if horse_id and horse_name:
            horses.append({
                "horse_id": horse_id,
                "horse_name": horse_name,
                "odds": odds_val,
                "gate": gate,
                "jockey": jockey,
                "handicap": handicap,
                "horse_weight": weight_str,
            })

    return horses

def parse_result_odds(race_id: str) -> dict[str, float]:
    """過去レースの確定オッズを result.html から取得"""
    url = f"{NETKEIBA_RACE}/race/result.html"
    try:
        html = fetch_html(url, {"race_id": race_id})
    except Exception:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    odds_map = {}

    for tr in soup.select("tr.HorseList, tr.FirstDisplay"):
        id_match = re.search(r"horse/(\d{10,12})", str(tr))
        if not id_match:
            continue
        hid = id_match.group(1)

        odds_cells = tr.select("td.Odds")
        if len(odds_cells) < 2:
            continue

        span = odds_cells[1].select_one("span")
        if span:
            try:
                odds_map[hid] = float(span.get_text(strip=True))
            except ValueError:
                pass

    return odds_map

def main():
    parser = argparse.ArgumentParser(description="軽量出走表スクレイパー")
    parser.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
    parser.add_argument("--output", default="scraped_data.json", help="出力JSONファイル")
    parser.add_argument("--race-ids", default=None, help="カンマ区切りのレースID (指定時はそのレースのみ)")
    args = parser.parse_args()

    tz_jst = timezone(timedelta(hours=9))
    today = datetime.now(tz_jst).strftime("%Y-%m-%d")
    target_date = args.date
    is_past = target_date < today

    print(f"[INFO] Target: {target_date}{' (past → odds from result.html)' if is_past else ''}")

    # 1. 日付リスト
    date_entries = fetch_date_list(target_date)
    if not date_entries:
        print("[INFO] No races found for this date. Outputting empty file.")
        json.dump([], open(args.output, "w", encoding="utf-8"), ensure_ascii=False)
        return

    # 2. レース一覧
    all_races = []
    seen_race_ids = set()
    for de in date_entries:
        race_list = fetch_race_list_sub(de)
        for r in race_list:
            if r["race_id"] not in seen_race_ids:
                seen_race_ids.add(r["race_id"])
                all_races.append(r)

    print(f"[INFO] Found {len(all_races)} races from {len(date_entries)} date entries")

    # race-ids フィルタ
    race_ids_filter = set(args.race_ids.split(",")) if args.race_ids else None
    if race_ids_filter:
        found_race_ids = set(r["race_id"] for r in all_races)
        missing_race_ids = race_ids_filter - found_race_ids
        all_races = [r for r in all_races if r["race_id"] in race_ids_filter]
        print(f"[INFO] Filtered to {len(all_races)} races (missing: {len(missing_race_ids)})")
        # 日付リストにないレースは直接 shutuba から取得 (netkeiba group 重複の制約回避)
        for mrid in missing_race_ids:
            venue_code = mrid[4:6]
            all_races.append({
                "race_id": mrid,
                "date": f"{mrid[0:4]}-{mrid[4:6]}-{mrid[6:8]}",
                "venue": VENUE_MAP.get(venue_code, venue_code),
                "distance": 0,
                "track_condition": "良",
            })

    # 3. 各レースの出走表
    output = []
    for i, race in enumerate(all_races):
        rid = race["race_id"]
        print(f"  [{i+1}/{len(all_races)}] {rid} {race['venue']}{race['distance']}m ...", end=" ", flush=True)

        horses = parse_shutuba(rid)
        race["horses"] = horses

        # 過去レースの場合は確定オッズを補完
        if is_past:
            odds_map = parse_result_odds(rid)
            if odds_map:
                updated = 0
                for h in horses:
                    if odds_map.get(h["horse_id"]):
                        h["odds"] = odds_map[h["horse_id"]]
                        updated += 1
                print(f"{len(horses)} horses, {updated} odds from result")
            else:
                print(f"{len(horses)} horses")
        else:
            print(f"{len(horses)} horses")

        output.append(race)
        time.sleep(1.5)  # レース間の間隔

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_horses = sum(len(r.get("horses", [])) for r in output)
    print(f"[DONE] {len(output)} races, {total_horses} horses → {args.output}")

if __name__ == "__main__":
    main()
