import sqlite3
from pathlib import Path

import config


MODERATION_DB_PATH = Path(config.MODERATION_DB_PATH)


def moderation_db() -> sqlite3.Connection:
    MODERATION_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(MODERATION_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def same_database_path(path: str | Path | None) -> bool:
    if path is None:
        return False

    return Path(path).expanduser().resolve() == MODERATION_DB_PATH.expanduser().resolve()
