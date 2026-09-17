"""Minesweeper game engine with RichMessage buttons."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Optional

from aiogram.types import (
    InputRichMessage,
    InputRichBlockButtons,
    RichMessageButton,
)

from .config import redis_client

GAME_TTL = 600  # 10 minutes

MINE = "💣"
FLAG = "🚩"
HIDDEN = "▪️"
EMPTY = "⬜"
EXPLODED = "💥"
NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]


@dataclass
class MinesweeperGame:
    rows: int = 5
    cols: int = 5
    mines: int = 5
    board: list[list[int]] = field(default_factory=list)
    revealed: list[list[bool]] = field(default_factory=list)
    flagged: list[list[bool]] = field(default_factory=list)
    game_over: bool = False
    won: bool = False
    first_move: bool = True

    def __post_init__(self):
        if not self.board:
            self.board = [[0] * self.cols for _ in range(self.rows)]
            self.revealed = [[False] * self.cols for _ in range(self.rows)]
            self.flagged = [[False] * self.cols for _ in range(self.rows)]

    def place_mines(self, safe_row: int, safe_col: int) -> None:
        safe_zone = set()
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                r, c = safe_row + dr, safe_col + dc
                if 0 <= r < self.rows and 0 <= c < self.cols:
                    safe_zone.add((r, c))

        candidates = [
            (r, c)
            for r in range(self.rows)
            for c in range(self.cols)
            if (r, c) not in safe_zone
        ]
        mine_positions = random.sample(candidates, min(self.mines, len(candidates)))

        for r, c in mine_positions:
            self.board[r][c] = -1

        for r in range(self.rows):
            for c in range(self.cols):
                if self.board[r][c] == -1:
                    continue
                count = 0
                for dr in range(-1, 2):
                    for dc in range(-1, 2):
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < self.rows and 0 <= nc < self.cols and self.board[nr][nc] == -1:
                            count += 1
                self.board[r][c] = count

        self.first_move = False

    def reveal(self, row: int, col: int) -> str:
        if self.game_over or self.revealed[row][col] or self.flagged[row][col]:
            return "safe"

        if self.first_move:
            self.place_mines(row, col)

        if self.board[row][col] == -1:
            self.game_over = True
            self.revealed[row][col] = True
            return "mine"

        self._flood_reveal(row, col)

        if self._check_win():
            self.game_over = True
            self.won = True

        return "empty"

    def _flood_reveal(self, row: int, col: int) -> None:
        if (
            row < 0 or row >= self.rows
            or col < 0 or col >= self.cols
            or self.revealed[row][col]
            or self.flagged[row][col]
        ):
            return
        self.revealed[row][col] = True
        if self.board[row][col] == 0:
            for dr in range(-1, 2):
                for dc in range(-1, 2):
                    self._flood_reveal(row + dr, col + dc)

    def toggle_flag(self, row: int, col: int) -> None:
        if self.game_over or self.revealed[row][col]:
            return
        self.flagged[row][col] = not self.flagged[row][col]

    def _check_win(self) -> bool:
        for r in range(self.rows):
            for c in range(self.cols):
                if self.board[r][c] != -1 and not self.revealed[r][c]:
                    return False
        return True

    def _cell_text(self, r: int, c: int) -> str:
        if self.revealed[r][c]:
            if self.board[r][c] == -1:
                return EXPLODED
            elif self.board[r][c] == 0:
                return EMPTY
            else:
                return NUMBERS[self.board[r][c] - 1]
        elif self.flagged[r][c]:
            return FLAG
        else:
            return HIDDEN

    def get_rich_message(self, flag_mode: bool = False, status: str = "") -> InputRichMessage:
        blocks = []

        # Header
        flags = sum(self.flagged[r][c] for r in range(self.rows) for c in range(self.cols))
        if self.game_over:
            if self.won:
                header = f"<b>🎉 YOU WIN!</b>  <code>{self.rows}×{self.cols}</code>"
            else:
                header = f"<b>💥 GAME OVER!</b>  <code>{self.rows}×{self.cols}</code>"
        else:
            header = f"<b>💣 MINESWEEPER</b>  <code>{self.rows}×{self.cols}</code>  <b>{self.mines} mines</b>  🚩 {flags}"

        from aiogram.types import InputRichBlockParagraph
        blocks.append(InputRichBlockParagraph(text=header))

        # Board buttons — one row per board row
        for r in range(self.rows):
            row_buttons = []
            for c in range(self.cols):
                text = self._cell_text(r, c)
                row_buttons.append(RichMessageButton(text=text, callback_data=f"ms:{r}:{c}"))
            blocks.append(InputRichBlockButtons(buttons=row_buttons))

        # Control buttons
        flag_text = "🚩 Flag Mode: ON" if flag_mode else "💣 Tap Mode"
        blocks.append(InputRichBlockButtons(buttons=[
            RichMessageButton(text="🔄 New Game", callback_data="ms:new"),
            RichMessageButton(text=flag_text, callback_data="ms:flag_toggle"),
        ]))

        return InputRichMessage(blocks=blocks)

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "mines": self.mines,
            "board": self.board,
            "revealed": self.revealed,
            "flagged": self.flagged,
            "game_over": self.game_over,
            "won": self.won,
            "first_move": self.first_move,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MinesweeperGame:
        return cls(
            rows=data["rows"],
            cols=data["cols"],
            mines=data["mines"],
            board=data["board"],
            revealed=data["revealed"],
            flagged=data["flagged"],
            game_over=data["game_over"],
            won=data["won"],
            first_move=data["first_move"],
        )


async def save_game(chat_id: int, user_id: int, game: MinesweeperGame) -> None:
    key = f"ms_game:{chat_id}:{user_id}"
    await redis_client.set(key, json.dumps(game.to_dict()), ex=GAME_TTL)


async def load_game(chat_id: int, user_id: int) -> Optional[MinesweeperGame]:
    key = f"ms_game:{chat_id}:{user_id}"
    data = await redis_client.get(key)
    if not data:
        return None
    try:
        return MinesweeperGame.from_dict(json.loads(data))
    except Exception:
        return None


async def delete_game(chat_id: int, user_id: int) -> None:
    key = f"ms_game:{chat_id}:{user_id}"
    await redis_client.delete(key)
