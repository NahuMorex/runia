"""Generación del plan de la semana en curso a pedido del usuario (RF-21)."""

import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import config, planner
from app.auth import current_user
from app.db import get_db
from app.guardrails import (
    INTERVAL,
    NOVICE_QUALITY_WEEKS,
    InvalidPlan,
    Session,
    WeekSummary,
    apply_guardrails,
    check_structure,
    check_walk_jog_blocks,
    interval_seconds,
    load_caps,
    log_adjustments,
    novice_quality_allowed,
    weekly_km,
)
from app.log import log_event
from app.profile import require_plan_data, seconds_to_pace

router = APIRouter()

PLAN_EXISTS_TEXT = "Ya tenés un plan para la semana en curso"
UNAVAILABLE_TEXT = "No es posible generar un plan en este momento."
WEEK_DAYS = 7


class BlockResponse(BaseModel):
    kind: str
    km: float
    target_seconds: int | None = None  # tiempo objetivo del tramo (AC-15)
    recovery_seconds: int | None = None


class SessionResponse(BaseModel):
    date: date
    kind: str
    jog_km: float
    target_pace: str | None = None
    blocks: list[BlockResponse]


class PlanResponse(BaseModel):
    week_start: date
    is_partial: bool
    planned_km: float
    sessions: list[SessionResponse]


def today() -> date:
    """Fecha actual en la zona horaria de Buenos Aires; las semanas van de lunes a domingo."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def partial_session_count(frequency: int, days_left: int) -> int:
    """Sesiones de un plan de mitad de semana: la frecuencia, en proporción a los días que quedan."""
    return min(days_left, math.ceil(frequency * days_left / WEEK_DAYS))


def _previous_weeks(conn: sqlite3.Connection, user_id: int, week_start: date) -> list[WeekSummary]:
    """Resúmenes de las últimas semanas con plan, la más reciente primero.

    El registro de sesiones está fuera de alcance, así que ninguna semana tiene
    sesiones Completadas ni Parciales/Cortadas.
    """
    rows = conn.execute(
        """
        SELECT week_start, planned_km, is_partial FROM plans
        WHERE user_id = ? AND week_start < ? ORDER BY week_start DESC LIMIT ?
        """,
        (user_id, week_start.isoformat(), planner.MAX_PROMPT_WEEKS),
    ).fetchall()
    return [
        WeekSummary(
            planned_km=r["planned_km"],
            is_partial=bool(r["is_partial"]),
            week_start=date.fromisoformat(r["week_start"]),
        )
        for r in rows
    ]


def _last_full_week_km(conn: sqlite3.Connection, user_id: int, week_start: date) -> float | None:
    row = conn.execute(
        """
        SELECT planned_km FROM plans
        WHERE user_id = ? AND week_start < ? AND is_partial = 0 ORDER BY week_start DESC LIMIT 1
        """,
        (user_id, week_start.isoformat()),
    ).fetchone()
    return row["planned_km"] if row else None


def _session_response(session: Session) -> SessionResponse:
    pace = session.pace_seconds
    return SessionResponse(
        date=session.day,
        kind=session.kind,
        jog_km=session.jog_km,
        target_pace=seconds_to_pace(pace) if pace is not None else None,
        blocks=[
            BlockResponse(
                kind=b.kind,
                km=b.km,
                target_seconds=interval_seconds(b.km, pace) if b.kind == INTERVAL else None,
                recovery_seconds=b.recovery_seconds,
            )
            for b in session.blocks
        ],
    )


def _save_plan(conn: sqlite3.Connection, user_id: int, plan: PlanResponse) -> None:
    """Guarda el plan y sus sesiones en una sola transacción."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO plans (user_id, week_start, planned_km, is_partial, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                plan.week_start.isoformat(),
                plan.planned_km,
                int(plan.is_partial),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.executemany(
            "INSERT INTO sessions (plan_id, user_id, date, kind, jog_km, detail) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    cursor.lastrowid,
                    user_id,
                    s.date.isoformat(),
                    s.kind,
                    s.jog_km,
                    s.model_dump_json(include={"target_pace", "blocks"}, exclude_none=True),
                )
                for s in plan.sessions
            ],
        )
        conn.commit()
    finally:
        # Sin efecto si ya se hizo commit; deshace la transacción ante cualquier error.
        conn.rollback()


