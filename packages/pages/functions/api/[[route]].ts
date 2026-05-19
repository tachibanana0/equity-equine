import { Hono } from "hono";

type Bindings = {
  TURSO_DATABASE_URL: string;
  TURSO_AUTH_TOKEN: string;
};

const app = new Hono<{ Bindings: Bindings }>();

function tursoURL(bindings: Bindings): string {
  let url = bindings.TURSO_DATABASE_URL;
  if (url.startsWith("libsql://")) url = url.replace("libsql://", "https://");
  return `${url}/v2/pipeline`;
}

async function query(bindings: Bindings, sql: string, args: unknown[] = []) {
  const resp = await fetch(tursoURL(bindings), {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${bindings.TURSO_AUTH_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      requests: [{ type: "execute", stmt: { sql, args } }],
    }),
  });
  const data = await resp.json() as Record<string, unknown>;
  const results = (data as any).results;
  if (!results || !results[0] || results[0].type !== "ok") {
    throw new Error(`Turso error: ${JSON.stringify(data).slice(0, 200)}`);
  }
  const cols = (results[0].response?.result?.cols || []).map((c: any) => c.name);
  const rows = (results[0].response?.result?.rows || []).map((row: any[]) =>
    Object.fromEntries(cols.map((name: string, i: number) => [name, row[i]]))
  );
  return rows;
}

app.get("/api/recommended", async (c) => {
  try {
    const rows = await query(c.env, `
      SELECT
        p.race_id as raceId,
        p.horse_id as horseId,
        h.name as horseName,
        p.win_probability as winProbability,
        p.odds_at_prediction as odds,
        p.expected_value as expectedValue,
        p.reasoning_logic as reasoning,
        p.model_name as model,
        r.date as raceDate,
        r.venue as venue,
        r.distance as distance,
        ar.finish_order as result,
        p.created_at as predictedAt
      FROM predictions p
      JOIN horses h ON h.id = p.horse_id
      JOIN races r ON r.id = p.race_id
      LEFT JOIN actual_results ar ON ar.race_id = p.race_id AND ar.horse_id = p.horse_id
      WHERE p.recommended = 1
      ORDER BY p.expected_value DESC
      LIMIT 100
    `);
    return c.json({ items: rows });
  } catch (e) {
    return c.json({ error: String(e), items: [] }, 500);
  }
});

app.get("/api/brier", async (c) => {
  try {
    const rows = await query(c.env, `
      SELECT
        r.date as date,
        p.model_name as model,
        COUNT(*) as predictions,
        AVG(ar.brier_score) as avgBrier,
        SUM(CASE WHEN ar.hit = 1 THEN 1 ELSE 0 END) as hits,
        COUNT(ar.id) as results
      FROM predictions p
      JOIN races r ON r.id = p.race_id
      LEFT JOIN actual_results ar ON ar.race_id = p.race_id AND ar.horse_id = p.horse_id
      WHERE ar.brier_score IS NOT NULL
      GROUP BY r.date, p.model_name
      ORDER BY r.date DESC
      LIMIT 200
    `);
    return c.json({ items: rows });
  } catch (e) {
    return c.json({ error: String(e), items: [] }, 500);
  }
});

app.get("/api/roi", async (c) => {
  try {
    const rows = await query(c.env, `
      SELECT
        r.date as date,
        COUNT(*) as totalBets,
        SUM(CASE WHEN ar.hit = 1 THEN p.odds_at_prediction ELSE 0 END) as totalReturn,
        ROUND(SUM(CASE WHEN ar.hit = 1 THEN p.odds_at_prediction ELSE 0 END) * 100.0 / COUNT(*), 1) as roiPercent,
        SUM(CASE WHEN ar.hit = 1 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN ar.hit = 1 THEN p.expected_value ELSE 0 END) as totalEV
      FROM predictions p
      JOIN races r ON r.id = p.race_id
      LEFT JOIN actual_results ar ON ar.race_id = p.race_id AND ar.horse_id = p.horse_id
      WHERE p.recommended = 1 AND ar.id IS NOT NULL
      GROUP BY r.date
      ORDER BY r.date DESC
      LIMIT 200
    `);
    return c.json({ items: rows });
  } catch (e) {
    return c.json({ error: String(e), items: [] }, 500);
  }
});

app.get("/api/stats", async (c) => {
  try {
    const [races] = await query(c.env, "SELECT COUNT(*) as cnt FROM races");
    const [predictions] = await query(c.env, "SELECT COUNT(*) as cnt FROM predictions");
    const [roiRow] = await query(c.env, `
      SELECT
        COUNT(*) as bets,
        ROUND(SUM(CASE WHEN ar.hit = 1 THEN p.odds_at_prediction ELSE 0 END) * 100.0 / COUNT(*), 1) as roi
      FROM predictions p
      LEFT JOIN actual_results ar ON ar.race_id = p.race_id AND ar.horse_id = p.horse_id
      WHERE p.recommended = 1 AND ar.id IS NOT NULL
    `);
    return c.json({
      totalRaces: (races as any)?.cnt || 0,
      totalPredictions: (predictions as any)?.cnt || 0,
      recommendedBets: (roiRow as any)?.bets || 0,
      roiPercent: (roiRow as any)?.roi || 0,
    });
  } catch (e) {
    return c.json({
      totalRaces: 0, totalPredictions: 0, recommendedBets: 0, roiPercent: 0, error: String(e),
    }, 500);
  }
});

app.get("/api/debug", (c) => {
  return c.json({
    hasUrl: typeof c.env.TURSO_DATABASE_URL === "string",
    urlPrefix: typeof c.env.TURSO_DATABASE_URL === "string" ? c.env.TURSO_DATABASE_URL.slice(0, 20) : "MISSING",
    hasToken: typeof c.env.TURSO_AUTH_TOKEN === "string",
    envKeys: Object.keys(c.env),
    branch: c.env.CF_PAGES_BRANCH,
    url: c.env.CF_PAGES_URL,
  });
});

export const onRequest = (ctx: { request: Request; env: Bindings }) => {
  return app.fetch(ctx.request, ctx.env);
};
