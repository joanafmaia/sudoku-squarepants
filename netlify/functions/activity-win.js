import {
  activityWinReward,
  corsHeaders,
  discordUserFromBearer,
  ensureUserStats,
  json,
  loadLeaderboardData,
  saveLeaderboardData,
} from "./_shared.js";

export async function handler(event) {
  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 204, headers: corsHeaders(), body: "" };
  }
  if (event.httpMethod !== "POST") {
    return json(405, { error: "method_not_allowed" });
  }

  const user = await discordUserFromBearer(event);
  if (!user?.id) {
    return json(401, { error: "unauthorized" });
  }

  let body;
  try {
    body = JSON.parse(event.body || "{}");
  } catch {
    return json(400, { error: "invalid_json" });
  }

  const difficulty = body.difficulty || "medium";
  const elapsed = Math.max(0, Math.floor(Number(body.elapsed) || 0));
  // Prefer the Discord guild where the Activity is running; fall back to "0" (global/activity).
  const guildId = String(body.guild_id ?? "0");
  const displayName =
    body.name || user.global_name || user.username || "Unknown";

  try {
    const data = await loadLeaderboardData();
    if (!data[guildId] || typeof data[guildId] !== "object") {
      data[guildId] = {};
    }
    const gstats = data[guildId];
    const stats = ensureUserStats(gstats, user.id);

    stats.name = displayName;
    stats.wins += 1;
    stats.games += 1;
    stats.streak += 1;
    stats.best_streak = Math.max(stats.best_streak, stats.streak);
    if (stats.best_time == null || elapsed < stats.best_time) {
      stats.best_time = elapsed;
    }

    const coins = activityWinReward(stats.streak, difficulty);
    const xp = coins;
    stats.coins += coins;
    stats.xp = (Number(stats.xp) || 0) + xp;
    stats.activity_wins = (Number(stats.activity_wins) || 0) + 1;
    stats.last_activity_win_at = Date.now() / 1000;

    await saveLeaderboardData(data);

    return json(200, {
      ok: true,
      coins,
      xp,
      streak: stats.streak,
      career_xp: stats.xp,
      pocket: stats.coins,
      best_time: stats.best_time,
      elapsed,
      difficulty,
      guild_id: guildId,
      user_id: String(user.id),
    });
  } catch (err) {
    console.error(err);
    return json(500, { error: "win_failed", message: String(err.message || err) });
  }
}
