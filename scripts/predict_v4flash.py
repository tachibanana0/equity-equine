"""
スクレイピングデータから V4 Flash で分析+勝率推論して Worker に保存。
コメントが空でも数値データ（タイム/通過/上り3F）から推論可能。

Usage:
  python predict_v4flash.py --input scraped_data.json --race-id 202605020801
"""

import json
import os
import re
import sys
import time
import argparse
import hashlib
import hmac

import requests
from openai import OpenAI

MODEL = "deepseek/deepseek-v4-flash"


def get_config():
    ok = os.environ.get("OPENROUTER_API_KEY")
    wu = os.environ.get("WORKER_SAVE_PREDICTIONS_URL") or (os.environ.get("WORKER_PREDICT_URL", "") + "/save-predictions")
    ap = os.environ.get("API_SECRET")
    tu = os.environ.get("TURSO_DATABASE_URL")
    ta = os.environ.get("TURSO_AUTH_TOKEN")
    missing = []
    if not ok: missing.append("OPENROUTER_API_KEY")
    if not wu: missing.append("WORKER_SAVE_PREDICTIONS_URL")
    if not ap: missing.append("API_SECRET")
    if missing:
        print(f"[ERROR] Missing env vars: {missing}")
        sys.exit(1)
    return ok, tu, ta, wu, ap


def turso_query(db_url: str, auth: str, sql: str, args: list = None) -> list[dict]:
    url = db_url
    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://")

    typed_args = []
    for a in (args or []):
        if a is None:
            typed_args.append({"type": "null"})
        elif isinstance(a, int):
            typed_args.append({"type": "integer", "value": str(a)})
        elif isinstance(a, float):
            typed_args.append({"type": "float", "value": a})
        else:
            typed_args.append({"type": "text", "value": str(a)})

    resp = requests.post(
        f"{url}/v2/pipeline",
        headers={"Authorization": f"Bearer {auth}", "Content-Type": "application/json"},
        json={"requests": [{"type": "execute", "stmt": {"sql": sql, "args": typed_args}}]},
        timeout=30,
    )
    data = resp.json()
    results = data.get("results", [])
    if not results or results[0].get("type") != "ok":
        raise Exception(f"Turso error: {json.dumps(data)[:200]}")

    rd = results[0].get("response", {}).get("result", {})
    cols = [c["name"] for c in rd.get("cols", [])]

    def unwrap(v):
        if v is None: return None
        if isinstance(v, dict) and v.get("type") == "null": return None
        if isinstance(v, dict) and "value" in v: return v["value"]
        return v

    return [dict(zip(cols, [unwrap(r[i]) for i in range(len(cols))])) for r in rd.get("rows", [])]


def fetch_horse_data_from_turso(db_url: str, auth: str, horse_id: str) -> dict:
    horses = turso_query(db_url, auth, "SELECT * FROM horses WHERE id = ?", [horse_id])
    if not horses:
        return {"sire": "不明", "damsire": "不明", "past_results": []}
    h = horses[0]
    past = turso_query(db_url, auth,
        "SELECT * FROM past_results WHERE horse_id = ? ORDER BY race_date DESC LIMIT 5", [horse_id])
    return {
        "sire": h.get("sire", ""),
        "damsire": h.get("damsire", ""),
        "past_results": [{
            "race_date": p.get("race_date", ""),
            "finish_time": p.get("finish_time"),
            "passage_rank": p.get("passage_rank"),
            "last_3furlong": p.get("last_3furlong"),
            "race_comment": p.get("race_comment", ""),
            "structured_comment": p.get("structured_comment"),
        } for p in past],
    }


def parse_passage_rank(pr: str) -> list[int]:
    """passage_rank 文字列 '1-1-2-2' を整数リストにパース"""
    try:
        return [int(x) for x in pr.split("-")]
    except (ValueError, AttributeError):
        return []


