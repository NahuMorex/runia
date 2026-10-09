import json
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from google.genai import errors

from app import planner
from app.guardrails import Block, Caps, Session, WeekSummary
from app.planner import GenerationError, PlanRequest, build_prompt, generate_plan, parse_plan

LUNES = date(2026, 10, 5)
SEMANA = tuple(LUNES + timedelta(days=i) for i in range(7))
TOPES = Caps(5, "tope_inicial_sesion", 20, "tope_inicial_semanal")

INTERMEDIO = PlanRequest(
    level="intermedio",
    target_km=21.1,
    pace_seconds=360,
    days=SEMANA,
    session_count=4,
    caps=TOPES,
    quality_allowed=True,
)
NOVATA = PlanRequest(
    level="novato",
    target_km=10,
    pace_seconds=None,
    days=SEMANA[3:],
    session_count=2,
    caps=Caps(2, "tope_inicial_sesion", 6, "tope_inicial_semanal"),
    quality_allowed=False,
)

RESPUESTA = json.dumps(
    {
        "sessions": [
            {
                "date": "2026-10-05",
                "kind": "suave",
                "target_pace": None,
                "blocks": [{"kind": "caminata", "km": 0.3}, {"kind": "trote", "km": 4}],
            },
            {
                "date": "2026-10-07",
                "kind": "calidad",
                "target_pace": "5:45",
                "blocks": [
                    {"kind": "trote", "km": 2},
                    {"kind": "tramo", "km": 1, "recovery_seconds": 90},
                    {"kind": "trote", "km": 1},
                ],
            },
        ]
    }
)


@pytest.fixture
def gemini(monkeypatch):
    """Reemplaza la llamada a Gemini: los tests nunca salen a la red."""

    def _gemini(respuesta):
        prompts = []

        def falso(prompt):
            prompts.append(prompt)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        monkeypatch.setattr(planner, "_call_gemini", falso)
        return prompts

    return _gemini


def registros(capsys):
    return [json.loads(linea) for linea in capsys.readouterr().out.strip().splitlines()]


# --- Prompt ---


def test_el_prompt_incluye_perfil_dias_cantidad_y_topes():
    prompt = build_prompt(INTERMEDIO)
    assert "Nivel: intermedio" in prompt
    assert "Distancia objetivo: 21.1 km" in prompt
    assert "Ritmo actual: 6:00 min/km" in prompt
    assert "2026-10-05 (lunes)" in prompt and "2026-10-11 (domingo)" in prompt
    assert "Cantidad de sesiones: 4" in prompt
    assert "Máximo 5 km de trote por sesión" in prompt
    assert "Máximo 20 km de trote en toda la semana" in prompt


# AC-22
def test_el_prompt_informa_el_rango_de_ritmo_objetivo_permitido():
    assert "como máximo una, con ritmo objetivo entre 5:40 y 6:00 min/km" in build_prompt(INTERMEDIO)


# AC-20, AC-14
def test_el_prompt_de_una_novata_en_su_primera_semana_no_admite_calidad_y_pide_bloques():
    prompt = build_prompt(NOVATA)
    assert "Sesiones de calidad: ninguna" in prompt
    assert "Ritmo actual: sin cargar" in prompt
    assert "alterna bloques de caminata y de trote" in prompt
    assert "2026-10-08 (jueves)" in prompt and "2026-10-07" not in prompt
    assert "Sin semanas anteriores" in prompt


def test_el_prompt_pide_bloques_de_trote_cortos_solo_a_los_novatos():
    pedido = "Cada bloque de trote es corto, de 0.4 km como máximo"
    assert pedido in build_prompt(NOVATA)
    con_historial = PlanRequest(**{**NOVATA.__dict__, "weeks": (WeekSummary(4, week_start=LUNES),)})
    assert pedido in build_prompt(con_historial)
    assert pedido not in build_prompt(INTERMEDIO)


def test_el_prompt_solo_lleva_resumenes_de_las_ultimas_4_semanas():
    semanas = tuple(
        WeekSummary(20 + i, completed=i, week_start=LUNES - timedelta(weeks=i + 1)) for i in range(6)
    )
    prompt = build_prompt(PlanRequest(**{**INTERMEDIO.__dict__, "weeks": semanas}))
    assert "- Semana del 2026-09-28: 20 km de trote planificados, semana completa, 0 sesiones completadas" in prompt
    assert prompt.count("Semana del") == 4
    assert "2026-08-31" not in prompt


def test_el_resumen_semanal_marca_los_planes_parciales_y_los_cortes():
    semana = WeekSummary(6.5, is_partial=True, completed=1, hard_cut=True, week_start=LUNES)
    prompt = build_prompt(PlanRequest(**{**INTERMEDIO.__dict__, "weeks": (semana,)}))
    assert "6.5 km de trote planificados, plan parcial, 1 sesiones completadas, alguna sesión cortada" in prompt


# --- Respuesta ---


