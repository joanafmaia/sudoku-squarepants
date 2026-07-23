"""Core Sudoku logic shared by the Discord Activity (PyScript) client.

Extracted from the Thcoku bot so puzzle generation / validation works without
discord.py or Pillow.
"""

from __future__ import annotations

import random

DEFAULT_DIFFICULTY = "medium"

DIFFICULTY_TIERS: dict[str, dict] = {
    "very_easy": {"label": "Very Easy", "clues": 46},
    "easy": {"label": "Easy", "clues": 40},
    "medium": {"label": "Medium", "clues": 34},
    "hard": {"label": "Hard", "clues": 28},
    "very_hard": {"label": "Very Hard", "clues": 24},
    "expertttt": {"label": "Expertttt", "clues": 22},
}


def make_cell(value: int = 0, pencil_marks: list[int] | None = None) -> dict:
    return {"value": int(value), "pencil_marks": list(pencil_marks or [])}


def cell_value(board: list[list[dict]], r: int, c: int) -> int:
    return int(board[r][c].get("value", 0))


def set_cell_value(board: list[list[dict]], r: int, c: int, value: int) -> None:
    board[r][c]["value"] = int(value)
    if value:
        board[r][c]["pencil_marks"] = []


def toggle_pencil(board: list[list[dict]], r: int, c: int, digit: int) -> list[int]:
    marks = list(board[r][c].get("pencil_marks") or [])
    if digit in marks:
        marks.remove(digit)
    else:
        marks.append(digit)
        marks.sort()
    board[r][c]["pencil_marks"] = marks
    return marks


