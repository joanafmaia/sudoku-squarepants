/**
 * Discord Embedded App SDK bootstrap for Thcoku.
 * Initializes the Discord session as soon as the page loads, then starts PyScript.
 */
import { DiscordSDK } from "@discord/embedded-app-sdk";

const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;
const bootEl = document.getElementById("boot");
const statusEl = document.getElementById("boot-status");
const appEl = document.getElementById("app");

function setStatus(message) {
  if (statusEl) statusEl.textContent = message;
}

function showGame() {
  if (bootEl) bootEl.hidden = true;
  if (appEl) appEl.hidden = false;
}

function startPyGame() {
  if (document.querySelector('script[type="py-game"]')) return;

  const script = document.createElement("script");
  script.type = "py-game";
  script.src = "/game/main.py";
  script.setAttribute("config", "/game/pyscript.toml");
  script.setAttribute("target", "canvas");
  appEl.appendChild(script);
}

function launchLocal(reason) {
  console.info("[Thcoku]", reason);
  window.__DISCORD_AUTH__ = null;
  window.__DISCORD_SDK__ = null;
  setStatus(reason);
  showGame();
  startPyGame();
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    }),
  ]);
}

/**
 * Full OAuth handshake when running inside Discord.
 * Outside Discord (local browser preview), we skip authorize and just load the game.
 */
async function setupDiscordSdk() {
  if (!CLIENT_ID || CLIENT_ID === "YOUR_DISCORD_CLIENT_ID_HERE") {
    launchLocal("VITE_DISCORD_CLIENT_ID em falta — modo local.");
    return;
  }

  const discordSdk = new DiscordSDK(CLIENT_ID);
  window.__DISCORD_SDK__ = discordSdk;

  setStatus("A aguardar handshake do Discord…");
  try {
    await withTimeout(discordSdk.ready(), 4000, "discordSdk.ready()");
  } catch (err) {
    launchLocal(
      "Sem frame Discord (pré-visualização local). A carregar o jogo…"
    );
    return;
  }

  setStatus("A pedir autorização…");
  const { code } = await discordSdk.commands.authorize({
    client_id: CLIENT_ID,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify", "guilds"],
  });

  setStatus("A trocar o código por token…");
  // Inside Discord Activities, API calls go through the /.proxy/ path.
  const response = await fetch("/.proxy/api/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });

  if (!response.ok) {
    // Fallback for local tunnel setups that map /api without /.proxy
    const fallback = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (!fallback.ok) {
      throw new Error(`Token exchange failed (${response.status})`);
    }
    const { access_token } = await fallback.json();
    const auth = await discordSdk.commands.authenticate({ access_token });
    window.__DISCORD_AUTH__ = auth;
    setStatus(`Ligado como ${auth?.user?.username ?? "jogador"}. A carregar…`);
    showGame();
    startPyGame();
    return;
  }

  const { access_token } = await response.json();

  setStatus("A autenticar sessão…");
  const auth = await discordSdk.commands.authenticate({ access_token });
  window.__DISCORD_AUTH__ = auth;

  const name = auth?.user?.username ?? "jogador";
  setStatus(`Ligado como ${name}. A carregar o jogo…`);
  showGame();
  startPyGame();
}

setupDiscordSdk().catch((err) => {
  console.error(err);
  launchLocal(
    `Falha no Discord SDK: ${err?.message ?? err}. A abrir o jogo na mesma…`
  );
});
