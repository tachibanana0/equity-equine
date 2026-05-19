import { Hono } from "hono";
import { cors } from "hono/cors";

type Bindings = {
  OPENROUTER_API_KEY: string;
  TURSO_DATABASE_URL: string;
  TURSO_AUTH_TOKEN: string;
  API_SECRET: string;
};

const app = new Hono<{ Bindings: Bindings }>();

// =========================================================================
// Helpers
// =========================================================================

function tursoUrl(dbUrl: string) {
  return dbUrl.replace("libsql://", "https://");
}

function typedArgs(args: (string | number | null)[]) {
  return args.map((v) => {
    if (v === null || v === undefined) return { type: "null" };
    if (typeof v === "number") return { type: "float", value: v };
    return { type: "text", value: String(v) };
  });
}

async function tursoQuery(
  dbUrl: string,
  authToken: string,
  sql: string,
  args: (string | number | null)[] = []
): Promise<Record<string, unknown>[]> {
  const resp = await fetch(`${tursoUrl(dbUrl)}/v2/pipeline`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${authToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      requests: [{ type: "execute", stmt: { sql, args: typedArgs(args) } }],
    }),
  });
  const data = (await resp.json()) as {
    results: { type: string; response?: { result?: { cols: { name: string }[]; rows: unknown[][] } } }[];
  };
  const result = data.results?.[0];
  if (result?.type !== "ok" || !result.response?.result) return [];
  const { cols, rows } = result.response.result;
  return (rows || []).map((row) => {
    const obj: Record<string, unknown> = {};
    (cols || []).forEach((c, i) => {
      const val = (row as unknown[])[i] as { type?: string; value?: unknown } | null;
      if (!val || val.type === "null") obj[c.name] = null;
      else if (val.type === "integer") obj[c.name] = parseInt(String(val.value), 10);
      else if (val.type === "float") obj[c.name] = Number(val.value);
      else obj[c.name] = val.value;
    });
    return obj;
  });
}

async function tursoBatch(
  dbUrl: string,
  authToken: string,
  stmts: { sql: string; args?: (string | number | null)[] }[]
): Promise<void> {
  const CHUNK = 50;
  for (let i = 0; i < stmts.length; i += CHUNK) {
    const chunk = stmts.slice(i, i + CHUNK);
    await fetch(`${tursoUrl(dbUrl)}/v2/pipeline`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${authToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        requests: chunk.map((s) => ({
          type: "execute" as const,
          stmt: { sql: s.sql, args: typedArgs(s.args || []) },
        })),
      }),
    });
  }
}

async function verifySignature(
  body: string,
  signature: string,
  secret: string
): Promise<boolean> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"]
  );
  const sigBytes = new Uint8Array(
    signature.match(/.{1,2}/g)!.map((b) => parseInt(b, 16))
  );
  return crypto.subtle.verify("HMAC", key, sigBytes, encoder.encode(body));
}

async function callOpenRouter(
  apiKey: string,
  model: string,
  systemPrompt: string,
  userPrompt: string,
  maxTokens: number,
  signal?: AbortSignal
) {
  const resp = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user", content: userPrompt },
      ],
      temperature: 0.1,
      max_tokens: maxTokens,
    }),
    signal,
  });
  return resp.json() as Promise<{ choices?: { message?: { content?: string } }[] }>;
}

// =========================================================================
// POST /save-and-predict — データ保存 (GH Actions から呼出)
// =========================================================================

