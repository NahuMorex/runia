import json
import time
from datetime import date, timedelta

import pytest
from google.genai import errors

from app import writer
from app.climate import Alert
from app.daily import build_daily, word_count
from app.guardrails import Block, Session
from app.weather import WindowForecast
from app.writer import build_prompt, rejection_reason, write_message

LUNES = date(2026, 10, 5)
JUEVES = LUNES + timedelta(days=3)
RANGO = ("07:00", "09:00")
CALIDAD = Session(
    LUNES,
    "calidad",
    (Block("trote", 2), Block("tramo", 0.4, recovery_seconds=90), Block("tramo", 0.4, recovery_seconds=90)),
    pace_seconds=345,
)
NOVATA = Session(LUNES, "suave", (Block("caminata", 0.4), Block("trote", 0.4)) * 3)
LLUVIA = Alert(("lluvia",), WindowForecast(80, 20.0), alternative=JUEVES)

# Sesión de calidad, fatiga 5, con alerta de lluvia y día alternativo.
DIARIO = build_daily(CALIDAD, *RANGO, fatigue=5, alert=LLUVIA)
REDACCION = (
    "¡Buen día! Hoy toca calidad. Salí entre las 07:00 y las 09:00, con ritmo objetivo de 5:45 min/km. "
    "La secuencia es: 2 km trote, 2 x 0,4 km a ritmo objetivo (en 2:18, recuperación 90 s). "
    "Ojo con el clima: hay probabilidad de lluvia de 80% en tu horario. "
    "El jueves 8/10 no tiene condición desfavorable."
)


def registros(capsys):
    return [json.loads(linea) for linea in capsys.readouterr().out.strip().splitlines()]


@pytest.fixture
def gemini(monkeypatch):
    """Reemplaza la llamada a Gemini y guarda los prompts recibidos; nunca se sale a la red."""
    prompts = []

    def _gemini(respuesta):
        def falso(prompt):
            prompts.append(prompt)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        monkeypatch.setattr(writer, "_call_gemini", falso)
        return prompts

    return _gemini


# --- Prompt ---


def test_el_prompt_lleva_los_datos_de_la_plantilla_y_las_frases_a_copiar():
    prompt = build_prompt(DIARIO)
    assert prompt.startswith("Datos de la sesión de hoy:\n" + DIARIO.text + "\n\nCopiá textual:\n")
    assert prompt.endswith(
        "- calidad\n- 07:00\n- 09:00\n- 5:45\n"
        "- 2 km trote, 2 x 0,4 km a ritmo objetivo (en 2:18, recuperación 90 s)\n"
        "- probabilidad de lluvia de 80%\n- jueves 8/10"
    )


def test_el_prompt_pide_copiar_suave_recuperacion_con_fatiga_alta():
    assert "- suave/recuperación\n" in build_prompt(build_daily(CALIDAD, *RANGO, fatigue=9))


def test_las_instrucciones_prohiben_inventar_datos_preguntar_y_dar_consejo_medico():
    instrucciones = writer.SYSTEM_INSTRUCTION
    assert "Usá solo los datos que recibís" in instrucciones
    assert "No hagas preguntas ni ofrezcas reprogramar" in instrucciones
    assert "no lleva signos de pregunta" in instrucciones
    assert 'Empezá con "Hola." y nada más como saludo' in instrucciones
    assert "No agregues indicaciones ni consejos que no estén en los datos" in instrucciones
    assert "No des consejo médico ni de lesiones" in instrucciones
    assert "Clima no disponible: seguí las pautas base de ritmo e hidratación." in instrucciones
    assert "No hay una alternativa disponible esta semana" in instrucciones


# --- Validación de la redacción ---


def test_una_redaccion_que_conserva_los_datos_es_valida():
    assert rejection_reason(REDACCION, DIARIO) is None


def test_la_validacion_no_distingue_mayusculas():
    assert rejection_reason(REDACCION.replace("toca calidad", "toca CALIDAD"), DIARIO) is None


@pytest.mark.parametrize("texto", [None, "", "   \n"])
def test_una_redaccion_vacia_se_rechaza(texto):
    assert rejection_reason(texto, DIARIO) == "vacio"


# AC-27
def test_una_redaccion_de_mas_de_150_palabras_se_rechaza():
    larga = REDACCION + " dale" * 150
    assert word_count(larga) > 150
    assert rejection_reason(larga, DIARIO) == "supera_150_palabras"


# AC-28, AC-23, AC-24
@pytest.mark.parametrize(
    "dato",
    [
        "calidad",  # tipo de sesión
        "07:00",  # rango horario sugerido
        "09:00",
        "5:45",  # ritmo objetivo
        "2 km trote, 2 x 0,4 km a ritmo objetivo (en 2:18, recuperación 90 s)",  # secuencia
        "probabilidad de lluvia de 80%",  # motivo de la alerta
        "jueves 8/10",  # día alternativo
    ],
)
def test_una_redaccion_a_la_que_le_falta_un_dato_se_rechaza(dato):
    assert dato in REDACCION
    assert rejection_reason(REDACCION.replace(dato, "eso"), DIARIO) == "falta_un_dato"


@pytest.mark.parametrize("agregado", ["Tomá 3 vasos de agua.", "Sumá 1,5 km al final.", "Probá a las 18:30."])
def test_una_redaccion_con_un_numero_que_no_esta_en_los_datos_se_rechaza(agregado):
    assert rejection_reason(f"{REDACCION} {agregado}", DIARIO) == "numero_inventado"


