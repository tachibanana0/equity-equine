import { Hono } from "hono";
import { createClient } from "@libsql/client/web";
import { drizzle } from "drizzle-orm/libsql";
import * as schema from "./schema";
import { eq, inArray } from "drizzle-orm";

type Bindings = {
  OPENROUTER_API_KEY: string;
  API_SECRET: string;
  TURSO_DATABASE_URL: string;
  TURSO_AUTH_TOKEN: string;
};

type HorseInput = {
  horse_id: string;
  odds: number;
};

type RaceInput = {
  race_id: string;
  horses: HorseInput[];
};

type Payload = {
  races?: RaceInput[];
  race_ids?: string[];
  race_ids_order?: string[];
};

type RaceHorseData = {
  horseId: string;
  horseName: string;
  odds: number;
  sire: string;
  damsire: string;
  pastResults: {
    raceDate: string;
    finishTime: number | null;
    passageRank: string | null;
    last3Furlong: number | null;
    raceComment: string | null;
    structuredComment: string | null;
  }[];
};

const app = new Hono<{ Bindings: Bindings }>();

function getDB(bindings: Bindings) {
  const client = createClient({
    url: bindings.TURSO_DATABASE_URL,
    authToken: bindings.TURSO_AUTH_TOKEN,
  });
  return drizzle(client, { schema });
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
  return crypto.subtle.verify(
    "HMAC",
    key,
    sigBytes,
    encoder.encode(body)
  );
}

function buildPredictionPrompt(raceData: { venue: string; distance: number; trackCondition: string }, horses: RaceHorseData[]): string {
  const horseEntries = horses.map((h, i) => {
    const pastStr = h.pastResults
      .map(
        (p) =>
          `  ${p.raceDate}: タイム${p.finishTime ?? "?"}秒 通過${p.passageRank ?? "?"} 上り3F${p.last3Furlong ?? "?"} ${p.raceComment ?? ""}`
      )
      .join("\n");

    const structuredStr = h.pastResults
      .filter((p) => p.structuredComment)
      .map((p) => {
        try {
          const sc = JSON.parse(p.structuredComment!);
          return Object.entries(sc)
            .filter(([, v]) => v)
            .map(([k, v]) => `  ${k}: ${v}`)
            .join("\n");
        } catch {
          return "";
        }
      })
      .filter(Boolean)
      .join("\n");

    return `馬${i + 1}: ID=${h.horseId} 馬名=${h.horseName} 父=${h.sire} 母父=${h.damsire}
過去戦績:
${pastStr}
${structuredStr ? "分析要素:\n" + structuredStr : ""}`;
  }).join("\n\n");

  return `あなたは競馬の専門家です。以下のレース情報と出走馬の過去データのみから、各馬の勝率を計算してください。

レース情報: ${raceData.venue} ${raceData.distance}m 馬場:${raceData.trackCondition}

${horseEntries}

指示:
1. 戦績、血統（父・母父）、通過順位、上がり3ハロン、不利要素（出遅れ/進路不利/展開不向き）だけを考慮する
2. オッズ情報は一切参照しない
3. 全馬の勝率の合計がちょうど1.0（100%）になるように小数点以下4桁で算出する
4. 推論ロジックを簡潔に説明する

以下のJSON形式で回答してください:
{
  "horses": [
    {
      "horse_index": 1,
      "win_probability": 0.1234,
      "reasoning": "この馬の推論理由..."
    }
  ]
}`;
}

async function fetchHorseData(
  db: ReturnType<typeof getDB>,
  horseIds: string[]
): Promise<Map<string, { name: string; sire: string | null; damsire: string | null; pastResults: typeof schema.pastResults.$inferSelect[] }>> {
  const rows = await db
    .select()
    .from(schema.horses)
    .leftJoin(schema.pastResults, eq(schema.horses.id, schema.pastResults.horseId))
    .where(inArray(schema.horses.id, horseIds));

  const map = new Map<string, { name: string; sire: string | null; damsire: string | null; pastResults: typeof schema.pastResults.$inferSelect[] }>();
  for (const row of rows) {
    if (!map.has(row.horses.id)) {
      map.set(row.horses.id, {
        name: row.horses.name,
        sire: row.horses.sire,
        damsire: row.horses.damsire,
        pastResults: [],
      });
    }
    if (row.past_results) {
      map.get(row.horses.id)!.pastResults.push(row.past_results);
    }
  }
  return map;
}

