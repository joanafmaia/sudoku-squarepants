/**
 * Thcoku Sudoku — Bikini Bottom canvas board + themed HTML controls.
 */
import {
  DEFAULT_DIFFICULTY,
  DIFF_KEYS,
  cellValue,
  clearPencilDigitPeers,
  difficultyLabel,
  filledCount,
  findConflicts,
  isSolved,
  makePuzzle,
  setCellValue,
  togglePencil,
  difficultyKeyFromLabel,
} from "./sudoku-core.js";

const LIGHT_PALETTE = {
  empty: "#fffef5",
  given: "#facc15",
  givenHl: "#fde047",
  matchHl: "#bbf7d0",
  select: "#fde047",
  boxHl: "#a5f3fc",
  conflict: "#fda4af",
  line: "#94a3b8",
  thick: "#0f766e",
  text: "#1d4ed8",
  textGiven: "#713f12",
  textConflict: "#be123c",
  pencil: "#64748b",
  header: "#0f766e",
  panel: "#fff8dc",
  win: "#34d399",
  bubble: "#bae6fd",
  gold: "#f59e0b",
  goldDeep: "#b45309",
  sand: "#fde68a",
  sandDeep: "#fbbf24",
  leaf: "#16a34a",
  leafDark: "#15803d",
  cardInner: "#fffbeb",
  bg0: "#7dd3fc",
  bg1: "#38bdf8",
  bg2: "#0ea5e9",
  bg3: "#0284c7",
  sunGlow: "rgba(254, 243, 199, 0.4)",
  bubbleStroke: "rgba(186, 230, 253, 0.75)",
  bubbleFill: "rgba(255, 255, 255, 0.25)",
  pinFill: "rgba(255, 248, 220, 0.98)",
  pinShine: "rgba(255, 255, 255, 0.5)",
  headerStroke: "rgba(245, 158, 11, 0.45)",
  spongePore: "rgba(146, 64, 14, 0.14)",
};

const DARK_PALETTE = {
  empty: "#1e293b",
  given: "#b45309",
  givenHl: "#ca8a04",
  matchHl: "#164e63",
  select: "#d97706",
  boxHl: "#0e7490",
  conflict: "#9f1239",
  line: "#475569",
  thick: "#38bdf8",
  text: "#7dd3fc",
  textGiven: "#fef08a",
  textConflict: "#fecdd3",
  pencil: "#94a3b8",
  header: "#38bdf8",
  panel: "#0f172a",
  win: "#10b981",
  bubble: "#0284c7",
  gold: "#f59e0b",
  goldDeep: "#d97706",
  sand: "#334155",
  sandDeep: "#1e293b",
  leaf: "#38bdf8",
  leafDark: "#0284c7",
  cardInner: "#1e293b",
  bg0: "#1e293b",
  bg1: "#0f172a",
  bg2: "#0c1222",
  bg3: "#020617",
  sunGlow: "rgba(56, 189, 248, 0.12)",
  bubbleStroke: "rgba(56, 189, 248, 0.45)",
  bubbleFill: "rgba(125, 211, 252, 0.12)",
  pinFill: "rgba(30, 41, 59, 0.95)",
  pinShine: "rgba(125, 211, 252, 0.35)",
  headerStroke: "rgba(56, 189, 248, 0.35)",
  spongePore: "rgba(254, 240, 138, 0.12)",
};

const JELLYFISH_PALETTE = {
  empty: "#2e1065",
  given: "#9333ea",
  givenHl: "#a855f7",
  matchHl: "#581c87",
  select: "#c084fc",
  boxHl: "#581c87",
  conflict: "#be123c",
  line: "#6b21a8",
  thick: "#f472b6",
  text: "#f9a8d4",
  textGiven: "#fef08a",
  textConflict: "#fecdd3",
  pencil: "#d8b4fe",
  header: "#f472b6",
  panel: "#1e1b4b",
  win: "#ec4899",
  bubble: "#e879f9",
  gold: "#f59e0b",
  goldDeep: "#d97706",
  sand: "#3b0764",
  sandDeep: "#581c87",
  leaf: "#f472b6",
  leafDark: "#db2777",
  cardInner: "#312e81",
  bg0: "#4c1d95",
  bg1: "#2e1065",
  bg2: "#1e1b4b",
  bg3: "#0f0a2e",
  sunGlow: "rgba(244, 114, 182, 0.18)",
  bubbleStroke: "rgba(232, 121, 249, 0.55)",
  bubbleFill: "rgba(250, 232, 255, 0.15)",
  pinFill: "rgba(49, 46, 129, 0.95)",
  pinShine: "rgba(249, 168, 212, 0.35)",
  headerStroke: "rgba(244, 114, 182, 0.4)",
  spongePore: "rgba(254, 240, 138, 0.1)",
};

const KRABS_PALETTE = {
  empty: "#0c4a6e",
  given: "#ea580c",
  givenHl: "#fbbf24",
  matchHl: "#075985",
  select: "#f59e0b",
  boxHl: "#0284c7",
  conflict: "#9f1239",
  line: "#0369a1",
  thick: "#fbbf24",
  text: "#fef08a",
  textGiven: "#ffffff",
  textConflict: "#fecdd3",
  pencil: "#7dd3fc",
  header: "#fbbf24",
  panel: "#082f49",
  win: "#f59e0b",
  bubble: "#38bdf8",
  gold: "#fbbf24",
  goldDeep: "#d97706",
  sand: "#075985",
  sandDeep: "#0369a1",
  leaf: "#fbbf24",
  leafDark: "#d97706",
  cardInner: "#0c4a6e",
  bg0: "#0e7490",
  bg1: "#0c4a6e",
  bg2: "#082f49",
  bg3: "#042f44",
  sunGlow: "rgba(251, 191, 36, 0.2)",
  bubbleStroke: "rgba(56, 189, 248, 0.55)",
  bubbleFill: "rgba(254, 243, 199, 0.12)",
  pinFill: "rgba(8, 47, 73, 0.95)",
  pinShine: "rgba(254, 243, 199, 0.35)",
  headerStroke: "rgba(251, 191, 36, 0.45)",
  spongePore: "rgba(255, 255, 255, 0.1)",
};

const ROCKBOTTOM_PALETTE = {
  empty: "#020617",
  given: "#4338ca",
  givenHl: "#6366f1",
  matchHl: "#1e293b",
  select: "#4f46e5",
  boxHl: "#1e293b",
  conflict: "#881337",
  line: "#334155",
  thick: "#22d3ee",
  text: "#67e8f9",
  textGiven: "#e0e7ff",
  textConflict: "#fecdd3",
  pencil: "#94a3b8",
  header: "#22d3ee",
  panel: "#090d16",
  win: "#06b6d4",
  bubble: "#0891b2",
  gold: "#38bdf8",
  goldDeep: "#0284c7",
  sand: "#0f172a",
  sandDeep: "#1e293b",
  leaf: "#22d3ee",
  leafDark: "#0891b2",
  cardInner: "#0f172a",
  bg0: "#0f172a",
  bg1: "#090d16",
  bg2: "#050810",
  bg3: "#020617",
  sunGlow: "rgba(34, 211, 238, 0.1)",
  bubbleStroke: "rgba(34, 211, 238, 0.4)",
  bubbleFill: "rgba(125, 211, 252, 0.1)",
  pinFill: "rgba(15, 23, 42, 0.95)",
  pinShine: "rgba(34, 211, 238, 0.3)",
  headerStroke: "rgba(34, 211, 238, 0.35)",
  spongePore: "rgba(224, 231, 255, 0.1)",
};

const RGB = { ...LIGHT_PALETTE };
let activeTheme = "light";

const WIDTH = 720;
const HEIGHT = 780;
// Extra side margin so pin badges sit fully outside the pineapple frame.
const BOARD_ORIGIN = { x: 64, y: 108 };
const CELL = 64;
const BOARD_SIZE = CELL * 9;
const FRAME_PAD = 16;
// Slightly smaller badges so half-cell spacing fits ~54 unique pins.
const PIN_RADIUS = 16;

const TITLE_HEADER_LINES = {
  "Very Easy": "Ahoy, {title}!",
  Easy: "I'm ready, {title}!",
  Medium: "Order up, {title}!",
  Hard: "Aye aye, {title}!",
  "Very Hard": "Jumping jellyfish, {title}!",
  Expertttt: "Barnacles, {title}!",
};

const STATUS_OK = [
  "I'm ready! · {n}/81",
  "Order up! · {n}/81",
  "Good nihilism · {n}/81",
  "Tartar sauce… · {n}/81",
  "Firmly grasp it · {n}/81",
];
const STATUS_CLEAR = [
  "Wiped · {n}/81",
  "Back to square one · {n}/81",
  "Empty Krabby Patty · {n}/81",
];
const STATUS_PICK = [
  "Pick a number",
  "Choose wisely…",
  "Which digit, sailor?",
];
const WIN_CONFETTI = ["🍍", "🍔", "⭐", "🪼", "🫧", "🍦"];