app.post("/save-and-predict", async (c) => {
  const body = await c.req.text();
  const sig = c.req.header("X-API-Signature");
  const secret = c.env.API_SECRET || "dev-secret";
  if (sig) {
    const valid = await verifySignature(body, sig, secret);
    if (!valid) return c.json({ error: "invalid signature" }, 403);
  }

  let payload: { races?: { race_id: string; date?: string; venue?: string; distance?: number; track_condition?: string; horses?: { horse_id: string; horse_name: string; odds?: number; sire?: string; damsire?: string; past_results?: { race_date: string; finish_time?: number; passage_rank?: string; last_3furlong?: number; race_comment?: string; structured_comment?: string }[] }[] }[] };
  try { payload = JSON.parse(body); } catch { return c.json({ error: "invalid json" }, 400); }

  const races = payload.races || [];
  if (!races.length) return c.json({ error: "no races" }, 400);

  const dbUrl = c.env.TURSO_DATABASE_URL;
  const auth = c.env.TURSO_AUTH_TOKEN;
  const stmts: { sql: string; args: (string | number | null)[] }[] = [];

  for (const race of races) {
    const rid = race.race_id;
    stmts.push({
      sql: "INSERT OR REPLACE INTO races (id, date, venue, distance, track_condition, result_confirmed) VALUES (?, ?, ?, ?, ?, 0)",
      args: [rid, race.date || "", race.venue || "", race.distance || 0, race.track_condition || "良"],
    });

    for (const h of race.horses || []) {
      stmts.push({
        sql: "INSERT OR REPLACE INTO horses (id, name, sire, damsire) VALUES (?, ?, ?, ?)",
        args: [h.horse_id, h.horse_name, h.sire || null, h.damsire || null],
      });
      for (const p of h.past_results || []) {
        stmts.push({
          sql: "INSERT INTO past_results (horse_id, race_date, finish_time, passage_rank, last_3furlong, race_comment, structured_comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
          args: [h.horse_id, p.race_date, p.finish_time ?? null, p.passage_rank || null, p.last_3furlong ?? null, p.race_comment || null, p.structured_comment || null],
        });
      }
    }
  }

  await tursoBatch(dbUrl, auth, stmts);
  return c.json({ status: "ok", races: races.length, horses: races.reduce((s, r) => s + (r.horses?.length || 0), 0) });
});

// =========================================================================
// POST /save-predictions — 推論結果保存 (Python predict_v4flash_direct.py 用)
// =========================================================================

app.post("/save-predictions", async (c) => {
  const body = await c.req.text();
  const sig = c.req.header("X-API-Signature");
  const secret = c.env.API_SECRET || "dev-secret";
  if (sig) {
    const valid = await verifySignature(body, sig, secret);
    if (!valid) return c.json({ error: "invalid signature" }, 403);
  }

  let payload: { predictions?: { race_id: string; horse_id: string; win_probability: number; reasoning_logic?: string; odds_at_prediction?: number; expected_value?: number; model_name: string; recommended?: boolean }[] };
  try { payload = JSON.parse(body); } catch { return c.json({ error: "invalid json" }, 400); }

  const preds = payload.predictions || [];
  if (!preds.length) return c.json({ error: "no predictions" }, 400);

  const dbUrl = c.env.TURSO_DATABASE_URL;
  const auth = c.env.TURSO_AUTH_TOKEN;

  // 重複排除: 同一 race+horse+model の古い予測を削除
  for (let i = 0; i < preds.length; i += 50) {
    const chunk = preds.slice(i, i + 50);
    const placeholders = chunk.map(() => "(?, ?, ?)").join(", ");
    const args = chunk.flatMap((p) => [p.race_id, p.horse_id, p.model_name]);
    await tursoQuery(dbUrl, auth,
      `DELETE FROM predictions WHERE (race_id, horse_id, model_name) IN (VALUES ${placeholders})`,
      args
    );
  }

  const stmts = preds.map((p) => ({
    sql: "INSERT INTO predictions (race_id, horse_id, win_probability, reasoning_logic, odds_at_prediction, expected_value, model_name, recommended) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    args: [p.race_id, p.horse_id, p.win_probability, p.reasoning_logic || null, p.odds_at_prediction ?? null, p.expected_value ?? null, p.model_name, (p.recommended ? 1 : 0)] as (string | number | null)[],
  }));

  await tursoBatch(dbUrl, auth, stmts);
  return c.json({ status: "ok", saved: preds.length });
});

// =========================================================================
// POST /predict-now — テスト用 V4 Pro 直接推論
// =========================================================================

