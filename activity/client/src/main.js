/**
 * Discord Embedded App SDK bootstrap for Thcoku.
 * Initializes Discord session, then starts the Canvas puzzle (no leaderboard UI).
 * Saves in-progress boards to Mongo and offers Resume / New puzzle on next /play.
 */
import { DiscordSDK, RPCCloseCodes } from "@discord/embedded-app-sdk";
import {
  startThcokuGame,
  isMusicEnabled,
  isSfxEnabled,
  cycleMusicPreset,
  getMusicPresetMeta,
  setSfxEnabled,
} from "./game.js";
import { pauseProceduralBgm, resumeProceduralBgm } from "./procedural-bgm.js";
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
let exitHooksBound = false;
let sessionOpenedAt = 0;
let hideEndWatchTimer = null;
let spectating = false;
let spectatorPollTimer = null;
let watcherPollTimer = null;
const SPECTATOR_POLL_MS = 3500;
const WATCHERS_POLL_MS = 5000;
const HIDE_END_WATCH_DELAY_MS = 5000;

function setStatus(message) {
  if (statusEl) statusEl.textContent = message;
}

function guildId() {
  const id = window.__DISCORD_SDK__?.guildId;
  if (id == null || id === "") return "0";
  return String(id);
}

async function resolveGuildId(maxWaitMs = 8000) {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    const id = window.__DISCORD_SDK__?.guildId;
    if (id != null && id !== "" && String(id) !== "0") {
      return String(id);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return guildId();
}

async function buildSessionPayload(snap) {
  const gid = await resolveGuildId();
  return {
    ...snap,
    guild_id: gid,
    channel_id: channelId(),
    name: playerName(),
  };
}

function userId() {
  return window.__DISCORD_AUTH__?.user?.id || "local";
}

let cachedGuildId = null;

function localSessionStorageKey(gid) {
  return `thcoku_session_v1:${gid}:${userId()}`;
}

function localSessionKey() {
  return localSessionStorageKey(cachedGuildId || guildId());
}

function readLocalSessionForGuild(gid) {
  const keys = [localSessionStorageKey(gid)];
  if (gid && gid !== "0") {
    keys.push(localSessionStorageKey("0"));
  }
  for (const key of keys) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      const session = JSON.parse(raw);
      if (!session?.board || !session?.given) continue;
      return session;
    } catch {
      /* try next key */
    }
  }
  return null;
}

function channelId() {
  return window.__DISCORD_SDK__?.channelId || null;
}

function playerName() {
  return (
    window.__DISCORD_AUTH__?.user?.global_name ||
    window.__DISCORD_AUTH__?.user?.username ||
    undefined
  );
}

async function sessionPayloadAsync(snap) {
  const gid = await resolveGuildId(8000);
  cachedGuildId = gid;
  return {
    ...snap,
    guild_id: gid,
    channel_id: channelId(),
    name: playerName(),
  };
}

function sessionPayload(snap) {
  return {
    ...snap,
    guild_id: cachedGuildId || guildId(),
    channel_id: channelId(),
    name: playerName(),
  };
}

function writeLocalSession(snap) {
  if (!snap) return;
  try {
    localStorage.setItem(
      localSessionKey(),
      JSON.stringify({ ...snap, saved_at: Date.now() })
    );
  } catch (err) {
    console.warn("[Thcoku] local session write failed", err);
  }
}

function readLocalSession() {
  return readLocalSessionForGuild(cachedGuildId || guildId());
}

function clearLocalSession() {
  const keys = new Set([localSessionKey(), localSessionStorageKey("0")]);
  const gid = cachedGuildId || guildId();
  if (gid) keys.add(localSessionStorageKey(gid));
  // Sweep other guild keys for this user to avoid stale resume after server switch.
  try {
    const uid = userId();
    const prefix = `thcoku_session_v1:`;
    const suffix = uid ? `:${uid}` : null;
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith(prefix)) continue;
      if (suffix && key.endsWith(suffix)) keys.add(key);
    }
  } catch {
    /* ignore */
  }
  for (const key of keys) {
    try {
      localStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  }
}

function givenFingerprint(given) {
  if (!Array.isArray(given) || given.length !== 9) return "";
  const parts = [];
  for (let r = 0; r < 9; r++) {
    const row = given[r];
    if (!Array.isArray(row) || row.length !== 9) return "";
    for (let c = 0; c < 9; c++) {
      // Boolean mask: true = clue. Digits live on board; compare mask + board clue cells below.
      parts.push(row[c] ? "1" : "0");
    }
  }
  return parts.join("");
}

