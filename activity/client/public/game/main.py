"""Thcoku — Sudoku playable in the browser via PyScript + Pygame-CE.

Touch-first polish: tap cell → tap number, soft feedback, win celebration.
"""

from __future__ import annotations

import asyncio
import math
import random
import sys
import time

import pygame

from sudoku_core import (
    DEFAULT_DIFFICULTY,
    DIFFICULTY_TIERS,
    cell_value,
    clear_pencil_digit_peers,
    difficulty_label,
    filled_count,
    find_conflicts,
    is_solved,
    make_puzzle,
    set_cell_value,
    toggle_pencil,
)

# Bikini Bottom palette
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
RGB_PAD = (255, 251, 235)
RGB_PAD_PRESS = (253, 224, 71)
RGB_WIN = (52, 211, 153)
RGB_BUBBLE = (186, 230, 253)

WIDTH, HEIGHT = 720, 980
BOARD_ORIGIN = (48, 100)
CELL = 68
BOARD_SIZE = CELL * 9
DIFF_KEYS = list(DIFFICULTY_TIERS.keys())

PAD_KEY = 72
PAD_GAP = 10
PAD_ORIGIN_Y = 740


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


def lerp_color(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


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
            self.font_pad = pygame.font.Font("fonts/Fredoka-Variable.ttf", 32)
            self.font_win = pygame.font.Font("fonts/Fredoka-Variable.ttf", 42)
        except Exception:
            self.font_lg = pygame.font.SysFont("arial", 36, bold=True)
            self.font_md = pygame.font.SysFont("arial", 28, bold=True)
            self.font_sm = pygame.font.SysFont("arial", 18)
            self.font_xs = pygame.font.SysFont("arial", 14)
            self.font_pad = pygame.font.SysFont("arial", 32, bold=True)
            self.font_win = pygame.font.SysFont("arial", 42, bold=True)

        self.diff_index = DIFF_KEYS.index(DEFAULT_DIFFICULTY)
        self.selected = (0, 0)
        self.pencil_mode = False
        self.status = "A gerar puzzle…"
        self.won = False
        self.started_at = time.time()
        self.board: list = []
        self.given: list = []
        self.solution: list = []

        # Polish / animation state
        self.t0 = time.time()
        self.flash_cell: tuple[int, int] | None = None
        self.flash_until = 0.0
        self.press_key: int | str | None = None
        self.press_until = 0.0
        self.win_at = 0.0
        self.bubbles: list[dict] = []
        self.shake_until = 0.0

        pad_w = 3 * PAD_KEY + 2 * PAD_GAP
        pad_x = (WIDTH - pad_w) // 2
        self.pad_keys: dict[int | str, pygame.Rect] = {}
        for n in range(1, 10):
            row, col = (n - 1) // 3, (n - 1) % 3
            self.pad_keys[n] = pygame.Rect(
                pad_x + col * (PAD_KEY + PAD_GAP),
                PAD_ORIGIN_Y + row * (PAD_KEY + PAD_GAP),
                PAD_KEY,
                PAD_KEY,
            )
        self.pad_keys["clear"] = pygame.Rect(
            pad_x,
            PAD_ORIGIN_Y + 3 * (PAD_KEY + PAD_GAP),
            pad_w,
            48,
        )

        action_y = self.pad_keys["clear"].bottom + 14
        self.buttons = {
            "new": pygame.Rect(40, action_y, 140, 44),
            "diff": pygame.Rect(200, action_y, 200, 44),
            "pencil": pygame.Rect(420, action_y, 140, 44),
        }

        self.new_game()

    def _spawn_bubbles(self, count: int = 28) -> None:
        self.bubbles = []
        ox, oy = BOARD_ORIGIN
        for _ in range(count):
            self.bubbles.append(
                {
                    "x": ox + random.uniform(0, BOARD_SIZE),
                    "y": oy + BOARD_SIZE + random.uniform(0, 40),
                    "r": random.uniform(6, 16),
                    "vy": random.uniform(40, 110),
                    "vx": random.uniform(-18, 18),
                    "phase": random.uniform(0, math.tau),
                }
            )

    def new_game(self) -> None:
        key = DIFF_KEYS[self.diff_index]
        self.status = f"A gerar ({difficulty_label(key)})…"
        self.won = False
        self.bubbles = []
        self.draw()
        pygame.display.flip()
        self.board, self.given, self.solution = make_puzzle(difficulty=key)
        self.selected = (0, 0)
        self.started_at = time.time()
        self.pencil_mode = False
        self.flash_cell = None
        user = discord_username()
        hello = f"Olá, {user}! " if user else ""
        self.status = f"{hello}Toca numa célula, depois num número"

    def cell_at(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        x, y = pos
        ox, oy = BOARD_ORIGIN
        # Account for win-shake offset visually only in draw; hits use raw board
        if not (ox <= x < ox + BOARD_SIZE and oy <= y < oy + BOARD_SIZE):
            return None
        return (y - oy) // CELL, (x - ox) // CELL

    def place(self, digit: int) -> None:
        if self.won:
            return
        r, c = self.selected
        if self.given[r][c]:
            self.status = "Essa célula é uma pista fixa"
            self.shake_until = time.time() + 0.25
            return
        if self.pencil_mode and digit:
            toggle_pencil(self.board, r, c, digit)
            self.status = "Nota a lápis"
            self.flash_cell = (r, c)
            self.flash_until = time.time() + 0.2
            return
        set_cell_value(self.board, r, c, digit)
        if digit:
            clear_pencil_digit_peers(self.board, r, c, digit)
        self.flash_cell = (r, c)
        self.flash_until = time.time() + 0.22

        if digit and find_conflicts(self.board) & {(r, c)}:
            self.status = "Conflito — tenta outro número"
            self.shake_until = time.time() + 0.28
        elif is_solved(self.board, self.solution):
            self.won = True
            self.win_at = time.time()
            elapsed = int(time.time() - self.started_at)
            self.status = f"Resolvido em {elapsed // 60:02d}:{elapsed % 60:02d}!"
            self._spawn_bubbles()
            self._report_win(elapsed)
        else:
            filled = filled_count(self.board)
            self.status = f"{'Ok' if digit else 'Apagado'} · {filled}/81"

    def _report_win(self, elapsed: int) -> None:
        try:
            from js import window  # type: ignore

            difficulty = DIFF_KEYS[self.diff_index]
            report = getattr(window, "thcokuReportWin", None)
            if report is not None:
                report(difficulty, elapsed)
        except Exception:
            pass

    def handle_pointer(self, pos: tuple[int, int]) -> None:
        if self.won and time.time() - self.win_at > 0.8:
            # Tap anywhere after win celebration → soft prompt
            for name, rect in self.buttons.items():
                if rect.collidepoint(pos) and name == "new":
                    self.new_game()
                    return
            if self.buttons["diff"].collidepoint(pos):
                self.diff_index = (self.diff_index + 1) % len(DIFF_KEYS)
                self.new_game()
                return

        cell = self.cell_at(pos)
        if cell is not None and not self.won:
            self.selected = cell
            self.status = "Célula escolhida — toca num número"
            return

        for key, rect in self.pad_keys.items():
            if not rect.collidepoint(pos):
                continue
            self.press_key = key
            self.press_until = time.time() + 0.15
            if key == "clear":
                self.place(0)
            else:
                self.place(int(key))
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
            return

    def handle_key(self, key: int) -> None:
        if self.won and key == pygame.K_n:
            self.new_game()
            return
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
            self.press_key = "clear"
            self.press_until = time.time() + 0.15
            self.place(0)
        elif pygame.K_1 <= key <= pygame.K_9:
            d = key - pygame.K_0
            self.press_key = d
            self.press_until = time.time() + 0.15
            self.place(d)
        elif pygame.K_KP1 <= key <= pygame.K_KP9:
            d = key - pygame.K_KP0
            self.press_key = d
            self.press_until = time.time() + 0.15
            self.place(d)
        elif key == pygame.K_n:
            self.new_game()

    def draw_button(self, rect: pygame.Rect, label: str, active: bool = False) -> None:
        color = RGB_SELECT if active else RGB_UI
        pygame.draw.rect(self.screen, color, rect, border_radius=12)
        pygame.draw.rect(self.screen, RGB_HEADER, rect, 2, border_radius=12)
        text = self.font_sm.render(label, True, RGB_HEADER)
        self.screen.blit(text, text.get_rect(center=rect.center))

    def draw_pad_key(self, key: int | str, rect: pygame.Rect, label: str, *, wide: bool = False) -> None:
        pressed = self.press_key == key and time.time() < self.press_until
        fill = RGB_PAD_PRESS if pressed else RGB_PAD
        draw_rect = rect.move(0, 2) if pressed else rect
        pygame.draw.rect(self.screen, fill, draw_rect, border_radius=14)
        pygame.draw.rect(self.screen, RGB_HEADER, draw_rect, 3, border_radius=14)
        font = self.font_sm if wide else self.font_pad
        text = font.render(label, True, RGB_HEADER)
        self.screen.blit(text, text.get_rect(center=draw_rect.center))

    def _board_offset(self) -> tuple[int, int]:
        if time.time() >= self.shake_until:
            return 0, 0
        t = time.time()
        return int(math.sin(t * 55) * 4), int(math.cos(t * 40) * 2)

    def draw(self) -> None:
        now = time.time()
        self.screen.fill(RGB_BG)

        # Soft lagoon wash
        pygame.draw.circle(self.screen, (186, 230, 253), (80, 900), 120)
        pygame.draw.circle(self.screen, (254, 243, 199), (680, 60), 90)

        pygame.draw.rect(self.screen, RGB_PANEL, (20, 16, WIDTH - 40, 64), border_radius=16)
        title = self.font_lg.render("Thcoku", True, RGB_HEADER)
        self.screen.blit(title, (36, 22))

        elapsed = int(now - self.started_at) if not self.won else int(self.win_at - self.started_at)
        timer = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        filled = filled_count(self.board) if self.board else 0
        meta = self.font_xs.render(
            f"{difficulty_label(DIFF_KEYS[self.diff_index])}  ·  {timer}  ·  {filled}/81",
            True,
            RGB_HEADER,
        )
        self.screen.blit(meta, (180, 22))
        status = self.font_sm.render(self.status[:42], True, RGB_HEADER)
        self.screen.blit(status, (180, 42))

        conflicts = find_conflicts(self.board) if self.board else set()
        sr, sc = self.selected
        ox, oy = BOARD_ORIGIN
        dx, dy = self._board_offset()
        ox, oy = ox + dx, oy + dy

        # Selection pulse
        pulse = 0.5 + 0.5 * math.sin(now * 3.2)

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
                    fill = lerp_color(RGB_SELECT, (255, 255, 200), pulse * 0.35)
                elif r == sr or c == sc or same_box:
                    fill = RGB_BOX_HL
                elif (r, c) in conflicts:
                    fill = RGB_CONFLICT
                elif self.given and self.given[r][c]:
                    fill = RGB_GIVEN
                else:
                    fill = RGB_EMPTY

                if self.flash_cell == (r, c) and now < self.flash_until:
                    fill = lerp_color(fill, RGB_WIN, 0.55)

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

        # Progress bar under board
        bar = pygame.Rect(BOARD_ORIGIN[0], 720, BOARD_SIZE, 8)
        pygame.draw.rect(self.screen, (255, 255, 255), bar, border_radius=4)
        if filled:
            fill_w = int(BOARD_SIZE * (filled / 81))
            pygame.draw.rect(
                self.screen, RGB_WIN if self.won else RGB_UI, (bar.x, bar.y, fill_w, bar.h), border_radius=4
            )

        hint = self.font_xs.render(
            "Toca célula → número   ·   conflitos a coral   ·   N = novo",
            True,
            RGB_HEADER,
        )
        self.screen.blit(hint, (40, 732))

        for n in range(1, 10):
            self.draw_pad_key(n, self.pad_keys[n], str(n))
        self.draw_pad_key("clear", self.pad_keys["clear"], "Apagar", wide=True)

        self.draw_button(self.buttons["new"], "Novo")
        self.draw_button(
            self.buttons["diff"], difficulty_label(DIFF_KEYS[self.diff_index])
        )
        self.draw_button(self.buttons["pencil"], "Lápis", active=self.pencil_mode)

        if self.won:
            self._draw_win_fx(now)

    def _draw_win_fx(self, now: float) -> None:
        # Rising bubbles
        for b in self.bubbles:
            age = now - self.win_at
            if age > 4.5:
                continue
            b["y"] -= b["vy"] * (1 / 60)
            b["x"] += b["vx"] * (1 / 60) + math.sin(now * 2 + b["phase"]) * 0.4
            r = int(b["r"])
            pygame.draw.circle(
                self.screen, RGB_BUBBLE, (int(b["x"]), int(b["y"])), r, 2
            )
            pygame.draw.circle(
                self.screen,
                (255, 255, 255),
                (int(b["x"] - r // 3), int(b["y"] - r // 3)),
                max(1, r // 4),
            )

        # Banner
        pop = min(1.0, (now - self.win_at) * 2.2)
        scale = 0.85 + 0.15 * (1 - (1 - pop) ** 3)
        banner = pygame.Rect(0, 0, int(420 * scale), int(88 * scale))
        banner.center = (WIDTH // 2, BOARD_ORIGIN[1] + BOARD_SIZE // 2)
        pygame.draw.rect(self.screen, RGB_WIN, banner, border_radius=20)
        pygame.draw.rect(self.screen, RGB_HEADER, banner, 3, border_radius=20)
        msg = self.font_win.render("Yay!", True, RGB_PANEL)
        sub = self.font_sm.render(self.status, True, RGB_PANEL)
        self.screen.blit(msg, msg.get_rect(center=(banner.centerx, banner.centery - 14)))
        self.screen.blit(sub, sub.get_rect(center=(banner.centerx, banner.centery + 18)))

    def handle_events(self) -> bool:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self.handle_pointer(event.pos)
            elif event.type == getattr(pygame, "FINGERDOWN", -1):
                px = int(event.x * WIDTH)
                py = int(event.y * HEIGHT)
                self.handle_pointer((px, py))
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
