"""
netkeiba から出走表と各馬の過去5走データを取得するスクレイピングスクリプト。

パイプライン:
  1. race_list_get_date_list.html → 日付タブと group 値を取得
  2. race_list_sub.html → 各 group のレース一覧 (race_id, venue, race_no 等)
  3. race/shutuba.html → 出走馬一覧 (horse_name, horse_id, 枠番, 騎手, 斤量, 馬体重)
  3b. race/result.html → 過去レースの確定単勝オッズ (shutuba はマスクされるため)
  4. db.netkeiba.com/horse/{id}/ → 過去5走 + 血統 (父, 母父)

出力: レースIDごとの出走馬リストと過去走データのJSON
"""

import os
import re
import json
import time
import sys
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

NETKEIBA_RACE = "https://race.netkeiba.com"
NETKEIBA_DB   = "https://db.netkeiba.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9",
}

# 場コード → 場名
VENUE_MAP = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟",
    "05": "東京", "06": "中山", "07": "中京", "08": "京都",
    "09": "阪神", "10": "小倉",
}


def fetch_html(url: str, params: Optional[dict] = None) -> str:
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    resp.encoding = resp.apparent_encoding or "EUC-JP"
    resp.raise_for_status()
    return resp.text


def _ymd_to_yyyymmdd(date_str: str) -> str:
    """YYYY-MM-DD → YYYYMMDD"""
    if re.match(r"^\d{4}-?\d{2}-?\d{2}$", date_str):
        return date_str.replace("-", "")
    return date_str


# ---------------------------------------------------------------------------
# Layer 1: 日付リスト（date_list）
# ---------------------------------------------------------------------------

def fetch_date_list(date_ymd: str) -> list[dict]:
    """
    race_list_get_date_list.html から開催日一覧を取得。

    Returns:
        [{date: "20241222", group: "202412220506"}, ...]
    """
    d = _ymd_to_yyyymmdd(date_ymd)
    url = f"{NETKEIBA_RACE}/top/race_list_get_date_list.html"
    html = fetch_html(url, {"kaisai_date": d, "encoding": "UTF-8"})
    soup = BeautifulSoup(html, "html.parser")

    entries: list[dict] = []
    for li in soup.select("li[date]"):
        li_date = li.get("date", "")
        li_group = li.get("group", "")
        if li_date and li_group:
            entries.append({"date": li_date, "group": li_group})

    return entries


# ---------------------------------------------------------------------------
# Layer 2: レース一覧（race_list_sub）
# ---------------------------------------------------------------------------

def fetch_race_list_sub(date_ymd: str, group: str) -> list[dict]:
    """
    race_list_sub.html からレース一覧を取得。

    Returns:
        [{race_id: "20241222050611", race_no: "11", venue: "中山", race_name: "有馬記念"}, ...]
    """
    d = _ymd_to_yyyymmdd(date_ymd)
    url = f"{NETKEIBA_RACE}/top/race_list_sub.html"
    html = fetch_html(url, {"kaisai_date": d, "current_group": group})
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

        # レース名は同じ親ブロック内のタイトル要素を探す
        parent = a.find_parent("div", class_=re.compile("RaceList", re.I)) or a.find_parent("li")
        name_el = None
        if parent:
            name_el = parent.select_one(".RaceList_ItemTitle, .RaceName, .ItemTitle")
        race_name = name_el.get_text(strip=True) if name_el else "?"

        # 距離・条件情報
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
# Layer 3: 出走表（shutuba.html）
# ---------------------------------------------------------------------------

