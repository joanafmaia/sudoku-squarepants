"""Thcoku — Sudoku playable in the browser via PyScript + Pygame-CE."""

from __future__ import annotations

import asyncio
import sys
import time

import pygame

from sudoku_core import (
    DEFAULT_DIFFICULTY,
    DIFFICULTY_TIERS,
    cell_value,
    clear_pencil_digit_peers,
    difficulty_label,
    find_conflicts,
    is_solved,
    make_puzzle,
    set_cell_value,
    toggle_pencil,
)

# Bikini Bottom palette (aligned with the Discord bot board theme)
RGB_BG = (125, 211, 252)
RGB_CARD = (255, 251, 235)
RGB_EMPTY = (255, 254, 245)
RGB_GIVEN = (254, 243, 199)
RGB_SELECT = (253, 224, 71)
RGB_BOX_HL = (165, 243, 252)
RGB_CONFLICT = (253, 164, 175)
RGB_LINE = (148, 163, 184)
RGB_THICK = (15, 118, 110)
RGB_TEXT = (29, 78, 216)
RGB_TEXT_GIVEN = (19, 78, 74)
RGB_TEXT_CONFLICT = (190, 18, 60)
RGB_PENCIL = (100, 116, 139)
RGB_HEADER = (15, 118, 110)
RGB_UI = (245, 158, 11)
RGB_PANEL = (255, 248, 220)

WIDTH, HEIGHT = 720, 820
BOARD_ORIGIN = (40, 110)
CELL = 68
BOARD_SIZE = CELL * 9
DIFF_KEYS = list(DIFFICULTY_TIERS.keys())


def discord_username() -> str:
    try:
        from js import window  # type: ignore

        auth = getattr(window, "__DISCORD_AUTH__", None)
        if auth is None:
            return ""
        user = getattr(auth, "user", None)
        if user is None:
            return ""
        return str(getattr(user, "username", "") or "")
    except Exception:
        return ""


