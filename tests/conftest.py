import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

PERFIL_CORREDOR = {
    "level": "intermedio",
    "frequency": 3,
    "target_km": 21.1,
    "pace": "6:00",
    "city": "Rosario",
    "window_start": "07:00",
    "window_end": "09:00",
    "send_time": "06:30",
}
TROTE_5_KM = {"blocks": [{"kind": "trote", "km": 5}]}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Cada test usa su propia base y secretos de prueba; nunca los reales."""
    monkeypatch.setenv("RUNIA_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("JWT_SECRET", "secreto-de-prueba-de-al-menos-32-bytes")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")
    # Sin token de Telegram la app no levanta el bot: los tests no salen a la red.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-openweather")


@pytest.fixture
def client(env):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def conn(client):
    """Conexión directa a la base del test, con el esquema ya creado por la app."""
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def auth(client):
    """Registra un usuario y devuelve los headers con su token."""

    def _auth(email="pepe@example.com"):
        credenciales = {"email": email, "password": "correr10km"}
        client.post("/register", json=credenciales)
        token = client.post("/login", json=credenciales).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _auth


@pytest.fixture
def corredor(client, auth, conn):
    """Crea un usuario con perfil, chat vinculado y sesiones planificadas; devuelve sus headers.

    Cada sesión es una fecha (trote suave de 5 km) o una tupla (fecha, tipo, detalle).
    """

    def _corredor(email="pepe@example.com", chat=1001, sesiones=(), perfil=PERFIL_CORREDOR):
        headers = auth(email)
        assert client.put("/profile", json=perfil, headers=headers).status_code == 200
        user_id = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
        conn.execute("UPDATE users SET telegram_chat_id = ? WHERE id = ?", (chat, user_id))
        fechas = [s if not isinstance(s, tuple) else s[0] for s in sesiones]
        if fechas:
            lunes = min(fechas) - timedelta(days=min(fechas).weekday())
            plan_id = conn.execute(
                "INSERT INTO plans (user_id, week_start, planned_km, created_at) VALUES (?, ?, 5, ?)",
                (user_id, lunes.isoformat(), f"{lunes.isoformat()}T03:00:00+00:00"),
            ).lastrowid
        for sesion in sesiones:
            dia, tipo, detalle = sesion if isinstance(sesion, tuple) else (sesion, "suave", TROTE_5_KM)
            km = sum(b["km"] for b in detalle["blocks"] if b["kind"] != "caminata")
            conn.execute(
                "INSERT INTO sessions (plan_id, user_id, date, kind, jog_km, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, user_id, dia.isoformat(), tipo, km, json.dumps(detalle)),
            )
        conn.commit()
        return headers

    return _corredor


@pytest.fixture
def telegram():
    """Bot falso que guarda los mensajes enviados; nunca se sale a la red."""
    return SimpleNamespace(send_message=AsyncMock())
