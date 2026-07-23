/** Sudoku core logic (ported from public/game/sudoku_core.py). */

export const DEFAULT_DIFFICULTY = "medium";

export const DIFFICULTY_TIERS = {
  very_easy: { label: "Very Easy", clues: 46 },
  easy: { label: "Easy", clues: 40 },
  medium: { label: "Medium", clues: 34 },
  hard: { label: "Hard", clues: 28 },
  very_hard: { label: "Very Hard", clues: 24 },
  expertttt: { label: "Expertttt", clues: 22 },
};

export const DIFF_KEYS = Object.keys(DIFFICULTY_TIERS);

function makeCell(value = 0, pencilMarks = []) {
  return { value: value | 0, pencil_marks: [...pencilMarks] };
}

export function cellValue(board, r, c) {
  return board[r][c].value | 0;
}

export function setCellValue(board, r, c, value) {
  board[r][c].value = value | 0;
  if (value) board[r][c].pencil_marks = [];
}

export function togglePencil(board, r, c, digit) {
  const marks = [...(board[r][c].pencil_marks || [])];
  const i = marks.indexOf(digit);
  if (i >= 0) marks.splice(i, 1);
  else {
    marks.push(digit);
    marks.sort((a, b) => a - b);
  }
  board[r][c].pencil_marks = marks;
  return marks;
}

export function clearPencilDigitPeers(board, r, c, digit) {
  digit |= 0;
  if (digit < 1 || digit > 9) return;
  const br = Math.floor(r / 3) * 3;
  const bc = Math.floor(c / 3) * 3;
  const peers = new Set();
  for (let i = 0; i < 9; i++) {
    peers.add(`${r},${i}`);
    peers.add(`${i},${c}`);
  }
  for (let i = 0; i < 3; i++) {
    for (let j = 0; j < 3; j++) peers.add(`${br + i},${bc + j}`);
  }
  peers.delete(`${r},${c}`);
  for (const key of peers) {
    const [pr, pc] = key.split(",").map(Number);
    const marks = [...(board[pr][pc].pencil_marks || [])];
    const idx = marks.indexOf(digit);
    if (idx >= 0) {
      marks.splice(idx, 1);
      board[pr][pc].pencil_marks = marks;
    }
  }
}

export function difficultyLabel(key) {
  return (DIFFICULTY_TIERS[key] || DIFFICULTY_TIERS[DEFAULT_DIFFICULTY]).label;
}

function difficultyClues(key) {
  return (DIFFICULTY_TIERS[key] || DIFFICULTY_TIERS[DEFAULT_DIFFICULTY]).clues | 0;
}

function candidates(grid, r, c) {
  const used = Array(10).fill(false);
  for (let j = 0; j < 9; j++) used[grid[r][j]] = true;
  for (let i = 0; i < 9; i++) used[grid[i][c]] = true;
  const br = Math.floor(r / 3) * 3;
  const bc = Math.floor(c / 3) * 3;
  for (let i = br; i < br + 3; i++) {
    for (let j = bc; j < bc + 3; j++) used[grid[i][j]] = true;
  }
  const out = [];
  for (let v = 1; v <= 9; v++) if (!used[v]) out.push(v);
  return out;
}

function pickEmpty(grid) {
  let best = null;
  let bestN = 10;
  for (let r = 0; r < 9; r++) {
    for (let c = 0; c < 9; c++) {
      if (grid[r][c] !== 0) continue;
      const cands = candidates(grid, r, c);
      const n = cands.length;
      if (n === 0) return { r, c, cands: [] };
      if (n < bestN) {
        best = { r, c, cands };
        bestN = n;
        if (n === 1) return best;
      }
    }
  }
  return best;
}

function shuffle(arr, rng) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
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

function fillGrid(grid, rng) {
  const pick = pickEmpty(grid);
  if (!pick) return true;
  const { r, c, cands } = pick;
  if (!cands.length) return false;
  shuffle(cands, rng);
  for (const v of cands) {
    grid[r][c] = v;
    if (fillGrid(grid, rng)) return true;
    grid[r][c] = 0;
  }
  return false;
}