function playPuzzleCompatible(remote, local) {
  if (!remote?.given || !local?.given) return false;
  if (!Array.isArray(remote.given) || !Array.isArray(local.given)) return false;
  if (remote.given.length !== 9 || local.given.length !== 9) return false;
  for (let r = 0; r < 9; r++) {
    if (!Array.isArray(remote.given[r]) || remote.given[r].length !== 9) return false;
    if (!Array.isArray(local.given[r]) || local.given[r].length !== 9) return false;
  }
  if (givenFingerprint(remote.given) !== givenFingerprint(local.given)) return false;
  // Also compare clue digits where both mark a cell as given.
  for (let r = 0; r < 9; r++) {
    for (let c = 0; c < 9; c++) {
      if (!remote.given[r][c] || !local.given[r][c]) continue;
      const rv = remote.board?.[r]?.[c]?.value ?? remote.board?.[r]?.[c] ?? 0;
      const lv = local.board?.[r]?.[c]?.value ?? local.board?.[r]?.[c] ?? 0;
      if (Number(rv) !== Number(lv)) return false;
    }
  }
  return true;
}

function sessionsCompatible(remote, local) {
  if (!remote || !local) return false;
  const rk = remote.session_kind || "play";
  const lk = local.session_kind || "play";
  if (rk !== lk) return false;
  if (rk === "daily") {
    if (remote.daily_date && local.daily_date) {
      if (String(remote.daily_date) !== String(local.daily_date)) return false;
    } else if (!playPuzzleCompatible(remote, local)) {
      // Legacy local without daily_date — only merge if same puzzle clues.
      return false;
    }
  }
  if (rk === "challenge") {
    if (remote.match_id && local.match_id) {
      if (String(remote.match_id) !== String(local.match_id)) return false;
    } else if (!playPuzzleCompatible(remote, local)) {
      return false;
    }
  }
  if (rk === "play" || (!remote.session_kind && !local.session_kind)) {
    if (!playPuzzleCompatible(remote, local)) return false;
  }
  // Daily/challenge with matching ids still need matching clues before board merge.
  if ((rk === "daily" || rk === "challenge") && remote.given && local.given) {
    if (!playPuzzleCompatible(remote, local)) return false;
  }
  return true;
}

let currentTheme = localStorage.getItem("thcoku_theme") || "light";

const THEMES = ["light", "dark", "jellyfish", "krabs", "rockbottom"];
const THEME_ICONS = {
  light: "☀️",
  dark: "🌙",
  jellyfish: "🪼",
  krabs: "🦀",
  rockbottom: "🌀",
};

function applyTheme(theme) {
  currentTheme = theme;
  try {
    localStorage.setItem("thcoku_theme", theme);
  } catch {
    /* localStorage disabled */
  }
  document.body.className = `theme-${theme}`;
  if (gameApi?.setTheme) {
    gameApi.setTheme(theme);
  }
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.textContent = THEME_ICONS[theme] || "🌙";
    btn.title = `Theme: ${theme.toUpperCase()} (Click to change)`;
  }
}

function applyMusicUi() {
  const btn = document.getElementById("music-toggle");
  if (!btn) return;
  const enabled = isMusicEnabled();
  const meta = getMusicPresetMeta();
  if (!enabled || !meta) {
    btn.textContent = "🔇";
    btn.title = "Ocean ambience off — click to start (Calm lagoon)";
    btn.setAttribute("aria-pressed", "true");
    btn.classList.add("is-muted");
    return;
  }
  btn.textContent = meta.emoji;
  btn.title = `${meta.label} — click for next ambience (or mute)`;
  btn.setAttribute("aria-pressed", "false");
  btn.classList.remove("is-muted");
}

function applySfx(enabled) {
  setSfxEnabled(enabled);
  const btn = document.getElementById("sfx-toggle");
  if (btn) {
    btn.textContent = enabled ? "🔊" : "🔇";
    btn.title = enabled ? "Sons do jogo ligados (clica para desligar)" : "Sons desligados (clica para ligar)";
    btn.setAttribute("aria-pressed", enabled ? "false" : "true");
  }
}

document.getElementById("music-toggle")?.addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  cycleMusicPreset();
  applyMusicUi();
});

document.getElementById("sfx-toggle")?.addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  applySfx(!isSfxEnabled());
});

applyMusicUi();
applySfx(isSfxEnabled());

document.addEventListener("visibilitychange", () => {
  if (!isMusicEnabled()) return;
  if (document.visibilityState === "hidden") pauseProceduralBgm();
  else resumeProceduralBgm();
});

