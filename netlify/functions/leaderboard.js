import {
  collectTopXp,
  corsHeaders,
  json,
  loadLeaderboardData,
} from "./_shared.js";

export async function handler(event) {
  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 204, headers: corsHeaders(), body: "" };
  }
  if (event.httpMethod !== "GET") {
    return json(405, { error: "method_not_allowed" });
  }

  try {
    const params = event.queryStringParameters || {};
    const guildId = params.guild_id || null;
    const limit = Math.min(50, Math.max(1, Number(params.limit) || 10));
    const data = await loadLeaderboardData();
    const top = collectTopXp(data, { guildId, limit });
    return json(200, { top, guild_id: guildId, updated: true });
  } catch (err) {
    console.error(err);
    return json(500, { error: "leaderboard_failed", message: String(err.message || err) });
  }
}
