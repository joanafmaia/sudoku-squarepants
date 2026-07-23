/**
 * Thcoku Sudoku — Canvas board + HTML controls (mobile-friendly).
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
  panel: "#fff8dc",
  win: "#34d399",
  bubble: "#bae6fd",
};

const WIDTH = 720;
const HEIGHT = 720;
const BOARD_ORIGIN = { x: 36, y: 88 };
const CELL = 72;
const BOARD_SIZE = CELL * 9;

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

function ensureControls(shell) {
  let bar = document.getElementById("game-controls");
  if (bar) return bar;
  bar = document.createElement("div");
  bar.id = "game-controls";
  bar.innerHTML = `
    <div class="ctrl-pad" role="group" aria-label="Números">
      ${[1, 2, 3, 4, 5, 6, 7, 8, 9]
        .map((n) => `<button type="button" class="ctrl-digit" data-digit="${n}">${n}</button>`)
        .join("")}
      <button type="button" class="ctrl-clear" data-action="clear">Apagar</button>
    </div>
    <div class="ctrl-actions" role="group" aria-label="Ações">
      <button type="button" data-action="new">Novo</button>
      <button type="button" data-action="diff" id="ctrl-diff">Medium</button>
      <button type="button" data-action="pencil" id="ctrl-pencil">Lápis</button>
    </div>
  `;
  shell.appendChild(bar);
  return bar;
}

export function startThcokuGame(canvas) {
  if (!canvas) return null;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const shell = canvas.closest("#game-shell") || canvas.parentElement;
  const controls = ensureControls(shell);

  canvas.width = WIDTH;
  canvas.height = HEIGHT;

  const state = {
    diffIndex: Math.max(0, DIFF_KEYS.indexOf(DEFAULT_DIFFICULTY)),
    selected: [0, 0],
    pencilMode: false,
    status: "A gerar…",
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
    shakeUntil: 0,
    raf: 0,
  };

  const diffBtn = controls.querySelector("#ctrl-diff");
  const pencilBtn = controls.querySelector("#ctrl-pencil");

  function syncControls() {
    if (diffBtn) diffBtn.textContent = difficultyLabel(DIFF_KEYS[state.diffIndex]);
    if (pencilBtn) {
      pencilBtn.textContent = state.pencilMode ? "Lápis ON" : "Lápis";
      pencilBtn.classList.toggle("is-active", state.pencilMode);
    }
  }

  function spawnBubbles() {
    state.bubbles = [];
    for (let i = 0; i < 24; i++) {
      state.bubbles.push({
        x: BOARD_ORIGIN.x + Math.random() * BOARD_SIZE,
        y: BOARD_ORIGIN.y + BOARD_SIZE + Math.random() * 20,
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
    state.status = user ? `Olá, ${user}!` : "Toca numa célula";
    syncControls();
    draw();
  }

  function place(digit) {
    if (state.won) return;
    const [r, c] = state.selected;
    if (state.given[r][c]) {
      state.status = "Célula fixa";
      state.shakeUntil = Date.now() + 250;
      draw();
      ensureAnim();
      return;
    }
    if (state.pencilMode && digit) {
      togglePencil(state.board, r, c, digit);
      state.status = "Nota a lápis";
      state.flashCell = [r, c];
      state.flashUntil = Date.now() + 200;
      draw();
      ensureAnim();
      return;
    }
    setCellValue(state.board, r, c, digit);
    if (digit) clearPencilDigitPeers(state.board, r, c, digit);
    state.flashCell = [r, c];
    state.flashUntil = Date.now() + 220;

    const conflicts = findConflicts(state.board);
    if (digit && conflicts.has(`${r},${c}`)) {
      state.status = "Conflito";
      state.shakeUntil = Date.now() + 280;
    } else if (isSolved(state.board, state.solution)) {
      state.won = true;
      state.winAt = Date.now();
      const elapsed = Math.floor((Date.now() - state.startedAt) / 1000);
      const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
      const ss = String(elapsed % 60).padStart(2, "0");
      state.status = `Resolvido ${mm}:${ss}!`;
      spawnBubbles();
      if (typeof window.thcokuReportWin === "function") {
        window.thcokuReportWin(state.difficulty, elapsed);
      }
    } else {
      state.status = digit ? `Ok · ${filledCount(state.board)}/81` : `Apagado · ${filledCount(state.board)}/81`;
    }
    draw();
    ensureAnim();
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
    state.status = "Escolhe um número";
    draw();
  }

  function draw() {
    if (!state.board?.length || !state.given?.length) {
      ctx.fillStyle = "#38bdf8";
      ctx.fillRect(0, 0, WIDTH, HEIGHT);
      ctx.fillStyle = RGB.panel;
      roundRect(ctx, 20, 16, WIDTH - 40, 56, 14);
      ctx.fill();
      ctx.fillStyle = RGB.header;
      ctx.font = "700 26px Fredoka, Segoe UI, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText("Thcoku", 36, 44);
      ctx.font = "600 16px Fredoka, Segoe UI, sans-serif";
      ctx.fillText(state.status, 160, 44);
      return;
    }

    const now = Date.now();
    let shakeX = 0;
    if (now < state.shakeUntil) shakeX = Math.sin(now / 30) * 4;

    ctx.clearRect(0, 0, WIDTH, HEIGHT);
    const grad = ctx.createLinearGradient(0, 0, WIDTH, HEIGHT);
    grad.addColorStop(0, "#7dd3fc");
    grad.addColorStop(0.55, "#38bdf8");
    grad.addColorStop(1, "#0ea5e9");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, WIDTH, HEIGHT);

    ctx.fillStyle = "rgba(254,243,199,0.45)";
    ctx.beginPath();
    ctx.arc(660, 40, 70, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = RGB.panel;
    roundRect(ctx, 20, 14, WIDTH - 40, 56, 14);
    ctx.fill();
    ctx.fillStyle = RGB.header;
    ctx.font = "700 26px Fredoka, Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText("Thcoku", 36, 42);
    ctx.font = "600 16px Fredoka, Segoe UI, sans-serif";
    ctx.fillText(String(state.status).slice(0, 34), 150, 42);

    ctx.save();
    ctx.translate(shakeX, 0);

    const conflicts = findConflicts(state.board);
    const [sr, sc] = state.selected;
    const ox = BOARD_ORIGIN.x;
    const oy = BOARD_ORIGIN.y;

    ctx.fillStyle = "#fffbeb";
    roundRect(ctx, ox - 8, oy - 8, BOARD_SIZE + 16, BOARD_SIZE + 16, 12);
    ctx.fill();

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

        let fill = state.given[r][c] ? RGB.given : RGB.empty;
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
      }
      const bw = 360;
      const bh = 72;
      const bx = (WIDTH - bw) / 2;
      const by = 300;
      ctx.fillStyle = RGB.win;
      roundRect(ctx, bx, by, bw, bh, 18);
      ctx.fill();
      ctx.strokeStyle = RGB.header;
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.fillStyle = RGB.panel;
      ctx.font = "700 32px Fredoka, Segoe UI, sans-serif";
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
        now < state.flashUntil ||
        (state.won && now - state.winAt < 5000);
      draw();
      if (need) state.raf = requestAnimationFrame(tick);
    };
    state.raf = requestAnimationFrame(tick);
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
    else if (action === "new") newGame();
    else if (action === "diff") {
      state.diffIndex = (state.diffIndex + 1) % DIFF_KEYS.length;
      newGame();
    } else if (action === "pencil") {
      state.pencilMode = !state.pencilMode;
      state.status = state.pencilMode ? "Lápis ON" : "Lápis OFF";
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
  return { newGame, place, draw };
}