document.getElementById("theme-toggle")?.addEventListener("click", () => {
  const idx = THEMES.indexOf(currentTheme);
  const next = THEMES[(idx + 1) % THEMES.length];
  applyTheme(next);
});

function startGameOnce(cosmetics = null, gameOptions = {}) {
  if (gameStarted) {
    if (cosmetics && gameApi?.setCosmetics) gameApi.setCosmetics(cosmetics);
    if (cosmetics && gameApi?.setPocketSponges) {
      gameApi.setPocketSponges(cosmetics.pocketSponges);
    }
    applyTheme(currentTheme);
    return gameApi;
  }
  gameStarted = true;
  const canvas = document.getElementById("canvas");
  try {
    gameApi = startThcokuGame(canvas, {
      cosmetics: cosmetics || { title: null, pins: [], seed: 1 },
      pocketSponges: Number(cosmetics?.pocketSponges) || 0,
      hintSpongeCost: Number(cosmetics?.hintSpongeCost) || 15,
      autoStart: gameOptions.autoStart !== false,
      sessionKind: gameOptions.sessionKind || null,
      dailyDate: gameOptions.dailyDate || null,
      matchId: gameOptions.matchId || null,
      playerSlot: gameOptions.playerSlot || null,
      initialDiffIndex: gameOptions.initialDiffIndex ?? null,
      onQuit: () => {
        stopWatcherPolling();
        if (spectating) {
          stopSpectatorPolling();
          closeDiscordActivity();
          return;
        }
        quitAndClose();
      },
      onWin: () => {
        stopWatcherPolling();
        endWatchOnExit({ force: true, challengeForfeit: false });
        setTimeout(() => closeDiscordActivity(), 2500);
      },
      onNewGame: () => {
        clearLocalSession();
        // Drop remote session so the next save can authorize a fresh puzzle.
        clearSavedSession();
      },
      onBoardReady: () => {
        saveSessionNow({ force: true });
      },
      onProgress: () => {
        // Persist immediately so Discord "Exit" cannot race the async flush.
        saveSessionNow({ keepalive: false, force: true });
      },
      onHint: async ({ row, col, board }) => {
        if (!window.__DISCORD_ACCESS_TOKEN__) {
          return { ok: false, error: "offline" };
        }
        try {
          const gid = await resolveGuildId(8000);
          cachedGuildId = gid;
          const res = await apiFetch("/api/activity/hint", {
            method: "POST",
            body: JSON.stringify({
              guild_id: gid,
              row,
              col,
              board,
            }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res?.ok) {
            return { ok: false, ...(typeof data === "object" ? data : {}) };
          }
          if (gameApi?.setPocketSponges && data.pocket != null) {
            gameApi.setPocketSponges(data.pocket);
          }
          return data;
        } catch (err) {
          console.warn("[Thcoku] hint request failed", err);
          return { ok: false, error: "hint_failed" };
        }
      },
    });
    applyTheme(currentTheme);
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
    const gid = await resolveGuildId(3000);
    cachedGuildId = gid;
    const res = await apiFetch(`/api/activity/profile?guild_id=${encodeURIComponent(gid)}`);
    if (!res || !res.ok) return null;
    const data = await res.json();
    return {
      title: data.title || null,
      pins: Array.isArray(data.pins) ? data.pins : [],
      seed: Number(data.user_id) || Date.now(),
      pocketSponges: Number(data.coins) || 0,
      hintSpongeCost: Number(data.hint_sponge_cost) || 15,
    };
  } catch (err) {
    console.warn("[Thcoku] profile load failed", err);
    return null;
  }
}

async function loadSavedSession() {
  const gid = window.__DISCORD_ACCESS_TOKEN__
    ? await resolveGuildId(3000)
    : guildId();
  cachedGuildId = gid;
  let remote = null;
  if (window.__DISCORD_ACCESS_TOKEN__) {
    try {
      const res = await apiFetch(`/api/activity/session?guild_id=${encodeURIComponent(gid)}`);
      if (res && res.ok) {
        const data = await res.json();
        const session = data?.session;
        if (session) {
          if (session.won_at) {
            clearLocalSession();
            if (session.diff_index != null) {
              remote = { diff_index: Number(session.diff_index), board: null };
            }
          } else if (session.board && session.given) {
            remote = session;
          } else if (session.diff_index != null) {
            // Preference-only session (no board): used to start at the requested difficulty.
            remote = { diff_index: Number(session.diff_index), board: null };
          }
        }
      }
    } catch (err) {
      console.warn("[Thcoku] session load failed", err);
    }
  }
  const local = readLocalSessionForGuild(gid);
  // Daily / challenge: always prefer the server session (avoid wrong puzzle from localStorage).
  if (remote?.board && (remote.session_kind === "daily" || remote.session_kind === "challenge")) {
    if (local && !sessionsCompatible(remote, local)) {
      clearLocalSession();
    } else if (
      local &&
      sessionsCompatible(remote, local) &&
      remote.session_kind === "daily"
    ) {
      // Daily may merge fresher local fills; challenge always trusts the server board
      // (wrong local merges were overwriting race progress).
      const rFilled = Number(remote.filled) || 0;
      const lFilled = Number(local.filled) || 0;
      if (lFilled > rFilled) {
        return {
          ...remote,
          board: local.board,
          filled: lFilled,
          elapsed: local.elapsed ?? remote.elapsed,
          hints_used: Math.max(
            Number(remote.hints_used) || 0,
            Number(local.hints_used) || 0
          ),
        };
      }
    } else if (local && sessionsCompatible(remote, local) && remote.session_kind === "challenge") {
      // Keep local hints if higher; never replace the server board.
      const hints = Math.max(
        Number(remote.hints_used) || 0,
        Number(local.hints_used) || 0
      );
      if (hints > (Number(remote.hints_used) || 0)) {
        return { ...remote, hints_used: hints };
      }
    }
    return remote;
  }
  // Never resume a local daily/challenge without a matching remote session.
  if (!remote?.board && local && (local.session_kind === "daily" || local.session_kind === "challenge")) {
    clearLocalSession();
    return remote;
  }
  // Prefer the freshest full-board progress for /play; ignore board-less pref when local exists.
  if (remote?.board && local && !remote?.won_at) {
    if (!sessionsCompatible(remote, local)) {
      clearLocalSession();
      return remote;
    }
    const rFilled = Number(remote.filled) || 0;
    const lFilled = Number(local.filled) || 0;
    if (rFilled > lFilled) return remote;
    if (lFilled > rFilled) return local;
    const rTs = Number(remote.updated_at || remote.last_move_at || 0);
    const lTs = Number(local.saved_at || 0);
    return rTs >= lTs ? remote : local;
  }
  return remote || local;
}

async function clearSavedSession() {
  clearLocalSession();
  if (!window.__DISCORD_ACCESS_TOKEN__) return;
  const gid = await resolveGuildId(3000);
  cachedGuildId = gid;
  const encoded = encodeURIComponent(gid);
  try {
    let res = await apiFetch(`/api/activity/session?guild_id=${encoded}`, { method: "DELETE" });
    if (res?.ok) {
      const data = await res.json().catch(() => ({}));
      if (data.cleared !== false) return;
      if (data.reason === "active_challenge") {
        console.info("[Thcoku] session kept — challenge race in progress");
        return;
      }
    }
    if (res && (res.ok || res.status === 401 || res.status === 404)) return;
    res = await apiFetch("/api/activity/session", {
      method: "POST",
      body: JSON.stringify({ clear: true, guild_id: gid }),
    });
    if (res?.ok) {
      const data = await res.json().catch(() => ({}));
      if (data.cleared === false && data.reason === "active_challenge") {
        console.info("[Thcoku] session kept — challenge race in progress");
      }
    }
  } catch (err) {
    console.warn("[Thcoku] session clear failed", err);
  }
}

function currentSessionSnap() {
  // Prefer live progress; getStartSnapshot also works before any moves (watch notify).
  return gameApi?.getSnapshot?.() || gameApi?.getStartSnapshot?.() || null;
}

async function saveSessionNow({ keepalive = false, force = false, snap = null } = {}) {
  if (!snap) {
    snap = currentSessionSnap();
  }
  if (!snap) return;
  writeLocalSession(snap);
  if (!window.__DISCORD_ACCESS_TOKEN__) return;
  if (saving && !force && !keepalive) return;
  saving = true;
  try {
    const payload = await buildSessionPayload(snap);
    const res = await apiFetch("/api/activity/session", {
      method: "POST",
      body: JSON.stringify(payload),
      keepalive,
    });
    if (!res?.ok) {
      const data = await res.json().catch(() => ({}));
      console.warn("[Thcoku] session save failed", res?.status, data.error || data);
      if (data.error === "invalid_board" && (data.challenge || snap.session_kind === "challenge")) {
        // Mistakes mid-race are rejected silently; only reload a "full" forged board.
        const filled = Number(snap.filled) || 0;
        if (filled < 81) return;
        showWinToast("Challenge board mismatch — reloading race puzzle…");
        try {
          clearLocalSession();
          const session = await loadSavedSession();
          if (session?.board && session.session_kind === "challenge") {
            await beginPlay({ resumeSession: session });
          }
        } catch (reloadErr) {
          console.warn("[Thcoku] challenge reload after invalid_board failed", reloadErr);
        }
      }
    }
  } catch (err) {
    console.warn("[Thcoku] session save failed", err);
  } finally {
    saving = false;
  }
}

async function reportSessionActive() {
  if (!window.__DISCORD_ACCESS_TOKEN__ || !gameApi) return;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const snap = currentSessionSnap();
    if (!snap) {
      await new Promise((resolve) => setTimeout(resolve, 400));
      continue;
    }
    await saveSessionNow({ force: true, snap });
    return;
  }
}

function endWatchOnExit({ force = false, challengeForfeit = false } = {}) {
  if (!window.__DISCORD_ACCESS_TOKEN__) return;
  // Fire-and-forget; resolve guild id when possible so we don't target activity:0:uid.
  const post = (gid) => {
    const body = JSON.stringify({
      end_watch: true,
      force,
      challenge_forfeit: challengeForfeit,
      guild_id: gid,
    });
    for (const url of apiUrlCandidates("/api/activity/session")) {
      try {
        fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${window.__DISCORD_ACCESS_TOKEN__}`,
          },
          body,
          keepalive: true,
        });
        break;
      } catch {
        /* try next candidate */
      }
    }
  };
  const immediate = guildId();
  if (immediate && immediate !== "0") {
    post(immediate);
    return;
  }
  resolveGuildId(8000).then(post).catch(() => post(cachedGuildId || guildId()));
}

function scheduleEndWatchOnHide() {
  if (hideEndWatchTimer) clearTimeout(hideEndWatchTimer);
  hideEndWatchTimer = setTimeout(() => {
    hideEndWatchTimer = null;
    if (document.visibilityState === "hidden") {
      // Remove the "is playing" chat post when the Activity closes.
      // Short delay avoids Discord remount flicker right after open.
      endWatchOnExit({ force: true, challengeForfeit: false });
    }
  }, HIDE_END_WATCH_DELAY_MS);
}

function cancelEndWatchOnHide() {
  if (hideEndWatchTimer) {
    clearTimeout(hideEndWatchTimer);
    hideEndWatchTimer = null;
  }
}

function flushSessionOnExit({ endWatch = false } = {}) {
  const snap = currentSessionSnap();
  // Never forfeit on unload/remount — only Quit may.
  const run = async () => {
    if (snap) {
      writeLocalSession(snap);
      if (window.__DISCORD_ACCESS_TOKEN__) {
        const body = JSON.stringify(await sessionPayloadAsync(snap));
        for (const url of apiUrlCandidates("/api/activity/session")) {
          try {
            fetch(url, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${window.__DISCORD_ACCESS_TOKEN__}`,
              },
              body,
              keepalive: true,
            });
            break;
          } catch {
            /* try next candidate */
          }
        }
      }
    }
    if (endWatch) {
      endWatchOnExit({ force: true, challengeForfeit: false });
    }
  };
  void run();
}