app.post("/predict", async (c) => {
  const body = await c.req.text();
  const signature = c.req.header("X-API-Signature");

  if (!signature) {
    return c.json({ error: "missing signature" }, 401);
  }

  const isLocal = c.env.API_SECRET === "";
  const secret = isLocal ? "dev-secret" : c.env.API_SECRET;
  const valid = await verifySignature(body, signature, secret);
  if (!valid && !isLocal) {
    return c.json({ error: "invalid signature" }, 403);
  }

  let payload: Payload;
  try {
    payload = JSON.parse(body);
  } catch {
    return c.json({ error: "invalid json" }, 400);
  }

  // サポートする2種類のリクエスト形式:
  //   1. { "races": [{ "race_id": "...", "horses": [{"horse_id": "...", "odds": 1.5}] }] }
  //   2. { "race_ids": ["..."] }  ← 後方互換 (odds なし)
  const raceEntries: { race_id: string; horse_ids: string[]; oddsMap: Map<string, number> }[] = [];

  if (payload.races && payload.races.length > 0) {
    for (const r of payload.races) {
      const oddsMap = new Map<string, number>();
      const ids: string[] = [];
      for (const h of r.horses) {
        ids.push(h.horse_id);
        if (h.odds > 0) oddsMap.set(h.horse_id, h.odds);
      }
      raceEntries.push({ race_id: r.race_id, horse_ids: ids, oddsMap });
    }
  } else {
    const raceIds = payload.race_ids || payload.race_ids_order || [];
    for (const rid of raceIds) {
      raceEntries.push({ race_id: rid, horse_ids: [], oddsMap: new Map() });
    }
  }

  if (raceEntries.length === 0) {
    return c.json({ error: "no race data" }, 400);
  }

  const db = getDB(c.env);
  const results: string[] = [];

  for (const entry of raceEntries) {
    try {
      const raceRows = await db
        .select()
        .from(schema.races)
        .where(eq(schema.races.id, entry.race_id))
        .limit(1);

      if (raceRows.length === 0) {
        results.push(`${entry.race_id}: race not found`);
        continue;
      }
      const race = raceRows[0];

      if (entry.horse_ids.length === 0) {
        results.push(`${entry.race_id}: skipped (no horse IDs)`);
        continue;
      }
      const horseIds = entry.horse_ids;

      const horseDataMap = await fetchHorseData(db, horseIds);

      if (horseDataMap.size === 0) {
        results.push(`${entry.race_id}: no horse data found`);
        continue;
      }

      const horsesForPrompt: RaceHorseData[] = horseIds
        .filter((id) => horseDataMap.has(id))
        .map((id) => {
          const h = horseDataMap.get(id)!;
          return {
            horseId: id,
            horseName: h.name,
            odds: entry.oddsMap.get(id) || 0,
            sire: h.sire ?? "不明",
            damsire: h.damsire ?? "不明",
            pastResults: h.pastResults.map((p) => ({
              raceDate: p.raceDate,
              finishTime: p.finishTime,
              passageRank: p.passageRank,
              last3Furlong: p.last3Furlong,
              raceComment: p.raceComment,
              structuredComment: p.structuredComment,
            })),
          };
        });

      const prompt = buildPredictionPrompt(
        { venue: race.venue, distance: race.distance, trackCondition: race.trackCondition },
        horsesForPrompt
      );

      const openrouterKey = c.env.OPENROUTER_API_KEY || "";
      const isLocalDev = openrouterKey === "";

      interface PredictionOutput {
        horse_index: number;
        win_probability: number;
        reasoning: string;
      }

      let prediction: { horses: PredictionOutput[] };

      if (isLocalDev) {
        const n = horsesForPrompt.length;
        const equalP = 1.0 / n;
        prediction = {
          horses: horsesForPrompt.map((h, i) => ({
            horse_index: i + 1,
            win_probability: Number(equalP.toFixed(4)),
            reasoning: `[DEV] 均等割り: 1/${n}`,
          })),
        };
      } else {
        let retries = 0;
        const maxRetries = 3;
        let aiContent = "";

        while (retries < maxRetries) {
          try {
            const res = await fetch("https://openrouter.ai/api/v1/chat/completions", {
              method: "POST",
              headers: {
                "Authorization": `Bearer ${openrouterKey}`,
                "Content-Type": "application/json",
              },
              body: JSON.stringify({
                model: "deepseek/deepseek-v4-pro",
                messages: [
                  { role: "system", content: "You are a horse racing expert. Respond ONLY with valid JSON. No markdown." },
                  { role: "user", content: prompt },
                ],
                temperature: 0.1,
                max_tokens: 4096,
              }),
            });
            const data = await res.json() as { choices?: { message?: { content?: string } }[] };
            aiContent = data.choices?.[0]?.message?.content || "";
            const jsonMatch = aiContent.match(/\{[\s\S]*\}/);
            if (jsonMatch) {
              prediction = JSON.parse(jsonMatch[0]);
              break;
            }
          } catch {
            // retry
          }
          retries++;
        }

        if (!prediction!) {
          results.push(`${entry.race_id}: AI prediction failed (retries: ${maxRetries})`);
          continue;
        }
      }

      const modelName = isLocalDev ? "dev-equal-distribution" : "deepseek/deepseek-v4-pro";
      let recommendedCount = 0;

      for (const hp of prediction.horses) {
        const horseEntry = horsesForPrompt[hp.horse_index - 1];
        if (!horseEntry) continue;

        const oddsAtPrediction = horseEntry.odds;
        const expectedValue = oddsAtPrediction > 0
          ? Number((hp.win_probability * oddsAtPrediction).toFixed(4))
          : null;
        const recommended = expectedValue !== null && expectedValue > 1.25;
        if (recommended) recommendedCount++;

        await db.insert(schema.predictions).values({
          raceId: entry.race_id,
          horseId: horseEntry.horseId,
          winProbability: hp.win_probability,
          reasoningLogic: hp.reasoning,
          oddsAtPrediction: oddsAtPrediction || null,
          expectedValue,
          modelName,
          recommended: recommended ? true : false,
        });
      }

      results.push(`${entry.race_id}: ${prediction.horses.length} predicted, ${recommendedCount} recommended`);
    } catch (err) {
      results.push(`${entry.race_id}: error - ${String(err).slice(0, 100)}`);
    }
  }

  return c.json({ status: "ok", results });
});

app.get("/health", (c) => c.json({ ok: true }));

export default app;
