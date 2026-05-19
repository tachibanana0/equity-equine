"""
netkeiba から出走表と各馬の過去5走データを取得するスクレイピングスクリプト。
出力: レースIDごとの出走馬リストと過去走データのJSON
"""

import os
import re
import json
import time
import sys
import argparse
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

NETKEIBA_BASE = "https://race.netkeiba.com"
NETKEIBA_DB = "https://db.netkeiba.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9",
}

# 場コード→場名
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


def parse_race_list(date_str: str) -> list[dict]:
    """
    指定日の全レース一覧を netkeiba から取得。
    date_str: YYYY-MM-DD
    """
    races = []
    race_list_url = f"{NETKEIBA_BASE}/top/race_list.html"
    params = {"kaisai_date": date_str}
    html = fetch_html(race_list_url, params)
    soup = BeautifulSoup(html, "html.parser")

    for item in soup.select("a[href*='/race/shutuba.html']"):
        href = item.get("href", "")
        m = re.search(r"race_id=(\d+)", href)
        if not m:
            continue
        race_id = m.group(1)
        race_no = race_id[8:10] if len(race_id) >= 10 else "?"
        venue_code = race_id[4:6]
        venue = VENUE_MAP.get(venue_code, venue_code)
        race_name_tag = item.select_one(".RaceName, .Item04")
        race_name = race_name_tag.get_text(strip=True) if race_name_tag else "?"
        races.append({
            "race_id": race_id,
            "race_no": race_no,
            "venue": venue,
            "race_name": race_name,
        })

    return races


def parse_shutuba(race_id: str) -> dict:
    """出走表ページからレース情報 + 出走馬情報を取得"""
    url = f"{NETKEIBA_BASE}/race/shutuba.html"
    params = {"race_id": race_id}
    html = fetch_html(url, params)
    soup = BeautifulSoup(html, "html.parser")

    # レース基本情報
    race_data_el = soup.select_one(".RaceData01, .RaceData")
    distance = 0
    track_condition = "良"
    if race_data_el:
        text = race_data_el.get_text(" ", strip=True)
        m_dist = re.search(r"(\d+)m", text)
        if m_dist:
            distance = int(m_dist.group(1))
        if "稍重" in text:
            track_condition = "稍重"
        elif "重" in text and "稍重" not in text:
            track_condition = "重"
        elif "不良" in text:
            track_condition = "不良"
        else:
            track_condition = "良"

    date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", soup.get_text())
    race_date = ""
    if date_match:
        race_date = f"{date_match.group(1)}-{date_match.group(2).zfill(2)}-{date_match.group(3).zfill(2)}"

    horses = []
    for row in soup.select(".HorseList tbody tr, .Shutuba_Table tbody tr"):
        name_el = row.select_one(".HorseName a, .Horse_Name a")
        if not name_el:
            continue
        horse_name = name_el.get_text(strip=True)
        horse_href = name_el.get("href", "")
        m_horse_id = re.search(r"horse/(\d+)", horse_href)
        horse_id = m_horse_id.group(1) if m_horse_id else horse_name

        odds_el = row.select_one(".Odds, .Tansho")
        odds_text = odds_el.get_text(strip=True) if odds_el else ""
        odds_val = 0.0
        try:
            odds_val = float(odds_text.replace(",", ""))
        except ValueError:
            pass

        horses.append({
            "horse_id": horse_id,
            "horse_name": horse_name,
            "odds": odds_val,
        })

    return {
        "race_id": race_id,
        "date": race_date,
        "distance": distance,
        "track_condition": track_condition,
        "horses": horses,
    }


def parse_past_results(horse_id: str, limit: int = 5) -> list[dict]:
    """DBサイトから当該馬の過去レース結果（最大n走）を取得"""
    url = f"{NETKEIBA_DB}/horse/{horse_id}/"
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    # 血統情報
    sire = ""
    damsire = ""
    blood_el = soup.select_one(".blood_table, .Blood_Table")
    if blood_el:
        links = blood_el.select("a")
        for link in links:
            text = link.get_text(strip=True)
            href = link.get("href", "")
            if not sire and "horse/ped/" in href:
                sire = text
            elif sire and not damsire and "horse/ped/" in href:
                damsire = text
                break

    results = []
    for row in soup.select(".RaceTable01 tbody tr, .db_h_race_results tbody tr")[:limit]:
        cols = row.select("td")
        if len(cols) < 10:
            continue

        race_date_el = cols[0]
        race_date = race_date_el.get_text(strip=True) if race_date_el else ""

        distance_el = cols[0] if len(cols) > 0 else None
        # 列構成がサイトによって異なるためインデックスを柔軟に
        finish_time = ""
        passage_rank = ""
        last_3f = ""
        comment = ""

        for i, col in enumerate(cols):
            txt = col.get_text(strip=True)
            if re.match(r"\d:\d{2}\.\d", txt):
                finish_time = txt
            if re.match(r"\d+-\d+-\d+", txt) and "-" in txt and len(txt) < 10:
                passage_rank = txt
            if re.match(r"\d{2}\.\d$", txt) and i > 5:
                last_3f = txt

        # レース短評
        for col in cols:
            txt = col.get_text(strip=True)
            if len(txt) > 15 and any(kw in txt for kw in ["出遅", "進路", "不利", "展開", "スタート", "追走", "直線", "手応え", "余裕"]):
                comment = txt
                break

        if race_date:
            results.append({
                "race_date": race_date,
                "finish_time": finish_time,
                "passage_rank": passage_rank,
                "last_3furlong": float(last_3f) if last_3f else None,
                "race_comment": comment,
            })

    return results, sire, damsire


def main():
    parser = argparse.ArgumentParser(description="netkeiba 出走表スクレイピング")
    parser.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
    parser.add_argument("--output", default="scraped_data.json", help="出力JSONパス")
    args = parser.parse_args()

    races = parse_race_list(args.date)
    print(f"[INFO] Found {len(races)} races for {args.date}")

    all_data = []
    for r in races:
        print(f"[INFO] Scraping race {r['race_id']} ({r['race_name']})")
        try:
            race_data = parse_shutuba(r["race_id"])
        except Exception as e:
            print(f"[WARN] Failed to parse shutuba for {r['race_id']}: {e}")
            continue

        for h in race_data["horses"]:
            try:
                past, sire, damsire = parse_past_results(h["horse_id"])
            except Exception as e:
                print(f"[WARN] Failed to parse past results for {h['horse_id']}: {e}")
                past, sire, damsire = [], "", ""

            h["past_results"] = past
            h["sire"] = sire
            h["damsire"] = damsire
            time.sleep(0.5)  # サーバー負荷軽減

        all_data.append(race_data)
        time.sleep(1.0)

    output_path = args.output
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)

    print(f"[DONE] Saved {len(all_data)} races to {output_path}")


if __name__ == "__main__":
    main()
