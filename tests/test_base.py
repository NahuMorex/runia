import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.log import REDACTED, log_event
from app.main import app


def test_init_db_crea_las_tablas_y_es_idempotente():
    db.init_db()
    db.init_db()
    conn = db.connect()
    tablas = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"users", "link_codes", "plans", "sessions"} <= tablas


def test_init_db_agrega_la_columna_de_clima_a_una_base_anterior():
    conn = db.connect()
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY);"
        "CREATE TABLE fatigue_checks (id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT, "
        "asked_at TEXT, fatigue INTEGER, closed_at TEXT);"
        "INSERT INTO fatigue_checks (user_id, date, asked_at) VALUES (1, '2026-10-05', 'x');"
    )
    conn.close()

    db.init_db()
    db.init_db()

    conn = db.connect()
    fila = conn.execute("SELECT date, weather FROM fatigue_checks").fetchone()
    conn.close()
    assert tuple(fila) == ("2026-10-05", None)


def test_la_base_aplica_claves_foraneas():
    db.init_db()
    conn = db.connect()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO link_codes (code, user_id, created_at) VALUES ('abc', 999, '2026-01-01')"
        )
    conn.close()


def test_la_app_levanta_e_inicializa_la_base():
    with TestClient(app):
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        conn.close()


def test_require_env_falla_si_falta_la_variable(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY")
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        config.google_api_key()


def test_log_event_emite_json_y_redacta_secretos(capsys):
    log_event("guardrail", regla="tope_10", antes=12.0, despues=11.0, password="hunter2", access_token="x")
    registro = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert registro["event"] == "guardrail"
    assert registro["antes"] == 12.0 and registro["despues"] == 11.0
    assert registro["password"] == REDACTED and registro["access_token"] == REDACTED
    assert "ts" in registro and registro["level"] == "INFO"