app.post("/predict-now", async (c) => {
  let payload: { race_id: string; horse_ids?: string[] };
  try { payload = await c.req.json(); } catch { return c.json({ error: "invalid json" }, 400); }

  const raceId = payload.race_id;
  const dbUrl = c.env.TURSO_DATABASE_URL;
  const auth = c.env.TURSO_AUTH_TOKEN;
  const apiKey = c.env.OPENROUTER_API_KEY;

  if (!apiKey) return c.json({ error: "OPENROUTER_API_KEY not set" }, 500);

  // レース基本情報
  const raceRows = await tursoQuery(dbUrl, auth, "SELECT * FROM races WHERE id = ?", [raceId]);
  if (!raceRows.length) return c.json({ error: "race not found" }, 404);
  const race = raceRows[0];

  // 馬情報 + 過去走
  const horses = payload.horse_ids && payload.horse_ids.length > 0
    ? payload.horse_ids
    : (await tursoQuery(dbUrl, auth, "SELECT DISTINCT horse_id FROM past_results")).map((r) => r.horse_id as string);

  const placeholders = horses.map(() => "?").join(",");
  const horseRows = await tursoQuery(dbUrl, auth,
    `SELECT h.id, h.name, h.sire, h.damsire, pr.race_date, pr.finish_time, pr.passage_rank, pr.last_3furlong, pr.race_comment FROM horses h LEFT JOIN past_results pr ON pr.horse_id = h.id WHERE h.id IN (${placeholders}) ORDER BY pr.race_date DESC`,
    horses
  );

  const horseMap = new Map<string, { name: string; sire: string; damsire: string; pasts: string[] }>();
  for (const r of horseRows) {
    const id = r.id as string;
    if (!horseMap.has(id)) {
      horseMap.set(id, { name: (r.name as string) || "?", sire: (r.sire as string) || "不明", damsire: (r.damsire as string) || "不明", pasts: [] });
    }
    if (horseMap.get(id)!.pasts.length >= 3) continue;
    const past = `  ${r.race_date || "?"}: タイム${r.finish_time ?? "?"} 通過${r.passage_rank ?? "?"} 上り3F${r.last_3furlong ?? "?"} ${r.race_comment || ""}`;
    horseMap.get(id)!.pasts.push(past);
  }

  // 予測前オッズ (actual_resultsがあるならそこから)
  const oddsRows = await tursoQuery(dbUrl, auth,
    `SELECT horse_id, confirmed_odds FROM actual_results WHERE race_id = ?`,
    [raceId]
  );
  const oddsMap = new Map<string, number>();
  for (const r of oddsRows) {
    const o = r.confirmed_odds as number;
    if (o > 0) oddsMap.set(r.horse_id as string, o);
  }

  // プロンプト作成
  const horseEntries = horses.filter((id) => horseMap.has(id)).map((id, i) => {
    const h = horseMap.get(id)!;
    return `馬${i + 1}: ${h.name} 父=${h.sire} 母父=${h.damsire}\n${h.pasts.join("\n")}`;
  });

  if (!horseEntries.length) return c.json({ error: "no horse data" }, 400);

  const prompt = `競馬の専門家として、以下のレースの全出走馬の勝率を算出してください。

レース: ${race.venue} ${race.distance}m 馬場:${race.track_condition}

${horseEntries.join("\n\n")}

指示:
1. 各馬の過去走タイムは異なる距離・馬場のものなので単純比較はできない。通過順位・上がり3ハロン・血統（父/母父）から総合的に判断すること。
2. オッズ情報は一切参照しない。
3. 全馬の勝率合計がちょうど1.0になるよう小数点4桁で算出せよ。
4. 推論理由を簡潔に述べよ。

以下のJSON形式のみで回答:
{
  "horses": [
    {"horse_index": 1, "win_probability": 0.1234, "reasoning": "理由..."},
    ...
  ]
}`;

  const model = "deepseek/deepseek-v4-flash";

  let result;
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 50000);
    const data = await callOpenRouter(apiKey, model, "JSON only, no markdown.", prompt, 4096, controller.signal);
    clearTimeout(timeout);

    const content = data.choices?.[0]?.message?.content || "";
    const jsonMatch = content.match(/\{[\s\S]*\}/);
    if (jsonMatch) {
      result = { model, prediction: JSON.parse(jsonMatch[0]) };
    }
  } catch (e) {
    console.error(`${model}: ${e}`);
  }

  if (!result) return c.json({ error: "all models failed" }, 500);

  // 正規化
  const horses2: { horse_index: number; win_probability: number; reasoning: string }[] = result.prediction.horses || [];
  const totalP = horses2.reduce((s: number, h: { win_probability: number }) => s + h.win_probability, 0);
  if (totalP > 0 && Math.abs(totalP - 1) > 0.01) {
    for (const h of horses2) {
      h.win_probability = Number((h.win_probability / totalP).toFixed(4));
    }
  }

  // 保存
  const stmts = horses2.map((hp: { horse_index: number; win_probability: number; reasoning: string }) => {
    const idx = hp.horse_index - 1;
    const hid = horses[idx];
    const odds = oddsMap.get(hid) || 0;
    const ev = odds > 0 ? Number((hp.win_probability * odds).toFixed(4)) : null;
    return {
      sql: "INSERT INTO predictions (race_id, horse_id, win_probability, reasoning_logic, odds_at_prediction, expected_value, model_name, recommended) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
      args: [raceId, hid, hp.win_probability, hp.reasoning, odds || null, ev, result.model, (ev !== null && ev > 1.25 ? 1 : 0)] as (string | number | null)[],
    };
  });

  // 古い予測削除
  await tursoQuery(dbUrl, auth, "DELETE FROM predictions WHERE race_id = ? AND model_name = ?", [raceId, result.model]);

  await tursoBatch(dbUrl, auth, stmts);

  return c.json({
    status: "ok",
    model: result.model,
    horses: horses2.length,
    probSum: Number(totalP.toFixed(4)),
    recommended: stmts.filter((s) => s.args[7] === 1).length,
  });
});