def _has_plan(conn: sqlite3.Connection, user_id: int, week_start: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM plans WHERE user_id = ? AND week_start = ?", (user_id, week_start.isoformat())
    ).fetchone()
    return row is not None


@router.post("/plan", status_code=status.HTTP_201_CREATED, response_model=PlanResponse)
def post_plan(
    user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)
):
    require_plan_data(user)
    current = today()
    week_start = current - timedelta(days=current.weekday())
    sunday = week_start + timedelta(days=WEEK_DAYS - 1)
    if _has_plan(conn, user["id"], week_start):
        raise HTTPException(status.HTTP_409_CONFLICT, PLAN_EXISTS_TEXT)

    # AC-57: un plan pedido a mitad de semana cubre desde hoy hasta el domingo.
    days = tuple(current + timedelta(days=i) for i in range((sunday - current).days + 1))
    is_partial = current != week_start
    session_count = (
        partial_session_count(user["frequency"], len(days)) if is_partial else user["frequency"]
    )

    weeks = _previous_weeks(conn, user["id"], week_start)
    previous = next((w for w in weeks if w.week_start == week_start - timedelta(weeks=1)), None)
    oldest_recent = week_start - timedelta(weeks=NOVICE_QUALITY_WEEKS)
    history = dict(
        previous_week=previous,
        last_full_week_km=_last_full_week_km(conn, user["id"], week_start),
        recent_weeks=[w for w in weeks if w.week_start >= oldest_recent],
    )
    profile = dict(
        level=user["level"],
        frequency=user["frequency"],
        target_km=user["target_km"],
        pace_seconds=user["pace_seconds"],
    )
    request = planner.PlanRequest(
        level=user["level"],
        target_km=user["target_km"],
        pace_seconds=user["pace_seconds"],
        days=days,
        session_count=session_count,
        caps=load_caps(
            user["level"],
            user["frequency"],
            user["target_km"],
            history["previous_week"],
            history["last_full_week_km"],
        ),
        quality_allowed=user["level"] != "novato"
        or novice_quality_allowed(user["pace_seconds"], history["recent_weeks"]),
        weeks=tuple(weeks),
    )

    try:
        proposed = planner.generate_plan(request, user["id"])
        check_structure(proposed, session_count, current, sunday)
        if user["level"] == "novato" and not weeks:
            check_walk_jog_blocks(proposed)  # AC-14: primera semana de una novata
        sessions, adjustments = apply_guardrails(proposed, **profile, **history)
    except planner.GenerationError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE_TEXT)
    except InvalidPlan as error:
        # El modelo respondió, pero con un plan que no se puede corregir sin inventar datos.
        log_event("plan_invalido", level=logging.ERROR, motivo=str(error), user_id=user["id"])
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE_TEXT)
    log_adjustments(adjustments, user["id"])

    plan = PlanResponse(
        week_start=week_start,
        is_partial=is_partial,
        planned_km=weekly_km(sessions),
        sessions=[_session_response(s) for s in sorted(sessions, key=lambda s: s.day)],
    )
    try:
        _save_plan(conn, user["id"], plan)
    except sqlite3.IntegrityError:
        # Solo es un 409 si otro request generó el plan de esta semana mientras se esperaba
        # al modelo; cualquier otro choque al guardar es un error y no se disfraza.
        if not _has_plan(conn, user["id"], week_start):
            raise
        raise HTTPException(status.HTTP_409_CONFLICT, PLAN_EXISTS_TEXT)
    return plan
