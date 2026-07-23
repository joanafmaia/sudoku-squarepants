/**
 * Discord Embedded App SDK bootstrap for Thcoku.
 * Initializes Discord session, wires Mongo APIs, then starts PyScript.
 */
import { DiscordSDK } from "@discord/embedded-app-sdk";

const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;
const bootEl = document.getElementById("boot");
const statusEl = document.getElementById("boot-status");
const appEl = document.getElementById("app");
const lbListEl = document.getElementById("lb-list");
const winToastEl = document.getElementById("win-toast");

function setStatus(message) {
  if (statusEl) statusEl.textContent = message;
}

function showGame() {
  if (bootEl) bootEl.hidden = true;
  if (appEl) appEl.hidden = false;
}

function apiUrl(path) {
  // Discord Activity iframe uses the mapped proxy; local Vite uses /api via proxy or Netlify.
  const clean = path.startsWith("/") ? path : `/${path}`;
  if (window.__DISCORD_IN_CLIENT__) {
    return `/.proxy${clean}`;
  }
  return clean;
}

/** When Activities map `/api` → host, Discord strips `/api`, so `/api/token` becomes `/token`. */
function apiUrlCandidates(path) {
  const clean = path.startsWith("/") ? path : `/${path}`;
  const urls = [];
  const push = (u) => {
    if (u && !urls.includes(u)) urls.push(u);
  };
  // Inside the Activity iframe (auth may still be pending during token exchange).
  const inFrame = Boolean(window.__DISCORD_IN_CLIENT__ || window.__DISCORD_SDK__);
  if (inFrame) {
    push(`/.proxy${clean}`);
    if (clean.startsWith("/api/")) {
      push(`/.proxy${clean.slice(4)}`); // /api/token → /.proxy/token
    }
  }
  push(clean);
  if (clean.startsWith("/api/")) {
    push(clean.slice(4)); // /api/token → /token
  }
  return urls;
}

async function apiFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const token = window.__DISCORD_ACCESS_TOKEN__;
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  let last = null;
  for (const url of apiUrlCandidates(path)) {
    last = await fetch(url, { ...options, headers });
    if (last.status !== 404) return last;
  }
  return last;
}