def test_una_redaccion_que_hace_una_pregunta_se_rechaza():
    assert rejection_reason(REDACCION + " ¿La hacés hoy o la pasás al jueves?", DIARIO) == "pide_confirmacion"


def test_una_redaccion_con_markdown_se_rechaza():
    assert rejection_reason(REDACCION.replace("calidad", "**calidad**"), DIARIO) == "formato"


# AC-13
def test_sin_clima_la_redaccion_tiene_que_llevar_el_texto_literal():
    diario = build_daily(NOVATA, *RANGO, fatigue=2, weather_available=False)
    base = "Hoy toca suave, de 07:00 a 09:00: 3 x (0,4 km caminata + 0,4 km trote). "
    literal = "Clima no disponible: seguí las pautas base de ritmo e hidratación."
    assert rejection_reason(base + literal, diario) is None
    assert rejection_reason(base + "No pudimos ver el clima, hidratate.", diario) == "falta_un_dato"


# AC-24, AC-64
def test_sin_alternativa_la_redaccion_tiene_que_llevar_el_texto_literal():
    alerta = Alert(("calor",), WindowForecast(0, 36.0), alternative=None)
    diario = build_daily(NOVATA, *RANGO, alert=alerta)
    base = (
        "Hoy toca suave, de 07:00 a 09:00: 3 x (0,4 km caminata + 0,4 km trote). "
        "Atención: temperatura de hasta 36 °C en tu horario. "
    )
    assert rejection_reason(base + "No hay una alternativa disponible esta semana.", diario) is None
    assert rejection_reason(base + "Esta semana no hay otro día.", diario) == "falta_un_dato"


# RF-16
def test_con_fatiga_alta_la_redaccion_tiene_que_decir_suave_recuperacion():
    diario = build_daily(CALIDAD, *RANGO, fatigue=9)
    base = "de 07:00 a 09:00, con esta secuencia: 2,8 km trote."
    assert rejection_reason(f"Hoy va suave/recuperación, {base}", diario) is None
    assert rejection_reason(f"Hoy va tranqui, {base}", diario) == "falta_un_dato"


# --- Redacción con Gemini ---


def test_write_message_devuelve_la_redaccion_de_gemini_y_registra_la_llamada(gemini, capsys):
    prompts = gemini(f"  {REDACCION}\n")

    assert write_message(DIARIO, user_id=7) == REDACCION
    assert prompts == [build_prompt(DIARIO)]
    (registro,) = registros(capsys)
    assert registro["event"] == "llm_call" and registro["user_id"] == 7
    assert (registro["modelo"], registro["uso"]) == ("gemini-3.5-flash-lite", "mensaje_diario")
    assert registro["respuesta"] == {
        "palabras": word_count(REDACCION),
        "resultado": "usada",
        "motivo": None,
    }
    # RNF-02: el log lleva un resumen, no los textos.
    assert "prompt" in registro and "trote" not in json.dumps(registro)


def test_una_redaccion_invalida_se_descarta_y_se_envia_la_plantilla(gemini, capsys):
    gemini(REDACCION + " Tomá 3 vasos de agua.")

    assert write_message(DIARIO, user_id=7) == DIARIO.text
    (registro,) = registros(capsys)
    assert registro["event"] == "llm_call"
    assert registro["respuesta"]["resultado"] == "descartada"
    assert registro["respuesta"]["motivo"] == "numero_inventado"


@pytest.mark.parametrize(
    "error, motivo, codigo",
    [
        (errors.ServerError(503, {"error": {"message": "caído"}}), "error_de_api", 503),
        (errors.ClientError(429, {"error": {"message": "rate limit"}}), "error_de_api", 429),
        (ConnectionError("sin red"), "error_de_conexion", None),
    ],
)
def test_si_gemini_falla_se_envia_la_plantilla_y_se_registra_el_error(gemini, capsys, error, motivo, codigo):
    gemini(error)

    assert write_message(DIARIO, user_id=7) == DIARIO.text
    (registro,) = registros(capsys)
    assert registro["event"] == "external_error" and registro["level"] == "ERROR"
    assert (registro["servicio"], registro["motivo"], registro["codigo"]) == ("gemini", motivo, codigo)


def test_si_gemini_no_responde_a_tiempo_se_envia_la_plantilla(monkeypatch, capsys):
    monkeypatch.setattr(writer, "MESSAGE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(writer, "_call_gemini", lambda prompt: time.sleep(0.5) or REDACCION)
    inicio = time.monotonic()

    assert write_message(DIARIO, user_id=7) == DIARIO.text
    assert time.monotonic() - inicio < 0.4
    assert registros(capsys)[0]["motivo"] == "timeout"


def test_el_tiempo_limite_de_la_redaccion_es_de_5_segundos():
    assert writer.MESSAGE_TIMEOUT_SECONDS == 5


def test_el_plazo_que_se_le_pasa_a_gemini_no_baja_de_10_segundos(monkeypatch):
    # La API rechaza con un 400 los plazos de menos de 10 segundos.
    recibido = {}

    class ClienteFalso:
        def __init__(self, api_key, http_options):
            recibido["timeout_ms"] = http_options.timeout
            self.models = self

        def generate_content(self, **kwargs):
            return type("Respuesta", (), {"text": "hola"})()

    monkeypatch.setattr(writer.genai, "Client", ClienteFalso)
    assert writer._call_gemini("prompt") == "hola"
    assert recibido["timeout_ms"] == 10_000