def parse_shutuba(race_id: str) -> dict:
    """出走表ページからレース情報 + 出走馬情報を取得"""
    url = f"{NETKEIBA_RACE}/race/shutuba.html"
    params = {"race_id": race_id}
    html = fetch_html(url, params)
    soup = BeautifulSoup(html, "html.parser")

    # --- レース基本情報 ---
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

    # レース名
    title_el = soup.select_one(".RaceName, .RaceList_ItemTitle, h1")
    if title_el:
        race_name = title_el.get_text(strip=True)

    # 日付
    date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", soup.get_text())
    race_date = ""
    if date_match:
        race_date = f"{date_match.group(1)}-{date_match.group(2).zfill(2)}-{date_match.group(3).zfill(2)}"

    # --- 出走馬 (最大18頭、補欠馬は除外) ---
    horses = []
    for row in soup.select("tr.HorseList"):
        name_el = row.select_one(".HorseInfo .HorseName a, .HorseInfo a")
        if not name_el:
            continue
        horse_name = name_el.get_text(strip=True)
        horse_href = name_el.get("href", "")
        m_horse_id = re.search(r"horse/(\d+)", horse_href)
        horse_id = m_horse_id.group(1) if m_horse_id else horse_name

        # 単勝オッズ (td.Popular、td.Tansho、.Odds の順に試す)
        odds_val = 0.0
        for sel in [".Popular", ".Tansho", ".Odds"]:
            odds_el = row.select_one(sel)
            if odds_el:
                odds_text = odds_el.get_text(strip=True)
                try:
                    odds_val = float(re.sub(r"[^\d.]", "", odds_text))
                    break
                except ValueError:
                    continue

        # 枠番
        gate_el = row.select_one(".Waku span, .Waku, td:first-child")
        gate = gate_el.get_text(strip=True) if gate_el else ""
        try: gate = int(re.sub(r"[^\d]", "", str(gate)))
        except: gate = 0

        # 騎手
        jockey_el = row.select_one(".Jockey a, .Jockey")
        jockey = jockey_el.get_text(strip=True) if jockey_el else ""

        # 斤量
        handicap_el = row.select_one(".Handicap, .Weight:not(:last-child)")
        handicap = 0.0
        if handicap_el:
            ht = handicap_el.get_text(strip=True)
            try: handicap = float(re.sub(r"[^\d.]", "", ht))
            except: pass

        # 馬体重 (最終列にあることが多い)
        weight_el = row.select_one("td.Weight:last-child, td:last-child")
        horse_weight = weight_el.get_text(strip=True) if weight_el else ""
        # 数字+記号パターンだけ抽出 (例: "500(+2)" → keep, 他の長文は破棄)
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

    horses = horses[:18]  # JRA 平地戦最大18頭、超過分は補欠馬

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
# Layer 3b: 確定オッズ (過去レース用 result.html)
# ---------------------------------------------------------------------------

def parse_result_odds(race_id: str) -> dict[str, float]:
    """result.html から 馬ID → 確定単勝オッズ のマッピングを取得"""
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
# Layer 4: 馬DB（過去走 + 血統）
# ---------------------------------------------------------------------------