function discordUsername() {
  return (
    window.__DISCORD_AUTH__?.user?.global_name ||
    window.__DISCORD_AUTH__?.user?.username ||
    ""
  );
}

function pick(list) {
  return list[(Math.random() * list.length) | 0];
}

function fmt(template, n) {
  return template.replace("{n}", String(n));
}

function roundRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

import {
  armTrackBgm,
  configureTrackBgm,
  cycleTrack,
  getTrackMeta,
  pauseTrackBgm,
  resumeTrackBgm,
  setTrackIndex,
  syncTrackBgmEnabled,
} from "./track-bgm.js";

let audioCtx = null;
const MUSIC_ENABLED_KEY = "thcoku_music_on";
const MUSIC_STORAGE_KEY = "thcoku_music";
const SFX_STORAGE_KEY = "thcoku_sfx";
const LEGACY_SOUND_KEY = "thcoku_sound";

function readStoredBool(key, fallback) {
  try {
    const stored = localStorage.getItem(key);
    if (stored === "off" || stored === "0") return false;
    if (stored === "on" || stored === "1") return true;
  } catch {
    /* localStorage disabled */
  }
  return fallback;
}

function persistMusicPrefs() {
  try {
    localStorage.setItem(MUSIC_ENABLED_KEY, musicEnabled ? "on" : "off");
    localStorage.setItem(MUSIC_STORAGE_KEY, musicEnabled ? "on" : "off");
  } catch {
    /* localStorage disabled */
  }
}

function readInitialMusicPrefs() {
  try {
    const enabledStored = localStorage.getItem(MUSIC_ENABLED_KEY);
    if (enabledStored === "on" || enabledStored === "off") {
      return { enabled: enabledStored === "on" };
    }

    const legacy = localStorage.getItem(MUSIC_STORAGE_KEY);
    if (legacy === "off" || legacy === "false") return { enabled: false };
    if (legacy === "on" || legacy === "true") return { enabled: true };
    const legacySound = localStorage.getItem(LEGACY_SOUND_KEY);
    if (legacySound === "off" || legacySound === "0") return { enabled: false };
  } catch {
    /* localStorage disabled */
  }
  return { enabled: true };
}

function readInitialAudioPrefs() {
  const music = readInitialMusicPrefs();
  return {
    musicEnabled: music.enabled,
    sfx: readStoredBool(SFX_STORAGE_KEY, true),
  };
}

const initialAudio = readInitialAudioPrefs();
let musicEnabled = initialAudio.musicEnabled;
let sfxEnabled = initialAudio.sfx;

export function isMusicEnabled() {
  return musicEnabled;
}

export function getMusicPresetMeta() {
  return getTrackMeta();
}

export function cycleMusicTrack(delta = 1) {
  // Turn music on first so track switch can arm/play with isEnabled() true.
  if (!musicEnabled) {
    musicEnabled = true;
    persistMusicPrefs();
  }
  const meta = cycleTrack(delta);
  applyMusicPlayback();
  return meta;
}

export function setMusicTrackIndex(index) {
  if (!musicEnabled) {
    musicEnabled = true;
    persistMusicPrefs();
  }
  const meta = setTrackIndex(index);
  applyMusicPlayback();
  return meta;
}

function applyMusicPlayback() {
  if (!musicEnabled) {
    syncTrackBgmEnabled(false);
    pauseTrackBgm();
    maybeSuspendAudioCtx();
    return;
  }
  syncTrackBgmEnabled(true);
  ensureBgmStarted();
  resumeTrackBgm();
}

export function setMusicEnabled(enabled) {
  musicEnabled = Boolean(enabled);
  persistMusicPrefs();
  applyMusicPlayback();
}

export function isSfxEnabled() {
  return sfxEnabled;
}

export function setSfxEnabled(enabled) {
  sfxEnabled = Boolean(enabled);
  try {
    localStorage.setItem(SFX_STORAGE_KEY, sfxEnabled ? "on" : "off");
  } catch {
    /* localStorage disabled */
  }
  if (sfxEnabled) getAudioCtx();
  else maybeSuspendAudioCtx();
}

function maybeSuspendAudioCtx() {
  if (musicEnabled || sfxEnabled) return;
  if (audioCtx?.state === "running") {
    audioCtx.suspend().catch(() => { });
  }
}

function getAudioCtx() {
  if (!audioCtx) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass) audioCtx = new AudioContextClass();
  }
  if (audioCtx && audioCtx.state === "suspended") {
    audioCtx.resume().catch(() => { });
  }
  return audioCtx;
}

configureTrackBgm({
  isEnabled: () => musicEnabled,
});

function ensureBgmStarted() {
  armTrackBgm();
}

applyMusicPlayback();

export function playFx(type) {
  if (!sfxEnabled) return;
  try {
    const ctx = getAudioCtx();
    if (!ctx) return;
    if (musicEnabled) ensureBgmStarted();
    const now = ctx.currentTime;

    if (type === "pop") {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "sine";
      osc.frequency.setValueAtTime(440, now);
      osc.frequency.exponentialRampToValueAtTime(880, now + 0.08);
      gain.gain.setValueAtTime(0.15, now);
      gain.gain.exponentialRampToValueAtTime(0.01, now + 0.08);
      osc.start(now);
      osc.stop(now + 0.08);
    } else if (type === "error") {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "sawtooth";
      osc.frequency.setValueAtTime(160, now);
      osc.frequency.setValueAtTime(120, now + 0.08);
      gain.gain.setValueAtTime(0.2, now);
      gain.gain.exponentialRampToValueAtTime(0.01, now + 0.18);
      osc.start(now);
      osc.stop(now + 0.18);
    } else if (type === "hint") {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "triangle";
      osc.frequency.setValueAtTime(523.25, now);
      osc.frequency.setValueAtTime(659.25, now + 0.08);
      osc.frequency.setValueAtTime(783.99, now + 0.16);
      gain.gain.setValueAtTime(0.15, now);
      gain.gain.exponentialRampToValueAtTime(0.01, now + 0.28);
      osc.start(now);
      osc.stop(now + 0.28);
    } else if (type === "win") {
      const notes = [523.25, 659.25, 783.99, 1046.5];
      notes.forEach((freq, idx) => {
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.type = "sine";
        o.frequency.setValueAtTime(freq, now + idx * 0.1);
        g.connect(ctx.destination);
        o.connect(g);
        g.gain.setValueAtTime(0.2, now + idx * 0.1);
        g.gain.exponentialRampToValueAtTime(0.01, now + idx * 0.1 + 0.25);
        o.start(now + idx * 0.1);
        o.stop(now + idx * 0.1 + 0.25);
      });
    }
  } catch (e) {
    // audio context suppressed by browser policy until gesture
  }
}

function ensureControls(shell) {
  let bar = document.getElementById("game-controls");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "game-controls";
    bar.innerHTML = `
    <div class="ctrl-pad" role="group" aria-label="Numbers">
      ${[1, 2, 3, 4, 5, 6, 7, 8, 9]
      .map((n) => `<button type="button" class="ctrl-digit" data-digit="${n}">${n}</button>`)
      .join("")}
    </div>
    <div class="ctrl-actions ctrl-actions-edit" role="group" aria-label="Editing Actions">
      <button type="button" data-action="undo" id="ctrl-undo" title="Undo move">↩ Undo</button>
      <button type="button" data-action="clear" class="ctrl-clear" title="Clear selected cell">Clear</button>
      <button type="button" data-action="pencil" id="ctrl-pencil">Notes</button>
    </div>
    <div class="ctrl-actions ctrl-actions-meta" id="ctrl-meta" role="group" aria-label="Game Setup Actions">
      <button type="button" data-action="quit" id="ctrl-quit" class="btn-danger">🚪 Quit</button>
      <button type="button" data-action="hint" id="ctrl-hint" title="Get a hint">💡 Hint</button>
      <button type="button" data-action="reset" id="ctrl-reset" title="Clear all your entries (keeps clues)">↺ Reset</button>
      <button type="button" data-action="diff" id="ctrl-diff">Medium</button>
    </div>
  `;
    shell.appendChild(bar);
  } else {
    // Migrate Reset into the meta row (Diff's slot) if it lived on the edit row.
    let resetBtn = bar.querySelector("#ctrl-reset");
    const meta = bar.querySelector("#ctrl-meta");
    const hintBtn = bar.querySelector("#ctrl-hint");
    if (!resetBtn && meta && hintBtn) {
      resetBtn = document.createElement("button");
      resetBtn.type = "button";
      resetBtn.id = "ctrl-reset";
      resetBtn.dataset.action = "reset";
      resetBtn.title = "Clear all your entries (keeps clues)";
      resetBtn.textContent = "↺ Reset";
      hintBtn.insertAdjacentElement("afterend", resetBtn);
    } else if (resetBtn && meta && !meta.contains(resetBtn) && hintBtn) {
      hintBtn.insertAdjacentElement("afterend", resetBtn);
    }
  }
  return bar;
}