function formatTime(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

async function refreshLeaderboard() {
  if (!lbListEl) return;
  lbListEl.textContent = "A carregar…";
  try {
    const guildId = window.__DISCORD_SDK__?.guildId;
    const qs = guildId ? `?guild_id=${encodeURIComponent(guildId)}&limit=10` : "?limit=10";
    const res = await apiFetch(`/api/leaderboard${qs}`);
    if (!res.ok) {
      lbListEl.textContent = "Leaderboard indisponível";
      return;
    }
    const payload = await res.json();
    const top = payload.top || [];
    if (!top.length) {
      lbListEl.textContent = "Ainda sem XP — resolve um puzzle!";
      return;
    }
    lbListEl.innerHTML = top
      .map(
        (row, i) =>
          `<li><span class="rank">#${i + 1}</span> <strong>${escapeHtml(
            row.name
          )}</strong> · ${row.xp} XP · ${row.wins} wins</li>`
      )
      .join("");
  } catch (err) {
    console.error(err);
    lbListEl.textContent = "Falha ao ler Mongo";
  }
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showWinToast(message) {
  if (!winToastEl) return;
  winToastEl.hidden = false;
  winToastEl.textContent = message;
  // retrigger CSS animation
  winToastEl.style.animation = "none";
  void winToastEl.offsetWidth;
  winToastEl.style.animation = "";
  clearTimeout(showWinToast._t);
  showWinToast._t = setTimeout(() => {
    winToastEl.hidden = true;
  }, 6500);
}

/** Called from PyScript/Pygame after a solved board. */
window.thcokuReportWin = async function thcokuReportWin(difficulty, elapsed) {
  if (!window.__DISCORD_ACCESS_TOKEN__) {
    showWinToast("Vitória local (sem Discord auth — XP não gravado).");
    return null;
  }
  try {
    const res = await apiFetch("/api/activity/win", {
      method: "POST",
      body: JSON.stringify({
        difficulty,
        elapsed: Math.floor(Number(elapsed) || 0),
        guild_id: window.__DISCORD_SDK__?.guildId ?? "0",
        name:
          window.__DISCORD_AUTH__?.user?.global_name ||
          window.__DISCORD_AUTH__?.user?.username ||
          undefined,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showWinToast(`Vitória OK, mas Mongo falhou (${data.error || res.status}).`);
      return null;
    }
    showWinToast(
      `+${data.xp} XP · +${data.coins} sponges · streak ${data.streak} · ${formatTime(
        elapsed
      )}`
    );
    refreshLeaderboard();
    return data;
  } catch (err) {
    console.error(err);
    showWinToast("Vitória OK, mas não foi possível gravar no Mongo.");
    return null;
  }
};

window.thcokuRefreshLeaderboard = refreshLeaderboard;

function startPyGame() {
  if (document.querySelector('script[type="py-game"]')) return;

  const script = document.createElement("script");
  script.type = "py-game";
  script.src = "/game/main.py";
  script.setAttribute("config", "/game/pyscript.toml");
  script.setAttribute("target", "canvas");
  appEl.appendChild(script);
}

function finishBoot(auth, accessToken, { inDiscord }) {
  window.__DISCORD_AUTH__ = auth;
  window.__DISCORD_ACCESS_TOKEN__ = accessToken || null;
  window.__DISCORD_IN_CLIENT__ = Boolean(inDiscord);
  showGame();
  startPyGame();
  refreshLeaderboard();
}

function launchLocal(reason) {
  console.info("[Thcoku]", reason);
  window.__DISCORD_SDK__ = null;
  setStatus(reason);
  finishBoot(null, null, { inDiscord: false });
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    }),
  ]);
}

async function exchangeToken(code) {
  const body = JSON.stringify({ code });
  const headers = { "Content-Type": "application/json" };
  let response = null;
  for (const url of [
    "/.proxy/api/token",
    "/.proxy/token", // Discord /api URL mapping strips the /api prefix
    "/api/token",
    "/token",
  ]) {
    response = await fetch(url, { method: "POST", headers, body });
    if (response.status !== 404) break;
  }
  if (!response || !response.ok) {
    let detail = "";
    try {
      const errBody = await response.clone().json();
      detail =
        errBody.error_description ||
        errBody.message ||
        errBody.error ||
        errBody.body ||
        "";
    } catch {
      /* ignore */
    }
    const status = response?.status ?? "network";
    throw new Error(
      detail
        ? `Token exchange failed (${status}: ${detail})`
        : `Token exchange failed (${status})`
    );
  }
  const { access_token } = await response.json();
  return access_token;
}

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
  } catch {
    launchLocal("Sem frame Discord (pré-visualização local). A carregar o jogo…");
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
  const access_token = await exchangeToken(code);

  setStatus("A autenticar sessão…");
  const auth = await discordSdk.commands.authenticate({ access_token });
  const name = auth?.user?.username ?? "jogador";
  setStatus(`Ligado como ${name}. A carregar o jogo…`);
  finishBoot(auth, access_token, { inDiscord: true });
}

document.getElementById("lb-refresh")?.addEventListener("click", () => {
  refreshLeaderboard();
});

setupDiscordSdk().catch((err) => {
  console.error(err);
  const raw = String(err?.message ?? err);
  const tip = /redirect_uri/i.test(raw)
    ? 'No Developer Portal → OAuth2 → Redirects, adiciona https://127.0.0.1 e grava. Depois reinicia a Activity.'
    : raw;
  launchLocal(`Falha no Discord SDK: ${tip} A abrir o jogo na mesma…`);
});