def parse_past_results(horse_id: str, limit: int = 5) -> tuple[list[dict], str, str]:
    """DBサイトから当該馬の過去レース結果（最大n走）と血統を取得"""
    # レース結果
    results: list[dict] = []
    try:
        url = f"{NETKEIBA_DB}/horse/result/{horse_id}/"
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")

        table = soup.select_one(".db_h_race_results")
        if table:
            rows = table.select("tr")[1:]  # skip header
            count = 0
            for row in rows:
                if count >= limit:
                    break
                cols = row.select("td")
                if len(cols) < 10:
                    continue

                race_date = cols[0].get_text(strip=True)
                if not re.match(r"\d{4}/\d{2}/\d{2}", race_date):
                    continue

                # 列構造 (テーブルにより変動あり):
                #   18: タイム, 25: 通過, 27: 上り3F, 29: 厩舎ｺﾒﾝﾄ, 30: 備考
                finish_time = ""
                passage_rank = ""
                last_3f = ""
                comment = ""

                # 優先的に既知の列位置から取得
                if len(cols) > 18:
                    finish_time = cols[18].get_text(strip=True)
                if len(cols) > 25:
                    passage_rank = cols[25].get_text(strip=True)
                if len(cols) > 27:
                    last_3f = cols[27].get_text(strip=True)
                if len(cols) > 29:
                    comment = cols[29].get_text(strip=True)
                if not comment and len(cols) > 30:
                    comment = cols[30].get_text(strip=True)

                # フォールバック: 全列スキャンでパターンマッチ
                if not finish_time:
                    for col in cols:
                        txt = col.get_text(strip=True)
                        if re.match(r"^\d:\d{2}\.\d$", txt):
                            finish_time = txt
                            break
                if not passage_rank:
                    for col in cols:
                        txt = col.get_text(strip=True)
                        if re.match(r"^\d+-\d+-\d+", txt) and len(txt) <= 7:
                            passage_rank = txt
                            break
                if not last_3f:
                    for col in cols:
                        txt = col.get_text(strip=True)
                        if re.match(r"^\d{2}\.\d$", txt):
                            last_3f = txt
                            break
                if not comment:
                    for col in cols:
                        txt = col.get_text(strip=True)
                        if len(txt) > 15 and any(kw in txt for kw in ["出遅", "進路", "不利", "展開", "スタート", "追走", "直線", "手応え", "余裕"]):
                            comment = txt
                            break

                results.append({
                    "race_date": race_date,
                    "finish_time": finish_time,
                    "passage_rank": passage_rank,
                    "last_3furlong": float(last_3f) if last_3f else None,
                    "race_comment": comment,
                })
                count += 1
    except Exception:
        pass

    # 血統情報
    sire = ""
    damsire = ""
    try:
        url = f"{NETKEIBA_DB}/horse/ped/{horse_id}/"
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")

        table = soup.select_one(".blood_table.detail")
        if table:
            trs = table.select("tr")
            if len(trs) > 0:
                # 父: 1行目の1列目にある馬名リンク
                sire_td = trs[0].select_one("td")
                if sire_td:
                    sire_a = sire_td.select_one("a[href*='/horse/'][href$='/'][href]:not([href*='/ped/']):not([href*='/sire/'])")
                    if not sire_a:
                        # フォールバック: 最初の馬リンク
                        sire_a = sire_td.select_one("a[href*='/horse/']")
                    if sire_a:
                        sire = sire_a.get_text(strip=True)

            # 母父: 母の行（8行目付近）の2列目
            if len(trs) > 8:
                damsire_tds = trs[8].select("td")
                if len(damsire_tds) >= 2:
                    damsire_a = damsire_tds[1].select_one("a[href*='/horse/'][href$='/'][href]:not([href*='/ped/']):not([href*='/sire/'])")
                    if not damsire_a:
                        damsire_a = damsire_tds[1].select_one("a[href*='/horse/']")
                    if damsire_a:
                        damsire = damsire_a.get_text(strip=True)
    except Exception:
        pass

    return results, sire, damsire


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="netkeiba 出走表スクレイピング")
    parser.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
    parser.add_argument("--output", default="scraped_data.json", help="出力JSONパス")
    args = parser.parse_args()

    d = _ymd_to_yyyymmdd(args.date)

    # Layer 1: 日付リスト
    date_entries = fetch_date_list(d)
    if not date_entries:
        print(f"[INFO] No race dates found for {args.date}")
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump([], f)
        sys.exit(0)

    print(f"[INFO] Found {len(date_entries)} date entries")

    # Layer 2: レース一覧 (重複排除のため unique group のみ)
    # 指定日にマッチするエントリのみを使う
    target_ymd = _ymd_to_yyyymmdd(args.date)
    matching = [e for e in date_entries if e["date"] == target_ymd]
    if not matching:
        # マッチしなければ最新のエントリを使用
        matching = [date_entries[-1]] if date_entries else []
        print(f"[INFO] No exact match for {args.date}, using last entry")

    all_races: list[dict] = []
    for entry in matching:
        group = entry["group"]
        entry_date = entry["date"]
        print(f"[INFO] Fetching race list for date={entry_date} group={group}")
        time.sleep(0.5)

        race_cards = fetch_race_list_sub(entry_date, group)
        print(f"  -> {len(race_cards)} races")
        all_races.extend(race_cards)

    # レースID 重複除去
    seen_ids: set[str] = set()
    unique_races: list[dict] = []
    for rc in all_races:
        if rc["race_id"] not in seen_ids:
            seen_ids.add(rc["race_id"])
            unique_races.append(rc)
    all_races = unique_races
    print(f"[INFO] Total races: {len(all_races)}")

    if not all_races:
        print(f"[INFO] No races found for {args.date}")
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump([], f)
        sys.exit(0)

    JST = timezone(timedelta(hours=9))
    today = datetime.now(JST).date()

    # Layer 3 + 4: 出走表 + 過去走
    output_data: list[dict] = []
    for i, rc in enumerate(all_races):
        print(f"[INFO] ({i+1}/{len(all_races)}) Scraping {rc['race_id']} {rc['venue']}{rc['race_no']}R {rc['race_name']}")
        try:
            race_data = parse_shutuba(rc["race_id"])
            race_data["race_name"] = rc.get("race_name", race_data.get("race_name", "?"))
        except Exception as e:
            print(f"  [WARN] Failed: {e}")
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
                            print(f"  [OK] Updated odds for {updated}/{len(race_data['horses'])} horses from result page")
                    except Exception as e:
                        print(f"  [WARN] Failed to get result odds: {e}")
            except ValueError:
                pass

        for h in race_data["horses"]:
            try:
                past, sire, damsire = parse_past_results(h["horse_id"])
            except Exception as e:
                print(f"  [WARN] Horse {h['horse_id']} past: {e}")
                past, sire, damsire = [], "", ""

            h["past_results"] = past
            h["sire"] = sire
            h["damsire"] = damsire
            time.sleep(0.5)

        output_data.append(race_data)
        time.sleep(1.0)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"[DONE] Saved {len(output_data)} races to {args.output}")


if __name__ == "__main__":
    main()
