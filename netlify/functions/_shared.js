/**
 * Shared Mongo helpers for Netlify Functions.
 * Uses the same leaderboard document shape as challenge_store.MongoMatchStore:
 *   collection "leaderboard", doc { _id: "main", data: {...}, updated_at }
 */

import { MongoClient } from "mongodb";

const DIFFICULTY_MULT = {
  very_easy: 0.5,
  easy: 0.75,
  medium: 1,
  hard: 1.5,
  very_hard: 2,
  expertttt: 3,
};

const BASE_WIN_REWARD = 50;
const STREAK_BONUS_PER = 5;

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
  s.name = s.name || "Unknown";
  return s;
}

export function difficultyMultiplier(difficulty) {
  if (!difficulty) return 1;
  if (DIFFICULTY_MULT[difficulty] != null) return DIFFICULTY_MULT[difficulty];
  const lower = String(difficulty).toLowerCase().replace(/\s+/g, "_");
  if (DIFFICULTY_MULT[lower] != null) return DIFFICULTY_MULT[lower];
  for (const [key, meta] of Object.entries({
    very_easy: "Very Easy",
    easy: "Easy",
    medium: "Medium",
    hard: "Hard",
    very_hard: "Very Hard",
    expertttt: "Expertttt",
  })) {
    if (meta.toLowerCase() === String(difficulty).toLowerCase()) {
      return DIFFICULTY_MULT[key];
    }
  }
  return 1;
}

/** Solo Activity win reward (mirrors bot win_reward without daily/challenge). */
export function activityWinReward(streak, difficulty) {
  let coins = BASE_WIN_REWARD + Math.max(0, streak - 1) * STREAK_BONUS_PER;
  coins = Math.round(coins * difficultyMultiplier(difficulty));
  return Math.max(20, coins);
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
      });
    }
  }
  rows.sort((a, b) => b.xp - a.xp);
  return rows.slice(0, limit);
}