def detect_disadvantage(past_results: list[dict]) -> list[str]:
    """
    過去走の通過順位から 出遅れ・掛かり を検出。
    戻り値は直近レースの不利要素フラグリスト。
    """
    all_ranks = []
    for p in past_results:
        pr = p.get("passage_rank", "")
        ranks = parse_passage_rank(pr)
        if ranks:
            all_ranks.append({
                "race_date": p.get("race_date", ""),
                "first": ranks[0],
                "last": ranks[-1],
                "full": ranks,
            })

    if len(all_ranks) < 2:
        return []  # 過去走が1走以下なら比較不可

    # 過去N-1走の中央値を「通常パターン」とする（直近を除く）
    historical = all_ranks[:-1]
    recent = all_ranks[-1]

    first_positions = sorted([h["first"] for h in historical])
    med_first = first_positions[len(first_positions) // 2]

    flags = []

    # 出遅れ: 通常2〜4番手なのに直近だけ15番手以上
    if 2 <= med_first <= 4 and recent["first"] >= 15:
        flags.append(f"出遅れ(直近{recent['race_date']}: 通過{recent['full']}, 通常{med_first}番手)")

    # 掛かり: 通常5番手以下なのに直近が極端な先行(1〜2番手)かつ大幅失速(最終位置-最初位置 >= 5)
    fade = recent["last"] - recent["first"]
    if med_first >= 5 and recent["first"] <= 2 and fade >= 5:
        flags.append(f"掛かり(直近{recent['race_date']}: 通過{recent['full']}, 先行→失速{fade}ポジション後退)")

    return flags


def build_prompt(race_info: dict, horses: list[dict]) -> str:
    venue = race_info.get("venue", "")
    distance = race_info.get("distance", 0)
    condition = race_info.get("track_condition", "良")

    # ラップタイム情報（レース後のみ存在）
    lap_info = ""
    lap_raw = race_info.get("lap_times")
    if lap_raw:
        try:
            lt = json.loads(lap_raw) if isinstance(lap_raw, str) else lap_raw
            pace = lt.get("pace", "")
            splits = lt.get("splits", [])
            pace_label = {"H": "ハイペース", "M": "平均ペース", "S": "スローペース"}.get(pace, pace)
            lap_info = f"\nラップタイム: {pace_label} {'-'.join(splits)}"
        except Exception:
            pass

    entries = []
    for i, h in enumerate(horses):
        past_lines = []
        for p in h.get("past_results", []):
            parts = [f"日付{p.get('race_date','?')}"]
            if p.get("finish_time"): parts.append(f"タイム{p['finish_time']}秒")
            if p.get("passage_rank"): parts.append(f"通過{p['passage_rank']}")
            if p.get("last_3furlong"): parts.append(f"上り3F{p['last_3furlong']}")
            if p.get("race_comment"): parts.append(p["race_comment"])
            past_lines.append("  " + " ".join(parts))

        past_str = "\n".join(past_lines) if past_lines else "  過去データなし"

        # 不利要素検出
        disadvantages = detect_disadvantage(h.get("past_results", []))
        dis_str = ""
        if disadvantages:
            dis_str = "\n不利要素:\n  " + "\n  ".join(disadvantages)

        entries.append(
            f"馬{i+1}: {h['horse_name']} 父={h.get('sire','不明')} 母父={h.get('damsire','不明')}\n"
            f"過去戦績:\n{past_str}{dis_str}"
        )

    return f"""あなたは競馬の専門家です。以下の出走馬の過去データから各馬の勝率を算出してください。
オッズは一切参照しないでください。

レース情報: {venue} {distance}m 馬場:{condition}{lap_info}

{chr(10).join(entries)}

指示:
1. タイム・通過順位・上がり3ハロン・血統のみから総合的に判断する
2. 通過順位から「出遅れ」「掛かり（先行しすぎて最後に失速）」等の展開を推測する。不利要素がある馬は勝率を下げる方向に評価する
3. 上がり3ハロンが遅い馬は「末脚不足」と判断する
4. 全馬の勝率合計がちょうど1.0になるように小数点以下4桁で算出する
5. 推論理由を簡潔に説明する

以下のJSONで回答（JSONのみ、コードブロック不要）：
{{
  "horses": [
    {{"horse_index": 1, "win_probability": 0.1234, "reasoning": "推論理由..."}}
  ]
}}"""


def call_ai(client: OpenAI, prompt: str, n_horses: int) -> dict:
    print(f"[INFO] Prompt: {len(prompt)} chars, {n_horses} horses")

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": "JSON only. No markdown. No explanations outside JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=4096,
            )
            content = resp.choices[0].message.content or ""
            json_match = re.search(r"\{[\s\S]*\}", content)
            if json_match:
                parsed = json.loads(json_match[0])
                horses = parsed.get("horses", [])
                total = sum(h.get("win_probability", 0) for h in horses)
                print(f"[INFO] Sum of probabilities: {total:.4f}")
                return parsed
            else:
                print(f"[WARN] No JSON in response: {content[:200]}")
        except Exception as e:
            print(f"[WARN] Attempt {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(3)

    raise Exception(f"AI call failed after 3 attempts")


def save_to_worker(worker_url: str, api_secret: str, race_id: str,
                   predictions: dict, horse_list: list[dict]):
    pred_payload = []
    for hp in predictions.get("horses", []):
        idx = hp["horse_index"] - 1
        if idx < 0 or idx >= len(horse_list):
            continue
        h = horse_list[idx]
        odds = h.get("odds") or 0
        ev = round(hp["win_probability"] * odds, 4) if odds else None
        pred_payload.append({
            "race_id": race_id,
            "horse_id": h["horse_id"],
            "win_probability": hp["win_probability"],
            "reasoning_logic": hp.get("reasoning", ""),
            "odds_at_prediction": odds if odds else None,
            "expected_value": ev,
            "model_name": MODEL,
            "recommended": ev is not None and ev > 1.25,
        })

    body = json.dumps({"predictions": pred_payload}).encode()
    resp = requests.post(
        worker_url,
        headers={"Content-Type": "application/json"},
        data=body,
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"[ERROR] Worker returned {resp.status_code}: {resp.text[:300]}")
        return False

    data = resp.json()
    print(f"[OK] Saved {data.get('saved', 0)} predictions, {len([p for p in pred_payload if p['recommended']])} recommended")
    return True


def main():
    parser = argparse.ArgumentParser(description="V4 Flash 分析+勝率推論")
    parser.add_argument("--input", required=True, help="スクレイピング済みJSON (scraped_data.json)")
    parser.add_argument("--race-id", help="特定レースID（省略時は全レース）")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        scraped = json.load(f)

    races = [r for r in scraped if not args.race_id or r["race_id"] == args.race_id]
    if not races:
        print(f"[INFO] No races found")
        return

    if not races[0].get("horses"):
        print(f"[INFO] No horses in scraped data")
        return

    openrouter_key, db_url, auth, worker_url, api_secret = get_config()
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=openrouter_key)

    for race in races:
        race_id = race["race_id"]
        horses = race.get("horses", [])
        if not horses:
            print(f"[WARN] {race_id}: no horses")
            continue

        print(f"\n[INFO] Race {race_id}: {len(horses)} horses")

        # Turso から各馬の血統 + 過去5走を補完 (Tursoが設定されている場合のみ)
        if db_url and auth:
            for h in horses:
                db_data = fetch_horse_data_from_turso(db_url, auth, h["horse_id"])
                h["sire"] = h.get("sire") or db_data["sire"]
                h["damsire"] = h.get("damsire") or db_data["damsire"]
                if not h.get("past_results"):
                    h["past_results"] = db_data["past_results"]

        race_info = {
            "venue": race.get("venue", ""),
            "distance": race.get("distance", 0),
            "track_condition": race.get("track_condition", "良"),
        }
        # Turso から lap_times を取得（レース後のみ存在）
        if db_url and auth:
            try:
                laps = turso_query(db_url, auth, "SELECT lap_times FROM races WHERE id = ?", [race_id])
                if laps and laps[0].get("lap_times"):
                    race_info["lap_times"] = laps[0]["lap_times"]
            except Exception:
                pass

        prompt = build_prompt(race_info, horses)
        prediction = call_ai(client, prompt, len(horses))
        save_to_worker(worker_url, api_secret, race_id, prediction, horses)

    print("\n[DONE]")


if __name__ == "__main__":
    main()
