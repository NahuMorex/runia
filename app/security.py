"""Hash de contraseñas (Argon2id) y tokens de sesión (JWT)."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app import config

# RNF-03: Argon2id con los parámetros mínimos de OWASP (m en KiB).
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)

JWT_ALGORITHM = "HS256"
TOKEN_TTL = timedelta(hours=2)
MAX_SESSION_AGE = timedelta(days=7)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_token(
    user_id: int, now: datetime | None = None, login_at: datetime | None = None
) -> str:
    """Emite un token de sesión que vence a las 2 horas.

    login_at es el momento del login original; se conserva al renovar el token
    para poder cortar la sesión a los 7 días (RF-02b).
    """
    now = now or datetime.now(timezone.utc)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + TOKEN_TTL,
        "login_at": int((login_at or now).timestamp()),
    }
    return jwt.encode(claims, config.jwt_secret(), algorithm=JWT_ALGORITHM)


@dataclass(frozen=True)
class Session:
    user_id: int
    login_at: datetime

    def renewable(self, now: datetime | None = None) -> bool:
        """True mientras no hayan pasado 7 días desde el login original."""
        return (now or datetime.now(timezone.utc)) - self.login_at < MAX_SESSION_AGE


def decode_token(token: str) -> Session | None:
    """Devuelve la sesión del token, o None si es inválido o está vencido."""
    try:
        claims = jwt.decode(
            token,
            config.jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp", "login_at"]},
        )
        return Session(
            user_id=int(claims["sub"]),
            login_at=datetime.fromtimestamp(claims["login_at"], timezone.utc),
        )
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError, OSError):
        return None
