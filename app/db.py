"""Acceso a SQLite con el módulo sqlite3 de la stdlib."""

import sqlite3
from collections.abc import Iterator

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Perfil (RF-03a a RF-03g); NULL mientras no se cargue.
    level TEXT CHECK (level IN ('novato', 'intermedio', 'avanzado')),
    frequency INTEGER,
    target_km REAL,
    pace_seconds INTEGER,
    city TEXT,
    window_start TEXT,
    window_end TEXT,
    send_time TEXT,
    -- RF-14: un único chat por cuenta y una única cuenta por chat.
    telegram_chat_id INTEGER UNIQUE
);

CREATE TABLE IF NOT EXISTS link_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    week_start TEXT NOT NULL,  -- lunes de la semana (YYYY-MM-DD)
    planned_km REAL NOT NULL,  -- volumen planificado en km de trote, tal como se generó
    is_partial INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, week_start)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date TEXT NOT NULL,  -- YYYY-MM-DD
    kind TEXT NOT NULL CHECK (kind IN ('suave', 'calidad')),
    jog_km REAL NOT NULL,
    detail TEXT NOT NULL,  -- JSON con los bloques o tramos de la sesión
    UNIQUE (plan_id, date)
);

-- RF-15: una pregunta de fatiga por usuario y por día con sesión planificada.
CREATE TABLE IF NOT EXISTS fatigue_checks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date TEXT NOT NULL,  -- día de la sesión (YYYY-MM-DD)
    asked_at TEXT NOT NULL,  -- primera pregunta; la espera de 2 horas cuenta desde acá (AC-75)
    fatigue INTEGER,  -- respuesta de 1 a 10; NULL si la espera venció sin respuesta
    closed_at TEXT,  -- NULL mientras se espera la respuesta
    UNIQUE (user_id, date)
);
"""


def connect() -> sqlite3.Connection:
    # check_same_thread=False: FastAPI puede abrir y usar la conexión en hilos distintos del pool.
    conn = sqlite3.connect(config.db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """Dependencia de FastAPI: una conexión por request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