function countSolutions(grid, limit = 2) {
  let count = 0;
  function bt() {
    if (count >= limit) return;
    const pick = pickEmpty(grid);
    if (!pick) {
      count += 1;
      return;
    }
    const { r, c, cands } = pick;
    if (!cands.length) return;
    for (const v of cands) {
      grid[r][c] = v;
      bt();
      grid[r][c] = 0;
      if (count >= limit) return;
    }
  }
  bt();
  return count;
}

function generateUniqueSudoku(targetClues, seed) {
  const target = Math.max(17, Math.min(50, targetClues | 0));
  const baseSeed = seed ?? Math.floor(Math.random() * (1 << 30));
  let bestPuzzle = null;
  let bestSolution = null;
  let bestClues = 81;

  for (let attempt = 0; attempt < 5; attempt++) {
    const rng = mulberry32(baseSeed + attempt * 1000003);
    const solution = Array.from({ length: 9 }, () => Array(9).fill(0));
    if (!fillGrid(solution, rng)) continue;

    const puzzle = solution.map((row) => row.slice());
    const order = [];
    for (let r = 0; r < 9; r++) for (let c = 0; c < 9; c++) order.push([r, c]);
    shuffle(order, rng);

    for (const [r, c] of order) {
      const cluesNow = puzzle.flat().filter((v) => v).length;
      if (cluesNow <= target) break;
      const backup = puzzle[r][c];
      puzzle[r][c] = 0;
      if (countSolutions(puzzle, 2) !== 1) puzzle[r][c] = backup;
    }

    const clues = puzzle.flat().filter((v) => v).length;
    if (clues < bestClues) {
      bestClues = clues;
      bestPuzzle = puzzle;
      bestSolution = solution;
    }
    if (clues <= target) break;
  }

  if (!bestPuzzle || !bestSolution) {
    return generateUniqueSudoku(target, undefined);
  }
  return { puzzle: bestPuzzle, solution: bestSolution };
}

export function makePuzzle(difficulty = DEFAULT_DIFFICULTY) {
  const key = DIFFICULTY_TIERS[difficulty] ? difficulty : DEFAULT_DIFFICULTY;
  const { puzzle, solution } = generateUniqueSudoku(difficultyClues(key));
  const board = puzzle.map((row) => row.map((v) => makeCell(v)));
  const given = puzzle.map((row) => row.map((v) => v !== 0));
  return { board, given, solution, difficulty: key };
}

function peers(r, c) {
  const cells = new Set();
  for (let i = 0; i < 9; i++) {
    cells.add(`${r},${i}`);
    cells.add(`${i},${c}`);
  }
  const br = 3 * Math.floor(r / 3);
  const bc = 3 * Math.floor(c / 3);
  for (let i = br; i < br + 3; i++) {
    for (let j = bc; j < bc + 3; j++) cells.add(`${i},${j}`);
  }
  cells.delete(`${r},${c}`);
  return [...cells].map((k) => k.split(",").map(Number));
}

export function findConflicts(board) {
  const bad = new Set();
  for (let r = 0; r < 9; r++) {
    for (let c = 0; c < 9; c++) {
      const val = cellValue(board, r, c);
      if (!val) continue;
      for (const [pr, pc] of peers(r, c)) {
        if (cellValue(board, pr, pc) === val) {
          bad.add(`${r},${c}`);
          bad.add(`${pr},${pc}`);
        }
      }
    }
  }
  return bad;
}

export function filledCount(board) {
  let n = 0;
  for (let r = 0; r < 9; r++) {
    for (let c = 0; c < 9; c++) if (cellValue(board, r, c)) n += 1;
  }
  return n;
}

export function isSolved(board, solution) {
  if (filledCount(board) < 81) return false;
  if (findConflicts(board).size) return false;
  if (!solution) return true;
  for (let r = 0; r < 9; r++) {
    for (let c = 0; c < 9; c++) {
      if (cellValue(board, r, c) !== solution[r][c]) return false;
    }
  }
  return true;
}