def clear_pencil_digit_peers(board: list[list[dict]], r: int, c: int, digit: int) -> None:
    digit = int(digit)
    if digit < 1 or digit > 9:
        return
    br, bc = (r // 3) * 3, (c // 3) * 3
    peers: set[tuple[int, int]] = set()
    for i in range(9):
        peers.add((r, i))
        peers.add((i, c))
    for i in range(3):
        for j in range(3):
            peers.add((br + i, bc + j))
    peers.discard((r, c))
    for pr, pc in peers:
        marks = list(board[pr][pc].get("pencil_marks") or [])
        if digit in marks:
            marks.remove(digit)
            board[pr][pc]["pencil_marks"] = marks


def values_grid(board: list[list[dict]]) -> list[list[int]]:
    return [[cell_value(board, r, c) for c in range(9)] for r in range(9)]


def difficulty_clues(key: str) -> int:
    return int(DIFFICULTY_TIERS.get(key, DIFFICULTY_TIERS[DEFAULT_DIFFICULTY])["clues"])


def difficulty_label(key: str | None) -> str:
    if not key:
        return DIFFICULTY_TIERS[DEFAULT_DIFFICULTY]["label"]
    if key in DIFFICULTY_TIERS:
        return DIFFICULTY_TIERS[key]["label"]
    for meta in DIFFICULTY_TIERS.values():
        if meta["label"] == key:
            return key
    return DIFFICULTY_TIERS[DEFAULT_DIFFICULTY]["label"]


def difficulty_key_from_label(label: str) -> str:
    for key, meta in DIFFICULTY_TIERS.items():
        if meta["label"] == label or key == label:
            return key
    return DEFAULT_DIFFICULTY


def _sudoku_cell_ok(grid: list[list[int]], r: int, c: int, v: int) -> bool:
    if any(grid[r][j] == v for j in range(9)):
        return False
    if any(grid[i][c] == v for i in range(9)):
        return False
    br, bc = (r // 3) * 3, (c // 3) * 3
    for i in range(br, br + 3):
        for j in range(bc, bc + 3):
            if grid[i][j] == v:
                return False
    return True


def _sudoku_candidates(grid: list[list[int]], r: int, c: int) -> list[int]:
    used = [False] * 10
    for j in range(9):
        used[grid[r][j]] = True
    for i in range(9):
        used[grid[i][c]] = True
    br, bc = (r // 3) * 3, (c // 3) * 3
    for i in range(br, br + 3):
        for j in range(bc, bc + 3):
            used[grid[i][j]] = True
    return [v for v in range(1, 10) if not used[v]]


def _sudoku_pick_empty(grid: list[list[int]]) -> tuple[int, int, list[int]] | None:
    best: tuple[int, int, list[int]] | None = None
    best_n = 10
    for r in range(9):
        for c in range(9):
            if grid[r][c] != 0:
                continue
            cands = _sudoku_candidates(grid, r, c)
            n = len(cands)
            if n == 0:
                return r, c, []
            if n < best_n:
                best = (r, c, cands)
                best_n = n
                if n == 1:
                    return best
    return best


def _sudoku_fill(grid: list[list[int]], rng: random.Random) -> bool:
    pick = _sudoku_pick_empty(grid)
    if pick is None:
        return True
    r, c, cands = pick
    if not cands:
        return False
    rng.shuffle(cands)
    for v in cands:
        grid[r][c] = v
        if _sudoku_fill(grid, rng):
            return True
        grid[r][c] = 0
    return False


def _sudoku_count_solutions(grid: list[list[int]], limit: int = 2) -> int:
    count = 0

    def bt() -> None:
        nonlocal count
        if count >= limit:
            return
        pick = _sudoku_pick_empty(grid)
        if pick is None:
            count += 1
            return
        r, c, cands = pick
        if not cands:
            return
        for v in cands:
            grid[r][c] = v
            bt()
            grid[r][c] = 0
            if count >= limit:
                return

    bt()
    return count


def generate_unique_sudoku(
    *,
    target_clues: int,
    seed: int | None = None,
) -> tuple[list[list[int]], list[list[int]]]:
    target = max(17, min(50, int(target_clues)))
    base_seed = seed if seed is not None else random.randrange(1 << 30)

    best_puzzle: list[list[int]] | None = None
    best_solution: list[list[int]] | None = None
    best_clues = 81

    for attempt in range(5):
        rng = random.Random(base_seed + attempt * 1_000_003)
        solution = [[0] * 9 for _ in range(9)]
        if not _sudoku_fill(solution, rng):
            continue

        puzzle = [row[:] for row in solution]
        order = [(r, c) for r in range(9) for c in range(9)]
        rng.shuffle(order)

        for r, c in order:
            clues_now = sum(1 for row in puzzle for v in row if v)
            if clues_now <= target:
                break
            backup = puzzle[r][c]
            puzzle[r][c] = 0
            if _sudoku_count_solutions(puzzle, limit=2) != 1:
                puzzle[r][c] = backup

        clues = sum(1 for row in puzzle for v in row if v)
        if clues < best_clues:
            best_clues = clues
            best_puzzle = puzzle
            best_solution = solution
        if clues <= target:
            break

    if best_puzzle is None or best_solution is None:
        return generate_unique_sudoku(target_clues=target, seed=None)

    return best_puzzle, best_solution


def make_puzzle(
    difficulty: str = DEFAULT_DIFFICULTY, seed: int | None = None
) -> tuple[list[list[dict]], list[list[bool]], list[list[int]]]:
    key = difficulty_key_from_label(difficulty)
    clues = difficulty_clues(key)
    puzzle, solution = generate_unique_sudoku(target_clues=clues, seed=seed)
    board = [[make_cell(int(v)) for v in row] for row in puzzle]
    given = [[v != 0 for v in row] for row in puzzle]
    return board, given, solution


def peers(r: int, c: int) -> list[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    for i in range(9):
        cells.add((r, i))
        cells.add((i, c))
    br, bc = 3 * (r // 3), 3 * (c // 3)
    for i in range(br, br + 3):
        for j in range(bc, bc + 3):
            cells.add((i, j))
    cells.discard((r, c))
    return list(cells)


def find_conflicts(board: list[list[dict]]) -> set[tuple[int, int]]:
    bad: set[tuple[int, int]] = set()
    for r in range(9):
        for c in range(9):
            val = cell_value(board, r, c)
            if val == 0:
                continue
            for pr, pc in peers(r, c):
                if cell_value(board, pr, pc) == val:
                    bad.add((r, c))
                    bad.add((pr, pc))
    return bad


def filled_count(board: list[list[dict]]) -> int:
    return sum(1 for r in range(9) for c in range(9) if cell_value(board, r, c) != 0)


def is_solved(board: list[list[dict]], solution: list[list[int]] | None = None) -> bool:
    if filled_count(board) < 81:
        return False
    if find_conflicts(board):
        return False
    if not solution:
        return True
    return values_grid(board) == [[int(c) for c in row] for row in solution]