async function refreshCosmeticsIfPlaying() {
  if (!gameStarted || !gameApi?.setCosmetics || !window.__DISCORD_ACCESS_TOKEN__) return;
  const cosmetics = await loadCosmetics();
  if (cosmetics) gameApi.setCosmetics(cosmetics);
}

function startAutosave() {
  stopAutosave();
  autosaveTimer = setInterval(() => {
    saveSessionNow();
    refreshCosmeticsIfPlaying();
  }, 4000);
  if (exitHooksBound) return;
  exitHooksBound = true;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      flushSessionOnExit({ endWatch: false });
      scheduleEndWatchOnHide();
    } else {
      cancelEndWatchOnHide();
      reportSessionActive();
    }
  });
  window.addEventListener("pagehide", (event) => {
    flushSessionOnExit({ endWatch: !event.persisted });
  });
  window.addEventListener("beforeunload", () => flushSessionOnExit({ endWatch: true }));
  // Discord Embedded App may freeze the frame without a full unload.
  document.addEventListener("freeze", () => flushSessionOnExit({ endWatch: true }));
}

export function closeDiscordActivity() {
  // Drop the channel "X is playing Sudoku!" post when the window closes.
  endWatchOnExit({ force: true, challengeForfeit: false });
  try {
    const sdk = window.__DISCORD_SDK__;
    if (sdk && typeof sdk.close === "function") {
      sdk.close(RPCCloseCodes.CLOSE_NORMAL, "Quit");
      return;
    }
    window.close();
  } catch (err) {
    console.warn("closeDiscordActivity error:", err);
    try { window.close(); } catch {}
  }
}

