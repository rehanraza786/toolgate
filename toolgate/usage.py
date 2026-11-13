"""Call log in SQLite and the 'which tools are hot' decision."""
from __future__ import annotations

import fnmatch
import sqlite3
import time
from pathlib import Path


class Usage:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS calls (ts REAL, server TEXT, tool TEXT, ok INT, ms INT, via TEXT)"
        )
        self.db.commit()

    def record(self, server: str, tool: str, ok: bool, ms: int, via: str) -> None:
        self.db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?)", (time.time(), server, tool, int(ok), ms, via))
        self.db.commit()

    def counts(self, window_days: float) -> dict[tuple[str, str], int]:
        since = time.time() - window_days * 86400
        rows = self.db.execute(
            "SELECT server, tool, COUNT(*) FROM calls WHERE ts >= ? AND ok = 1 GROUP BY server, tool", (since,)
        )
        return {(s, t): n for s, t, n in rows}

    def total(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]


def matches(server: str, tool: str, patterns: list[str]) -> bool:
    key = f"{server}/{tool}"
    return any(fnmatch.fnmatchcase(key, p) for p in patterns)


def pick_hot(keys: list[tuple[str, str]], counts: dict, settings: dict, have_history: bool) -> set[tuple[str, str]]:
    keys = [k for k in keys if not matches(*k, settings["blocked"])]
    if not have_history and settings["bootstrap"] == "all":
        return set(keys)
    hot = {k for k in keys if matches(*k, settings["pinned"])}
    ranked = sorted((k for k in keys if k not in hot and counts.get(k, 0) >= settings["min_calls"]),
                    key=lambda k: -counts[k])
    room = max(0, settings["max_hot"] - len(hot))
    return hot | set(ranked[:room])
