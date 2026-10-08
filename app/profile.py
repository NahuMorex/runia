"""Carga, edición y consulta del perfil del corredor (RF-03a a RF-03g, RF-04, RF-18a)."""

import re
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.auth import current_user
from app.db import get_db

router = APIRouter()

PACE_MIN_SECONDS = 3 * 60
PACE_MAX_SECONDS = 10 * 60
MIN_WINDOW_MINUTES = 60

_PACE_RE = re.compile(r"(\d{1,2}):([0-5]\d)")
# Horas en zona horaria de Buenos Aires (RF-03f, RF-03g).
HourMinute = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]

MISSING_DATA_TEXT = "No hay datos sobre tu {campo} cargados en tu perfil"
MISSING_CHAT_TEXT = "Tu cuenta no tiene un chat de Telegram vinculado"
# Datos obligatorios en el orden de RF-04, con el nombre que lleva el mensaje de AC-09.
_REQUIRED_FIELDS = (
    ("nivel", ("level",)),
    ("frecuencia", ("frequency",)),
    ("distancia objetivo", ("target_km",)),
    ("ciudad", ("city",)),
    ("rango horario", ("window_start", "window_end")),
    ("hora de envío", ("send_time",)),
)


def pace_to_seconds(pace: str) -> int:
    """Convierte un ritmo mm:ss por km a segundos; falla si el formato no es válido."""
    match = _PACE_RE.fullmatch(pace)
    if not match:
        raise ValueError("El ritmo debe tener formato mm:ss")
    return int(match[1]) * 60 + int(match[2])


def seconds_to_pace(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _minutes(hour_minute: str) -> int:
    hours, minutes = hour_minute.split(":")
    return int(hours) * 60 + int(minutes)


class Profile(BaseModel):
    """Datos del perfil. En el PUT, los campos que no se envían conservan su valor."""

    model_config = ConfigDict(extra="forbid")

    level: Literal["novato", "intermedio", "avanzado"] | None = None
    frequency: int | None = Field(default=None, strict=True, ge=1, le=7)
    target_km: float | None = Field(default=None, strict=True, ge=1, le=42.2)
    pace: str | None = None
    city: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
        | None
    ) = None
    window_start: HourMinute | None = None
    window_end: HourMinute | None = None
    send_time: HourMinute | None = None

    @field_validator("pace")
    @classmethod
    def _pace_in_range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        seconds = pace_to_seconds(value)
        if not PACE_MIN_SECONDS <= seconds <= PACE_MAX_SECONDS:
            raise ValueError("El ritmo debe estar entre 3:00 y 10:00 por km")
        return seconds_to_pace(seconds)


def schedule_error(profile: Profile) -> str | None:
    """Valida las reglas entre horarios (RF-03f, RF-03g) sobre el perfil ya combinado."""
    start, end, send = profile.window_start, profile.window_end, profile.send_time
    if start and end and _minutes(end) - _minutes(start) < MIN_WINDOW_MINUTES:
        return "El rango horario debe empezar antes de terminar y durar al menos 1 hora"
    if send and end and _minutes(send) >= _minutes(end):
        return "La hora de envío debe ser anterior a la hora de fin del rango horario"
    return None


def profile_from_row(user: sqlite3.Row) -> Profile:
    return Profile(
        level=user["level"],
        frequency=user["frequency"],
        target_km=user["target_km"],
        pace=seconds_to_pace(user["pace_seconds"]) if user["pace_seconds"] is not None else None,
        city=user["city"],
        window_start=user["window_start"],
        window_end=user["window_end"],
        send_time=user["send_time"],
    )


def missing_plan_data(user: sqlite3.Row) -> str | None:
    """Mensaje del primer dato obligatorio que falta para generar un plan (RF-04), o None."""
    for campo, columns in _REQUIRED_FIELDS:
        if any(user[column] is None for column in columns):
            return MISSING_DATA_TEXT.format(campo=campo)
    if user["telegram_chat_id"] is None:
        return MISSING_CHAT_TEXT
    # AC-10: el novato no necesita ritmo actual.
    if user["level"] != "novato" and user["pace_seconds"] is None:
        return MISSING_DATA_TEXT.format(campo="ritmo")
    return None


def require_plan_data(user: sqlite3.Row) -> None:
    """Rechaza con 422 si falta algún dato obligatorio para generar un plan (AC-08, AC-09, AC-11)."""
    error = missing_plan_data(user)
    if error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, error)


@router.get("/profile", response_model=Profile)
def get_profile(user: sqlite3.Row = Depends(current_user)):
    return profile_from_row(user)


@router.put("/profile", response_model=Profile)
def put_profile(
    body: Profile,
    user: sqlite3.Row = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    merged = profile_from_row(user).model_copy(update=body.model_dump(exclude_unset=True))
    error = schedule_error(merged)
    if error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, error)
    conn.execute(
        """
        UPDATE users
        SET level = ?, frequency = ?, target_km = ?, pace_seconds = ?, city = ?,
            window_start = ?, window_end = ?, send_time = ?
        WHERE id = ?
        """,
        (
            merged.level,
            merged.frequency,
            merged.target_km,
            pace_to_seconds(merged.pace) if merged.pace is not None else None,
            merged.city,
            merged.window_start,
            merged.window_end,
            merged.send_time,
            user["id"],
        ),
    )
    conn.commit()
    return merged
