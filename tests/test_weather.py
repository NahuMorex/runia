import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import weather
from app.weather import Reading, WeatherError, WindowForecast, get_forecast, window_forecast

BA = ZoneInfo("America/Argentina/Buenos_Aires")
LUNES = date(2026, 10, 5)
MARTES = LUNES + timedelta(days=1)


def lectura(dia, hora, lluvia=0, temp=18.0):
    return Reading(datetime(dia.year, dia.month, dia.day, hora, tzinfo=BA), lluvia, temp)


def item(dia, hora, pop=0.0, temp=18.0):
    """Una lectura como la devuelve OpenWeatherMap: hora en UTC y lluvia de 0 a 1."""
    dt = datetime(dia.year, dia.month, dia.day, hora, tzinfo=BA).timestamp()
    return {"dt": int(dt), "main": {"temp": temp}, "pop": pop}


def registros(capsys):
    return [json.loads(linea) for linea in capsys.readouterr().out.strip().splitlines()]


@pytest.fixture
def openweather(monkeypatch):
    """Reemplaza la consulta a OpenWeatherMap y guarda las ciudades pedidas; nunca se sale a la red."""
    ciudades = []

    def _openweather(respuesta):
        def falso(city):
            ciudades.append(city)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        monkeypatch.setattr(weather, "_fetch", falso)
        return ciudades

    return _openweather


def respuesta_ok(*items):
    return httpx.Response(200, json={"cod": "200", "list": list(items)})


# --- Máximos dentro del rango horario ---


def test_toma_el_maximo_de_lluvia_y_de_temperatura_dentro_del_rango():
    lecturas = [
        lectura(LUNES, 9, lluvia=20, temp=24.0),
        lectura(LUNES, 12, lluvia=70, temp=29.5),
        lectura(LUNES, 15, lluvia=10, temp=33.0),
    ]
    assert window_forecast(lecturas, LUNES, "09:00", "15:00") == WindowForecast(70, 33.0)


def test_ignora_las_lecturas_lejos_del_rango_y_las_de_otros_dias():
    lecturas = [
        lectura(LUNES, 3, lluvia=90, temp=35.0),
        lectura(LUNES, 6, lluvia=10, temp=15.0),
        lectura(LUNES, 9, lluvia=20, temp=19.0),
        lectura(LUNES, 12, lluvia=95, temp=36.0),
        lectura(MARTES, 6, lluvia=100, temp=40.0),
    ]
    assert window_forecast(lecturas, LUNES, "06:00", "09:00") == WindowForecast(20, 19.0)


def test_un_rango_sin_lecturas_adentro_usa_las_que_lo_rodean():
    lecturas = [
        lectura(LUNES, 3, lluvia=99, temp=39.0),
        lectura(LUNES, 6, lluvia=65, temp=12.0),
        lectura(LUNES, 9, lluvia=30, temp=17.0),
        lectura(LUNES, 12, lluvia=99, temp=39.0),
    ]
    assert window_forecast(lecturas, LUNES, "07:00", "08:00") == WindowForecast(65, 17.0)


def test_si_la_lectura_anterior_ya_paso_alcanza_con_la_siguiente():
    # A las 06:30 el pronóstico ya no trae la lectura de las 06:00.
    lecturas = [lectura(LUNES, 9, lluvia=40, temp=17.0), lectura(LUNES, 12, lluvia=80, temp=22.0)]
    assert window_forecast(lecturas, LUNES, "07:00", "09:00") == WindowForecast(40, 17.0)


def test_sin_lecturas_para_ese_dia_no_hay_pronostico():
    assert window_forecast([lectura(LUNES, 9)], MARTES, "07:00", "09:00") is None


# --- Consulta ---


