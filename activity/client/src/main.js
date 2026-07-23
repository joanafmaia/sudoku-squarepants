/**
 * Discord Embedded App SDK bootstrap for Thcoku.
 * Initializes Discord session, then starts the Canvas puzzle (no leaderboard UI).
 * Saves in-progress boards to Mongo and offers Resume / New puzzle on next /play.
 */
import { DiscordSDK } from "@discord/embedded-app-sdk";
import { startThcokuGame } from "./game.js";
import { difficultyLabel } from "./sudoku-core.js";

const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;
const bootEl = document.getElementById("boot");
const statusEl = document.getElementById("boot-status");
const winToastEl = document.getElementById("win-toast");
const gameHintEl = document.getElementById("game-hint");
const resumeEl = document.getElementById("resume");
const resumeCopyEl = document.getElementById("resume-copy");
const resumeContinueBtn = document.getElementById("resume-continue");
const resumeNewBtn = document.getElementById("resume-new");

let gameStarted = false;
let gameApi = null;
let autosaveTimer = null;
let saving = false;

function setStatus(message) {
  if (statusEl) statusEl.textContent = message;
}

function guildId() {
  return window.__DISCORD_SDK__?.guildId || "0";
}

function playerName() {
  return (
    window.__DISCORD_AUTH__?.user?.global_name ||
    window.__DISCORD_AUTH__?.user?.username ||
    undefined
  );
}

function startGameOnce(cosmetics = null, gameOptions = {}) {
  if (gameStarted) {
    if (cosmetics && gameApi?.setCosmetics) gameApi.setCosmetics(cosmetics);
    return gameApi;
  }
  gameStarted = true;
  const canvas = document.getElementById("canvas");
  try {
    gameApi = startThcokuGame(canvas, {
      cosmetics: cosmetics || { title: null, pins: [], seed: 1 },
      autoStart: gameOptions.autoStart !== false,
      onNewGame: () => {
        clearSavedSession();
      },
    });
    if (gameHintEl) gameHintEl.hidden = true;
  } catch (err) {
    console.error(err);
    if (gameHintEl) {
      gameHintEl.hidden = false;
      gameHintEl.textContent = `Failed to start: ${err?.message || err}`;
    }
  }
  return gameApi;
}

async function loadCosmetics() {
  if (!window.__DISCORD_ACCESS_TOKEN__) return null;
  try {
    const res = await apiFetch(`/api/activity/profile?guild_id=${encodeURIComponent(guildId())}`);
    if (!res || !res.ok) return null;
    const data = await res.json();
    return {
      title: data.title || null,
      pins: Array.isArray(data.pins) ? data.pins : [],
      seed: Number(data.user_id) || Date.now(),
    };
  } catch (err) {
    console.warn("[Thcoku] profile load failed", err);
    return null;
  }
}

async function loadSavedSession() {
  if (!window.__DISCORD_ACCESS_TOKEN__) return null;
  try {
    const res = await apiFetch(`/api/activity/session?guild_id=${encodeURIComponent(guildId())}`);
    if (!res || !res.ok) return null;
    const data = await res.json();
    const session = data?.session;
    if (!session?.board || !session?.given || !session?.solution) return null;
    if ((session.filled || 0) <= 0) return null;
    return session;
  } catch (err) {
    console.warn("[Thcoku] session load failed", err);
    return null;
  }
}

async function clearSavedSession() {
  if (!window.__DISCORD_ACCESS_TOKEN__) return;
  const gid = encodeURIComponent(guildId());
  try {
    let res = await apiFetch(`/api/activity/session?guild_id=${gid}`, { method: "DELETE" });
    if (res && (res.ok || res.status === 401 || res.status === 404)) return;
    await apiFetch("/api/activity/session", {
      method: "POST",
      body: JSON.stringify({ clear: true, guild_id: guildId() }),
    });
  } catch (err) {
    console.warn("[Thcoku] session clear failed", err);
  }
}

async function saveSessionNow() {
  if (!window.__DISCORD_ACCESS_TOKEN__ || !gameApi?.getSnapshot || saving) return;
  const snap = gameApi.getSnapshot();
  if (!snap) return;
  saving = true;
  try {
    await apiFetch("/api/activity/session", {
      method: "POST",
      body: JSON.stringify({
        ...snap,
        guild_id: guildId(),
        name: playerName(),
      }),
    });
  } catch (err) {
    console.warn("[Thcoku] session save failed", err);
  } finally {
    saving = false;
  }
}