async function quitAndClose() {
  const snap = currentSessionSnap();
  const isChallenge = snap?.session_kind === "challenge";
  clearLocalSession();

  const notifyServer = async () => {
    if (!window.__DISCORD_ACCESS_TOKEN__) return;
    const immediate = cachedGuildId || guildId();
    const gid =
      immediate && immediate !== "0" ? immediate : await resolveGuildId(2000);
    if (isChallenge) {
      await apiFetch("/api/activity/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          challenge_forfeit: true,
          end_watch: true,
          force: true,
          guild_id: gid,
        }),
      });
      return;
    }
    await apiFetch("/api/activity/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "clear",
        guild_id: gid,
        end_watch: true,
        force: true,
      }),
    });
  };

  try {
    await Promise.race([
      notifyServer(),
      new Promise((resolve) => setTimeout(resolve, 2500)),
    ]);
  } catch (err) {
    console.warn("quitAndClose failed:", err);
  } finally {
    closeDiscordActivity();
  }
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

function stopSpectatorPolling() {
  if (spectatorPollTimer) {
    clearInterval(spectatorPollTimer);
    spectatorPollTimer = null;
  }
}

function renderWatchers(watchers) {
  gameApi?.setWatchers?.(watchers);
}

function stopWatcherPolling() {
  if (watcherPollTimer) {
    clearInterval(watcherPollTimer);
    watcherPollTimer = null;
  }
  renderWatchers([]);
}

