import pytest

from app.auth import RENEWED_TOKEN_HEADER

PERFIL = {
    "level": "intermedio",
    "frequency": 4,
    "target_km": 21.1,
    "pace": "5:30",
    "city": "Rosario",
    "window_start": "07:00",
    "window_end": "09:00",
    "send_time": "06:30",
}
VACIO = dict.fromkeys(PERFIL)


def auth(client, email="pepe@example.com"):
    """Registra un usuario y devuelve los headers con su token."""
    credenciales = {"email": email, "password": "correr10km"}
    client.post("/register", json=credenciales)
    token = client.post("/login", json=credenciales).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_un_usuario_nuevo_tiene_el_perfil_vacio(client):
    r = client.get("/profile", headers=auth(client))
    assert r.status_code == 200 and r.json() == VACIO


# AC-06
def test_put_con_datos_validos_persiste_y_devuelve_200(client):
    headers = auth(client)
    r = client.put("/profile", json=PERFIL, headers=headers)
    assert r.status_code == 200 and r.json() == PERFIL
    assert client.get("/profile", headers=headers).json() == PERFIL


# AC-06: el novato no necesita ritmo actual.
def test_put_de_novato_sin_ritmo_es_valido(client):
    headers = auth(client)
    novato = {**PERFIL, "level": "novato"}
    del novato["pace"]
    assert client.put("/profile", json=novato, headers=headers).status_code == 200
    assert client.get("/profile", headers=headers).json() == {**novato, "pace": None}


# AC-06: valores en los límites de cada rango.
@pytest.mark.parametrize(
    "cambio",
    [
        {"frequency": 1},
        {"frequency": 7},
        {"target_km": 1},
        {"target_km": 42.2},
        {"pace": "3:00"},
        {"pace": "10:00"},
        {"city": "x" * 100},
        {"window_start": "08:00", "window_end": "09:00"},
        {"window_start": "00:00", "window_end": "23:59", "send_time": "23:58"},
    ],
)
def test_put_acepta_los_valores_limite(client, cambio):
    assert client.put("/profile", json={**PERFIL, **cambio}, headers=auth(client)).status_code == 200


def test_el_ritmo_se_devuelve_normalizado(client):
    r = client.put("/profile", json={"pace": "05:07"}, headers=auth(client))
    assert r.json()["pace"] == "5:07"


# AC-07
@pytest.mark.parametrize(
    "cambio",
    [
        {"level": "experto"},
        {"frequency": 0},
        {"frequency": 8},
        {"frequency": 3.5},
        {"frequency": "3"},
        {"target_km": 0.9},
        {"target_km": 42.3},
        {"target_km": "10"},
        {"pace": "2:59"},
        {"pace": "10:01"},
        {"pace": "5:60"},
        {"pace": "5.30"},
        {"pace": 330},
        {"city": ""},
        {"city": "   "},
        {"city": "x" * 101},
        {"window_start": "7:00"},
        {"window_end": "24:00"},
        {"send_time": "06:60"},
        {"send_time": "mañana"},
        # El inicio debe ser anterior al fin, con al menos 1 hora de duración.
        {"window_start": "09:00", "window_end": "09:00"},
        {"window_start": "10:00", "window_end": "09:00"},
        {"window_start": "08:01", "window_end": "09:00"},
        # La hora de envío debe ser anterior al fin del rango.
        {"send_time": "09:00"},
        {"send_time": "12:00"},
        {"apodo": "Pepe"},
    ],
)
def test_put_con_un_dato_invalido_devuelve_422_sin_persistir_nada(client, cambio):
    headers = auth(client)
    r = client.put("/profile", json={**PERFIL, **cambio}, headers=headers)
    assert r.status_code == 422
    assert client.get("/profile", headers=headers).json() == VACIO


# AC-07: las reglas entre horarios también se validan contra lo ya guardado.
@pytest.mark.parametrize(
    "cambio",
    [{"window_end": "07:30"}, {"window_start": "08:30"}, {"send_time": "09:30"}, {"window_end": "06:30"}],
)
def test_put_parcial_que_rompe_los_horarios_guardados_devuelve_422(client, cambio):
    headers = auth(client)
    client.put("/profile", json=PERFIL, headers=headers)
    assert client.put("/profile", json=cambio, headers=headers).status_code == 422
    assert client.get("/profile", headers=headers).json() == PERFIL


# AC-51
def test_editar_el_perfil_devuelve_los_valores_nuevos(client):
    headers = auth(client)
    client.put("/profile", json=PERFIL, headers=headers)
    nuevo = {
        "level": "avanzado",
        "frequency": 6,
        "target_km": 42.2,
        "pace": "4:15",
        "city": "Córdoba",
        "window_start": "18:00",
        "window_end": "20:30",
        "send_time": "17:00",
    }
    assert client.put("/profile", json=nuevo, headers=headers).status_code == 200
    assert client.get("/profile", headers=headers).json() == nuevo


# AC-51: los campos que no se envían conservan su valor.
def test_put_parcial_conserva_los_demas_campos(client):
    headers = auth(client)
    client.put("/profile", json=PERFIL, headers=headers)
    r = client.put("/profile", json={"city": "Mendoza"}, headers=headers)
    assert r.status_code == 200
    assert client.get("/profile", headers=headers).json() == {**PERFIL, "city": "Mendoza"}


# AC-55
def test_bajar_a_novato_no_tiene_restricciones(client):
    headers = auth(client)
    client.put("/profile", json=PERFIL, headers=headers)
    assert client.put("/profile", json={"level": "novato"}, headers=headers).status_code == 200
    assert client.get("/profile", headers=headers).json()["level"] == "novato"


# AC-37: cada usuario solo ve y modifica su propio perfil.
def test_el_perfil_es_el_del_usuario_del_token(client):
    pepe, mich = auth(client), auth(client, "mich@example.com")
    client.put("/profile", json=PERFIL, headers=pepe)
    assert client.get("/profile", headers=mich).json() == VACIO
    client.put("/profile", json={"city": "Salta"}, headers=mich)
    assert client.get("/profile", headers=pepe).json() == PERFIL


# AC-73
@pytest.mark.parametrize("metodo", ["get", "put"])
def test_el_perfil_exige_token(client, metodo):
    r = client.request(metodo, "/profile", json=PERFIL)
    assert r.status_code == 401 and "level" not in r.json()


# AC-05
def test_el_perfil_devuelve_el_token_renovado(client):
    assert RENEWED_TOKEN_HEADER in client.get("/profile", headers=auth(client)).headers
