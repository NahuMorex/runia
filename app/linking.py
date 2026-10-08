"""Vínculo de la cuenta con un chat de Telegram mediante un código de un solo uso (RF-14)."""

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import current_user
from app.db import get_db

router = APIRouter()

LINK_CODE_TTL = timedelta(minutes=15)
CODE_LENGTH = 8
# Sin 0/O ni 1/I, para que el código se pueda tipear sin confusiones.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class LinkResult(Enum):
    LINKED = "linked"
    INVALID_CODE = "invalid_code"  # inexistente, ya usado o vencido (AC-45)
    ALREADY_LINKED = "already_linked"  # el chat o la cuenta ya tienen vínculo (AC-46)


class LinkCodeResponse(BaseModel):
    code: str
    expires_in_minutes: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_link_code(conn: sqlite3.Connection, user_id: int, now: datetime | None = None) -> str:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    conn.execute(
        "INSERT INTO link_codes (code, user_id, created_at) VALUES (?, ?, ?)",
        (code, user_id, (now or _now()).isoformat()),
    )
    conn.commit()
    return code


def redeem_link_code(
    conn: sqlite3.Connection, code: str, chat_id: int, now: datetime | None = None
) -> LinkResult:
    """Asocia el chat a la cuenta dueña del código y lo deja usado, todo en una transacción."""
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT c.user_id, c.created_at, c.used_at, u.telegram_chat_id
            FROM link_codes c JOIN users u ON u.id = c.user_id
            WHERE c.code = ?
            """,
            (code.strip().upper(),),
        ).fetchone()
        if (
            row is None
            or row["used_at"] is not None
            or now - datetime.fromisoformat(row["created_at"]) > LINK_CODE_TTL
        ):
            return LinkResult.INVALID_CODE
        chat_taken = conn.execute(
            "SELECT 1 FROM users WHERE telegram_chat_id = ?", (chat_id,)
        ).fetchone()
        if row["telegram_chat_id"] is not None or chat_taken:
            return LinkResult.ALREADY_LINKED
        conn.execute(
            "UPDATE users SET telegram_chat_id = ? WHERE id = ?", (chat_id, row["user_id"])
        )
        conn.execute(
            "UPDATE link_codes SET used_at = ? WHERE code = ?",
            (now.isoformat(), code.strip().upper()),
        )
        conn.commit()
        return LinkResult.LINKED
    finally:
        # Sin efecto si ya se hizo commit; deshace la transacción en cualquier rechazo o error.
        conn.rollback()


@router.post(
    "/telegram/link-code", status_code=status.HTTP_201_CREATED, response_model=LinkCodeResponse
)
def post_link_code(
    user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)
):
    if user["telegram_chat_id"] is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Tu cuenta ya tiene un chat de Telegram vinculado"
        )
    return LinkCodeResponse(
        code=create_link_code(conn, user["id"]),
        expires_in_minutes=int(LINK_CODE_TTL.total_seconds() // 60),
    )