async function fetchWatchers() {
  if (!window.__DISCORD_ACCESS_TOKEN__ || spectating) return [];
  try {
    const immediate = cachedGuildId || guildId();
    const gid =
      immediate && immediate !== "0" ? immediate : await resolveGuildId(2000);
    const res = await apiFetch(
      `/api/activity/watchers?guild_id=${encodeURIComponent(gid)}`
    );
    if (!res?.ok) return [];
    const data = await res.json();
    return Array.isArray(data?.watchers) ? data.watchers : [];
  } catch (err) {
    console.warn("[Thcoku] watchers poll failed", err);
    return [];
  }
}

function startWatcherPolling() {
  stopWatcherPolling();
  const tick = async () => {
    renderWatchers(await fetchWatchers());
  };
  void tick();
  watcherPollTimer = setInterval(() => {
    void tick();
  }, WATCHERS_POLL_MS);
}

async function consumeSpectateIntent() {
  if (!window.__DISCORD_ACCESS_TOKEN__) return null;
  try {
    const gid = await resolveGuildId(5000);
    cachedGuildId = gid;
    const res = await apiFetch(
      `/api/activity/spectate/pending?guild_id=${encodeURIComponent(gid)}`
    );
    if (!res?.ok) return null;
    const data = await res.json();
    return data?.intent || null;
  } catch (err) {
    console.warn("[Thcoku] spectate intent failed", err);
    return null;
  }
}

async function fetchSpectateBoard(targetUserId) {
  if (!window.__DISCORD_ACCESS_TOKEN__) return null;
  try {
    const gid = await resolveGuildId();
    const res = await apiFetch(
      `/api/activity/spectate?guild_id=${encodeURIComponent(gid)}&target_user_id=${encodeURIComponent(targetUserId)}`
    );
    if (!res?.ok) return null;
    return res.json();
  } catch (err) {
    console.warn("[Thcoku] spectate poll failed", err);
    return null;
  }
}

function startSpectatorPolling(targetUserId) {
  stopSpectatorPolling();
  spectatorPollTimer = setInterval(async () => {
    const data = await fetchSpectateBoard(targetUserId);
    if (!data?.ok) return;
    if (!data.session) {
      if (data.ended && gameApi?.loadSpectatorSnapshot) {
        gameApi.loadSpectatorSnapshot({
          player_name: "Player",
          player_id: targetUserId,
          won_at: Date.now() / 1000,
        });
        stopSpectatorPolling();
      }
      return;
    }
    if (gameApi?.loadSpectatorSnapshot) {
      gameApi.loadSpectatorSnapshot(data.session);
      if (data.ended) stopSpectatorPolling();
    }
  }, SPECTATOR_POLL_MS);
}

