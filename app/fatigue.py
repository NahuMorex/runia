"""Pregunta diaria de fatiga (RF-15): a quién preguntarle, qué respondió y cuándo vence la espera.

Acá está el estado en la base y las reglas; el envío por Telegram está en app/bot.py.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from app import config

# AC-47: la respuesta se espera como máximo 2 horas desde la primera pregunta.
MAX_WAIT = timedelta(hours=2)
# AC-75: solo vale un entero de 1 a 10.
_FATIGUE_RE = re.compile(r"10|[1-9]")


@dataclass(frozen=True)
class Check:
    """Una pregunta de fatiga hecha a un usuario para la sesión de un día."""

    id: int
    user_id: int
    chat_id: int
    day: date
    asked_at: datetime


class ReplyStatus(Enum):
    NO_PENDING = "no_pending"  # no hay ninguna pregunta esperando respuesta en ese chat
    INVALID = "invalid"  # la respuesta no es un entero de 1 a 10 (AC-75)
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class Reply:
    status: ReplyStatus
    check: Check | None = None
    fatigue: int | None = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_fatigue(text: str) -> int | None:
    """La fatiga si el texto es un entero de 1 a 10, o None."""
    text = text.strip()
    return int(text) if _FATIGUE_RE.fullmatch(text) else None


def _check(row: sqlite3.Row) -> Check:
    return Check(
        id=row["id"],
        user_id=row["user_id"],
        chat_id=row["telegram_chat_id"],
        day=date.fromisoformat(row["date"]),
        asked_at=datetime.fromisoformat(row["asked_at"]),
    )


def open_due_checks(conn: sqlite3.Connection, now: datetime) -> list[Check]:
    """Abre la pregunta de hoy para cada usuario al que le llegó la hora de envío (AC-47).

    Toma a los usuarios con chat vinculado y sesión planificada para hoy a los que
    todavía no se les preguntó, usando la hora de envío vigente del perfil (AC-71).
    Si la hora de envío ya pasó (la app estuvo caída, o el plan se generó después),
    se pregunta igual mientras no haya terminado el rango horario preferido.
    """
    local = now.astimezone(ZoneInfo(config.TIMEZONE))
    today, hour = local.date().isoformat(), local.strftime("%H:%M")
    conn.execute("BEGIN IMMEDIATE")
    try:
        users = conn.execute(
            """
            SELECT u.id FROM users u
            WHERE u.telegram_chat_id IS NOT NULL
              AND u.send_time <= :hour AND :hour < u.window_end
              AND EXISTS (SELECT 1 FROM sessions s WHERE s.user_id = u.id AND s.date = :today)
              AND NOT EXISTS (
                  SELECT 1 FROM fatigue_checks c WHERE c.user_id = u.id AND c.date = :today
              )
            ORDER BY u.id
            """,
            {"hour": hour, "today": today},
        ).fetchall()
        ids = [
            conn.execute(
                "INSERT INTO fatigue_checks (user_id, date, asked_at) VALUES (?, ?, ?)",
                (u["id"], today, now.isoformat()),
            ).lastrowid
            for u in users
        ]
        conn.commit()
    finally:
        # Sin efecto si ya se hizo commit; deshace la transacción ante cualquier error.
        conn.rollback()
    return [c for c in (_get(conn, check_id) for check_id in ids) if c is not None]


def _get(conn: sqlite3.Connection, check_id: int) -> Check | None:
    row = conn.execute(
        """
        SELECT c.id, c.user_id, c.date, c.asked_at, u.telegram_chat_id
        FROM fatigue_checks c JOIN users u ON u.id = c.user_id WHERE c.id = ?
        """,
        (check_id,),
    ).fetchone()
    return _check(row) if row else None


def discard_check(conn: sqlite3.Connection, check: Check) -> None:
    """Borra una pregunta que no se pudo enviar, para que se vuelva a intentar."""
    conn.execute("DELETE FROM fatigue_checks WHERE id = ? AND closed_at IS NULL", (check.id,))
    conn.commit()


def _close(conn: sqlite3.Connection, check: Check, fatigue: int | None, now: datetime) -> bool:
    """Cierra la pregunta; False si ya la había cerrado otro (respuesta y vencimiento a la vez)."""
    cursor = conn.execute(
        "UPDATE fatigue_checks SET fatigue = ?, closed_at = ? WHERE id = ? AND closed_at IS NULL",
        (fatigue, now.isoformat(), check.id),
    )
    conn.commit()
    return cursor.rowcount == 1


def close_expired_checks(conn: sqlite3.Connection, now: datetime) -> list[Check]:
    """Cierra sin fatiga las preguntas que llevan 2 horas sin respuesta y las devuelve (AC-47)."""
    rows = conn.execute(
        """
        SELECT c.id, c.user_id, c.date, c.asked_at, u.telegram_chat_id
        FROM fatigue_checks c JOIN users u ON u.id = c.user_id
        WHERE c.closed_at IS NULL ORDER BY c.asked_at, c.id
        """
    ).fetchall()
    expired = [c for c in map(_check, rows) if now - c.asked_at >= MAX_WAIT]
    return [c for c in expired if _close(conn, c, None, now)]


def record_reply(conn: sqlite3.Connection, chat_id: int, text: str, now: datetime) -> Reply:
    """Procesa un mensaje del chat como respuesta a la pregunta de fatiga pendiente.

    Una respuesta inválida no cierra la pregunta ni reinicia la espera (AC-75). Pasadas
    las 2 horas la respuesta ya no cuenta: la pregunta se cierra por vencimiento.
    """
    row = conn.execute(
        """
        SELECT c.id, c.user_id, c.date, c.asked_at, u.telegram_chat_id
        FROM fatigue_checks c JOIN users u ON u.id = c.user_id
        WHERE u.telegram_chat_id = ? AND c.closed_at IS NULL
        ORDER BY c.asked_at DESC LIMIT 1
        """,
        (chat_id,),
    ).fetchone()
    if row is None:
        return Reply(ReplyStatus.NO_PENDING)
    check = _check(row)
    if now - check.asked_at >= MAX_WAIT:
        return Reply(ReplyStatus.NO_PENDING)
    fatigue = parse_fatigue(text)
    if fatigue is None:
        return Reply(ReplyStatus.INVALID, check)
    if not _close(conn, check, fatigue, now):
        return Reply(ReplyStatus.NO_PENDING)
    return Reply(ReplyStatus.ACCEPTED, check, fatigue)
