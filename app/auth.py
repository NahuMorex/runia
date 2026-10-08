"""Registro, login y dependencia de usuario autenticado (RF-01, RF-02a)."""

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from app.db import get_db
from app.security import create_token, decode_token, hash_password, verify_password

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

# Se verifica contra este hash cuando el correo no existe, para que el login
# tarde lo mismo y no revele qué correos están registrados.
_DUMMY_HASH = hash_password("contraseña-que-no-es-de-nadie")


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class RegisterResponse(BaseModel):
    id: int
    email: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=RegisterResponse)
def register(body: RegisterRequest, conn: sqlite3.Connection = Depends(get_db)):
    email = body.email.lower()
    try:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, hash_password(body.password), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "El correo ya está registrado")
    return RegisterResponse(id=cursor.lastrowid, email=email)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, conn: sqlite3.Connection = Depends(get_db)):
    user = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", (body.email.strip().lower(),)
    ).fetchone()
    password_ok = verify_password(user["password_hash"] if user else _DUMMY_HASH, body.password)
    if not user or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Correo o contraseña incorrectos")
    return TokenResponse(access_token=create_token(user["id"]))


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    conn: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    """Dependencia para endpoints autenticados: responde 401 sin token válido (AC-73)."""
    user_id = decode_token(credentials.credentials) if credentials else None
    user = (
        conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user_id is not None
        else None
    )
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Token ausente, inválido o vencido",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
