"""
スクレイピング済みのレース短評テキストを OpenRouter / DeepSeek V4 Flash で構造化するスクリプト。
出力: 出遅れ、進路不利、展開不向きなどをJSON化し、Turso DB に保存。
"""

import json
import os
import sys
import time
import argparse
from openai import OpenAI

FLASH_MODEL = "deepseek/deepseek-v4-flash"

STRUCTURE_PROMPT = """あなたは競馬アナリストです。以下の馬の過去5走のレース短評を読み、各レースについて以下の項目をJSON形式で構造化してください。

対象馬: {horse_name}

各レースの短評:
{comments}

出力形式（JSONのみ。コードブロックなしで出力）:
[
  {{
    "race_index": 0,
    "出遅れ": true/false,
    "進路不利": true/false,
    "展開不向き": true/false,
    "スタート良": true/false,
    "直線良": true/false,
    "手応え良": true/false,
    "要約": "短評の日本語要約一文"
  }},
  ...
]
レース短評がない場合は空文字列、該当項目がない場合はfalseとしてください。"""


def get_openrouter_client() -> OpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("[ERROR] OPENROUTER_API_KEY environment variable not set")
        sys.exit(1)
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )


def structure_comments(client: OpenAI, horse_name: str, past_results: list[dict]) -> list[dict]:
    if not past_results or all(not r.get("race_comment") for r in past_results):
        return []

    comments_text = "\n".join(
        f"レース{i}: {r.get('race_comment', '(短評なし)')}"
        for i, r in enumerate(past_results)
    )

    prompt = STRUCTURE_PROMPT.format(horse_name=horse_name, comments=comments_text)

    resp = client.chat.completions.create(
        model=FLASH_MODEL,
        messages=[
            {"role": "system", "content": "JSONのみを返してください。解説は不要です。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=2048,
    )

    content = resp.choices[0].message.content or "[]"
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1])

    try:
        structured = json.loads(content)
        return structured
    except json.JSONDecodeError:
        print(f"[WARN] JSON parse error for {horse_name}, raw: {content[:200]}")
        return []


def main():
    parser = argparse.ArgumentParser(description="V4 Flash データ構造化")
    parser.add_argument("--input", required=True, help="scrape_race.py の出力JSON")
    parser.add_argument("--output", default="structured_data.json", help="出力JSONパス")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        races = json.load(f)

    client = get_openrouter_client()

    for race in races:
        print(f"[INFO] Processing race {race['race_id']}")
        for h in race["horses"]:
            horse_name = h["horse_name"]
            past = h.get("past_results", [])
            if not past:
                continue

            try:
                structured = structure_comments(client, horse_name, past)
                for i, s in enumerate(structured):
                    if i < len(past):
                        past[i]["structured_comment"] = json.dumps(s, ensure_ascii=False)
                print(f"  [OK] {horse_name}: structured {len(structured)} comments")
            except Exception as e:
                print(f"  [ERR] {horse_name}: {e}")

            time.sleep(0.3)  # rate limit

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(races, f, ensure_ascii=False, indent=2)

    print(f"[DONE] Saved structured data to {args.output}")


if __name__ == "__main__":
    main()