// =========================================================================
// POST /results-collect — 結果収集 + Brier Score
// =========================================================================

app.post("/results-collect", async (c) => {
  let payload: { race_id: string; force?: boolean };
  try { payload = await c.req.json(); } catch { return c.json({ error: "invalid json" }, 400); }

  const raceId = payload.race_id;
  const dbUrl = c.env.TURSO_DATABASE_URL;
  const auth = c.env.TURSO_AUTH_TOKEN;

  // レース確認
  const raceRows = await tursoQuery(dbUrl, auth, "SELECT * FROM races WHERE id = ?", [raceId]);
  if (!raceRows.length) return c.json({ error: "race not found" }, 404);

  // netkeiba 結果ページ取得
  let html = "";
  try {
    const resp = await fetch(`https://race.netkeiba.com/race/result.html?race_id=${raceId}`, {
      headers: { "User-Agent": "Mozilla/5.0", "Accept-Language": "ja" },
    });
    const buf = await resp.arrayBuffer();
    html = new TextDecoder("euc-jp").decode(buf);
  } catch (e) {
    return c.json({ error: `fetch failed: ${e}` }, 502);
  }

  const results: { horse_id: string; finish_order: number; confirmed_odds: number | null }[] = [];
  const horseListRegex = /<tr[^>]*class="[^"]*HorseList[^"]*"[^>]*>([\s\S]*?)<\/tr>/gi;
  let match;
  while ((match = horseListRegex.exec(html)) !== null) {
    const row = match[1];

    const idMatch = row.match(/horse\/(\d+)/);
    const horseId = idMatch ? idMatch[1] : "";

    const orderMatch = row.match(/<td[^>]*class="[^"]*Result_Num[^"]*"[^>]*>[\s\S]*?(\d+)[\s\S]*?<\/td>/i);
    const finishOrder = orderMatch ? parseInt(orderMatch[1]) : 0;

    // オッズ: OddsPeople (人気) と Odds_Ninki/無印span (オッズ値) の2つがある
    // 2つ目のOdds tdから数値を抽出
    const oddsTdMatches = row.match(/<td[^>]*class="[^"]*Odds[^"]*"[^>]*>([\s\S]*?)<\/td>/gi);
    let confirmedOdds: number | null = null;
    if (oddsTdMatches && oddsTdMatches.length >= 2) {
      const secondOddsTd = oddsTdMatches[1];
      const spanMatch = secondOddsTd.match(/<span[^>]*?>([\d.]+)<\/span>/i);
      if (spanMatch) {
        confirmedOdds = parseFloat(spanMatch[1]);
      }
    }

    if (horseId && finishOrder > 0) {
      results.push({ horse_id: horseId, finish_order: finishOrder, confirmed_odds: confirmedOdds });
    }
  }

  if (!results.length) return c.json({ error: "no results found", htmlSnippet: html.slice(0, 500) });

  // ラップタイム抽出
  let lapTimes: string | null = null;
  const paceMatch = html.match(/RapPace_Title[^>]*>[^<]*<span[^>]*>([^<]+)<\/span>/i);
  const pace = paceMatch ? paceMatch[1].trim() : "";
  const tableMatch = html.match(/<table[^>]*summary="ラップタイム"[^>]*>([\s\S]*?)<\/table>/i);
  if (tableMatch) {
    const tbody = tableMatch[1];
    const checks = [...tbody.matchAll(/<th[^>]*>([^<]+)<\/th>/gi)].map((m: RegExpMatchArray) => m[1].trim());
    const rows = [...tbody.matchAll(/<tr[^>]*class="[^"]*HaronTime[^"]*"[^>]*>([\s\S]*?)<\/tr>/gi)];
    const cumulative = rows.length > 0
      ? [...rows[0][1].matchAll(/<td[^>]*>([^<]+)<\/td>/gi)].map((m: RegExpMatchArray) => m[1].trim())
      : [];
    const splits = rows.length > 1
      ? [...rows[1][1].matchAll(/<td[^>]*>([^<]+)<\/td>/gi)].map((m: RegExpMatchArray) => m[1].trim())
      : [];
    lapTimes = JSON.stringify({ pace, checkpoints: checks, cumulative, splits });
  }

  // 既存 predictions の取得
  const predictionRows = await tursoQuery(dbUrl, auth,
    "SELECT p.horse_id, MAX(p.win_probability) as win_probability FROM predictions p WHERE p.race_id = ? GROUP BY p.horse_id",
    [raceId]
  );
  const predMap = new Map<string, number>();
  for (const r of predictionRows) {
    predMap.set(r.horse_id as string, r.win_probability as number);
  }

  // 保存
  const stmts = results.map((r) => {
    const winner = r.finish_order === 1 ? 1 : 0;
    const winP = predMap.get(r.horse_id);
    const brier = winP !== undefined ? Number(((winP - winner) ** 2).toFixed(6)) : null;
    return {
      sql: "INSERT OR REPLACE INTO actual_results (race_id, horse_id, finish_order, confirmed_odds, hit, brier_score) VALUES (?, ?, ?, ?, ?, ?)",
      args: [raceId, r.horse_id, r.finish_order, r.confirmed_odds, winner, brier] as (string | number | null)[],
    };
  });

  stmts.push({
    sql: "UPDATE races SET result_confirmed = 1, lap_times = ? WHERE id = ?",
    args: [lapTimes, raceId],
  });

  await tursoBatch(dbUrl, auth, stmts);
  return c.json({ status: "ok", saved: results.length, hits: results.filter((r) => r.finish_order === 1).length });
});