def test_la_respuesta_json_se_convierte_en_sesiones():
    assert parse_plan(RESPUESTA) == [
        Session(LUNES, "suave", (Block("caminata", 0.3), Block("trote", 4))),
        Session(
            LUNES + timedelta(days=2),
            "calidad",
            (Block("trote", 2), Block("tramo", 1, 90), Block("trote", 1)),
            345,
        ),
    ]


@pytest.mark.parametrize(
    "texto",
    [
        None,
        "",
        "no es json",
        "{}",
        '{"sessions": [{"date": "ayer", "kind": "suave", "blocks": []}]}',
        '{"sessions": [{"date": "2026-10-05", "kind": "fartlek", "blocks": []}]}',
        '{"sessions": [{"date": "2026-10-05", "kind": "calidad", "target_pace": "rápido", "blocks": []}]}',
        '{"sessions": [{"date": "2026-10-05", "kind": "suave", "blocks": [{"kind": "trote"}]}]}',
    ],
)
def test_una_respuesta_con_formato_invalido_se_rechaza(texto):
    with pytest.raises(GenerationError):
        parse_plan(texto)


# --- Llamada ---


def test_generate_plan_devuelve_las_sesiones_y_registra_la_llamada(gemini, capsys):
    prompts = gemini(RESPUESTA)
    sesiones = generate_plan(INTERMEDIO, user_id=7)

    assert [s.kind for s in sesiones] == ["suave", "calidad"]
    assert prompts == [build_prompt(INTERMEDIO)]
    (registro,) = registros(capsys)
    assert registro["event"] == "llm_call" and registro["user_id"] == 7
    assert registro["modelo"] == "gemini-3.5-flash-lite"
    assert registro["prompt"]["sesiones_pedidas"] == 4
    assert registro["respuesta"] == {"sesiones": 2, "km_de_trote": 8.0, "sesiones_de_calidad": 1}


# AC-17: 5xx y rate limit.
@pytest.mark.parametrize(
    "error, codigo",
    [
        (errors.ServerError(503, {"error": {"message": "caído"}}), 503),
        (errors.ClientError(429, {"error": {"message": "rate limit"}}), 429),
    ],
)
def test_un_error_de_la_api_de_gemini_falla_y_se_registra(gemini, capsys, error, codigo):
    gemini(error)
    with pytest.raises(GenerationError):
        generate_plan(INTERMEDIO, user_id=7)
    (registro,) = registros(capsys)
    assert registro["event"] == "external_error" and registro["level"] == "ERROR"
    assert (registro["servicio"], registro["motivo"], registro["codigo"]) == ("gemini", "error_de_api", codigo)


def test_un_error_de_conexion_falla_y_se_registra(gemini, capsys):
    gemini(ConnectionError("sin red"))
    with pytest.raises(GenerationError):
        generate_plan(INTERMEDIO, user_id=7)
    assert registros(capsys)[0]["motivo"] == "error_de_conexion"


# AC-17: falta de respuesta en el tiempo límite.
def test_si_gemini_no_responde_a_tiempo_falla_por_timeout(monkeypatch, capsys):
    monkeypatch.setattr(planner, "GEMINI_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(planner, "_call_gemini", lambda prompt: time.sleep(0.5) or RESPUESTA)
    inicio = time.monotonic()
    with pytest.raises(GenerationError):
        generate_plan(INTERMEDIO, user_id=7)
    assert time.monotonic() - inicio < 0.4
    assert registros(capsys)[0]["motivo"] == "timeout"


def test_el_tiempo_limite_es_de_20_segundos():
    assert planner.GEMINI_TIMEOUT_SECONDS == 20


def test_una_respuesta_invalida_falla_y_se_registra(gemini, capsys):
    gemini("no es json")
    with pytest.raises(GenerationError):
        generate_plan(INTERMEDIO, user_id=7)
    assert registros(capsys)[0]["motivo"] == "respuesta_invalida"


# RNF-04 y salida estructurada: la llamada real al SDK, con el cliente reemplazado.
def test_la_llamada_al_sdk_pide_json_estructurado_con_timeout_y_la_key_del_entorno(monkeypatch):
    llamadas = {}

    class ClienteFalso:
        def __init__(self, **kwargs):
            llamadas["cliente"] = kwargs
            self.models = SimpleNamespace(generate_content=self.generate_content)

        def generate_content(self, **kwargs):
            llamadas["pedido"] = kwargs
            return SimpleNamespace(text=RESPUESTA)

    monkeypatch.setattr(planner.genai, "Client", ClienteFalso)
    assert planner._call_gemini("un prompt") == RESPUESTA

    assert llamadas["cliente"]["api_key"] == "test-google"
    assert llamadas["cliente"]["http_options"].timeout == 20_000
    assert llamadas["pedido"]["model"] == "gemini-3.5-flash-lite"
    assert llamadas["pedido"]["contents"] == "un prompt"
    config = llamadas["pedido"]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is planner.PlanOut