function mulberry32(seed) {
  let t = seed >>> 0;
  return () => {
    t += 0x6d2b79f5;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

function makeAmbient() {
  const bubbles = [];
  for (let i = 0; i < 22; i++) {
    bubbles.push({
      x: Math.random() * WIDTH,
      y: Math.random() * HEIGHT,
      r: 2.5 + Math.random() * 7,
      speed: 10 + Math.random() * 26,
      phase: Math.random() * Math.PI * 2,
      wobble: 0.3 + Math.random() * 0.7,
    });
  }
  return bubbles;
}

export function startThcokuGame(canvas, options = {}) {
  if (!canvas) return null;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const shell = canvas.closest("#game-shell") || canvas.parentElement;
  const controls = ensureControls(shell);
  const cosmetics = {
    title: options.cosmetics?.title || null,
    pins: Array.isArray(options.cosmetics?.pins) ? options.cosmetics.pins.slice() : [],
    seed: Number(options.cosmetics?.seed) || 1,
  };

  canvas.width = WIDTH;
  canvas.height = HEIGHT;

  const ambientBubbles = makeAmbient();
  let ambientRaf = 0;

  const state = {
    diffIndex: options.initialDiffIndex != null
      ? Math.max(0, Math.min(DIFF_KEYS.length - 1, Number(options.initialDiffIndex)))
      : Math.max(0, DIFF_KEYS.indexOf(DEFAULT_DIFFICULTY)),
    selected: [0, 0],
    pencilMode: false,
    status: "Generating…",
    won: false,
    reportingWin: false,
    boardGen: 0,
    // Active play time only (pauses when Activity is hidden/closed).
    baseElapsedSec: 0,
    runStartedAt: Date.now(),
    sessionStartedAt: Date.now(),
    board: [],
    given: [],
    solution: [],
    difficulty: DEFAULT_DIFFICULTY,
    flashCell: null,
    flashUntil: 0,
    winAt: 0,
    bubbles: [],
    confetti: [],
    shakeUntil: 0,
    raf: 0,
    winZoom: 1,
    sessionKind: options.sessionKind ?? "play",
    dailyDate: options.dailyDate || null,
    matchId: options.matchId || null,
    playerSlot: options.playerSlot || null,
    undoStack: [],
    hintsUsed: 0,
    hintsMax: null, // null = unlimited; Expertttt gets 3 from the server
    hintsGaryUsed: 0,
    garyWisdomBonus: 0,
    hintSpongeCost: Number(options.hintSpongeCost) || 15,
    pocketSponges: Number(options.pocketSponges) || 0,
    hintInFlight: false,
    serverHints: false,
    spectatorMode: Boolean(options.spectatorMode),
    spectatorName: "",
    spectatorTargetId: null,
    noHints: false,
    watchers: [],
  };

  const diffBtn = controls.querySelector("#ctrl-diff");
  const pencilBtn = controls.querySelector("#ctrl-pencil");

  function hintsMaxForDifficulty(key) {
    return key === "expertttt" ? 3 : null;
  }

  function titleBadge() {
    const t = cosmetics.title;
    if (!t) return "";
    const pin = (t.pin || t.label || "").replace(/^[^\wÀ-ÿ]+/u, "").trim() || t.pin || "";
    const em = (t.emoji || "").trim();
    if (em && pin) return `${em} ${pin}`;
    return em || pin;
  }

  function headerTitleLine() {
    const badge = titleBadge();
    const tier = difficultyLabel(DIFF_KEYS[state.diffIndex]);
    if (!badge) return `~ ${tier} ~`;
    const template = TITLE_HEADER_LINES[tier] || "I'm ready, {title}!";
    return `~ ${tier} ~  ${template.replace("{title}", badge)}`;
  }

  function drawBorderPins() {
    const pins = cosmetics.pins.filter(Boolean);
    if (!pins.length) return;
    const rng = mulberry32(cosmetics.seed || 1);
    const ox = BOARD_ORIGIN.x;
    const oy = BOARD_ORIGIN.y;
    // Frame outer edge — pin centers stay fully outside so badges are never clipped
    const frameLeft = ox - FRAME_PAD - 4;
    const frameRight = ox + BOARD_SIZE + FRAME_PAD + 4;
    const frameBottom = oy + BOARD_SIZE + FRAME_PAD + 4;
    const leftX = Math.max(PIN_RADIUS + 2, frameLeft - PIN_RADIUS - 4);
    const rightX = Math.min(WIDTH - PIN_RADIUS - 2, frameRight + PIN_RADIUS + 4);
    const bottomY = Math.min(HEIGHT - PIN_RADIUS - 8, frameBottom + PIN_RADIUS + 6);

    const slots = [];
    // Half-cell pitch (~18 per edge) → ~54 slots (was 27 at one-per-row).
    const step = CELL / 2;
    const count = 18;
    for (let i = 0; i < count; i++) {
      const y = oy + (i + 0.5) * step;
      slots.push({ x: leftX, y });
      slots.push({ x: rightX, y });
    }
    for (let i = 0; i < count; i++) {
      slots.push({ x: ox + (i + 0.5) * step, y: bottomY });
    }
    for (let i = slots.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      [slots[i], slots[j]] = [slots[j], slots[i]];
    }
    const unique = [];
    const seen = new Set();
    for (const p of pins) {
      if (!seen.has(p)) {
        seen.add(p);
        unique.push(p);
      }
    }
    for (let i = 0; i < Math.min(unique.length, slots.length); i++) {
      const emoji = unique[i];
      const slot = slots[i];
      ctx.beginPath();
      ctx.arc(slot.x, slot.y, PIN_RADIUS, 0, Math.PI * 2);
      ctx.fillStyle = RGB.pinFill;
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = RGB.gold;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(slot.x - 4, slot.y - 5, 5, 0, Math.PI * 2);
      ctx.fillStyle = RGB.pinShine;
      ctx.fill();
      ctx.font = "24px Apple Color Emoji, Segoe UI Emoji, Segoe UI Symbol, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(emoji, slot.x, slot.y + 1);
    }
  }

  function drawHeader() {
    ctx.fillStyle = RGB.panel;
    roundRect(ctx, 20, 14, WIDTH - 40, 64, 14);
    ctx.fill();
    ctx.strokeStyle = RGB.headerStroke;
    ctx.lineWidth = 2;
    roundRect(ctx, 20, 14, WIDTH - 40, 64, 14);
    ctx.stroke();

    ctx.fillStyle = RGB.header;
    ctx.font = "700 22px Fredoka, Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText("Thcoku", 36, 34);
    ctx.font = "600 15px Fredoka, Segoe UI, Apple Color Emoji, Segoe UI Emoji, sans-serif";
    ctx.fillText(headerTitleLine().slice(0, 48), 130, 34);
    ctx.font = "500 14px Fredoka, Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(String(state.status).slice(0, 42), 36, 58);
  }

  function getElapsedSec() {
    // Challenge races are ranked on wall-clock — keep the UI in sync.
    if (state.sessionKind === "challenge" && state.sessionStartedAt) {
      return Math.max(0, Math.floor((Date.now() - state.sessionStartedAt) / 1000));
    }
    let elapsed = Math.max(0, Number(state.baseElapsedSec) || 0);
    if (state.runStartedAt && !state.won) {
      elapsed += Math.max(0, Math.floor((Date.now() - state.runStartedAt) / 1000));
    }
    return elapsed;
  }

  function pauseTimer() {
    // Challenge races stay wall-clock on the server — keep the on-screen clock running.
    if (state.sessionKind === "challenge") return;
    if (!state.runStartedAt) return;
    state.baseElapsedSec = getElapsedSec();
    state.runStartedAt = null;
  }

  function resumeTimer() {
    if (state.runStartedAt || state.won || state.reportingWin || state.spectatorMode) {
      return;
    }
    state.runStartedAt = Date.now();
  }

  function setWatchers(watchers) {
    // Watcher list is rendered in the HTML toolbar chip (readable on mobile).
    state.watchers = Array.isArray(watchers) ? watchers.slice() : [];
  }

  function getSnapshot({ allowReporting = false } = {}) {
    if (!state.board?.length || state.won) return null;
    if (state.reportingWin && !allowReporting) return null;
    let userMoves = 0;
    for (let r = 0; r < 9; r++) {
      for (let c = 0; c < 9; c++) {
        if (state.given[r][c]) continue;
        const cell = state.board[r][c];
        if (cell?.value) userMoves += 1;
        else if (Array.isArray(cell?.pencil_marks) && cell.pencil_marks.length) userMoves += 1;
      }
    }
    if (userMoves <= 0) return null;
    return snapshotPayload();
  }

  function getStartSnapshot({ allowReporting = false } = {}) {
    /** Board state for watch notify as soon as /play opens (before any moves). */
    if (!state.board?.length || state.won) return null;
    if (state.reportingWin && !allowReporting) return null;
    return snapshotPayload();
  }

  function snapshotPayload() {
    const payload = {
      difficulty: state.difficulty,
      diff_index: state.diffIndex,
      elapsed: getElapsedSec(),
      board: state.board,
      given: state.given,
      filled: filledCount(state.board),
      session_kind: state.sessionKind,
      hints_used: state.hintsUsed,
      hints_max: state.hintsMax,
      hints_gary_used: state.hintsGaryUsed,
      gary_wisdom_bonus: state.garyWisdomBonus,
      no_hints: Boolean(state.noHints),
      // Original open time (metadata only) — elapsed is active screen time.
      started_at: (state.sessionStartedAt || Date.now()) / 1000,
      timer_active: Boolean(state.runStartedAt) && !state.won && !state.spectatorMode,
    };
    if (state.dailyDate) payload.daily_date = state.dailyDate;
    if (state.matchId) payload.match_id = state.matchId;
    if (state.playerSlot) payload.player_slot = state.playerSlot;
    if (!state.serverHints && Array.isArray(state.solution) && state.solution.length === 9) {
      payload.solution = state.solution;
    }
    return payload;
  }

  function loadSnapshot(snap) {
    if (!snap?.board || !snap?.given) return false;
    state.board = snap.board;
    state.given = snap.given;
    state.solution = Array.isArray(snap.solution) && snap.solution.length === 9 ? snap.solution : [];
    state.serverHints = !(Array.isArray(snap.solution) && snap.solution.length === 9);
    state.hintsUsed = Number(snap.hints_used) || 0;
    state.hintsGaryUsed = Number(snap.hints_gary_used) || 0;
    state.garyWisdomBonus = Number(snap.gary_wisdom_bonus) || 0;
    if (snap.no_hints) {
      state.noHints = true;
      state.hintsMax = 0;
    } else if (snap.hints_max == null || snap.hints_max === "") {
      state.noHints = false;
      state.hintsMax = null;
    } else {
      state.noHints = false;
      const n = Number(snap.hints_max);
      state.hintsMax = Number.isFinite(n) ? Math.max(0, n) : null;
    }
    if (snap.hint_sponge_cost != null) {
      state.hintSpongeCost = Number(snap.hint_sponge_cost) || 15;
    }
    if (snap.pocket != null) {
      state.pocketSponges = Number(snap.pocket) || 0;
    }
    state.difficulty = difficultyKeyFromLabel(snap.difficulty || DEFAULT_DIFFICULTY);
    const idx = DIFF_KEYS.indexOf(state.difficulty);
    const lockedKind = snap.session_kind || state.sessionKind;
    if (lockedKind === "daily" || lockedKind === "challenge") {
      state.diffIndex = idx >= 0 ? idx : 0;
      // Keep reported win difficulty aligned with the displayed tier.
      if (DIFF_KEYS[state.diffIndex]) state.difficulty = DIFF_KEYS[state.diffIndex];
    } else if (snap.diff_index != null && DIFF_KEYS[Number(snap.diff_index)]) {
      state.diffIndex = Number(snap.diff_index);
      state.difficulty = DIFF_KEYS[state.diffIndex];
    } else {
      state.diffIndex = idx >= 0 ? idx : Math.max(0, DIFF_KEYS.indexOf(DEFAULT_DIFFICULTY));
      if (DIFF_KEYS[state.diffIndex]) state.difficulty = DIFF_KEYS[state.diffIndex];
    }
    if (state.hintsMax == null && !snap.no_hints) {
      state.hintsMax = hintsMaxForDifficulty(state.difficulty);
    }
    state.selected = [0, 0];
    state.won = false;
    state.reportingWin = false;
    state.bubbles = [];
    state.confetti = [];
    state.pencilMode = false;
    state.flashCell = null;
    // Resume from frozen active seconds — do NOT re-anchor to wall-clock started_at.
    state.baseElapsedSec = Math.max(0, Number(snap.elapsed) || 0);
    state.sessionStartedAt =
      snap.started_at != null && Number(snap.started_at) > 0
        ? Number(snap.started_at) * 1000
        : Date.now() - state.baseElapsedSec * 1000;
    state.runStartedAt = state.spectatorMode ? null : Date.now();
    state.sessionKind = snap.session_kind || null;
    state.dailyDate = snap.daily_date || null;
    state.matchId = snap.match_id || null;
    state.playerSlot = snap.player_slot || null;
    state.undoStack = [];
    state.boardGen += 1;
    const user = discordUsername();
    const hello = user ? `Hey, ${user}! ` : "";
    state.status = `${hello}Continuing · ${filledCount(state.board)}/81`;
    syncControls();
    draw();
    // Remount with a full board (common in Discord Activities) must still POST /win.
    maybeCelebrateAfterMove();
    return true;
  }

  function loadSpectatorSnapshot(snap) {
    state.spectatorMode = true;
    state.spectatorName = snap.player_name || snap.name || "Player";
    state.spectatorTargetId = snap.player_id || null;
    state.serverHints = true;
    state.solution = [];
    if (snap.board && snap.given) {
      loadSnapshot(snap);
    }
    // Don't leave a default [0,0] selection wash on the watched board.
    state.selected = [-1, -1];
    if (snap.cosmetics) {
      setCosmetics({
        title: snap.cosmetics.title || null,
        pins: Array.isArray(snap.cosmetics.pins) ? snap.cosmetics.pins : [],
        seed:
          snap.cosmetics.seed != null
            ? Number(snap.cosmetics.seed)
            : Number(snap.player_id) || cosmetics.seed,
      });
    }
    const filled = snap.board ? filledCount(snap.board) : Number(snap.filled) || 0;
    if (snap.won_at || filled >= 81) {
      state.status = `${state.spectatorName} finished the puzzle!`;
      state.won = true;
    } else if (snap.board && snap.given) {
      state.status = `Watching ${state.spectatorName} · ${filled}/81`;
    } else {
      state.status = `Watching ${state.spectatorName} — waiting for board…`;
    }
    syncControls();
    draw();
    return true;
  }

  function setCosmetics(next) {
    cosmetics.title = next?.title || null;
    cosmetics.pins = Array.isArray(next?.pins) ? next.pins.slice() : [];
    if (next?.seed != null) cosmetics.seed = Number(next.seed) || cosmetics.seed;
    draw();
  }

  function difficultyLocked() {
    if (state.spectatorMode) return true;
    const k = state.sessionKind;
    // /play picks difficulty in the slash command — no in-game changes.
    return k === "play" || k === "daily" || k === "challenge";
  }

  function applyHintMeta(meta) {
    if (!meta || typeof meta !== "object") return;
    if (meta.hints_used != null) state.hintsUsed = Number(meta.hints_used) || 0;
    if (meta.hints_gary_used != null) {
      state.hintsGaryUsed = Number(meta.hints_gary_used) || 0;
    }
    if (meta.gary_wisdom_bonus != null) {
      state.garyWisdomBonus = Number(meta.gary_wisdom_bonus) || 0;
    } else if (meta.gary_free_left != null) {
      state.garyWisdomBonus =
        state.hintsGaryUsed + (Number(meta.gary_free_left) || 0);
    }
    if ("hints_max" in meta || meta.no_hints) {
      if (meta.no_hints) {
        state.noHints = true;
        state.hintsMax = 0;
      } else if (meta.hints_max == null || meta.hints_max === "") {
        state.noHints = false;
        state.hintsMax = null;
      } else {
        state.noHints = false;
        const n = Number(meta.hints_max);
        state.hintsMax = Number.isFinite(n) ? Math.max(0, n) : null;
      }
    }
    if (meta.pocket != null) state.pocketSponges = Number(meta.pocket) || 0;
    if (meta.hint_sponge_cost != null) {
      state.hintSpongeCost = Number(meta.hint_sponge_cost) || 15;
    }
    syncHintButton();
  }

  function syncHintButton() {
    const hintBtn = controls.querySelector("#ctrl-hint");
    if (!hintBtn || state.spectatorMode) return;
    const garyFree = Math.max(0, state.garyWisdomBonus - state.hintsGaryUsed);
    const unlimited =
      state.hintsMax == null || !Number.isFinite(Number(state.hintsMax));
    const exhausted = !unlimited && state.hintsUsed >= state.hintsMax;
    const progress = unlimited ? String(state.hintsUsed) : `${state.hintsUsed}/${state.hintsMax}`;
    hintBtn.disabled = Boolean(state.hintInFlight);
    hintBtn.style.opacity = exhausted || state.hintInFlight ? "0.55" : "";
    if (exhausted) {
      hintBtn.textContent = `💡 None left (${progress})`;
      hintBtn.title = "No hints left this puzzle";
      return;
    }
    if (garyFree > 0) {
      hintBtn.textContent = `💡 Hint · free ×${garyFree}`;
      hintBtn.title = `Gary's Wisdom — ${garyFree} free left, then paid hints (${state.hintSpongeCost} 🧽 each${unlimited ? "" : `, max ${state.hintsMax}`})`;
    } else {
      const cost = state.hintSpongeCost;
      const pocket = state.pocketSponges;
      hintBtn.textContent = `💡 Hint · ${cost} 🧽`;
      hintBtn.title = unlimited
        ? `Unlimited paid hints · ${cost} sponges each · ${pocket} in pocket · ${state.hintsUsed} used`
        : `${cost} sponges each · ${pocket} in pocket · ${progress}`;
      if (pocket < cost) {
        hintBtn.style.opacity = "0.65";
      }
    }
  }

  function syncControls() {
    if (diffBtn) diffBtn.textContent = difficultyLabel(DIFF_KEYS[state.diffIndex]);
    if (pencilBtn) {
      pencilBtn.textContent = state.pencilMode ? "Notes ON" : "Notes";
      pencilBtn.classList.toggle("is-active", state.pencilMode);
    }
    const spec = state.spectatorMode;
    const meta = controls.querySelector("#ctrl-meta");
    const hideDiff = difficultyLocked();
    const resetBtn = controls.querySelector("#ctrl-reset");
    if (diffBtn) {
      // Diff only when difficulty is free; otherwise Reset takes that slot.
      diffBtn.hidden = hideDiff || spec;
      diffBtn.style.display = "";
    }
    if (resetBtn) {
      resetBtn.hidden = spec || !hideDiff;
      resetBtn.style.display = "";
    }
    if (pencilBtn) {
      pencilBtn.hidden = spec;
      pencilBtn.style.display = "";
    }
    if (meta) {
      meta.classList.toggle("is-solo", spec);
      // Compact = Diff gone, Reset shown → still 3 equal slots (Quit · Hint · Reset)
      meta.classList.toggle("is-compact", !spec && hideDiff);
      meta.classList.toggle("is-free-diff", !spec && !hideDiff);
    }
    const newBtn = controls.querySelector('[data-action="new"]');
    if (newBtn) newBtn.style.display = spec ? "none" : "";
    const quitBtn = controls.querySelector("#ctrl-quit");
    if (quitBtn) {
      if (state.sessionKind === "challenge") {
        quitBtn.textContent = "🏳 Forfeit";
        quitBtn.title = "Leave the race (counts as a forfeit). Closing the window alone does not forfeit.";
      } else if (state.sessionKind === "daily") {
        quitBtn.textContent = "🚪 Quit";
        quitBtn.title = "Quit today's daily (forfeits the pineapple for today)";
      } else {
        quitBtn.textContent = "🚪 Quit";
        quitBtn.title = "Quit this puzzle";
      }
    }
    controls.querySelectorAll("[data-action]").forEach((btn) => {
      const action = btn.getAttribute("data-action");
      const allow = !spec || action === "quit";
      btn.disabled = spec && !allow;
      btn.style.opacity = spec && !allow ? "0.45" : "";
    });
    controls.querySelectorAll(".ctrl-digit").forEach((btn) => {
      if (spec) {
        btn.disabled = true;
        btn.style.opacity = "0.45";
      } else {
        btn.disabled = false;
        btn.style.opacity = "";
      }
    });
    syncHintButton();
  }

  function spawnBubbles() {
    state.bubbles = [];
    for (let i = 0; i < 28; i++) {
      state.bubbles.push({
        x: BOARD_ORIGIN.x + Math.random() * BOARD_SIZE,
        y: BOARD_ORIGIN.y + BOARD_SIZE + Math.random() * 20,
        r: 6 + Math.random() * 12,
        vy: 45 + Math.random() * 80,
        vx: -20 + Math.random() * 40,
        phase: Math.random() * Math.PI * 2,
      });
    }
  }

  function spawnConfetti() {
    state.confetti = [];
    for (let i = 0; i < 36; i++) {
      state.confetti.push({
        x: WIDTH / 2 + (Math.random() - 0.5) * 220,
        y: 280 + Math.random() * 40,
        vx: -90 + Math.random() * 180,
        vy: -120 - Math.random() * 160,
        rot: Math.random() * Math.PI * 2,
        spin: -0.12 + Math.random() * 0.24,
        emoji: WIN_CONFETTI[i % WIN_CONFETTI.length],
        size: 18 + Math.random() * 14,
      });
    }
  }

  function newGame() {
    if (state.spectatorMode) return;
    if (state.sessionKind === "daily" || state.sessionKind === "challenge") {
      return;
    }
    if (state.reportingWin) {
      state.status = "Saving win — wait a sec…";
      draw();
      return;
    }
    void (async () => {
      if (typeof options.onNewGame === "function") {
        try {
          await options.onNewGame();
        } catch (err) {
          console.warn("[Thcoku] onNewGame", err);
        }
      }
      const key = DIFF_KEYS[state.diffIndex];
      state.status = `Cooking (${difficultyLabel(key)})…`;
      state.won = false;
      state.reportingWin = false;
      state.boardGen += 1;
      const gen = state.boardGen;
      state.bubbles = [];
      state.confetti = [];
      state.undoStack = [];
      state.winZoom = 1;
      draw();
      const puzzle = makePuzzle(key);
      if (gen !== state.boardGen) return;
      state.board = puzzle.board;
      state.given = puzzle.given;
      state.solution = puzzle.solution;
      state.difficulty = puzzle.difficulty;
      state.selected = [0, 0];
      state.baseElapsedSec = 0;
      state.runStartedAt = Date.now();
      state.sessionStartedAt = Date.now();
      state.pencilMode = false;
      state.flashCell = null;
      state.hintsUsed = 0;
      state.hintsGaryUsed = 0;
      state.garyWisdomBonus = 0;
      state.hintsMax = hintsMaxForDifficulty(state.difficulty);
      state.noHints = false;
      state.serverHints = false;
      const user = discordUsername();
      state.status = user ? `Hey, ${user}! I'm ready!` : "Tap a cell — I'm ready!";
      syncControls();
      draw();
      if (typeof options.onBoardReady === "function") {
        try {
          await options.onBoardReady();
        } catch (err) {
          console.warn("[Thcoku] onBoardReady", err);
        }
      }
    })();
  }

  function saveUndoState() {
    if (!state.board) return;
    const boardCopy = state.board.map((row) =>
      row.map((cell) => ({
        value: cell.value | 0,
        pencil_marks: Array.isArray(cell.pencil_marks) ? [...cell.pencil_marks] : [],
      }))
    );
    state.undoStack.push(boardCopy);
    if (state.undoStack.length > 50) state.undoStack.shift();
  }

  function undo() {
    if (state.spectatorMode || state.won || state.reportingWin || !state.undoStack.length) return;
    const prev = state.undoStack.pop();
    state.board = prev.map((row) =>
      row.map((cell) => ({
        value: cell.value | 0,
        pencil_marks: Array.isArray(cell.pencil_marks) ? [...cell.pencil_marks] : [],
      }))
    );
    state.status = "Move undone ↩";
    playFx("pop");
    syncControls();
    draw();
    if (typeof options.onProgress === "function") {
      try {
        options.onProgress();
      } catch (err) {
        console.warn("[Thcoku] onProgress", err);
      }
    }
  }

  function resetBoard() {
    if (
      state.spectatorMode
      || state.won
      || state.reportingWin
      || state.hintInFlight
      || !state.board
      || !state.given
    ) {
      return;
    }
    let changed = false;
    for (let r = 0; r < 9; r++) {
      for (let c = 0; c < 9; c++) {
        if (state.given[r][c]) continue;
        const cell = state.board[r][c];
        if ((cell?.value | 0) || (cell?.pencil_marks?.length || 0)) {
          changed = true;
          break;
        }
      }
      if (changed) break;
    }
    if (!changed) {
      state.status = "Nothing to reset";
      draw();
      return;
    }
    saveUndoState();
    for (let r = 0; r < 9; r++) {
      for (let c = 0; c < 9; c++) {
        if (state.given[r][c]) continue;
        state.board[r][c].value = 0;
        state.board[r][c].pencil_marks = [];
      }
    }
    state.status = state.hintsUsed
      ? `Board reset — ${state.hintsUsed} hint(s) already spent`
      : "Board reset — Undo to restore";
    playFx("pop");
    syncControls();
    draw();
    if (typeof options.onProgress === "function") {
      try {
        options.onProgress();
      } catch (err) {
        console.warn("[Thcoku] onProgress", err);
      }
    }
  }

  function maybeCelebrateAfterMove() {
    const conflicts = findConflicts(state.board);
    const n = filledCount(state.board);
    if (!state.serverHints && isSolved(state.board, state.solution)) {
      celebrateWin();
      return true;
    }
    if (state.serverHints && n >= 81 && conflicts.size === 0) {
      celebrateWin();
      return true;
    }
    return false;
  }

  function celebrateWin() {
    if (state.won || state.reportingWin) return;
    state.reportingWin = true;
    pauseTimer();
    const elapsed = getElapsedSec();
    const gen = state.boardGen;
    const run = async () => {
      try {
        if (typeof window.thcokuReportWin === "function") {
          const data = await window.thcokuReportWin(state.difficulty, elapsed, {
            board: state.board,
            given: state.given,
            solution: state.solution,
          });
          if (!data) return;
        }
        // Abort celebration if the board was replaced mid-report.
        if (gen !== state.boardGen) return;
        state.won = true;
        state.winAt = Date.now();
        state.winZoom = 1.04;
        const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
        const ss = String(elapsed % 60).padStart(2, "0");
        state.status = `Order up! ${mm}:${ss}`;
        spawnBubbles();
        spawnConfetti();
        playFx("win");
        draw();
        if (typeof options.onWin === "function") {
          try {
            options.onWin();
          } catch (err) {
            console.warn("[Thcoku] onWin", err);
          }
        }
      } finally {
        if (gen === state.boardGen) state.reportingWin = false;
        else state.reportingWin = false;
      }
    };
    void run();
  }

  async function hint() {
    if (state.spectatorMode || state.won || state.reportingWin || !state.board) return;
    if (state.hintInFlight) return;
    const unlimited =
      state.hintsMax == null || !Number.isFinite(Number(state.hintsMax)) || Number(state.hintsMax) <= 0;
    if (!unlimited && state.hintsUsed >= state.hintsMax) {
      state.status = `No hints left (${state.hintsUsed}/${state.hintsMax}).`;
      syncHintButton();
      draw();
      return;
    }

    let targetR = state.selected[0];
    let targetC = state.selected[1];

    // Prefer server hints whenever available so sponge charges stay authoritative.
    // Local solution fill is offline-only fallback.
    if (state.serverHints || !state.solution?.length || typeof options.onHint === "function") {
      if (typeof options.onHint !== "function") {
        state.status = "Hints unavailable offline.";
        draw();
        return;
      }
      state.hintInFlight = true;
      syncHintButton();
      const gen = state.boardGen;
      try {
        const result = await options.onHint({
          row: targetR,
          col: targetC,
          board: state.board,
        });
        if (!result?.ok) {
          if (result?.error === "insufficient_sponges") {
            const cost = Number(result.hint_cost) || state.hintSpongeCost;
            const pocket = Number(result.pocket ?? state.pocketSponges);
            state.pocketSponges = pocket;
            state.status = `Need ${cost} sponges for a hint (${pocket} in pocket) — win puzzles or buy Gary's Wisdom`;
          } else if (result?.error === "no_hint_available") {
            state.status = "Nothing to hint — board already correct!";
          } else {
            state.status =
              result?.error === "hints_exhausted"
                ? "No hints left for this puzzle."
                : "Hint unavailable — try again.";
          }
          applyHintMeta(result);
          draw();
          return;
        }
        targetR = Number(result.row);
        targetC = Number(result.col);
        const correctVal = Number(result.value);
        applyHintMeta(result);
        if (result.hints_used == null) state.hintsUsed += 1;
        if (gen !== state.boardGen) return;
        const costNote =
          result.paid_with === "gary"
            ? " · free (Gary)"
            : Number(result.hint_cost) > 0
              ? ` · −${result.hint_cost} 🧽`
              : "";
        const garyLeft = Math.max(0, state.garyWisdomBonus - state.hintsGaryUsed);
        let followUp = "";
        if (result.paid_with === "gary" && garyLeft === 0) {
          followUp = unlimited
            ? ` · next hints cost ${state.hintSpongeCost} 🧽 each (no limit)`
            : ` · next hints cost ${state.hintSpongeCost} 🧽 (max ${state.hintsMax})`;
        } else if (result.paid_with === "gary" && garyLeft > 0) {
          followUp = ` · ${garyLeft} free left`;
        }
        saveUndoState();
        state.selected = [targetR, targetC];
        setCellValue(state.board, targetR, targetC, correctVal);
        clearPencilDigitPeers(state.board, targetR, targetC, correctVal);
        state.flashCell = [targetR, targetC];
        state.flashUntil = Date.now() + 300;
        state.status = `Hint applied! 💡 (${correctVal}) · ${state.hintsUsed} used${costNote}${followUp}`;
        syncHintButton();
        playFx("hint");
        if (!maybeCelebrateAfterMove()) {
          draw();
        }
        if (typeof options.onProgress === "function") {
          try {
            options.onProgress();
          } catch (err) {
            console.warn("[Thcoku] onProgress", err);
          }
        }
      } catch (err) {
        console.warn("[Thcoku] onHint", err);
        state.status = "Hint failed — check connection.";
        draw();
      } finally {
        state.hintInFlight = false;
        syncHintButton();
      }
      return;
    }

    if (state.given[targetR][targetC] || cellValue(state.board, targetR, targetC) === state.solution[targetR][targetC]) {
      let found = false;
      for (let ri = 0; ri < 9; ri++) {
        for (let ci = 0; ci < 9; ci++) {
          if (!state.given[ri][ci] && cellValue(state.board, ri, ci) !== state.solution[ri][ci]) {
            targetR = ri;
            targetC = ci;
            found = true;
            break;
          }
        }
        if (found) break;
      }
      if (!found) return;
    }
    saveUndoState();
    state.selected = [targetR, targetC];
    const correctVal = state.solution[targetR][targetC];
    setCellValue(state.board, targetR, targetC, correctVal);
    clearPencilDigitPeers(state.board, targetR, targetC, correctVal);
    state.hintsUsed += 1;
    state.flashCell = [targetR, targetC];
    state.flashUntil = Date.now() + 300;
    state.status = `Hint applied! 💡 (${correctVal}) · ${state.hintsUsed} used`;
    playFx("hint");
    if (isSolved(state.board, state.solution)) {
      celebrateWin();
    } else {
      draw();
    }
    if (typeof options.onProgress === "function") {
      try {
        options.onProgress();
      } catch (err) {
        console.warn("[Thcoku] onProgress", err);
      }
    }
  }

  function place(digit) {
    if (state.spectatorMode || state.won || state.reportingWin) return;
    const [r, c] = state.selected;
    if (state.given[r][c]) {
      state.status = "Fixed clue — barnacles!";
      state.shakeUntil = Date.now() + 250;
      playFx("error");
      draw();
      ensureAnim();
      return;
    }

    saveUndoState();

    const currentVal = cellValue(state.board, r, c);
    const targetDigit = (currentVal === digit && digit !== 0) ? 0 : digit;

    if (state.pencilMode && targetDigit) {
      togglePencil(state.board, r, c, targetDigit);
      state.status = "Mrs. Puff note";
      state.flashCell = [r, c];
      state.flashUntil = Date.now() + 200;
      playFx("pop");
      draw();
      ensureAnim();
      if (typeof options.onProgress === "function") {
        try {
          options.onProgress();
        } catch (err) {
          console.warn("[Thcoku] onProgress", err);
        }
      }
      return;
    }
    setCellValue(state.board, r, c, targetDigit);
    if (targetDigit) clearPencilDigitPeers(state.board, r, c, targetDigit);
    state.flashCell = [r, c];
    state.flashUntil = Date.now() + 220;

    const conflicts = findConflicts(state.board);
    const n = filledCount(state.board);
    if (targetDigit && conflicts.has(`${r},${c}`)) {
      state.status = "Conflict — tartar sauce!";
      state.shakeUntil = Date.now() + 280;
      playFx("error");
      draw();
    } else if (maybeCelebrateAfterMove()) {
      /* celebrateWin draws */
    } else {
      state.status = digit ? fmt(pick(STATUS_OK), n) : fmt(pick(STATUS_CLEAR), n);
      if (digit) playFx("pop");
      draw();
    }
    ensureAnim();
    if (!state.won && typeof options.onProgress === "function") {
      try {
        options.onProgress();
      } catch (err) {
        console.warn("[Thcoku] onProgress", err);
      }
    }
    syncControls();
  }

  function cellAt(x, y) {
    const { x: ox, y: oy } = BOARD_ORIGIN;
    if (x < ox || x >= ox + BOARD_SIZE || y < oy || y >= oy + BOARD_SIZE) return null;
    return [Math.floor((y - oy) / CELL), Math.floor((x - ox) / CELL)];
  }

  function handleBoardPointer(x, y) {
    if (state.spectatorMode) return;
    const cell = cellAt(x, y);
    if (!cell) return;
    if (state.won && Date.now() - state.winAt > 800) {
      if (state.sessionKind === "daily" || state.sessionKind === "challenge") {
        state.status = pick([
          "Nice solve!",
          "GG!",
          "Order up!",
          "I'm ready!",
        ]);
        draw();
        return;
      }
      newGame();
      return;
    }
    if (state.won) return;
    state.selected = cell;
    state.status = pick(STATUS_PICK);
    draw();
  }

  function drawLagoon(now) {
    const grad = ctx.createLinearGradient(0, 0, 0, HEIGHT);
    grad.addColorStop(0, RGB.bg0);
    grad.addColorStop(0.55, RGB.bg1);
    grad.addColorStop(0.82, RGB.bg2);
    grad.addColorStop(1, RGB.bg3);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, WIDTH, HEIGHT);

    ctx.fillStyle = RGB.sunGlow;
    ctx.beginPath();
    ctx.arc(640, 36, 78, 0, Math.PI * 2);
    ctx.fill();

    // Sand bed
    ctx.fillStyle = RGB.sand;
    ctx.beginPath();
    ctx.moveTo(0, HEIGHT - 54);
    ctx.quadraticCurveTo(WIDTH * 0.25, HEIGHT - 78, WIDTH * 0.5, HEIGHT - 50);
    ctx.quadraticCurveTo(WIDTH * 0.75, HEIGHT - 28, WIDTH, HEIGHT - 62);
    ctx.lineTo(WIDTH, HEIGHT);
    ctx.lineTo(0, HEIGHT);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = RGB.sandDeep;
    ctx.beginPath();
    ctx.moveTo(0, HEIGHT - 28);
    ctx.quadraticCurveTo(WIDTH * 0.4, HEIGHT - 42, WIDTH, HEIGHT - 22);
    ctx.lineTo(WIDTH, HEIGHT);
    ctx.lineTo(0, HEIGHT);
    ctx.closePath();
    ctx.fill();

    // Seaweed (sway)
    const sway = Math.sin(now / 700) * 6;
    drawSeaweed(28, HEIGHT - 50, 70, sway);
    drawSeaweed(WIDTH - 36, HEIGHT - 48, 78, -sway * 0.8);
    drawSeaweed(70, HEIGHT - 42, 48, sway * 0.6);

    // Ambient bubbles
    for (const b of ambientBubbles) {
      const t = now / 1000;
      const y = ((b.y - t * b.speed) % (HEIGHT + 40) + HEIGHT + 40) % (HEIGHT + 40);
      const x = b.x + Math.sin(t * b.wobble + b.phase) * 10;
      const yy = HEIGHT - y;
      ctx.strokeStyle = RGB.bubbleStroke;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(x, yy, b.r, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = RGB.bubbleFill;
      ctx.beginPath();
      ctx.arc(x - b.r * 0.3, yy - b.r * 0.3, b.r * 0.25, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawSeaweed(x, baseY, h, sway) {
    ctx.strokeStyle = RGB.leafDark;
    ctx.lineWidth = 5;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(x, baseY);
    ctx.quadraticCurveTo(x + sway, baseY - h * 0.45, x + sway * 1.4, baseY - h);
    ctx.stroke();
    ctx.strokeStyle = RGB.leaf;
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(x + 1, baseY);
    ctx.quadraticCurveTo(x + sway * 0.7, baseY - h * 0.5, x + sway * 1.2, baseY - h + 6);
    ctx.stroke();
  }

  function drawPineappleFrame(ox, oy) {
    const pad = FRAME_PAD;
    const x = ox - pad;
    const y = oy - pad;
    const w = BOARD_SIZE + pad * 2;
    const h = BOARD_SIZE + pad * 2;

    // Short crown — stays in the gap under the header (header is redrawn on top)
    const cx = x + w / 2;
    ctx.fillStyle = RGB.leafDark;
    for (const [dx, rot] of [
      [-22, -0.4],
      [-8, -0.12],
      [8, 0.12],
      [22, 0.4],
    ]) {
      ctx.save();
      ctx.translate(cx + dx, y + 2);
      ctx.rotate(rot);
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.quadraticCurveTo(6, -12, 0, -22);
      ctx.quadraticCurveTo(-6, -12, 0, 0);
      ctx.fill();
      ctx.restore();
    }
    ctx.fillStyle = RGB.leaf;
    ctx.beginPath();
    ctx.moveTo(cx, y + 4);
    ctx.quadraticCurveTo(cx + 7, y - 8, cx, y - 18);
    ctx.quadraticCurveTo(cx - 7, y - 8, cx, y + 4);
    ctx.fill();

    // Gold shell
    ctx.fillStyle = RGB.gold;
    roundRect(ctx, x - 4, y - 4, w + 8, h + 8, 18);
    ctx.fill();
    ctx.fillStyle = RGB.goldDeep;
    roundRect(ctx, x, y, w, h, 14);
    ctx.fill();
    ctx.fillStyle = RGB.cardInner;
    roundRect(ctx, x + 5, y + 5, w - 10, h - 10, 10);
    ctx.fill();
  }

  function drawSpongePores(x, y, r, c) {
    const rng = mulberry32((r + 1) * 97 + (c + 1) * 13);
    ctx.fillStyle = RGB.spongePore;
    for (let i = 0; i < 5; i++) {
      const px = x + 10 + rng() * (CELL - 20);
      const py = y + 10 + rng() * (CELL - 20);
      const pr = 1.2 + rng() * 2.2;
      ctx.beginPath();
      ctx.arc(px, py, pr, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function draw() {
    const now = Date.now();
    drawLagoon(now);

    if (!state.board?.length || !state.given?.length) {
      drawHeader();
      return;
    }

    let shakeX = 0;
    if (now < state.shakeUntil) shakeX = Math.sin(now / 30) * 4;

    if (state.won) {
      const age = (now - state.winAt) / 1000;
      state.winZoom = 1 + Math.max(0, 0.05 - age * 0.02);
    }
    const zoom = state.won ? state.winZoom : 1;

    ctx.save();
    ctx.translate(shakeX + WIDTH / 2, HEIGHT / 2);
    ctx.scale(zoom, zoom);
    ctx.translate(-WIDTH / 2, -HEIGHT / 2);

    const conflicts = findConflicts(state.board);
    // Spectators: no cell selection / peer wash — keep the board readable.
    const sel = state.selected;
    const highlightSel =
      !state.spectatorMode &&
      Array.isArray(sel) &&
      Number(sel[0]) >= 0 &&
      Number(sel[1]) >= 0;
    const [sr, sc] = highlightSel ? sel : [-1, -1];
    const ox = BOARD_ORIGIN.x;
    const oy = BOARD_ORIGIN.y;

    drawPineappleFrame(ox, oy);

    for (let r = 0; r < 9; r++) {
      for (let c = 0; c < 9; c++) {
        const x = ox + c * CELL;
        const y = oy + r * CELL;
        const isSel = r === sr && c === sc;
        const sameBox =
          Math.floor(r / 3) === Math.floor(sr / 3) && Math.floor(c / 3) === Math.floor(sc / 3);
        const sameLine = r === sr || c === sc;
        const conflict = conflicts.has(`${r},${c}`);
        const flash =
          state.flashCell &&
          state.flashCell[0] === r &&
          state.flashCell[1] === c &&
          now < state.flashUntil;
        const isGiven = state.given[r][c];
        const selectedVal = cellValue(state.board, sr, sc);
        const cellVal = cellValue(state.board, r, c);
        const isMatch = selectedVal !== 0 && cellVal === selectedVal;

        let fill = isGiven ? RGB.given : RGB.empty;
        if (sameBox || sameLine) fill = isGiven ? RGB.givenHl : RGB.boxHl;
        if (isMatch) fill = RGB.matchHl;
        if (isSel || flash) fill = RGB.select;
        if (conflict) fill = RGB.conflict;

        ctx.fillStyle = fill;
        ctx.fillRect(x, y, CELL, CELL);
        if (isGiven && !conflict && !isSel) drawSpongePores(x, y, r, c);

        const val = cellValue(state.board, r, c);
        if (val) {
          ctx.fillStyle = conflict
            ? RGB.textConflict
            : isGiven
              ? RGB.textGiven
              : RGB.text;
          ctx.font = isGiven
            ? "700 34px Fredoka, Segoe UI, sans-serif"
            : "600 34px Fredoka, Segoe UI, sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(String(val), x + CELL / 2, y + CELL / 2 + 1);
        } else {
          const marks = state.board[r][c]?.pencil_marks || [];
          if (marks.length) {
            ctx.fillStyle = RGB.pencil;
            ctx.font = "500 13px Fredoka, Segoe UI, sans-serif";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            for (const d of marks) {
              const mr = Math.floor((d - 1) / 3);
              const mc = (d - 1) % 3;
              ctx.fillText(String(d), x + 14 + mc * 22, y + 14 + mr * 22);
            }
          }
        }
      }
    }

    for (let i = 0; i <= 9; i++) {
      ctx.strokeStyle = i % 3 === 0 ? RGB.thick : RGB.line;
      ctx.lineWidth = i % 3 === 0 ? 3.5 : 1;
      ctx.beginPath();
      ctx.moveTo(ox + i * CELL, oy);
      ctx.lineTo(ox + i * CELL, oy + BOARD_SIZE);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(ox, oy + i * CELL);
      ctx.lineTo(ox + BOARD_SIZE, oy + i * CELL);
      ctx.stroke();
    }
    ctx.restore();

    // Pins after the board so badges sit on top of the frame (not behind it)
    drawBorderPins();
    // Header last so pineapple leaves never cover the title text
    drawHeader();

    if (state.won) {
      const dt = 1 / 60;
      for (const b of state.bubbles) {
        const age = (now - state.winAt) / 1000;
        if (age > 4.5) continue;
        b.y -= b.vy * dt;
        b.x += b.vx * dt + Math.sin(now / 500 + b.phase) * 0.4;
        ctx.strokeStyle = RGB.bubble;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2);
        ctx.stroke();
      }
      for (const p of state.confetti) {
        const age = (now - state.winAt) / 1000;
        if (age > 4.5) continue;
        p.vy += 280 * dt;
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        p.rot += p.spin;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.rot);
        ctx.font = `${p.size}px Apple Color Emoji, Segoe UI Emoji, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(p.emoji, 0, 0);
        ctx.restore();
      }

      const bw = 380;
      const bh = 78;
      const bx = (WIDTH - bw) / 2;
      const by = 292;
      ctx.fillStyle = RGB.win;
      roundRect(ctx, bx, by, bw, bh, 20);
      ctx.fill();
      ctx.strokeStyle = RGB.gold;
      ctx.lineWidth = 4;
      roundRect(ctx, bx, by, bw, bh, 20);
      ctx.stroke();
      ctx.fillStyle = RGB.panel;
      ctx.font = "700 34px Fredoka, Segoe UI, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("Order up! 🍍", WIDTH / 2, by + bh / 2);
    }
  }

  function ensureAnim() {
    if (state.raf) return;
    const tick = () => {
      state.raf = 0;
      const now = Date.now();
      const need =
        now < state.shakeUntil ||
        now < state.flashUntil ||
        (state.won && now - state.winAt < 5000);
      draw();
      if (need) state.raf = requestAnimationFrame(tick);
    };
    state.raf = requestAnimationFrame(tick);
  }

  function startAmbientLoop() {
    if (ambientRaf) return;
    const tick = () => {
      // Skip redraw when a transient anim owns the loop
      if (!state.raf) draw();
      ambientRaf = requestAnimationFrame(tick);
    };
    ambientRaf = requestAnimationFrame(tick);
  }

  function canvasPos(evt) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
      x: (evt.clientX - rect.left) * scaleX,
      y: (evt.clientY - rect.top) * scaleY,
    };
  }

  canvas.addEventListener("pointerdown", (evt) => {
    getAudioCtx();
    if (musicEnabled) ensureBgmStarted();
    const { x, y } = canvasPos(evt);
    handleBoardPointer(x, y);
  });

  controls.addEventListener("click", (evt) => {
    const btn = evt.target.closest("button");
    if (!btn) return;
    const digit = btn.dataset.digit;
    const action = btn.dataset.action;
    if (digit) {
      place(Number(digit));
      return;
    }
    if (action === "clear") place(0);
    else if (action === "reset") resetBoard();
    else if (action === "undo") undo();
    else if (action === "hint") void hint();
    else if (action === "quit") {
      if (state.sessionKind === "challenge") {
        const ok = window.confirm(
          "Forfeit this speedrun challenge?\n\n"
            + "Closing the Activity window without Forfeit keeps you in the race."
        );
        if (!ok) return;
      } else if (state.sessionKind === "daily") {
        const ok = window.confirm(
          "Quit today's daily?\n\nThis forfeits your pineapple for today."
        );
        if (!ok) return;
      }
      if (typeof options.onQuit === "function") options.onQuit();
    } else if (action === "new") {
      if (state.sessionKind !== "daily" && state.sessionKind !== "challenge") newGame();
    } else if (action === "diff") {
      if (!difficultyLocked()) {
        state.diffIndex = (state.diffIndex + 1) % DIFF_KEYS.length;
        newGame();
      }
    } else if (action === "pencil") {
      state.pencilMode = !state.pencilMode;
      state.status = state.pencilMode ? "Pencil ON — Mrs. Puff mode" : "Pencil OFF";
      syncControls();
      draw();
    }
  });

  window.addEventListener("keydown", (evt) => {
    if (evt.key >= "1" && evt.key <= "9") place(Number(evt.key));
    else if (evt.key === "0" || evt.key === "Backspace" || evt.key === "Delete") place(0);
    else if ((evt.ctrlKey || evt.metaKey) && evt.key.toLowerCase() === "z") {
      evt.preventDefault();
      undo();
    } else if (evt.key.toLowerCase() === "u") undo();
    else if (evt.key.toLowerCase() === "r") resetBoard();
    else if (evt.key.toLowerCase() === "h") void hint();
    else if (evt.key === "p" || evt.key === "P") {
      state.pencilMode = !state.pencilMode;
      syncControls();
      draw();
    } else if (evt.key === "q" || evt.key === "Q") {
      if (state.sessionKind === "challenge") {
        const ok = window.confirm(
          "Forfeit this speedrun challenge?\n\n"
            + "Closing the Activity window without Forfeit keeps you in the race."
        );
        if (!ok) return;
      } else if (state.sessionKind === "daily") {
        const ok = window.confirm(
          "Quit today's daily?\n\nThis forfeits your pineapple for today."
        );
        if (!ok) return;
      }
      if (typeof options.onQuit === "function") options.onQuit();
    }
    else if (state.spectatorMode) {
      /* arrows / selection disabled while watching */
    } else if (evt.key === "ArrowLeft") {
      state.selected[1] = (state.selected[1] + 8) % 9;
      draw();
    } else if (evt.key === "ArrowRight") {
      state.selected[1] = (state.selected[1] + 1) % 9;
      draw();
    } else if (evt.key === "ArrowUp") {
      state.selected[0] = (state.selected[0] + 8) % 9;
      draw();
    } else if (evt.key === "ArrowDown") {
      state.selected[0] = (state.selected[0] + 1) % 9;
      draw();
    }
  });

  if (musicEnabled) ensureBgmStarted();

  if (options.autoStart !== false && !options.spectatorMode) {
    newGame();
    if (typeof options.onBoardReady === "function") {
      try {
        options.onBoardReady();
      } catch (err) {
        console.warn("[Thcoku] onBoardReady", err);
      }
    }
  } else {
    state.status = options.spectatorMode ? "Connecting spectator view…" : "Loading…";
    if (options.spectatorMode) syncControls();
    draw();
  }
  function setTheme(themeName) {
    activeTheme = themeName;
    if (themeName === "dark") {
      Object.assign(RGB, DARK_PALETTE);
    } else if (themeName === "jellyfish") {
      Object.assign(RGB, JELLYFISH_PALETTE);
    } else if (themeName === "krabs") {
      Object.assign(RGB, KRABS_PALETTE);
    } else if (themeName === "rockbottom") {
      Object.assign(RGB, ROCKBOTTOM_PALETTE);
    } else {
      Object.assign(RGB, LIGHT_PALETTE);
    }
    draw();
  }

  return {
    newGame,
    place,
    draw,
    setCosmetics,
    setPocketSponges(n) {
      state.pocketSponges = Number(n) || 0;
      syncHintButton();
    },
    setTheme,
    getSnapshot,
    getStartSnapshot,
    loadSnapshot,
    loadSpectatorSnapshot,
    syncControls,
    applyHintMeta,
    setWatchers,
    pauseTimer,
    resumeTimer,
    getElapsedSec,
  };
}