// =========================================================================
// GET /dashboard/* — ダッシュボードAPI
// =========================================================================

const dashCors = cors({ origin: "*" });

app.get("/dashboard/races", dashCors, async (c) => {
  const rows = await tursoQuery(c.env.TURSO_DATABASE_URL, c.env.TURSO_AUTH_TOKEN,
    `SELECT r.id as raceId, r.date, r.venue, r.distance, r.track_condition,
      r.result_confirmed as resultConfirmed,
      COUNT(DISTINCT h.id) as horseCount
     FROM races r
     LEFT JOIN actual_results ar ON ar.race_id = r.id
     LEFT JOIN horses h ON h.id IN (SELECT horse_id FROM actual_results WHERE race_id = r.id)
     GROUP BY r.id
     ORDER BY r.date DESC, r.venue, r.id`
  );
  return c.json({ items: rows });
});

app.get("/dashboard/stats", dashCors, async (c) => {
  const dbUrl = c.env.TURSO_DATABASE_URL;
  const auth = c.env.TURSO_AUTH_TOKEN;

  const [raceCount] = await tursoQuery(dbUrl, auth, "SELECT COUNT(*) as cnt FROM races");
  const [predCount] = await tursoQuery(dbUrl, auth,
    "SELECT COUNT(DISTINCT p.race_id || '-' || p.horse_id) as cnt FROM predictions p"
  );
  const [recRows] = await tursoQuery(dbUrl, auth,
    `SELECT COUNT(*) as bets, ROUND(COALESCE(SUM(CASE WHEN ar.hit = 1 THEN p.odds_at_prediction ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 0), 1) as roi
     FROM predictions p
     LEFT JOIN actual_results ar ON ar.race_id = p.race_id AND ar.horse_id = p.horse_id
        AND ar.id IN (SELECT MAX(a2.id) FROM actual_results a2 GROUP BY a2.race_id, a2.horse_id)
      WHERE p.recommended = 1 AND p.id IN (SELECT MAX(p2.id) FROM predictions p2 GROUP BY p2.race_id, p2.horse_id)`
  );

  return c.json({
    totalRaces: (raceCount?.cnt as number) || 0,
    totalPredictions: (predCount?.cnt as number) || 0,
    recommendedBets: (recRows?.bets as number) || 0,
    roiPercent: (recRows?.roi as number) || 0,
  });
});

