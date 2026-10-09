"""Mensaje diario de punta a punta (RF-08): sesión del día, clima, fatiga y redacción.

El clima se evalúa a la hora de envío, antes de preguntar la fatiga (AC-23), y se
guarda con la pregunta. Con la fatiga ya resuelta se arma el texto a enviar. Siempre
se usan los valores vigentes del perfil (AC-71). El envío por Telegram está en app/bot.py.
"""

import json
import logging
import sqlite3
from datetime import date

from app import climate, db, weather, writer
from app.climate import Alert
from app.weather import WindowForecast
from app.daily import adjust_for_fatigue, build_daily
from app.fatigue import Check
from app.guardrails import Block, Session
from app.log import log_event
from app.profile import pace_to_seconds


def session_from_row(row: sqlite3.Row) -> Session:
    """Reconstruye la sesión guardada en la tabla sessions."""
    detail = json.loads(row["detail"])
    pace = detail.get("target_pace")
    return Session(
        day=date.fromisoformat(row["date"]),
        kind=row["kind"],
        blocks=tuple(
            Block(b["kind"], b["km"], b.get("recovery_seconds")) for b in detail["blocks"]
        ),
        pace_seconds=pace_to_seconds(pace) if pace else None,
    )


def check_weather(user: sqlite3.Row, day: date) -> tuple[Alert | None, bool]:
    """Evalúa el clima del día de la sesión: la alerta, si hay, y si el clima estuvo disponible.

    Registra la decisión de alerta y de alternativa (RNF-02). Si la consulta falla o el
    pronóstico no trae datos para el rango horario de hoy, sigue sin clima (AC-13).
    """
    start, end = user["window_start"], user["window_end"]
    try:
        readings = weather.get_forecast(user["city"], user["id"])
    except weather.WeatherError:
        readings = []  # el fallo ya quedó registrado como external_error (AC-12)
    forecasts = {
        d: weather.window_forecast(readings, d, start, end)
        for d in (day, *climate.alternative_days(day))
    }
    today = forecasts[day]
    if today is None:
        log_event("decision_climatica", user_id=user["id"], fecha=day.isoformat(), disponible=False)
        return None, False
    alert = climate.evaluate(day, forecasts)
    log_event(
        "decision_climatica",
        user_id=user["id"],
        fecha=day.isoformat(),
        disponible=True,
        lluvia=today.rain_percent,
        temperatura=today.temp_c,
        alerta=alert is not None,
        motivos=list(alert.reasons) if alert else [],
        alternativa=alert.alternative.isoformat() if alert and alert.alternative else None,
    )
    return alert, True


def weather_to_json(alert: Alert | None, available: bool) -> str:
    payload: dict = {"available": available, "alert": None}
    if alert is not None:
        payload["alert"] = {
            "reasons": list(alert.reasons),
            "rain_percent": alert.forecast.rain_percent,
            "temp_c": alert.forecast.temp_c,
            "alternative": alert.alternative.isoformat() if alert.alternative else None,
        }
    return json.dumps(payload)


def weather_from_json(text: str | None) -> tuple[Alert | None, bool]:
    """La evaluación guardada con la pregunta; sin evaluación guardada, el clima no está disponible."""
    if not text:
        return None, False
    payload = json.loads(text)
    alert = payload["alert"]
    if alert is None:
        return None, payload["available"]
    alternative = alert["alternative"]
    return (
        Alert(
            reasons=tuple(alert["reasons"]),
            forecast=WindowForecast(alert["rain_percent"], alert["temp_c"]),
            alternative=date.fromisoformat(alternative) if alternative else None,
        ),
        True,
    )


def evaluate_weather(check: Check) -> None:
    """Evalúa el clima de la sesión del día y lo guarda con la pregunta, antes de enviarla (AC-23)."""
    conn = db.connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (check.user_id,)).fetchone()
        if user is None:
            return
        alert, available = check_weather(user, check.day)
        conn.execute(
            "UPDATE fatigue_checks SET weather = ? WHERE id = ?",
            (weather_to_json(alert, available), check.id),
        )
        conn.commit()
    finally:
        conn.close()


def prepare_message(check: Check, fatigue_level: int | None) -> str | None:
    """Texto del mensaje diario de la pregunta ya resuelta, o None si no hay nada que enviar.

    `fatigue_level` es None cuando el usuario no respondió en 2 horas (AC-47).
    """
    conn = db.connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (check.user_id,)).fetchone()
        row = conn.execute(
            "SELECT date, kind, detail FROM sessions WHERE user_id = ? AND date = ?",
            (check.user_id, check.day.isoformat()),
        ).fetchone()
        saved = conn.execute(
            "SELECT weather FROM fatigue_checks WHERE id = ?", (check.id,)
        ).fetchone()
    finally:
        conn.close()
    if user is None or row is None:
        log_event(
            "mensaje_diario_sin_sesion",
            level=logging.WARNING,
            user_id=check.user_id,
            fecha=check.day.isoformat(),
        )
        return None

    planned = session_from_row(row)
    session = adjust_for_fatigue(planned, fatigue_level)
    if session != planned:
        # RNF-02: la reclasificación por fatiga, con el valor antes y después.
        log_event(
            "guardrail",
            regla="fatiga_alta",
            antes=planned.kind,
            despues=session.kind,
            fecha=check.day.isoformat(),
            user_id=check.user_id,
        )
    alert, weather_available = weather_from_json(saved["weather"] if saved else None)
    daily = build_daily(
        planned, user["window_start"], user["window_end"], fatigue_level, alert, weather_available
    )
    return writer.write_message(daily, check.user_id)
