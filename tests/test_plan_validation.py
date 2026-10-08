import pytest
from fastapi import HTTPException

from app.profile import missing_plan_data, require_plan_data

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
CHAT = 1001
EMAIL = "pepe@example.com"


@pytest.fixture
def usuario(client, auth, conn):
    """Crea un usuario con el perfil dado, con o sin chat vinculado, y devuelve su fila."""

    def _usuario(perfil, chat=CHAT):
        r = client.put("/profile", json=perfil, headers=auth())
        assert r.status_code == 200
        conn.execute("UPDATE users SET telegram_chat_id = ? WHERE email = ?", (chat, EMAIL))
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE email = ?", (EMAIL,)).fetchone()

    return _usuario


def sin(*campos):
    return {k: v for k, v in PERFIL.items() if k not in campos}


def test_un_perfil_completo_con_chat_vinculado_no_tiene_faltantes(usuario):
    user = usuario(PERFIL)
    assert missing_plan_data(user) is None
    require_plan_data(user)


# AC-08
@pytest.mark.parametrize("nivel", ["intermedio", "avanzado"])
def test_intermedio_o_avanzado_sin_ritmo_se_rechaza(usuario, nivel):
    user = usuario({**sin("pace"), "level": nivel})
    assert missing_plan_data(user) == "No hay datos sobre tu ritmo cargados en tu perfil"


# AC-09
@pytest.mark.parametrize(
    "faltantes, campo",
    [
        (("level",), "nivel"),
        (("frequency",), "frecuencia"),
        (("target_km",), "distancia objetivo"),
        (("city",), "ciudad"),
        (("window_start",), "rango horario"),
        (("window_end",), "rango horario"),
        (("window_start", "window_end"), "rango horario"),
        (("send_time",), "hora de envío"),
    ],
)
def test_un_dato_obligatorio_faltante_se_rechaza_con_su_nombre(usuario, faltantes, campo):
    user = usuario(sin(*faltantes))
    assert missing_plan_data(user) == f"No hay datos sobre tu {campo} cargados en tu perfil"


# AC-09: sin nivel no se puede saber si el ritmo es obligatorio; se informa el nivel.
def test_sin_nivel_ni_ritmo_se_informa_el_nivel(usuario):
    user = usuario(sin("level", "pace"))
    assert missing_plan_data(user) == "No hay datos sobre tu nivel cargados en tu perfil"


def test_un_usuario_sin_perfil_recibe_el_primer_faltante(usuario):
    assert missing_plan_data(usuario({}, chat=None)) == (
        "No hay datos sobre tu nivel cargados en tu perfil"
    )


# AC-10
def test_novato_sin_ritmo_no_tiene_faltantes(usuario):
    user = usuario({**sin("pace"), "level": "novato"})
    assert missing_plan_data(user) is None
    require_plan_data(user)


# AC-11
def test_sin_chat_de_telegram_vinculado_se_rechaza(usuario):
    user = usuario(PERFIL, chat=None)
    assert missing_plan_data(user) == "Tu cuenta no tiene un chat de Telegram vinculado"


# AC-08, AC-09, AC-11: el rechazo es un 422 con el mensaje exacto.
@pytest.mark.parametrize(
    "perfil, chat, mensaje",
    [
        (sin("pace"), CHAT, "No hay datos sobre tu ritmo cargados en tu perfil"),
        (sin("city"), CHAT, "No hay datos sobre tu ciudad cargados en tu perfil"),
        (PERFIL, None, "Tu cuenta no tiene un chat de Telegram vinculado"),
    ],
)
def test_require_plan_data_rechaza_con_422_y_el_mensaje(usuario, perfil, chat, mensaje):
    with pytest.raises(HTTPException) as error:
        require_plan_data(usuario(perfil, chat=chat))
    assert error.value.status_code == 422
    assert error.value.detail == mensaje