async function beginSpectate(targetUserId) {
  spectating = true;
  stopWatcherPolling();
  stopAutosave();
  if (bootEl) bootEl.hidden = true;
  if (gameHintEl) gameHintEl.hidden = true;

  startGameOnce(null, { autoStart: false, spectatorMode: true });

  const data = await fetchSpectateBoard(targetUserId);
  if (data?.session && gameApi?.loadSpectatorSnapshot) {
    gameApi.loadSpectatorSnapshot(data.session);
  }

  startSpectatorPolling(targetUserId);

  const cosmetics = await loadCosmetics();
  if (cosmetics && gameApi?.setCosmetics) gameApi.setCosmetics(cosmetics);
}

async function beginPlay({ resumeSession = null, initialDiffIndex = null } = {}) {
  sessionOpenedAt = Date.now();
  const sessionKind = resumeSession?.session_kind ?? "play";
  const sessionMeta = {
    sessionKind,
    dailyDate: resumeSession?.daily_date || null,
    matchId: resumeSession?.match_id || null,
    playerSlot: resumeSession?.player_slot || null,
  };
  if (resumeSession) {
    startGameOnce(null, { autoStart: false, ...sessionMeta });
    if (!gameApi?.loadSnapshot?.(resumeSession)) {
      // Never invent a random puzzle for challenge/daily — that board would
      // fail server grading against the real race solution.
      if (sessionKind === "challenge" || sessionKind === "daily") {
        showWinToast(
          sessionKind === "challenge"
            ? "Could not load race puzzle — reopen Activity from the Play button."
            : "Could not load daily puzzle — try again."
        );
        return;
      }
      gameApi?.newGame?.();
    }
  } else {
    startGameOnce(null, { autoStart: true, ...sessionMeta, initialDiffIndex });
  }
  startAutosave();
  await reportSessionActive();
  startWatcherPolling();

  // Diff stays available for /play; daily/challenge hide it via syncControls (compact row).
  if (sessionKind === "daily" || sessionKind === "challenge") {
    const newBtn = document.querySelector('[data-action="new"]');
    if (newBtn) newBtn.style.display = "none";
  }
  gameApi?.syncControls?.();

  const cosmetics = await loadCosmetics();
  if (cosmetics && gameApi?.setCosmetics) gameApi.setCosmetics(cosmetics);
}

async function prefetchSessionBoard(session) {
  if (!session?.board || !session?.given) return;
  if (!window.__DISCORD_ACCESS_TOKEN__) return;
  const { saved_at: _saved, ...rest } = session;
  const snap = {
    ...rest,
    difficulty: session.difficulty || "medium",
    diff_index: session.diff_index ?? 0,
    elapsed: session.elapsed ?? 0,
    board: session.board,
    given: session.given,
    filled: session.filled ?? 0,
    session_kind: session.session_kind,
    hints_used: session.hints_used ?? 0,
  };
  if (session.solution) snap.solution = session.solution;
  await saveSessionNow({ force: true, snap });
}

