"""Persistent player statistics and one-time achievements for MONKEYS CARDS."""

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterable


@dataclass(frozen=True)
class PlayerStatsRecord:
    played: int = 0
    wins: int = 0
    losses: int = 0


@dataclass(frozen=True)
class PendingAchievement:
    chat_id: int
    user_id: int
    display_name: str
    key: str


class PlayerStats:
    """SQLite-backed lifetime statistics; a match token is applied only once."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS player_stats (
                    user_id INTEGER PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    played INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    win_streak INTEGER NOT NULL DEFAULT 0,
                    sherlock_awarded INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS processed_matches (
                    match_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS achievements (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    achievement_key TEXT NOT NULL,
                    announced INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id, achievement_key)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def record_match(self, match_id: str, chat_id: int, results: Iterable[tuple[int, str, bool]]) -> bool:
        """Record one completed match. Each result is (user_id, display_name, won)."""
        rows = [(int(user_id), " ".join(name.split())[:80] or "Игрок", bool(won))
                for user_id, name, won in results]
        if not match_id or not rows:
            return False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO processed_matches(match_id) VALUES (?)", (match_id,)
            ).rowcount
            if not inserted:
                return False
            for user_id, name, won in rows:
                current = db.execute(
                    "SELECT win_streak, sherlock_awarded FROM player_stats WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                streak = int(current["win_streak"]) if current else 0
                already_awarded = bool(current["sherlock_awarded"]) if current else False
                played = 1
                wins = 1 if won else 0
                losses = 0 if won else 1
                next_streak = streak + 1 if won else 0
                db.execute(
                    """
                    INSERT INTO player_stats
                        (user_id, display_name, played, wins, losses, win_streak, sherlock_awarded)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        played = player_stats.played + 1,
                        wins = player_stats.wins + excluded.wins,
                        losses = player_stats.losses + excluded.losses,
                        win_streak = excluded.win_streak,
                        sherlock_awarded = MAX(player_stats.sherlock_awarded, excluded.sherlock_awarded)
                    """,
                    (user_id, name, played, wins, losses, next_streak,
                     int(already_awarded or (won and next_streak >= 2))),
                )
                if won and next_streak >= 2 and not already_awarded:
                    db.execute(
                        "INSERT OR IGNORE INTO achievements(chat_id, user_id, achievement_key) VALUES (?, ?, 'sherlock')",
                        (int(chat_id), user_id),
                    )
            return True

    def get(self, user_id: int) -> PlayerStatsRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT played, wins, losses FROM player_stats WHERE user_id = ?", (int(user_id),)
            ).fetchone()
        if row is None:
            return PlayerStatsRecord()
        return PlayerStatsRecord(int(row["played"]), int(row["wins"]), int(row["losses"]))

    def pending_achievements(self) -> list[PendingAchievement]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT a.chat_id, a.user_id, p.display_name, a.achievement_key
                FROM achievements AS a
                JOIN player_stats AS p ON p.user_id = a.user_id
                WHERE a.announced = 0
                ORDER BY a.rowid
                """
            ).fetchall()
        return [PendingAchievement(int(row["chat_id"]), int(row["user_id"]),
                                   str(row["display_name"]), str(row["achievement_key"])) for row in rows]

    def mark_achievements_announced(self, achievements: Iterable[PendingAchievement]) -> None:
        items = [(item.chat_id, item.user_id, item.key) for item in achievements]
        if not items:
            return
        with self._connect() as db:
            db.executemany(
                "UPDATE achievements SET announced = 1 WHERE chat_id = ? AND user_id = ? AND achievement_key = ?",
                items,
            )
