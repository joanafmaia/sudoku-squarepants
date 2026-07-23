import express from "express";
import dotenv from "dotenv";
import fetch from "node-fetch";

dotenv.config({ path: "../.env" });

const app = express();
const port = Number(process.env.PORT || 3001);

app.use(express.json());

app.get("/health", (_req, res) => {
  res.type("text").send("ok");
});

/**
 * Exchange the OAuth authorization code (from DiscordSDK.commands.authorize)
 * for an access_token. Never expose DISCORD_CLIENT_SECRET to the browser.
 */
app.post("/api/token", async (req, res) => {
  const code = req.body?.code;
  if (!code) {
    res.status(400).json({ error: "missing_code" });
    return;
  }

  const clientId = process.env.VITE_DISCORD_CLIENT_ID;
  const clientSecret = process.env.DISCORD_CLIENT_SECRET;
  if (!clientId || !clientSecret) {
    res.status(500).json({ error: "server_misconfigured" });
    return;
  }

  const response = await fetch("https://discord.com/api/oauth2/token", {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      grant_type: "authorization_code",
      code,
    }),
  });

  const data = await response.json();
  if (!response.ok) {
    res.status(response.status).json(data);
    return;
  }

  res.json({ access_token: data.access_token });
});

app.listen(port, () => {
  console.log(`Thcoku Activity server listening at http://localhost:${port}`);
});
