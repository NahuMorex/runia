"""Hash de contraseñas (Argon2id) y tokens de sesión (JWT)."""

from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app import config

# RNF-03: Argon2id con los parámetros mínimos de OWASP (m en KiB).
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)

JWT_ALGORITHM = "HS256"
TOKEN_TTL = timedelta(hours=2)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_token(user_id: int, now: datetime | None = None) -> str:
    """Emite un token de sesión que vence a las 2 horas."""
    now = now or datetime.now(timezone.utc)
    claims = {"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(claims, config.jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> int | None:
    """Devuelve el id del usuario, o None si el token es inválido o está vencido."""
    try:
        claims = jwt.decode(
            token,
            config.jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp"]},
        )
        return int(claims["sub"])
    except (jwt.InvalidTokenError, ValueError):
        return None