app.get("/dashboard/recommended", dashCors, async (c) => {
  const raceIdFilter = c.req.query("race_id");
  const dedup = `p.id IN (SELECT MAX(p2.id) FROM predictions p2 GROUP BY p2.race_id, p2.horse_id)`;
  const whereRace = raceIdFilter ? `AND p.race_id = ?` : "";
  const args = raceIdFilter ? [raceIdFilter] : [];

  const rows = await tursoQuery(c.env.TURSO_DATABASE_URL, c.env.TURSO_AUTH_TOKEN,
    `SELECT p.race_id as raceId, p.horse_id as horseId, h.name as horseName,
      p.win_probability as winProbability, p.odds_at_prediction as odds,
      p.expected_value as expectedValue, p.recommended as recommended,
      p.reasoning_logic as reasoning, p.model_name as model,
      r.date as raceDate, r.venue as venue, r.distance as distance,
      ar.finish_order as result
     FROM predictions p
     JOIN horses h ON h.id = p.horse_id
     JOIN races r ON r.id = p.race_id
      LEFT JOIN actual_results ar ON ar.race_id = p.race_id AND ar.horse_id = p.horse_id
        AND ar.id IN (SELECT MAX(a2.id) FROM actual_results a2 GROUP BY a2.race_id, a2.horse_id)
      WHERE ${dedup} ${whereRace}
     ORDER BY p.win_probability DESC
     LIMIT 100`,
    args
  );
  return c.json({ items: rows });
});

app.get("/dashboard/brier", dashCors, async (c) => {
  const rows = await tursoQuery(c.env.TURSO_DATABASE_URL, c.env.TURSO_AUTH_TOKEN,
    `SELECT r.date as date, p.model_name as model, COUNT(*) as predictions,
      AVG(ar.brier_score) as avgBrier,
      SUM(CASE WHEN ar.hit = 1 THEN 1 ELSE 0 END) as hits
     FROM predictions p
     JOIN races r ON r.id = p.race_id
      JOIN actual_results ar ON ar.race_id = p.race_id AND ar.horse_id = p.horse_id
        AND ar.id IN (SELECT MAX(a2.id) FROM actual_results a2 GROUP BY a2.race_id, a2.horse_id)
      WHERE p.id IN (SELECT MAX(p2.id) FROM predictions p2 GROUP BY p2.race_id, p2.horse_id)
      GROUP BY r.date, p.model_name
     ORDER BY r.date DESC`
  );
  return c.json({ items: rows });
});