class ThcokuGame:
    def __init__(self) -> None:
        pygame.init()
        pygame.display.set_caption("Thcoku")
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT))
        self.clock = pygame.time.Clock()

        try:
            self.font_lg = pygame.font.Font("fonts/Fredoka-Variable.ttf", 36)
            self.font_md = pygame.font.Font("fonts/Fredoka-Variable.ttf", 28)
            self.font_sm = pygame.font.Font("fonts/Fredoka-Variable.ttf", 18)
            self.font_xs = pygame.font.Font("fonts/Fredoka-Variable.ttf", 14)
        except Exception:
            self.font_lg = pygame.font.SysFont("arial", 36, bold=True)
            self.font_md = pygame.font.SysFont("arial", 28, bold=True)
            self.font_sm = pygame.font.SysFont("arial", 18)
            self.font_xs = pygame.font.SysFont("arial", 14)

        self.diff_index = DIFF_KEYS.index(DEFAULT_DIFFICULTY)
        self.selected = (0, 0)
        self.pencil_mode = False
        self.status = "A gerar puzzle…"
        self.won = False
        self.started_at = time.time()
        self.board = []
        self.given = []
        self.solution = []
        self.new_game()

        self.buttons = {
            "new": pygame.Rect(40, 740, 140, 44),
            "diff": pygame.Rect(200, 740, 180, 44),
            "pencil": pygame.Rect(400, 740, 140, 44),
            "clear": pygame.Rect(560, 740, 120, 44),
        }

    def new_game(self) -> None:
        key = DIFF_KEYS[self.diff_index]
        self.status = f"A gerar ({difficulty_label(key)})…"
        self.draw()
        pygame.display.flip()
        self.board, self.given, self.solution = make_puzzle(difficulty=key)
        self.selected = (0, 0)
        self.won = False
        self.started_at = time.time()
        self.pencil_mode = False
        user = discord_username()
        hello = f"Olá, {user}! " if user else ""
        self.status = f"{hello}{difficulty_label(key)} — clica numa célula"

    def cell_at(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        x, y = pos
        ox, oy = BOARD_ORIGIN
        if not (ox <= x < ox + BOARD_SIZE and oy <= y < oy + BOARD_SIZE):
            return None
        return (y - oy) // CELL, (x - ox) // CELL

    def place(self, digit: int) -> None:
        if self.won:
            return
        r, c = self.selected
        if self.given[r][c]:
            self.status = "Essa célula é uma pista fixa"
            return
        if self.pencil_mode and digit:
            toggle_pencil(self.board, r, c, digit)
            self.status = "Nota a lápis"
            return
        set_cell_value(self.board, r, c, digit)
        if digit:
            clear_pencil_digit_peers(self.board, r, c, digit)
        if is_solved(self.board, self.solution):
            self.won = True
            elapsed = int(time.time() - self.started_at)
            self.status = f"Resolvido em {elapsed // 60:02d}:{elapsed % 60:02d}!"
            self._report_win(elapsed)
        else:
            self.status = "Ok" if digit else "Apagado"

    def _report_win(self, elapsed: int) -> None:
        """Ask the JS bridge to persist XP/sponges to Mongo via Netlify."""
        try:
            from js import window  # type: ignore

            difficulty = DIFF_KEYS[self.diff_index]
            report = getattr(window, "thcokuReportWin", None)
            if report is not None:
                report(difficulty, elapsed)
        except Exception:
            pass

    def handle_click(self, pos: tuple[int, int]) -> None:
        cell = self.cell_at(pos)
        if cell is not None:
            self.selected = cell
            return
        for name, rect in self.buttons.items():
            if not rect.collidepoint(pos):
                continue
            if name == "new":
                self.new_game()
            elif name == "diff":
                self.diff_index = (self.diff_index + 1) % len(DIFF_KEYS)
                self.new_game()
            elif name == "pencil":
                self.pencil_mode = not self.pencil_mode
                self.status = "Modo lápis ON" if self.pencil_mode else "Modo lápis OFF"
            elif name == "clear":
                self.place(0)
            return

    def handle_key(self, key: int) -> None:
        r, c = self.selected
        if key in (pygame.K_LEFT, pygame.K_a):
            self.selected = (r, (c - 1) % 9)
        elif key in (pygame.K_RIGHT, pygame.K_d):
            self.selected = (r, (c + 1) % 9)
        elif key in (pygame.K_UP, pygame.K_w):
            self.selected = ((r - 1) % 9, c)
        elif key in (pygame.K_DOWN, pygame.K_s):
            self.selected = ((r + 1) % 9, c)
        elif key == pygame.K_p:
            self.pencil_mode = not self.pencil_mode
            self.status = "Modo lápis ON" if self.pencil_mode else "Modo lápis OFF"
        elif key in (pygame.K_BACKSPACE, pygame.K_DELETE, pygame.K_0, pygame.K_KP0):
            self.place(0)
        elif pygame.K_1 <= key <= pygame.K_9:
            self.place(key - pygame.K_0)
        elif pygame.K_KP1 <= key <= pygame.K_KP9:
            self.place(key - pygame.K_KP0)
        elif key == pygame.K_n:
            self.new_game()

    def draw_button(self, rect: pygame.Rect, label: str, active: bool = False) -> None:
        color = RGB_SELECT if active else RGB_UI
        pygame.draw.rect(self.screen, color, rect, border_radius=10)
        pygame.draw.rect(self.screen, RGB_HEADER, rect, 2, border_radius=10)
        text = self.font_sm.render(label, True, RGB_HEADER)
        self.screen.blit(text, text.get_rect(center=rect.center))

    def draw(self) -> None:
        self.screen.fill(RGB_BG)
        pygame.draw.rect(self.screen, RGB_PANEL, (20, 20, WIDTH - 40, 70), border_radius=16)
        title = self.font_lg.render("Thcoku", True, RGB_HEADER)
        self.screen.blit(title, (40, 32))
        status = self.font_sm.render(self.status, True, RGB_HEADER)
        self.screen.blit(status, (200, 42))

        conflicts = find_conflicts(self.board) if self.board else set()
        sr, sc = self.selected
        ox, oy = BOARD_ORIGIN

        pygame.draw.rect(
            self.screen,
            RGB_CARD,
            (ox - 8, oy - 8, BOARD_SIZE + 16, BOARD_SIZE + 16),
            border_radius=12,
        )

        for r in range(9):
            for c in range(9):
                x = ox + c * CELL
                y = oy + r * CELL
                rect = pygame.Rect(x, y, CELL, CELL)

                same_box = (r // 3 == sr // 3) and (c // 3 == sc // 3)
                if (r, c) == (sr, sc):
                    fill = RGB_SELECT
                elif r == sr or c == sc or same_box:
                    fill = RGB_BOX_HL
                elif (r, c) in conflicts:
                    fill = RGB_CONFLICT
                elif self.given and self.given[r][c]:
                    fill = RGB_GIVEN
                else:
                    fill = RGB_EMPTY
                pygame.draw.rect(self.screen, fill, rect)

                if self.board:
                    val = cell_value(self.board, r, c)
                    if val:
                        color = (
                            RGB_TEXT_CONFLICT
                            if (r, c) in conflicts
                            else RGB_TEXT_GIVEN
                            if self.given[r][c]
                            else RGB_TEXT
                        )
                        glyph = self.font_md.render(str(val), True, color)
                        self.screen.blit(glyph, glyph.get_rect(center=rect.center))
                    else:
                        marks = self.board[r][c].get("pencil_marks") or []
                        for m in marks:
                            mr, mc = (m - 1) // 3, (m - 1) % 3
                            px = x + 8 + mc * 20
                            py = y + 6 + mr * 20
                            mark = self.font_xs.render(str(m), True, RGB_PENCIL)
                            self.screen.blit(mark, (px, py))

        for i in range(10):
            width = 3 if i % 3 == 0 else 1
            color = RGB_THICK if i % 3 == 0 else RGB_LINE
            pygame.draw.line(
                self.screen, color, (ox, oy + i * CELL), (ox + BOARD_SIZE, oy + i * CELL), width
            )
            pygame.draw.line(
                self.screen, color, (ox + i * CELL, oy), (ox + i * CELL, oy + BOARD_SIZE), width
            )

        self.draw_button(self.buttons["new"], "Novo")
        self.draw_button(
            self.buttons["diff"], difficulty_label(DIFF_KEYS[self.diff_index])
        )
        self.draw_button(self.buttons["pencil"], "Lápis", active=self.pencil_mode)
        self.draw_button(self.buttons["clear"], "Apagar")

        hint = self.font_xs.render(
            "Setas / WASD · 1-9 · P lápis · N novo", True, RGB_HEADER
        )
        self.screen.blit(hint, (40, 700))

    def handle_events(self) -> bool:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self.handle_click(event.pos)
            elif event.type == pygame.KEYDOWN:
                self.handle_key(event.key)
        return True


async def run_game() -> None:
    game = ThcokuGame()
    running = True
    while running:
        running = game.handle_events()
        game.draw()
        pygame.display.flip()
        await asyncio.sleep(1 / 60)
    pygame.quit()
    sys.exit()


try:
    asyncio.get_running_loop()
    asyncio.create_task(run_game())
except RuntimeError:
    asyncio.run(run_game())
