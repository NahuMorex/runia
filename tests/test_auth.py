from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import current_user
from app.security import create_token

EMAIL = "milena@example.com"
PASSWORD = "correr10km"


def registrar(client, email=EMAIL, password=PASSWORD):
    return client.post("/register", json={"email": email, "password": password})


def cantidad_de_usuarios():
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def protegido(client):
    """App mínima con un endpoint autenticado, para probar la dependencia current_user."""
    app = FastAPI()

    @app.get("/yo")
    def yo(user=Depends(current_user)):
        return {"id": user["id"]}

    return TestClient(app)


# AC-01
def test_registro_crea_el_usuario_y_devuelve_201(client):
    r = registrar(client)
    assert r.status_code == 201
    assert r.json()["email"] == EMAIL
    assert cantidad_de_usuarios() == 1


# RNF-03
def test_la_contraseña_se_guarda_con_argon2id_y_parametros_owasp(client):
    registrar(client)
    conn = db.connect()
    guardado = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    conn.close()
    assert guardado.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert PASSWORD not in guardado


@pytest.mark.parametrize("largo", [8, 128])
def test_registro_acepta_los_limites_de_largo_de_contraseña(client, largo):
    assert registrar(client, password="a" * largo).status_code == 201


# AC-02
def test_registro_con_correo_ya_registrado_devuelve_409(client):
    registrar(client)
    assert registrar(client, password="otra-contraseña").status_code == 409
    assert registrar(client, email=EMAIL.upper()).status_code == 409
    assert cantidad_de_usuarios() == 1


# AC-72
@pytest.mark.parametrize(
    "email, password",
    [
        ("sin-arroba", PASSWORD),
        ("a@", PASSWORD),
        (EMAIL, "a" * 7),
        (EMAIL, "a" * 129),
    ],
)
def test_registro_con_datos_invalidos_devuelve_422(client, email, password):
    assert registrar(client, email, password).status_code == 422
    assert cantidad_de_usuarios() == 0


# AC-03
def test_login_correcto_devuelve_jwt_que_vence_a_las_2_horas(client):
    user_id = registrar(client).json()["id"]
    r = client.post("/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200
    claims = jwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["sub"] == str(user_id)
    assert claims["exp"] - claims["iat"] == 2 * 60 * 60


# AC-04
@pytest.mark.parametrize(
    "email, password",
    [(EMAIL, "contraseña-mala"), ("otro@example.com", PASSWORD), (EMAIL, "")],
)
def test_login_incorrecto_devuelve_401_sin_token(client, email, password):
    registrar(client)
    r = client.post("/login", json={"email": email, "password": password})
    assert r.status_code == 401
    assert "access_token" not in r.json()


def test_endpoint_autenticado_acepta_un_token_valido(client, protegido):
    user_id = registrar(client).json()["id"]
    token = client.post("/login", json={"email": EMAIL, "password": PASSWORD}).json()["access_token"]
    r = protegido.get("/yo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json() == {"id": user_id}


# AC-73
def test_endpoint_autenticado_sin_token_invalido_o_vencido_devuelve_401(client, protegido):
    user_id = registrar(client).json()["id"]
    hace_3_horas = datetime.now(timezone.utc) - timedelta(hours=3)
    vencido = create_token(user_id, now=hace_3_horas)
    otra_firma = jwt.encode({"sub": str(user_id), "iat": 0, "exp": 9999999999}, "x" * 32, "HS256")
    sin_usuario = create_token(user_id + 1)

    assert protegido.get("/yo").status_code == 401
    for token in ("no-es-un-jwt", vencido, otra_firma, sin_usuario):
        r = protegido.get("/yo", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
        assert "id" not in r.json()
