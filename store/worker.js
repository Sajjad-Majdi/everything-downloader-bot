// Durable state for the downloader bot (Render free wipes its disk on every deploy/restart):
// public seats (first come, first served) and the monthly outbound byte counter.
// Every request needs the header x-store-secret (Worker secret STORE_SECRET; the bot derives
// the same value from its token). D1 keeps seat grants and byte increments atomic.
//
//   GET  /state                                   -> {seats, usage, limits}
//   POST /sync {seats_add, max, month, add}       -> {seats, usage, limits, granted}
//
// seats_add ids are inserted only while fewer than `max` seats exist; `add` bytes are added to
// `month` ("YYYY-MM", UTC). limits (optional overrides, set by hand with wrangler d1 execute):
// keys seats, soft_gb, hard_gb.

// Tables: schema.sql (applied once with `wrangler d1 execute downloader-store --remote --file schema.sql`).

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

async function authorized(req, env) {
  const got = new TextEncoder().encode(req.headers.get("x-store-secret") || "");
  const want = new TextEncoder().encode(env.STORE_SECRET || "");
  if (!want.length || got.length !== want.length) return false;
  return crypto.subtle.timingSafeEqual(got, want);
}

async function state(db) {
  const [seats, usage, limits] = await db.batch([
    db.prepare("SELECT user_id FROM seats ORDER BY ts, user_id"),
    db.prepare("SELECT month, bytes FROM usage ORDER BY month DESC LIMIT 3"),
    db.prepare("SELECT k, v FROM limits"),
  ]);
  return {
    seats: seats.results.map((r) => r.user_id),
    usage: Object.fromEntries(usage.results.map((r) => [r.month, r.bytes])),
    limits: Object.fromEntries(limits.results.map((r) => [r.k, r.v])),
  };
}

export default {
  async fetch(req, env) {
    if (!(await authorized(req, env))) return json({ error: "forbidden" }, 403);
    const db = env.DB;
    const path = new URL(req.url).pathname;
    if (req.method === "GET" && path === "/state") return json(await state(db));
    if (req.method === "POST" && path === "/sync") {
      let body;
      try {
        body = await req.json();
      } catch {
        return json({ error: "bad json" }, 400);
      }
      const stmts = [];
      const now = Date.now();
      const max = Number.isInteger(body.max) ? body.max : 0;
      for (const id of Array.isArray(body.seats_add) ? body.seats_add : []) {
        if (!Number.isSafeInteger(id)) continue;
        stmts.push(db.prepare(
          "INSERT OR IGNORE INTO seats (user_id, ts) SELECT ?1, ?2 WHERE (SELECT COUNT(*) FROM seats) < ?3"
        ).bind(id, now, max));
      }
      const add = Number(body.add) || 0;
      if (add > 0 && /^\d{4}-\d{2}$/.test(body.month || "")) {
        stmts.push(db.prepare(
          "INSERT INTO usage (month, bytes) VALUES (?1, ?2) ON CONFLICT(month) DO UPDATE SET bytes = bytes + ?2"
        ).bind(body.month, Math.round(add)));
      }
      if (stmts.length) await db.batch(stmts);
      const out = await state(db);
      out.granted = (body.seats_add || []).filter((id) => out.seats.includes(id));
      return json(out);
    }
    return json({ error: "not found" }, 404);
  },
};
