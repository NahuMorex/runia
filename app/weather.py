"""Pronóstico de OpenWeatherMap por ciudad (RF-05): máximos dentro del rango horario preferido.

Este módulo solo consulta y resume el pronóstico; qué es una condición desfavorable
se decide aparte.
"""

import logging
import time as clock
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app import config
from app.log import log_event

FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
# El servicio solo se usa en Argentina (PRD, Fuera de Alcance).
COUNTRY_CODE = "AR"
# RNF-01: el clima y Gemini entran juntos en 10 segundos, así que el clima no espera más que esto.
WEATHER_TIMEOUT_SECONDS = 5
# El pronóstico trae una lectura cada 3 horas.
FORECAST_STEP = timedelta(hours=3)


class WeatherError(Exception):
    """OpenWeatherMap falló, tardó demasiado o devolvió algo que no se puede usar."""


@dataclass(frozen=True)
class Reading:
    """Una lectura del pronóstico, con la hora en zona horaria de Buenos Aires."""

    at: datetime
    rain_percent: int  # probabilidad de lluvia, de 0 a 100
    temp_c: float


@dataclass(frozen=True)
class WindowForecast:
    """Máximos pronosticados dentro del rango horario preferido de un día (RF-07a)."""

    rain_percent: int
    temp_c: float


def parse_forecast(payload: dict) -> list[Reading]:
    """Convierte la respuesta de OpenWeatherMap en lecturas; falla si le falta algún dato."""
    tz = ZoneInfo(config.TIMEZONE)
    return [
        Reading(
            at=datetime.fromtimestamp(item["dt"], tz),
            rain_percent=round(float(item["pop"]) * 100),
            temp_c=float(item["main"]["temp"]),
        )
        for item in payload["list"]
    ]


def window_forecast(
    readings: Sequence[Reading], day: date, window_start: str, window_end: str
) -> WindowForecast | None:
    """Máximo de lluvia y de temperatura en el rango horario de ese día, o None si no hay datos.

    Como hay una lectura cada 3 horas, además de las que caen dentro del rango se
    toman la última anterior al inicio y la primera posterior al fin: un rango de
    1 hora puede no contener ninguna.
    """
    tz = ZoneInfo(config.TIMEZONE)
    start = datetime.combine(day, time.fromisoformat(window_start), tz)
    end = datetime.combine(day, time.fromisoformat(window_end), tz)
    inside = [r for r in readings if start - FORECAST_STEP < r.at < end + FORECAST_STEP]
    if not inside:
        return None
    return WindowForecast(
        rain_percent=max(r.rain_percent for r in inside),
        temp_c=max(r.temp_c for r in inside),
    )


def _fetch(city: str) -> httpx.Response:
    return httpx.get(
        FORECAST_URL,
        params={
            "q": f"{city},{COUNTRY_CODE}",
            "units": "metric",
            "appid": config.openweather_api_key(),
        },
        timeout=WEATHER_TIMEOUT_SECONDS,
    )


def _fail(
    motivo: str, user_id: int, error: Exception | None = None, codigo: int | None = None
) -> WeatherError:
    # AC-12. No se registra el mensaje del error: puede traer la URL con la API key.
    log_event(
        "external_error",
        level=logging.ERROR,
        servicio="openweather",
        motivo=motivo,
        tipo=type(error).__name__ if error else None,
        codigo=codigo,
        user_id=user_id,
    )
    return WeatherError(motivo)


def get_forecast(city: str, user_id: int) -> list[Reading]:
    """Consulta el pronóstico de la ciudad; lanza WeatherError si falla (AC-12)."""
    started = clock.monotonic()
    try:
        response = _fetch(city)
    except httpx.TimeoutException as error:
        raise _fail("timeout", user_id, error) from error
    except Exception as error:  # red caída, falta la API key, etc.: todo es un fallo del servicio
        raise _fail("error_de_conexion", user_id, error) from error

    code = response.status_code
    if code == 429:
        raise _fail("rate_limit", user_id, codigo=code)
    if code >= 500:
        raise _fail("error_del_servidor", user_id, codigo=code)
    if code != 200:  # ciudad inexistente (404), API key rechazada (401), etc.
        raise _fail("error_http", user_id, codigo=code)

    try:
        readings = parse_forecast(response.json())
    except (ValueError, KeyError, TypeError) as error:
        raise _fail("respuesta_invalida", user_id, error) from error
    if not readings:
        raise _fail("respuesta_invalida", user_id)
    # RNF-02: cada herramienta invocada.
    log_event(
        "weather_call",
        ciudad=city,
        user_id=user_id,
        duracion_ms=round((clock.monotonic() - started) * 1000),
        lecturas=len(readings),
    )
    return readings