async function showGame() {
  if (bootEl) bootEl.hidden = true;

  const intent = await consumeSpectateIntent();
  if (intent?.target_user_id) {
    if (gameHintEl) {
      gameHintEl.hidden = false;
      gameHintEl.textContent = "Opening spectator view…";
    }
    await beginSpectate(intent.target_user_id);
    return;
  }

  if (gameHintEl) {
    gameHintEl.hidden = false;
    gameHintEl.textContent = "Checking saved progress…";
  }

  const session = await loadSavedSession();
  if (session && (Number(session.filled) >= 81 || session.won)) {
    // Challenge/daily full boards still need /win — never replace with a fresh /play
    // puzzle while the race (or daily) is active (that caused not_solved toasts).
    if (session.session_kind === "challenge" || session.session_kind === "daily") {
      await beginPlay({ resumeSession: session });
      return;
    }
    await clearSavedSession();
    await beginPlay({ resumeSession: null });
    return;
  }

  // Full session with a saved board → offer resume or start fresh.
  if (session && session.board) {
    await prefetchSessionBoard(session);
    if (session.session_kind === "daily" || session.session_kind === "challenge") {
      await beginPlay({ resumeSession: session });
      return;
    }
    const resume = await askResume(session);
    if (resume) {
      await beginPlay({ resumeSession: session });
    } else {
      await clearSavedSession();
      // Preserve the difficulty preference when starting fresh after declining resume.
      await beginPlay({ resumeSession: null, initialDiffIndex: session.diff_index ?? null });
    }
    return;
  }

  // No saved board — start fresh, but use any stored difficulty preference.
  const prefDiffIndex = session?.diff_index ?? null;
  await beginPlay({ resumeSession: null, initialDiffIndex: prefDiffIndex });
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
    return { ok: true, local: true, elapsed };
  }
  try {
    // Persist the solved board before /win — pass snap explicitly because
    // getSnapshot is blocked while reportingWin is true.
    const meta =
      gameApi?.getStartSnapshot?.({ allowReporting: true }) ||
      gameApi?.getSnapshot?.({ allowReporting: true }) ||
      {};
    await saveSessionNow({
      force: true,
      snap: {
        ...meta,
        board: boardPayload?.board ?? meta.board,
        given: boardPayload?.given ?? meta.given,
        solution: boardPayload?.solution ?? meta.solution,
        filled: 81,
      },
    });
    // resolveGuildId waits up to 8s for the SDK to populate guildId — avoids sending "0".
    const resolvedGuildId = await resolveGuildId();
    const sdk = window.__DISCORD_SDK__;
    const res = await apiFetch("/api/activity/win", {
      method: "POST",
      body: JSON.stringify({
        difficulty,
        elapsed: Math.floor(Number(elapsed) || 0),
        guild_id: resolvedGuildId,
        channel_id: sdk?.channelId ?? channelId(),
        name: playerName(),
        board: boardPayload?.board ?? null,
        given: boardPayload?.given ?? null,
        solution: boardPayload?.solution ?? null,
      }),
    });
    const data = await res.json().catch(() => ({}));
    // Soft wins may return HTTP 200 with ok:true + quiet/already_won flags.
    if (
      data.quiet ||
      data.already_won ||
      data.error === "already_won" ||
      data.error === "daily_locked"
    ) {
      clearLocalSession();
      showWinToast(
        data.error === "daily_locked"
          ? "Daily already locked for today."
          : "Win already recorded for this puzzle."
      );
      await clearSavedSession();
      return { ok: true, already_won: true, elapsed: data.elapsed };
    }
    if (!res.ok || data.ok === false) {
      if (
        data.error === "already_settled" ||
        data.error === "forfeited" ||
        data.error === "match_missing"
      ) {
        await clearSavedSession();
        showWinToast(
          data.error === "forfeited"
            ? "You already left this race."
            : "Race already settled."
        );
        return { ok: true, already_won: true, elapsed: data.elapsed };
      }
      if (data.error === "not_solved") {
        if (data.challenge || gameApi?.getStartSnapshot?.()?.session_kind === "challenge") {
          showWinToast("Challenge board mismatch — reloading race puzzle…");
          try {
            clearLocalSession();
            const session = await loadSavedSession();
            if (session?.board && session.session_kind === "challenge") {
              await beginPlay({ resumeSession: session });
            }
          } catch (reloadErr) {
            console.warn("[Thcoku] challenge reload failed", reloadErr);
          }
          return null;
        }
        showWinToast("Board looks full but isn't solved yet — keep going.");
        return null;
      }
      showWinToast(`Could not save win (${data.error || res.status}).`);
      return null;
    }
    await clearSavedSession();
    if (data.challenge) {
      const shown = formatTime(data.elapsed ?? elapsed);
      showWinToast(
        `Board complete · ${shown} — rewards when the race settles.`
      );
      return data;
    }
    const shown = formatTime(data.elapsed ?? elapsed);
    const chatNote =
      data.posted === false && data.post_error
        ? " · rewards saved (chat photo failed)"
        : data.posted
          ? " · photo in chat"
          : "";
    const xp = data.xp ?? 0;
    const coins = data.coins ?? 0;
    const streak = data.streak ?? "?";
    const boostNote = formatWinBoostNote(data);
    showWinToast(
      `Order up! +${xp} XP · +${coins} sponges · streak ${streak} · ${shown}${boostNote}${chatNote}`
    );
    return data;
  } catch (err) {
    console.error(err);
    showWinToast("Could not save win — check your connection.");
    return null;
  }
};

function formatWinBoostNote(data) {
  const parts = [];
  if (data.xp_boost_used) {
    const left =
      data.xp_boost_remaining != null ? ` (${data.xp_boost_remaining} left)` : "";
    parts.push(`🔮 2×${left}`);
  }
  if (data.krabby_snack_used) {
    const left =
      data.krabby_snack_remaining != null ? ` (${data.krabby_snack_remaining} left)` : "";
    parts.push(`🍟 +25% sponges${left}`);
  }
  if (data.golden_spatula_used) {
    const left =
      data.golden_spatula_remaining != null
        ? ` (${data.golden_spatula_remaining} left)`
        : "";
    parts.push(`🥇 +50% XP${left}`);
  }
  return parts.length ? ` · ${parts.join(" · ")}` : "";
}

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