app.get("/dashboard/roi", dashCors, async (c) => {
  const rows = await tursoQuery(c.env.TURSO_DATABASE_URL, c.env.TURSO_AUTH_TOKEN,
    `SELECT r.date as date, COUNT(*) as totalBets,
      ROUND(COALESCE(SUM(CASE WHEN ar.hit = 1 THEN p.odds_at_prediction ELSE 0 END), 0), 1) as totalReturn,
      ROUND(COALESCE(SUM(CASE WHEN ar.hit = 1 THEN p.odds_at_prediction ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 0), 1) as roiPercent,
      SUM(CASE WHEN ar.hit = 1 THEN 1 ELSE 0 END) as wins
     FROM predictions p
     JOIN races r ON r.id = p.race_id
      JOIN actual_results ar ON ar.race_id = p.race_id AND ar.horse_id = p.horse_id
        AND ar.id IN (SELECT MAX(a2.id) FROM actual_results a2 GROUP BY a2.race_id, a2.horse_id)
      WHERE p.recommended = 1 AND p.id IN (SELECT MAX(p2.id) FROM predictions p2 GROUP BY p2.race_id, p2.horse_id)
      GROUP BY r.date
     ORDER BY r.date DESC`
  );
  return c.json({ items: rows });
});

// =========================================================================
// POST /admin/* — 管理エンドポイント
// =========================================================================

app.post("/admin/reset-race", async (c) => {
  let payload: { race_id: string };
  try { payload = await c.req.json(); } catch { return c.json({ error: "invalid json" }, 400); }
  const { race_id: rid } = payload;
  const dbUrl = c.env.TURSO_DATABASE_URL;
  const auth = c.env.TURSO_AUTH_TOKEN;

  await tursoQuery(dbUrl, auth, "DELETE FROM predictions WHERE race_id = ?", [rid]);
  await tursoQuery(dbUrl, auth, "DELETE FROM actual_results WHERE race_id = ?", [rid]);
  await tursoQuery(dbUrl, auth, "UPDATE races SET result_confirmed = 0 WHERE id = ?", [rid]);

  return c.json({ status: "ok", race_id: rid });
});

app.post("/admin/cleanup-dupes", async (c) => {
  let payload: { race_id?: string };
  try { payload = await c.req.json(); } catch { payload = {}; }
  const dbUrl = c.env.TURSO_DATABASE_URL;
  const auth = c.env.TURSO_AUTH_TOKEN;

  const where = payload.race_id ? "WHERE race_id = ?" : "";
  const args = payload.race_id ? [payload.race_id] : [];

  await tursoQuery(dbUrl, auth,
    `DELETE FROM predictions WHERE id NOT IN (SELECT MAX(id) FROM predictions ${where} GROUP BY race_id, horse_id, model_name)`,
    args
  );
  await tursoQuery(dbUrl, auth,
    `DELETE FROM actual_results WHERE id NOT IN (SELECT MAX(id) FROM actual_results ${where ? where : ""} GROUP BY race_id, horse_id)`,
    args
  );

  return c.json({ status: "ok" });
});

// =========================================================================
// POST /admin/query — SQLデバッグ
// =========================================================================

app.post("/admin/query", async (c) => {
  let payload: { sql: string; args?: (string | number | null)[] };
  try { payload = await c.req.json(); } catch { return c.json({ error: "invalid json" }, 400); }
  const rows = await tursoQuery(c.env.TURSO_DATABASE_URL, c.env.TURSO_AUTH_TOKEN, payload.sql, payload.args || []);
  return c.json({ rows });
});

// =========================================================================
// GET /health
// =========================================================================

app.get("/health", async (c) => {
  return c.json({
    ok: true,
    hasOpenRouter: !!c.env.OPENROUTER_API_KEY,
    hasTurso: !!(c.env.TURSO_DATABASE_URL && c.env.TURSO_AUTH_TOKEN),
  });
});

export default app;