function startAutosave() {
  stopAutosave();
  if (!window.__DISCORD_ACCESS_TOKEN__) return;
  autosaveTimer = setInterval(() => {
    saveSessionNow();
  }, 12000);
  const flush = () => {
    saveSessionNow();
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
  window.addEventListener("pagehide", flush);
  window.addEventListener("beforeunload", flush);
}

function stopAutosave() {
  if (autosaveTimer) {
    clearInterval(autosaveTimer);
    autosaveTimer = null;
  }
}

function askResume(session) {
  return new Promise((resolve) => {
    if (!resumeEl) {
      resolve(true);
      return;
    }
    const filled = session.filled ?? "?";
    const diff = difficultyLabel(session.difficulty || "medium");
    const t = formatTime(session.elapsed);
    if (resumeCopyEl) {
      resumeCopyEl.textContent = `Krabby Patty mid-cook (${diff}) · ${filled}/81 · ${t}. Resume or start a new order?`;
    }
    resumeEl.hidden = false;
    if (gameHintEl) gameHintEl.hidden = true;

    const done = (resume) => {
      resumeEl.hidden = true;
      resumeContinueBtn?.removeEventListener("click", onContinue);
      resumeNewBtn?.removeEventListener("click", onNew);
      resolve(resume);
    };
    const onContinue = () => done(true);
    const onNew = () => done(false);
    resumeContinueBtn?.addEventListener("click", onContinue);
    resumeNewBtn?.addEventListener("click", onNew);
  });
}

async function beginPlay({ resumeSession = null } = {}) {
  const cosmetics = await loadCosmetics();
  if (resumeSession) {
    startGameOnce(cosmetics, { autoStart: false });
    if (!gameApi?.loadSnapshot?.(resumeSession)) {
      gameApi?.newGame?.();
    }
  } else {
    startGameOnce(cosmetics, { autoStart: true });
  }
  if (cosmetics && gameApi?.setCosmetics) gameApi.setCosmetics(cosmetics);
  startAutosave();
}

async function showGame() {
  if (bootEl) bootEl.hidden = true;

  if (!window.__DISCORD_ACCESS_TOKEN__) {
    startGameOnce(null, { autoStart: true });
    return;
  }

  if (gameHintEl) {
    gameHintEl.hidden = false;
    gameHintEl.textContent = "Checking saved progress…";
  }

  const session = await loadSavedSession();
  if (session) {
    const resume = await askResume(session);
    if (resume) {
      await beginPlay({ resumeSession: session });
    } else {
      await clearSavedSession();
      await beginPlay({ resumeSession: null });
    }
    return;
  }

  await beginPlay({ resumeSession: null });
}

/** When Activities map `/api` → host, Discord strips `/api`, so `/api/token` becomes `/token`. */
function apiUrlCandidates(path) {
  const clean = path.startsWith("/") ? path : `/${path}`;
  const urls = [];
  const push = (u) => {
    if (u && !urls.includes(u)) urls.push(u);
  };
  const inFrame = Boolean(window.__DISCORD_IN_CLIENT__ || window.__DISCORD_SDK__);
  if (inFrame) {
    push(`/.proxy${clean}`);
    if (clean.startsWith("/api/")) {
      push(`/.proxy${clean.slice(4)}`);
    }
  }
  push(clean);
  if (clean.startsWith("/api/")) {
    push(clean.slice(4));
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

function showWinToast(message) {
  if (!winToastEl) return;
  winToastEl.hidden = false;
  winToastEl.textContent = message;
  winToastEl.style.animation = "none";
  void winToastEl.offsetWidth;
  winToastEl.style.animation = "";
  clearTimeout(showWinToast._t);
  showWinToast._t = setTimeout(() => {
    winToastEl.hidden = true;
  }, 6500);
}

/** Called from the Canvas game after a solved board. */
window.thcokuReportWin = async function thcokuReportWin(difficulty, elapsed, boardPayload) {
  if (!window.__DISCORD_ACCESS_TOKEN__) {
    showWinToast("Local win (no Discord auth — XP not saved).");
    return null;
  }
  try {
    const sdk = window.__DISCORD_SDK__;
    const res = await apiFetch("/api/activity/win", {
      method: "POST",
      body: JSON.stringify({
        difficulty,
        elapsed: Math.floor(Number(elapsed) || 0),
        guild_id: sdk?.guildId ?? "0",
        channel_id: sdk?.channelId ?? null,
        name: playerName(),
        board: boardPayload?.board ?? null,
        given: boardPayload?.given ?? null,
        solution: boardPayload?.solution ?? null,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showWinToast(`Win OK, but Mongo failed (${data.error || res.status}).`);
      return null;
    }
    const posted = data.posted ? " · photo in chat" : "";
    showWinToast(
      `Order up! +${data.xp} XP · +${data.coins} sponges · streak ${data.streak} · ${formatTime(
        elapsed
      )}${posted}`
    );
    return data;
  } catch (err) {
    console.error(err);
    showWinToast("Win OK, but could not save to Mongo.");
    return null;
  }
};

function finishBoot(auth, accessToken, { inDiscord }) {
  window.__DISCORD_AUTH__ = auth;
  window.__DISCORD_ACCESS_TOKEN__ = accessToken || null;
  window.__DISCORD_IN_CLIENT__ = Boolean(inDiscord);
  showGame();
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
    "/.proxy/token",
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
    launchLocal("VITE_DISCORD_CLIENT_ID missing — local mode.");
    return;
  }

  const discordSdk = new DiscordSDK(CLIENT_ID);
  window.__DISCORD_SDK__ = discordSdk;

  setStatus("Waiting for Discord handshake…");
  try {
    await withTimeout(discordSdk.ready(), 4000, "discordSdk.ready()");
  } catch {
    launchLocal("No Discord frame (local preview). Loading game…");
    return;
  }

  setStatus("Requesting authorization…");
  const { code } = await discordSdk.commands.authorize({
    client_id: CLIENT_ID,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify", "guilds"],
  });

  setStatus("Exchanging code for token…");
  const access_token = await exchangeToken(code);

  setStatus("Authenticating session…");
  const auth = await discordSdk.commands.authenticate({ access_token });
  const name = auth?.user?.username ?? "player";
  setStatus(`Signed in as ${name}. Loading game…`);
  finishBoot(auth, access_token, { inDiscord: true });
}

setupDiscordSdk().catch((err) => {
  console.error(err);
  const raw = String(err?.message ?? err);
  const tip = /redirect_uri/i.test(raw)
    ? "In Developer Portal → OAuth2 → Redirects, add https://127.0.0.1 and save. Then restart the Activity."
    : raw;
  launchLocal(`Discord SDK failed: ${tip} Opening the game anyway…`);
});
