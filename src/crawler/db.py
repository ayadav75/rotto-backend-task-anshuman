import os
from pathlib import Path

import psycopg

SCHEMA = Path(__file__).parent / "store" / "schema.sql"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://crawler:crawler@localhost:5432/jobs")


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())


def migrate(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    conn.commit()
