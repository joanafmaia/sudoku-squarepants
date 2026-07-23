/**
 * Netlify Function — troca o código OAuth do Discord por access_token.
 * Variáveis (Netlify → Site settings → Environment variables):
 *   VITE_DISCORD_CLIENT_ID  (ou DISCORD_CLIENT_ID)
 *   DISCORD_CLIENT_SECRET
 */
export async function handler(event) {
  if (event.httpMethod === "OPTIONS") {
    return {
      statusCode: 204,
      headers: corsHeaders(),
      body: "",
    };
  }

  if (event.httpMethod !== "POST") {
    return json(405, { error: "method_not_allowed" });
  }

  let code;
  try {
    code = JSON.parse(event.body || "{}").code;
  } catch {
    return json(400, { error: "invalid_json" });
  }

  if (!code) {
    return json(400, { error: "missing_code" });
  }

  const clientId =
    process.env.VITE_DISCORD_CLIENT_ID || process.env.DISCORD_CLIENT_ID;
  const clientSecret = process.env.DISCORD_CLIENT_SECRET;

  if (!clientId || !clientSecret) {
    return json(500, { error: "server_misconfigured" });
  }

  const response = await fetch("https://discord.com/api/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      grant_type: "authorization_code",
      code,
      redirect_uri: process.env.DISCORD_OAUTH_REDIRECT_URI || "https://127.0.0.1",
    }),
  });

  const data = await response.json();
  if (!response.ok) {
    return json(response.status, data);
  }

  return json(200, { access_token: data.access_token });
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
  };
}

function json(statusCode, body) {
  return {
    statusCode,
    headers: {
      "Content-Type": "application/json",
      ...corsHeaders(),
    },
    body: JSON.stringify(body),
  };
}
