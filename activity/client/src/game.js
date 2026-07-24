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
} from "./sudoku-core.js";

const RGB = {
  empty: "#fffef5",
  given: "#facc15",
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
};

const WIDTH = 720;
const HEIGHT = 780;
// Extra side margin so pin badges sit fully outside the pineapple frame.
const BOARD_ORIGIN = { x: 64, y: 108 };
const CELL = 64;
const BOARD_SIZE = CELL * 9;
const FRAME_PAD = 16;
const PIN_RADIUS = 22;

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

function ensureControls(shell) {
  let bar = document.getElementById("game-controls");
  if (bar) return bar;
  bar = document.createElement("div");
  bar.id = "game-controls";
  bar.innerHTML = `
    <div class="ctrl-pad" role="group" aria-label="Numbers">
      ${[1, 2, 3, 4, 5, 6, 7, 8, 9]
        .map((n) => `<button type="button" class="ctrl-digit" data-digit="${n}">${n}</button>`)
        .join("")}
    </div>
    <button type="button" class="ctrl-clear" data-action="clear">Erase</button>
    <div class="ctrl-actions" role="group" aria-label="Actions">
      <button type="button" data-action="new">New order</button>
      <button type="button" data-action="diff" id="ctrl-diff">Medium</button>
      <button type="button" data-action="pencil" id="ctrl-pencil">Notes</button>
    </div>
  `;
  shell.appendChild(bar);
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
    diffIndex: Math.max(0, DIFF_KEYS.indexOf(DEFAULT_DIFFICULTY)),
    selected: [0, 0],
    pencilMode: false,
    status: "Generating…",
    won: false,
    startedAt: Date.now(),
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
    sessionKind: options.sessionKind || null,
  };

  const diffBtn = controls.querySelector("#ctrl-diff");
  const pencilBtn = controls.querySelector("#ctrl-pencil");

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
    for (let i = 0; i < 9; i++) {
      const y = oy + i * CELL + CELL / 2;
      slots.push({ x: leftX, y });
      slots.push({ x: rightX, y });
    }
    for (let i = 0; i < 9; i++) {
      slots.push({ x: ox + i * CELL + CELL / 2, y: bottomY });
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
      ctx.fillStyle = "rgba(255, 248, 220, 0.98)";
      ctx.fill();
      ctx.lineWidth = 3;
      ctx.strokeStyle = RGB.gold;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(slot.x - 5, slot.y - 6, 6, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255, 255, 255, 0.5)";
      ctx.fill();
      ctx.font = "32px Apple Color Emoji, Segoe UI Emoji, Segoe UI Symbol, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(emoji, slot.x, slot.y + 1);
    }
  }

  function drawHeader() {
    ctx.fillStyle = RGB.panel;
    roundRect(ctx, 20, 14, WIDTH - 40, 64, 14);
    ctx.fill();
    ctx.strokeStyle = "rgba(245, 158, 11, 0.45)";
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
    ctx.fillText(String(state.status).slice(0, 42), 36, 58);
  }

  function getSnapshot() {
    if (!state.board?.length || state.won) return null;
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

  function getStartSnapshot() {
    /** Board state for watch notify as soon as /play opens (before any moves). */
    if (!state.board?.length || state.won) return null;
    return snapshotPayload();
  }

  function snapshotPayload() {
    return {
      difficulty: state.difficulty,
      diff_index: state.diffIndex,
      elapsed: Math.floor((Date.now() - state.startedAt) / 1000),
      board: state.board,
      given: state.given,
      solution: state.solution,
      filled: filledCount(state.board),
      session_kind: state.sessionKind,
    };
  }

  function loadSnapshot(snap) {
    if (!snap?.board || !snap?.given || !snap?.solution) return false;
    state.board = snap.board;
    state.given = snap.given;
    state.solution = snap.solution;
    state.difficulty = snap.difficulty || DEFAULT_DIFFICULTY;
    const idx = DIFF_KEYS.indexOf(state.difficulty);
    state.diffIndex = snap.diff_index != null ? Number(snap.diff_index) : idx >= 0 ? idx : 0;
    state.selected = [0, 0];
    state.won = false;
    state.bubbles = [];
    state.confetti = [];
    state.pencilMode = false;
    state.flashCell = null;
    state.startedAt = Date.now() - Math.max(0, Number(snap.elapsed) || 0) * 1000;
    state.sessionKind = snap.session_kind || null;
    const user = discordUsername();
    const hello = user ? `Hey, ${user}! ` : "";
    state.status = `${hello}Continuing · ${filledCount(state.board)}/81`;
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

  function syncControls() {
    if (diffBtn) diffBtn.textContent = difficultyLabel(DIFF_KEYS[state.diffIndex]);
    if (pencilBtn) {
      pencilBtn.textContent = state.pencilMode ? "Notes ON" : "Notes";
      pencilBtn.classList.toggle("is-active", state.pencilMode);
    }
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
    if (state.sessionKind === "daily" || state.sessionKind === "challenge") {
      return;
    }
    if (typeof options.onNewGame === "function") {
      try {
        options.onNewGame();
      } catch (err) {
        console.warn("[Thcoku] onNewGame", err);
      }
    }
    const key = DIFF_KEYS[state.diffIndex];
    state.status = `Cooking (${difficultyLabel(key)})…`;
    state.won = false;
    state.bubbles = [];
    state.confetti = [];
    state.winZoom = 1;
    draw();
    const puzzle = makePuzzle(key);
    state.board = puzzle.board;
    state.given = puzzle.given;
    state.solution = puzzle.solution;
    state.difficulty = puzzle.difficulty;
    state.selected = [0, 0];
    state.startedAt = Date.now();
    state.pencilMode = false;
    state.flashCell = null;
    const user = discordUsername();
    state.status = user ? `Hey, ${user}! I'm ready!` : "Tap a cell — I'm ready!";
    syncControls();
    draw();
    if (typeof options.onBoardReady === "function") {
      try {
        options.onBoardReady();
      } catch (err) {
        console.warn("[Thcoku] onBoardReady", err);
      }
    }
  }

  function place(digit) {
    if (state.won) return;
    const [r, c] = state.selected;
    if (state.given[r][c]) {
      state.status = "Fixed clue — barnacles!";
      state.shakeUntil = Date.now() + 250;
      draw();
      ensureAnim();
      return;
    }
    if (state.pencilMode && digit) {
      togglePencil(state.board, r, c, digit);
      state.status = "Mrs. Puff note";
      state.flashCell = [r, c];
      state.flashUntil = Date.now() + 200;
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
    setCellValue(state.board, r, c, digit);
    if (digit) clearPencilDigitPeers(state.board, r, c, digit);
    state.flashCell = [r, c];
    state.flashUntil = Date.now() + 220;

    const conflicts = findConflicts(state.board);
    const n = filledCount(state.board);
    if (digit && conflicts.has(`${r},${c}`)) {
      state.status = "Conflict — tartar sauce!";
      state.shakeUntil = Date.now() + 280;
    } else if (isSolved(state.board, state.solution)) {
      state.won = true;
      state.winAt = Date.now();
      state.winZoom = 1.04;
      const elapsed = Math.floor((Date.now() - state.startedAt) / 1000);
      const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
      const ss = String(elapsed % 60).padStart(2, "0");
      state.status = `Order up! ${mm}:${ss}`;
      spawnBubbles();
      spawnConfetti();
      if (typeof window.thcokuReportWin === "function") {
        window.thcokuReportWin(state.difficulty, elapsed, {
          board: state.board,
          given: state.given,
          solution: state.solution,
        });
      }
    } else {
      state.status = digit ? fmt(pick(STATUS_OK), n) : fmt(pick(STATUS_CLEAR), n);
    }
    draw();
    ensureAnim();
    if (typeof options.onProgress === "function") {
      try {
        options.onProgress();
      } catch (err) {
        console.warn("[Thcoku] onProgress", err);
      }
    }
  }

  function cellAt(x, y) {
    const { x: ox, y: oy } = BOARD_ORIGIN;
    if (x < ox || x >= ox + BOARD_SIZE || y < oy || y >= oy + BOARD_SIZE) return null;
    return [Math.floor((y - oy) / CELL), Math.floor((x - ox) / CELL)];
  }

  function handleBoardPointer(x, y) {
    const cell = cellAt(x, y);
    if (!cell) return;
    if (state.won && Date.now() - state.winAt > 800) {
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
    grad.addColorStop(0, "#7dd3fc");
    grad.addColorStop(0.55, "#38bdf8");
    grad.addColorStop(0.82, "#0ea5e9");
    grad.addColorStop(1, "#0284c7");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, WIDTH, HEIGHT);

    // Soft sunlight
    ctx.fillStyle = "rgba(254, 243, 199, 0.4)";
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
      ctx.strokeStyle = "rgba(186, 230, 253, 0.75)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(x, yy, b.r, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = "rgba(255, 255, 255, 0.25)";
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
    ctx.fillStyle = "#fffbeb";
    roundRect(ctx, x + 5, y + 5, w - 10, h - 10, 10);
    ctx.fill();
  }

  function drawSpongePores(x, y, r, c) {
    const rng = mulberry32((r + 1) * 97 + (c + 1) * 13);
    ctx.fillStyle = "rgba(146, 64, 14, 0.14)";
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
    const [sr, sc] = state.selected;
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

        let fill = isGiven ? RGB.given : RGB.empty;
        if (sameBox || sameLine) fill = isGiven ? "#fde047" : RGB.boxHl;
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
    else if (action === "new") {
      if (state.sessionKind !== "daily" && state.sessionKind !== "challenge") newGame();
    } else if (action === "diff") {
      if (state.sessionKind !== "daily" && state.sessionKind !== "challenge") {
        state.diffIndex = (state.diffIndex + 1) % DIFF_KEYS.length;
        newGame();
      }
    } else if (action === "pencil") {
      state.pencilMode = !state.pencilMode;
      state.status = state.pencilMode ? "Notes ON — Mrs. Puff mode" : "Notes OFF";
      syncControls();
      draw();
    }
  });

  window.addEventListener("keydown", (evt) => {
    if (evt.key >= "1" && evt.key <= "9") place(Number(evt.key));
    else if (evt.key === "0" || evt.key === "Backspace" || evt.key === "Delete") place(0);
    else if (evt.key === "p" || evt.key === "P") {
      state.pencilMode = !state.pencilMode;
      syncControls();
      draw();
    } else if (evt.key === "n" || evt.key === "N") {
      if (state.sessionKind !== "daily" && state.sessionKind !== "challenge") newGame();
    }
    else if (evt.key === "ArrowLeft") {
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

  if (options.autoStart !== false) {
    newGame();
    if (typeof options.onBoardReady === "function") {
      try {
        options.onBoardReady();
      } catch (err) {
        console.warn("[Thcoku] onBoardReady", err);
      }
    }
  } else {
    state.status = "Loading…";
    draw();
  }
  startAmbientLoop();
  return { newGame, place, draw, setCosmetics, getSnapshot, getStartSnapshot, loadSnapshot };
}
