"""
netkeiba から出走表と各馬の過去5走データを取得するスクレイピングスクリプト (SP版専用)。

パイプライン:
  1. race.netkeiba.com → 日付タブと group 値を取得
  2. race.netkeiba.com → レース一覧 (race_id, venue, race_no 等)
  3. race.netkeiba.com → 出走表 (shutuba.html: 馬名/枠番/騎手/斤量/馬体重)
  3b. race.netkeiba.com → 確定オッズ (result.html: 過去レースのみ)
  4. db.sp.netkeiba.com → 馬過去レースID一覧 + 血統 (父/母父)
  5. db.sp.netkeiba.com → 各過去レース詳細 (タイム/通過/上り/厩舎コメント)

出力: レースIDごとの出走馬リストと過去走データのJSON
"""

import os
import re
import json
import time
import sys
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

NETKEIBA_RACE = "https://race.netkeiba.com"
NETKEIBA_SP   = "https://db.sp.netkeiba.com"

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
            if resp.status_code == 429:
                raise Exception("Rate limited (429)")
            if resp.status_code == 400:
                raise Exception(f"Blocked (400) from {url}")
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or encoding
            return resp.text
        except requests.HTTPError as e:
            if attempt < retries - 1 and resp.status_code in (403, 429, 502, 503, 504):
                wait = backoff * (2 ** attempt)
                print(f"  [RETRY] {resp.status_code} from {url[:60]}, waiting {wait:.0f}s (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            if attempt < retries - 1 and ("429" in str(e) or "400" in str(e) or "Blocked" in str(e) or "Rate limited" in str(e)):
                wait = backoff * (2 ** attempt)
                print(f"  [RETRY] {e}, waiting {wait:.0f}s (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            raise


def _ymd_to_yyyymmdd(date_str: str) -> str:
    if re.match(r"^\d{4}-?\d{2}-?\d{2}$", date_str):
        return date_str.replace("-", "")
    return date_str


def _jdate_to_ymd(jdate: str) -> str:
    """2026/03/28 → 2026-03-28"""
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", jdate)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return jdate


# ---------------------------------------------------------------------------
# Layer 1: 日付リスト (race.netkeiba.com)
# ---------------------------------------------------------------------------

def fetch_date_list(date_ymd: str) -> list[dict]:
    d = _ymd_to_yyyymmdd(date_ymd)
    url = f"{NETKEIBA_RACE}/top/race_list_get_date_list.html"
    html = fetch_html(url, {"kaisai_date": d, "encoding": "UTF-8"}, encoding="UTF-8")
    soup = BeautifulSoup(html, "html.parser")

    entries: list[dict] = []
    for li in soup.select("li[date]"):
        li_date = li.get("date", "")
        li_group = li.get("group", "")
        if li_date and li_group:
            entries.append({"date": li_date, "group": li_group})
    return entries


# ---------------------------------------------------------------------------
# Layer 2: レース一覧 (race.netkeiba.com)
# ---------------------------------------------------------------------------

def fetch_race_list_sub(date_ymd: str, group: str) -> list[dict]:
    d = _ymd_to_yyyymmdd(date_ymd)
    url = f"{NETKEIBA_RACE}/top/race_list_sub.html"
    html = fetch_html(url, {"kaisai_date": d, "current_group": group}, encoding="UTF-8")
    soup = BeautifulSoup(html, "html.parser")

    races: list[dict] = []
    for a in soup.select("a[href*='race_id=']"):
        href = a.get("href", "")
        m = re.search(r"race_id=(\d{12})", href)
        if not m:
            continue
        race_id = m.group(1)
        race_no = race_id[10:12]
        venue_code = race_id[4:6]
        venue = VENUE_MAP.get(venue_code, venue_code)

        parent = a.find_parent("div", class_=re.compile("RaceList", re.I)) or a.find_parent("li")
        name_el = parent.select_one(".RaceList_ItemTitle, .RaceName, .ItemTitle") if parent else None
        race_name = name_el.get_text(strip=True) if name_el else "?"

        info_el = parent.select_one(".RaceList_ItemInfo, .RaceData") if parent else None
        info_text = info_el.get_text(" ", strip=True) if info_el else ""

        races.append({
            "race_id": race_id,
            "race_no": race_no,
            "venue": venue,
            "race_name": race_name,
            "info": info_text,
        })
    return races


# ---------------------------------------------------------------------------
# Layer 3: 出走表 (race.netkeiba.com)
# ---------------------------------------------------------------------------

def parse_shutuba(race_id: str) -> dict:
    url = f"{NETKEIBA_RACE}/race/shutuba.html"
    html = fetch_html(url, {"race_id": race_id})
    soup = BeautifulSoup(html, "html.parser")

    race_data_el = soup.select_one(".RaceData01, .RaceData")
    distance = 0
    track_condition = "良"
    race_name = "?"
    if race_data_el:
        text = race_data_el.get_text(" ", strip=True)
        m_dist = re.search(r"(\d+)m", text)
        if m_dist:
            distance = int(m_dist.group(1))
        if "稍重" in text:
            track_condition = "稍重"
        elif "良" in text and "不良" not in text and "稍重" not in text:
            track_condition = "良"
        elif "重" in text and "稍重" not in text:
            track_condition = "重"
        elif "不良" in text:
            track_condition = "不良"

    title_el = soup.select_one(".RaceName, .RaceList_ItemTitle, h1")
    if title_el:
        race_name = title_el.get_text(strip=True)

    date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", soup.get_text())
    race_date = ""
    if date_match:
        race_date = f"{date_match.group(1)}-{date_match.group(2).zfill(2)}-{date_match.group(3).zfill(2)}"

    horses = []
    for row in soup.select("tr.HorseList"):
        name_el = row.select_one(".HorseInfo .HorseName a, .HorseInfo a")
        if not name_el:
            continue
        horse_name = name_el.get_text(strip=True)
        horse_href = name_el.get("href", "")
        m_horse_id = re.search(r"horse/(\d+)", horse_href)
        horse_id = m_horse_id.group(1) if m_horse_id else horse_name

        odds_val = 0.0
        for sel in ["td.Txt_R.Popular", ".Odds"]:
            odds_el = row.select_one(sel)
            if odds_el:
                odds_text = odds_el.get_text(strip=True)
                try:
                    v = float(re.sub(r"[^\d.]", "", odds_text))
                    if v > 0:
                        odds_val = v
                        break
                except ValueError:
                    continue

        gate_el = row.select_one(".Waku span, .Waku, td:first-child")
        gate = gate_el.get_text(strip=True) if gate_el else ""
        try: gate = int(re.sub(r"[^\d]", "", str(gate)))
        except: gate = 0

        jockey_el = row.select_one(".Jockey a, .Jockey")
        jockey = jockey_el.get_text(strip=True) if jockey_el else ""

        handicap_el = row.select_one(".Handicap, .Weight:not(:last-child)")
        handicap = 0.0
        if handicap_el:
            ht = handicap_el.get_text(strip=True)
            try: handicap = float(re.sub(r"[^\d.]", "", ht))
            except: pass

        weight_el = row.select_one("td.Weight:last-child, td:last-child")
        horse_weight = weight_el.get_text(strip=True) if weight_el else ""
        if horse_weight and not re.match(r"^[\d()+\-]+$", horse_weight):
            horse_weight = ""

        horses.append({
            "horse_id": horse_id,
            "horse_name": horse_name,
            "odds": odds_val,
            "gate": gate,
            "jockey": jockey,
            "handicap": handicap,
            "horse_weight": horse_weight,
        })

    horses = horses[:18]

    return {
        "race_id": race_id,
        "date": race_date,
        "venue": VENUE_MAP.get(race_id[4:6], race_id[4:6]),
        "distance": distance,
        "track_condition": track_condition,
        "race_name": race_name,
        "horses": horses,
    }


# ---------------------------------------------------------------------------
# Layer 3b: 確定オッズ (race.netkeiba.com result.html)
# ---------------------------------------------------------------------------

def parse_result_odds(race_id: str) -> dict[str, float]:
    url = f"{NETKEIBA_RACE}/race/result.html"
    html = fetch_html(url, {"race_id": race_id})
    soup = BeautifulSoup(html, "html.parser")

    odds_map: dict[str, float] = {}
    for row in soup.select("tr.HorseList"):
        horse_link = row.select_one("a[href*='horse/']")
        if not horse_link:
            continue
        m = re.search(r"horse/(\d+)", horse_link.get("href", ""))
        if not m:
            continue
        horse_id = m.group(1)
        odds_tds = row.select("td.Odds")
        if len(odds_tds) >= 2:
            span_el = odds_tds[1].select_one("span")
            if span_el:
                try:
                    odds_map[horse_id] = float(span_el.get_text(strip=True))
                except ValueError:
                    pass
    return odds_map


# ---------------------------------------------------------------------------
# Layer 4a: SP 馬過去レースID一覧 (db.sp.netkeiba.com)
# ---------------------------------------------------------------------------

def fetch_sp_horse_past_race_ids(horse_id: str, limit: int = 5) -> list[dict]:
    """
    SP馬結果ページから直近 past race IDs と基本情報を取得。
    Returns: [{race_date, race_id, finish_order}, ...]
    """
    results: list[dict] = []
    try:
        url = f"{NETKEIBA_SP}/horse/result/{horse_id}/"
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")

        for li in soup.select("#ResultsList ul.List_01 li"):
            link_a = li.select_one("a.LinkBox_Item02")
            if not link_a:
                continue
            href = link_a.get("href", "")
            m_race_id = re.search(r"/race/(\d{12})/", href)
            if not m_race_id:
                continue
            race_id = m_race_id.group(1)

            # 日付
            text_p = link_a.select_one(".List_TextBox p")
            date_text = text_p.get_text(strip=True) if text_p else ""
            m_date = re.match(r"(\d{4}/\d{2}/\d{2})", date_text)
            race_date = _jdate_to_ymd(m_date.group(1)) if m_date else ""

            # 着順
            rank_span = link_a.select_one("span[class*='ResultRank']")
            finish_order = 0
            if rank_span:
                rank_class = rank_span.get("class", [])
                for c in rank_class:
                    if c.startswith("ResultRank") and "0" in c:
                        try:
                            finish_order = int(c.replace("ResultRank", ""))
                        except ValueError:
                            pass
                        break
                if not finish_order:
                    try:
                        finish_order = int(re.sub(r"[^\d]", "", rank_span.get_text(strip=True)))
                    except ValueError:
                        pass

            results.append({
                "race_date": race_date,
                "race_id": race_id,
                "finish_order": finish_order,
            })

            if len(results) >= limit:
                break
    except Exception as e:
        print(f"  [WARN] fetch_sp_horse_past_race_ids({horse_id}): {e}")

    return results


# ---------------------------------------------------------------------------
# Layer 4b: SP レース詳細 (db.sp.netkeiba.com) — 全馬データ抽出
# ---------------------------------------------------------------------------

def parse_sp_race_detail(race_id: str) -> dict[str, dict]:
    """
    SPレース詳細ページから全出走馬の結果データを抽出。
    Returns: {horse_id: {finish_time, passage_rank, last_3f, comment, odds...}}
    """
    horse_data: dict[str, dict] = {}
    try:
        url = f"{NETKEIBA_SP}/race/{race_id}/"
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")

        table = soup.select_one("table.ResultsByRaceDetail")
        if not table:
            return horse_data

        # ヘッダー列名 → インデックス マッピング
        ths = table.select("thead th")
        col_map: dict[str, int] = {}
        for i, th in enumerate(ths):
            text = th.get_text(strip=True)
            col_map[text] = i

        for tr in table.select("tbody tr"):
            tds = tr.select("td")
            if len(tds) < 10:
                continue

            # 馬ID
            horse_link = tr.select_one("a[href*='/horse/']")
            if not horse_link:
                continue
            m = re.search(r"/horse/(\d+)/", horse_link.get("href", ""))
            if not m:
                continue
            horse_id = m.group(1)

            data: dict = {}

            # 各列をヘッダー名で取得
            for key, idx in col_map.items():
                if idx >= len(tds):
                    continue
                val = tds[idx].get_text(strip=True)
                if not val:
                    continue

                if key == "タイム":
                    data["finish_time"] = val
                elif key == "通過":
                    data["passage_rank"] = val
                elif key == "上り":
                    data["last_3f"] = val
                elif key == "単勝":
                    try: data["odds"] = float(val)
                    except ValueError: pass
                elif key == "馬体重":
                    data["horse_weight"] = val
                elif key == "着順":
                    try: data["finish_order"] = int(val)
                    except ValueError: pass
                elif key == "人気":
                    try: data["ninki"] = int(val)
                    except ValueError: pass
                elif key == "斤量":
                    try: data["handicap"] = float(val)
                    except ValueError: pass
                elif key == "備考":
                    data["notes"] = val

            # 厩舎コメントはリンク先の場合もある → テキストがあれば取得
            if "厩舎コメント" in col_map:
                c_idx = col_map["厩舎コメント"]
                if c_idx < len(tds):
                    comment_td = tds[c_idx]
                    comment_text = comment_td.get_text(strip=True)
                    if comment_text  and len(comment_text) > 3:
                        data["race_comment"] = comment_text

            if "finish_time" in data or "passage_rank" in data or "last_3f" in data:
                horse_data[horse_id] = data

    except Exception as e:
        print(f"  [WARN] parse_sp_race_detail({race_id}): {e}")

    return horse_data


# ---------------------------------------------------------------------------
# Layer 5: SP 血統 (db.sp.netkeiba.com)
# ---------------------------------------------------------------------------

def parse_sp_pedigree(horse_id: str) -> tuple[str, str]:
    sire = ""
    damsire = ""
    try:
        url = f"{NETKEIBA_SP}/horse/ped/{horse_id}/"
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")

        blood = soup.select_one("section.Blood")
        if not blood:
            return sire, damsire

        trs = blood.select("table tbody tr")
        if not trs:
            return sire, damsire

        # 父: 1行目の td.Male 内の最初の horse リンク
        male_tds = trs[0].select("td.Male")
        for td in male_tds:
            link = td.select_one("a[href*='/horse/']")
            if link:
                name = link.get_text(strip=True)
                if name and sire:
                    # 2つ目の Male が父
                    # 最初の td.Male は当該馬自身の場合もある
                    pass
                if not sire:
                    sire = name
                    continue
                else:
                    # 2つ目以降は父方の祖先 → 最初が当該馬、2番目が父
                    sire = name
                    break

        # 最初の td.Male が当該馬の場合、2番目の td.Male が父
        if len(male_tds) >= 2:
            first_link = male_tds[0].select_one("a[href*='/horse/']")
            second_link = male_tds[1].select_one("a[href*='/horse/']")
            if first_link and second_link:
                sire = second_link.get_text(strip=True)

        # 母父: 最初の行で td.Female (rowspan=8) の次に来る td.Male 内の horse リンク
        for i, td in enumerate(trs[0].select("td")):
            rowspan = int(td.get("rowspan", 0))
            if "Female" in td.get("class", []) and rowspan == 8:
                # 次の td 以降で Male を探す
                siblings = trs[0].select(f"td:nth-child({i+2}), td:nth-child({i+3}), td:nth-child({i+4})")
                for sib in siblings:
                    if "Male" in sib.get("class", []):
                        dlink = sib.select_one("a[href*='/horse/']")
                        if dlink:
                            damsire = dlink.get_text(strip=True)
                            break
                break

        # フォールバック: 全リンクを抽出
        if not damsire:
            all_links = blood.select("a[href*='/horse/']")
            # ダムサイアーは母方の最初の Male 祖先 = 大体3-6番目
            # 簡易: 最初が当該馬, 2番目が父とすると、母父はそれより後のユニークな名前
            names = []
            for link in all_links:
                name = link.get_text(strip=True)
                if name and name not in names:
                    names.append(name)
            if len(names) >= 3:
                # names[0]=当該馬, names[1]=父, names[2]=母父(簡易)
                damsire = names[2] if not damsire else damsire

    except Exception as e:
        print(f"  [WARN] parse_sp_pedigree({horse_id}): {e}")

    return sire, damsire


# ---------------------------------------------------------------------------
# Collect past results for all horses (batched + deduped)
# ---------------------------------------------------------------------------

def collect_past_results(horse_ids: set[str],
                         horse_past_ids: dict[str, list[dict]],
                         exclude_race_ids: set[str],
                         max_workers: int = 8) -> dict[str, list[dict]]:
    """
    全馬の過去走データを一括収集。
    - 過去レースIDを重複除去
    - ThreadPoolExecutor で並行取得
    Returns: {horse_id: [past_result, ...]}
    """
    # 全馬の過去レースIDを収集・重複除去
    all_past_ids: set[str] = set()
    for hid in horse_ids:
        for entry in horse_past_ids.get(hid, []):
            rid = entry["race_id"]
            if rid and rid not in exclude_race_ids:
                all_past_ids.add(rid)

    print(f"[INFO] Collecting details for {len(all_past_ids)} unique past races (horses={len(horse_ids)})...")

    # 並行取得
    race_details: dict[str, dict] = {}
    if all_past_ids:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(parse_sp_race_detail, rid): rid for rid in all_past_ids}
            for future in as_completed(futures):
                rid = futures[future]
                try:
                    race_details[rid] = future.result()
                except Exception as e:
                    print(f"  [WARN] Failed to fetch race detail {rid}: {e}")
                time.sleep(0.1)  # small delay to avoid burst

    # 各馬の過去走を構築
    horse_past: dict[str, list[dict]] = {hid: [] for hid in horse_ids}

    for hid in horse_ids:
        results: list[dict] = []
        for entry in horse_past_ids.get(hid, []):
            rid = entry["race_id"]
            if not rid or rid in exclude_race_ids:
                continue
            detail = race_details.get(rid, {})
            horse_detail = detail.get(hid, {})
            result = {
                "race_date": _jdate_to_ymd(entry.get("race_date", rid[:8])),
                "finish_time": horse_detail.get("finish_time", ""),
                "passage_rank": horse_detail.get("passage_rank", ""),
                "last_3furlong": float(horse_detail["last_3f"]) if horse_detail.get("last_3f") else None,
                "race_comment": horse_detail.get("race_comment", ""),
            }
            results.append(result)
        horse_past[hid] = results[:5]  # 最大5走

    return horse_past


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="netkeiba 出走表スクレイピング (SP版専用)")
    parser.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
    parser.add_argument("--output", default="scraped_data.json", help="出力JSONパス")
    parser.add_argument("--workers", type=int, default=8, help="並行fetch数 (default: 8)")
    parser.add_argument("--past-limit", type=int, default=5, help="過去走数上限 (default: 5)")
    args = parser.parse_args()

    d = _ymd_to_yyyymmdd(args.date)
    JST = timezone(timedelta(hours=9))
    today = datetime.now(JST).date()

    # Phase 1: 日付リスト
    date_entries = fetch_date_list(d)
    if not date_entries:
        print(f"[INFO] No race dates found for {args.date}")
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump([], f)
        sys.exit(0)
    print(f"[INFO] Found {len(date_entries)} date entries")

    # Phase 2: レース一覧
    target_ymd = _ymd_to_yyyymmdd(args.date)
    matching = [e for e in date_entries if e["date"] == target_ymd]
    if not matching:
        matching = [date_entries[-1]] if date_entries else []
        print(f"[INFO] No exact match for {args.date}, using last entry")

    all_races: list[dict] = []
    for entry in matching:
        group = entry["group"]
        entry_date = entry["date"]
        print(f"[INFO] Fetching race list for date={entry_date} group={group}")
        time.sleep(1.0)
        race_cards = fetch_race_list_sub(entry_date, group)
        print(f"  -> {len(race_cards)} races")
        all_races.extend(race_cards)

    seen_ids: set[str] = set()
    unique_races = [rc for rc in all_races if rc["race_id"] not in seen_ids and not seen_ids.add(rc["race_id"])]
    all_races = unique_races
    print(f"[INFO] Total target races: {len(all_races)}")

    if not all_races:
        print(f"[INFO] No races found for {args.date}")
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump([], f)
        sys.exit(0)

    # Phase 3: 出走表 (shutuba) + オッズ補完 (過去レース)
    output_data: list[dict] = []
    target_race_ids: set[str] = set()
    all_horse_ids: set[str] = set()

    for i, rc in enumerate(all_races):
        print(f"[INFO] ({i+1}/{len(all_races)}) Shutuba {rc['race_id']} {rc['venue']}{rc['race_no']}R {rc['race_name']}")
        try:
            race_data = parse_shutuba(rc["race_id"])
            race_data["race_name"] = rc.get("race_name", race_data.get("race_name", "?"))
        except Exception as e:
            print(f"  [WARN] Shutuba failed: {e}")
            continue

        # 過去レースの場合、result.html から確定オッズを補完
        race_date_str = race_data.get("date", "")
        if race_date_str:
            try:
                race_dt = datetime.strptime(race_date_str, "%Y-%m-%d").date()
                if race_dt < today:
                    try:
                        odds_map = parse_result_odds(rc["race_id"])
                        updated = 0
                        for h in race_data["horses"]:
                            if odds_map.get(h["horse_id"]):
                                h["odds"] = odds_map[h["horse_id"]]
                                updated += 1
                        if updated:
                            print(f"  [OK] Updated odds for {updated}/{len(race_data['horses'])} from result page")
                    except Exception as e:
                        print(f"  [WARN] Result odds: {e}")
            except ValueError:
                pass

        target_race_ids.add(rc["race_id"])
        for h in race_data["horses"]:
            all_horse_ids.add(h["horse_id"])

        output_data.append(race_data)
        time.sleep(1.0)

    if not all_horse_ids:
        print("[INFO] No horses found")
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        sys.exit(0)

    print(f"[INFO] {len(all_horse_ids)} unique horses across {len(output_data)} races")

    # Phase 4: 各馬の過去レースID一覧 + 血統 (SP 並行取得)
    print(f"[INFO] Fetching past race IDs + pedigree for {len(all_horse_ids)} horses (workers={args.workers})...")
    horse_past_ids: dict[str, list[dict]] = {}
    horse_pedigree: dict[str, tuple[str, str]] = {}

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures_past = {}
        futures_ped = {}
        for hid in all_horse_ids:
            futures_past[hid] = executor.submit(fetch_sp_horse_past_race_ids, hid, args.past_limit)
            futures_ped[hid] = executor.submit(parse_sp_pedigree, hid)

        # 進捗表示
        total = len(all_horse_ids)
        done_count = 0
        for future in as_completed(list(futures_past.values()) + list(futures_ped.values())):
            done_count += 1
            if done_count % 50 == 0 or done_count == 2 * total:
                print(f"  [PROGRESS] horse data {done_count}/{2*total}")

    # 結果回収
    for hid in all_horse_ids:
        try:
            horse_past_ids[hid] = futures_past[hid].result()
        except Exception as e:
            print(f"  [WARN] past_ids({hid}): {e}")
            horse_past_ids[hid] = []
        try:
            horse_pedigree[hid] = futures_ped[hid].result()
        except Exception as e:
            print(f"  [WARN] pedigree({hid}): {e}")
            horse_pedigree[hid] = ("", "")

    # Phase 5: 過去レース詳細を一括収集
    horse_past_results = collect_past_results(
        horse_ids=all_horse_ids,
        horse_past_ids=horse_past_ids,
        exclude_race_ids=target_race_ids,
        max_workers=args.workers,
    )

    # Phase 6: データを埋め込む
    for race_data in output_data:
        for h in race_data["horses"]:
            hid = h["horse_id"]
            h["past_results"] = horse_past_results.get(hid, [])
            sire, damsire = horse_pedigree.get(hid, ("", ""))
            h["sire"] = sire
            h["damsire"] = damsire

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    total_horses = sum(len(r["horses"]) for r in output_data)
    total_past = sum(len(h["past_results"]) for r in output_data for h in r["horses"])
    print(f"[DONE] {len(output_data)} races, {total_horses} horses, {total_past} past results → {args.output}")


if __name__ == "__main__":
    main()
