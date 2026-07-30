/**
 * Shared Mongo helpers for Netlify Functions.
 * Uses the same leaderboard document shape as challenge_store.MongoMatchStore:
 *   collection "leaderboard", doc { _id: "main", data: {...}, updated_at }
 */

import { MongoClient } from "mongodb";

let cachedClient = null;

export function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  };
}

export function json(statusCode, body) {
  return {
    statusCode,
    headers: {
      "Content-Type": "application/json",
      ...corsHeaders(),
    },
    body: JSON.stringify(body),
  };
}

export async function getDb() {
  const uri = (process.env.MONGODB_URI || "").trim();
  if (!uri) {
    throw new Error("MONGODB_URI missing");
  }
  const dbName = (process.env.MONGODB_DB || "sudoku").trim() || "sudoku";

  if (!cachedClient) {
    cachedClient = new MongoClient(uri);
    await cachedClient.connect();
  }
  return cachedClient.db(dbName);
}

export async function loadLeaderboardData() {
  const db = await getDb();
  const doc = await db.collection("leaderboard").findOne({ _id: "main" });
  if (!doc || typeof doc.data !== "object" || doc.data === null) {
    return {};
  }
  return doc.data;
}

export async function saveLeaderboardData(data) {
  const db = await getDb();
  await db.collection("leaderboard").replaceOne(
    { _id: "main" },
    { _id: "main", data, updated_at: Date.now() / 1000 },
    { upsert: true }
  );
}

export async function discordUserFromBearer(event) {
  const header = event.headers?.authorization || event.headers?.Authorization || "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  if (!match) {
    return null;
  }
  const accessToken = match[1].trim();
  const res = await fetch("https://discord.com/api/users/@me", {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!res.ok) {
    return null;
  }
  return res.json();
}

export function ensureUserStats(gstats, userId) {
  const key = String(userId);
  if (!gstats[key] || typeof gstats[key] !== "object") {
    gstats[key] = {};
  }
  const s = gstats[key];
  s.coins = Number(s.coins) || 0;
  s.xp = Number(s.xp) || 0;
  s.wins = Number(s.wins) || 0;
  s.games = Number(s.games) || 0;
  s.losses = Number(s.losses) || 0;
  s.streak = Number(s.streak) || 0;
  s.best_streak = Number(s.best_streak) || 0;
  s.best_time = s.best_time == null ? null : Number(s.best_time);
  s.longest_time = s.longest_time == null ? null : Number(s.longest_time);
  if (s.longest_time == null && s.best_time != null) {
    s.longest_time = s.best_time;
  }
  s.name = s.name || "Unknown";
  return s;
}

export function collectTopXp(data, { guildId = null, limit = 10 } = {}) {
  const rows = [];
  for (const [gid, gstats] of Object.entries(data || {})) {
    if (gid.startsWith("_")) continue;
    if (typeof gstats !== "object" || gstats === null) continue;
    if (guildId != null && String(gid) !== String(guildId)) continue;
    for (const [uid, stats] of Object.entries(gstats)) {
      if (uid.startsWith("_")) continue;
      if (typeof stats !== "object" || stats === null) continue;
      rows.push({
        guild_id: gid,
        user_id: uid,
        name: stats.name || "Unknown",
        xp: Number(stats.xp) || 0,
        coins: Number(stats.coins) || 0,
        wins: Number(stats.wins) || 0,
        streak: Number(stats.streak) || 0,
        best_time: stats.best_time == null ? null : Number(stats.best_time),
        longest_time:
          stats.longest_time == null
            ? stats.best_time == null
              ? null
              : Number(stats.best_time)
            : Number(stats.longest_time),
      });
    }
  }
  rows.sort((a, b) => b.xp - a.xp);
  return rows.slice(0, limit);
}
