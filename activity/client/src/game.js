/**
 * Thcoku Sudoku — Canvas 2D (no Pyodide; Discord Activity friendly).
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
  bg: "#7dd3fc",
  card: "#fffbeb",
  empty: "#fffef5",
  given: "#fef3c7",
  select: "#fde047",
  boxHl: "#a5f3fc",
  conflict: "#fda4af",
  line: "#94a3b8",
  thick: "#0f766e",
  text: "#1d4ed8",
  textGiven: "#134e4a",
  textConflict: "#be123c",
  pencil: "#64748b",
  header: "#0f766e",
  ui: "#f59e0b",
  panel: "#fff8dc",
  pad: "#fffbeb",
  padPress: "#fde047",
  win: "#34d399",
  bubble: "#bae6fd",
};

const WIDTH = 720;
const HEIGHT = 980;
const BOARD_ORIGIN = { x: 48, y: 100 };
const CELL = 68;
const BOARD_SIZE = CELL * 9;
const PAD_KEY = 72;
const PAD_GAP = 10;
const PAD_ORIGIN_Y = 740;

function discordUsername() {
  return (
    window.__DISCORD_AUTH__?.user?.global_name ||
    window.__DISCORD_AUTH__?.user?.username ||
    ""
  );
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

export function startThcokuGame(canvas) {
  if (!canvas) return null;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  canvas.width = WIDTH;
  canvas.height = HEIGHT;

  const padW = 3 * PAD_KEY + 2 * PAD_GAP;
  const padX = Math.floor((WIDTH - padW) / 2);
  const padKeys = {};
  for (let n = 1; n <= 9; n++) {
    const row = Math.floor((n - 1) / 3);
    const col = (n - 1) % 3;
    padKeys[n] = {
      x: padX + col * (PAD_KEY + PAD_GAP),
      y: PAD_ORIGIN_Y + row * (PAD_KEY + PAD_GAP),
      w: PAD_KEY,
      h: PAD_KEY,
    };
  }
  padKeys.clear = {
    x: padX,
    y: PAD_ORIGIN_Y + 3 * (PAD_KEY + PAD_GAP),
    w: padW,
    h: 48,
  };
  const actionY = padKeys.clear.y + padKeys.clear.h + 14;
  const buttons = {
    new: { x: 40, y: actionY, w: 140, h: 44 },
    diff: { x: 200, y: actionY, w: 200, h: 44 },
    pencil: { x: 420, y: actionY, w: 140, h: 44 },
  };

  const state = {
    diffIndex: DIFF_KEYS.indexOf(DEFAULT_DIFFICULTY),
    selected: [0, 0],
    pencilMode: false,
    status: "A gerar puzzle…",
    won: false,
    startedAt: Date.now(),
    board: [],
    given: [],
    solution: [],
    difficulty: DEFAULT_DIFFICULTY,
    flashCell: null,
    flashUntil: 0,
    pressKey: null,
    pressUntil: 0,
    winAt: 0,
    bubbles: [],
    shakeUntil: 0,
    raf: 0,
  };

  function hit(rect, x, y) {
    return x >= rect.x && x < rect.x + rect.w && y >= rect.y && y < rect.y + rect.h;
  }

  function spawnBubbles() {
    state.bubbles = [];
    for (let i = 0; i < 28; i++) {
      state.bubbles.push({
        x: BOARD_ORIGIN.x + Math.random() * BOARD_SIZE,
        y: BOARD_ORIGIN.y + BOARD_SIZE + Math.random() * 40,
        r: 6 + Math.random() * 10,
        vy: 40 + Math.random() * 70,
        vx: -18 + Math.random() * 36,
        phase: Math.random() * Math.PI * 2,
      });
    }
  }

  function newGame() {
    const key = DIFF_KEYS[state.diffIndex];
    state.status = `A gerar (${difficultyLabel(key)})…`;
    state.won = false;
    state.bubbles = [];
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
    const hello = user ? `Olá, ${user}! ` : "";
    state.status = `${hello}Toca numa célula, depois num número`;
    draw();
  }

  function place(digit) {
    if (state.won) return;
    const [r, c] = state.selected;
    if (state.given[r][c]) {
      state.status = "Essa célula é uma pista fixa";
      state.shakeUntil = Date.now() + 250;
      draw();
      return;
    }
    if (state.pencilMode && digit) {
      togglePencil(state.board, r, c, digit);
      state.status = "Nota a lápis";
      state.flashCell = [r, c];
      state.flashUntil = Date.now() + 200;
      draw();
      return;
    }
    setCellValue(state.board, r, c, digit);
    if (digit) clearPencilDigitPeers(state.board, r, c, digit);
    state.flashCell = [r, c];
    state.flashUntil = Date.now() + 220;

    const conflicts = findConflicts(state.board);
    if (digit && conflicts.has(`${r},${c}`)) {
      state.status = "Conflito — tenta outro número";
      state.shakeUntil = Date.now() + 280;
    } else if (isSolved(state.board, state.solution)) {
      state.won = true;
      state.winAt = Date.now();
      const elapsed = Math.floor((Date.now() - state.startedAt) / 1000);
      const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
      const ss = String(elapsed % 60).padStart(2, "0");
      state.status = `Resolvido em ${mm}:${ss}!`;
      spawnBubbles();
      if (typeof window.thcokuReportWin === "function") {
        window.thcokuReportWin(state.difficulty, elapsed);
      }
      ensureAnim();
    } else {
      const filled = filledCount(state.board);
      state.status = `${digit ? "Ok" : "Apagado"} · ${filled}/81`;
    }
    draw();
  }

  function cellAt(x, y) {
    const { x: ox, y: oy } = BOARD_ORIGIN;
    if (x < ox || x >= ox + BOARD_SIZE || y < oy || y >= oy + BOARD_SIZE) return null;
    return [Math.floor((y - oy) / CELL), Math.floor((x - ox) / CELL)];
  }

  function handlePointer(x, y) {
    if (state.won && Date.now() - state.winAt > 800) {
      if (hit(buttons.new, x, y)) {
        newGame();
        return;
      }
      if (hit(buttons.diff, x, y)) {
        state.diffIndex = (state.diffIndex + 1) % DIFF_KEYS.length;
        newGame();
        return;
      }
    }

    const cell = cellAt(x, y);
    if (cell && !state.won) {
      state.selected = cell;
      state.status = "Célula escolhida — toca num número";
      draw();
      return;
    }

    for (const [key, rect] of Object.entries(padKeys)) {
      if (!hit(rect, x, y)) continue;
      state.pressKey = key === "clear" ? "clear" : Number(key);
      state.pressUntil = Date.now() + 150;
      if (key === "clear") place(0);
      else place(Number(key));
      ensureAnim();
      return;
    }

    if (hit(buttons.new, x, y)) newGame();
    else if (hit(buttons.diff, x, y)) {
      state.diffIndex = (state.diffIndex + 1) % DIFF_KEYS.length;
      newGame();
    } else if (hit(buttons.pencil, x, y)) {
      state.pencilMode = !state.pencilMode;
      state.status = state.pencilMode ? "Modo lápis ON" : "Modo lápis OFF";
      draw();
    }
  }

  function drawButton(rect, label, active = false) {
    ctx.fillStyle = active ? RGB.padPress : RGB.ui;
    roundRect(ctx, rect.x, rect.y, rect.w, rect.h, 12);
    ctx.fill();
    ctx.strokeStyle = RGB.header;
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = RGB.header;
    ctx.font = "600 18px Fredoka, Segoe UI, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, rect.x + rect.w / 2, rect.y + rect.h / 2 + 1);
  }

  function drawPadKey(key, rect, label) {
    const pressed = state.pressKey === key && Date.now() < state.pressUntil;
    ctx.fillStyle = pressed ? RGB.padPress : RGB.pad;
    roundRect(ctx, rect.x, rect.y, rect.w, rect.h, 14);
    ctx.fill();
    ctx.strokeStyle = RGB.header;
    ctx.lineWidth = 3;
    ctx.stroke();
    ctx.fillStyle = RGB.header;
    ctx.font = key === "clear" ? "600 22px Fredoka, Segoe UI, sans-serif" : "700 32px Fredoka, Segoe UI, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, rect.x + rect.w / 2, rect.y + rect.h / 2 + 1);
  }

  function draw() {
    const now = Date.now();
    let shakeX = 0;
    if (now < state.shakeUntil) {
      shakeX = Math.sin(now / 30) * 4;
    }

    ctx.clearRect(0, 0, WIDTH, HEIGHT);
    const grad = ctx.createLinearGradient(0, 0, WIDTH, HEIGHT);
    grad.addColorStop(0, "#7dd3fc");
    grad.addColorStop(0.55, "#38bdf8");
    grad.addColorStop(1, "#0ea5e9");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, WIDTH, HEIGHT);

    ctx.fillStyle = "rgba(186,230,253,0.55)";
    ctx.beginPath();
    ctx.arc(80, 900, 120, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(254,243,199,0.55)";
    ctx.beginPath();
    ctx.arc(680, 60, 90, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = RGB.panel;
    roundRect(ctx, 20, 16, WIDTH - 40, 64, 16);
    ctx.fill();
    ctx.fillStyle = RGB.header;
    ctx.font = "700 28px Fredoka, Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText("Thcoku", 36, 48);
    ctx.font = "600 16px Fredoka, Segoe UI, sans-serif";
    ctx.fillText(state.status.slice(0, 42), 180, 48);

    ctx.save();
    ctx.translate(shakeX, 0);

    const conflicts = findConflicts(state.board);
    const [sr, sc] = state.selected;
    const ox = BOARD_ORIGIN.x;
    const oy = BOARD_ORIGIN.y;

    ctx.fillStyle = RGB.card;
    roundRect(ctx, ox - 8, oy - 8, BOARD_SIZE + 16, BOARD_SIZE + 16, 12);
    ctx.fill();

    for (let r = 0; r < 9; r++) {
      for (let c = 0; c < 9; c++) {
        const x = ox + c * CELL;
        const y = oy + r * CELL;
        const isSel = r === sr && c === sc;
        const sameBox = Math.floor(r / 3) === Math.floor(sr / 3) && Math.floor(c / 3) === Math.floor(sc / 3);
        const sameLine = r === sr || c === sc;
        const conflict = conflicts.has(`${r},${c}`);
        const flash =
          state.flashCell &&
          state.flashCell[0] === r &&
          state.flashCell[1] === c &&
          now < state.flashUntil;

        let fill = state.given[r]?.[c] ? RGB.given : RGB.empty;
        if (sameBox || sameLine) fill = RGB.boxHl;
        if (isSel || flash) fill = RGB.select;
        if (conflict) fill = RGB.conflict;

        ctx.fillStyle = fill;
        ctx.fillRect(x, y, CELL, CELL);

        const val = cellValue(state.board, r, c);
        if (val) {
          ctx.fillStyle = conflict
            ? RGB.textConflict
            : state.given[r][c]
              ? RGB.textGiven
              : RGB.text;
          ctx.font = state.given[r][c]
            ? "700 32px Fredoka, Segoe UI, sans-serif"
            : "600 32px Fredoka, Segoe UI, sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(String(val), x + CELL / 2, y + CELL / 2 + 1);
        } else {
          const marks = state.board[r][c]?.pencil_marks || [];
          if (marks.length) {
            ctx.fillStyle = RGB.pencil;
            ctx.font = "500 12px Fredoka, Segoe UI, sans-serif";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            for (const d of marks) {
              const mr = Math.floor((d - 1) / 3);
              const mc = (d - 1) % 3;
              ctx.fillText(
                String(d),
                x + 12 + mc * 22,
                y + 12 + mr * 22
              );
            }
          }
        }
      }
    }

    for (let i = 0; i <= 9; i++) {
      ctx.strokeStyle = i % 3 === 0 ? RGB.thick : RGB.line;
      ctx.lineWidth = i % 3 === 0 ? 3 : 1;
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

    ctx.fillStyle = "#fff";
    roundRect(ctx, ox, 720, BOARD_SIZE, 8, 4);
    ctx.fill();
    const progress = filledCount(state.board) / 81;
    ctx.fillStyle = RGB.header;
    roundRect(ctx, ox, 720, BOARD_SIZE * progress, 8, 4);
    ctx.fill();

    ctx.fillStyle = RGB.header;
    ctx.font = "600 14px Fredoka, Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("Teclado · toca no número ou usa 1–9", 40, 732);

    for (let n = 1; n <= 9; n++) drawPadKey(n, padKeys[n], String(n));
    drawPadKey("clear", padKeys.clear, "Apagar");
    drawButton(buttons.new, "Novo");
    drawButton(buttons.diff, difficultyLabel(DIFF_KEYS[state.diffIndex]));
    drawButton(buttons.pencil, state.pencilMode ? "Lápis ON" : "Lápis", state.pencilMode);

    if (state.won) {
      for (const b of state.bubbles) {
        const age = (now - state.winAt) / 1000;
        if (age > 4.5) continue;
        b.y -= b.vy * (1 / 60);
        b.x += b.vx * (1 / 60) + Math.sin(now / 500 + b.phase) * 0.4;
        ctx.strokeStyle = RGB.bubble;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2);
        ctx.stroke();
        ctx.fillStyle = "#fff";
        ctx.beginPath();
        ctx.arc(b.x - b.r / 3, b.y - b.r / 3, Math.max(1, b.r / 4), 0, Math.PI * 2);
        ctx.fill();
      }

      const pop = Math.min(1, ((now - state.winAt) / 1000) * 2.2);
      const scale = 0.85 + 0.15 * (1 - (1 - pop) ** 3);
      const bw = 420 * scale;
      const bh = 88 * scale;
      const bx = (WIDTH - bw) / 2;
      const by = 360;
      ctx.fillStyle = RGB.win;
      roundRect(ctx, bx, by, bw, bh, 20);
      ctx.fill();
      ctx.strokeStyle = RGB.header;
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.fillStyle = RGB.panel;
      ctx.font = "700 36px Fredoka, Segoe UI, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("Resolvido!", WIDTH / 2, by + bh / 2);
    }
  }

  function ensureAnim() {
    if (state.raf) return;
    const tick = () => {
      state.raf = 0;
      const now = Date.now();
      const need =
        now < state.shakeUntil ||
        now < state.pressUntil ||
        now < state.flashUntil ||
        (state.won && now - state.winAt < 5000);
      draw();
      if (need) {
        state.raf = requestAnimationFrame(tick);
      }
    };
    state.raf = requestAnimationFrame(tick);
  }

  function canvasPos(evt) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    const clientX = evt.touches?.[0]?.clientX ?? evt.clientX;
    const clientY = evt.touches?.[0]?.clientY ?? evt.clientY;
    return {
      x: (clientX - rect.left) * scaleX,
      y: (clientY - rect.top) * scaleY,
    };
  }

  canvas.addEventListener("pointerdown", (evt) => {
    canvas.setPointerCapture?.(evt.pointerId);
    const { x, y } = canvasPos(evt);
    handlePointer(x, y);
  });

  window.addEventListener("keydown", (evt) => {
    if (evt.key >= "1" && evt.key <= "9") place(Number(evt.key));
    else if (evt.key === "0" || evt.key === "Backspace" || evt.key === "Delete") place(0);
    else if (evt.key === "p" || evt.key === "P") {
      state.pencilMode = !state.pencilMode;
      state.status = state.pencilMode ? "Modo lápis ON" : "Modo lápis OFF";
      draw();
    } else if (evt.key === "n" || evt.key === "N") newGame();
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

  newGame();
  return { newGame, draw };
}