def test_get_forecast_devuelve_las_lecturas_en_hora_de_buenos_aires_y_registra_la_consulta(
    openweather, capsys
):
    ciudades = openweather(respuesta_ok(item(LUNES, 9, pop=0.61, temp=21.4), item(LUNES, 12)))

    lecturas = get_forecast("Rosario", user_id=7)

    assert lecturas == [lectura(LUNES, 9, lluvia=61, temp=21.4), lectura(LUNES, 12)]
    assert lecturas[0].at.utcoffset() == timedelta(hours=-3)
    assert ciudades == ["Rosario"]
    (registro,) = registros(capsys)
    assert registro["event"] == "weather_call"
    assert (registro["ciudad"], registro["user_id"], registro["lecturas"]) == ("Rosario", 7, 2)


def test_la_consulta_va_con_ciudad_pais_unidades_metricas_y_tiempo_limite(monkeypatch):
    pedido = {}

    def falso(url, **kwargs):
        pedido.update(url=url, **kwargs)
        return respuesta_ok(item(LUNES, 9))

    monkeypatch.setattr(httpx, "get", falso)
    get_forecast("Rosario", user_id=7)

    assert pedido["url"] == "https://api.openweathermap.org/data/2.5/forecast"
    assert pedido["params"] == {"q": "Rosario,AR", "units": "metric", "appid": "test-openweather"}
    assert pedido["timeout"] == weather.WEATHER_TIMEOUT_SECONDS == 5


# AC-12: timeout, rate limit o 5xx quedan registrados en logs.
@pytest.mark.parametrize(
    "respuesta, motivo, codigo",
    [
        (httpx.ReadTimeout("sin respuesta"), "timeout", None),
        (httpx.ConnectTimeout("sin respuesta"), "timeout", None),
        (httpx.Response(429, json={"cod": 429}), "rate_limit", 429),
        (httpx.Response(500, text="error"), "error_del_servidor", 500),
        (httpx.Response(503, text="error"), "error_del_servidor", 503),
        (httpx.Response(404, json={"cod": "404"}), "error_http", 404),
        (httpx.Response(401, json={"cod": 401}), "error_http", 401),
        (httpx.ConnectError("sin red"), "error_de_conexion", None),
    ],
)
def test_un_error_de_openweather_falla_y_se_registra(openweather, capsys, respuesta, motivo, codigo):
    openweather(respuesta)
    with pytest.raises(WeatherError):
        get_forecast("Rosario", user_id=7)
    (registro,) = registros(capsys)
    assert registro["event"] == "external_error" and registro["level"] == "ERROR"
    assert (registro["servicio"], registro["motivo"]) == ("openweather", motivo)
    assert (registro["codigo"], registro["user_id"]) == (codigo, 7)


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(200, text="no es json"),
        httpx.Response(200, json={"cod": "200"}),
        httpx.Response(200, json={"cod": "200", "list": []}),
        httpx.Response(200, json={"list": [{"dt": 1, "main": {"temp": 20}}]}),  # sin lluvia
        httpx.Response(200, json={"list": [{"dt": 1, "main": {}, "pop": 0.1}]}),  # sin temperatura
    ],
)
def test_una_respuesta_invalida_falla_y_se_registra(openweather, capsys, respuesta):
    openweather(respuesta)
    with pytest.raises(WeatherError):
        get_forecast("Rosario", user_id=7)
    assert registros(capsys)[0]["motivo"] == "respuesta_invalida"


def test_sin_api_key_falla_como_error_del_servicio_y_no_la_registra(monkeypatch, capsys):
    monkeypatch.delenv("OPENWEATHER_API_KEY")
    with pytest.raises(WeatherError):
        get_forecast("Rosario", user_id=7)
    (registro,) = registros(capsys)
    assert (registro["motivo"], registro["tipo"]) == ("error_de_conexion", "RuntimeError")


def test_el_log_de_error_no_incluye_la_api_key(openweather, capsys):
    openweather(httpx.ConnectError("fallo en https://api.openweathermap.org/?appid=test-openweather"))
    with pytest.raises(WeatherError):
        get_forecast("Rosario", user_id=7)
    assert "test-openweather" not in capsys.readouterr().out
