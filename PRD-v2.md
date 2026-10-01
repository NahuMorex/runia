# PRD-001: Runnia — Software de entrenamiento para running
## Contexto y Problema
Internet se encuentra plagado -más precisamente en redes sociales- de todo tipo de rutinas y planes para realizar un mismo objetivo de entrenamiento, y es normal encontrarte con opiniones diferentes que se contradigan, dificultando saber qué hacer, cuándo y cómo. Se necesita un sistema que, en base a las capacidades físicas del usuario, indique los pasos a seguir con respaldo técnico, permitiendo entrenar con seguridad y sin temor a lesiones.
Personas:
- Milena (Corredora novata): tiene pensado correr por primera vez 10 kilómetros, pero nunca corrió. Quiere saber cuáles son los pasos a seguir y cómo prepararse sin lesionarse.
- Mich (Corredor avanzado): no sabe qué tipo de ejercicio hacer para mejorar su velocidad y ritmo. Quiere saber qué rutina seguir según su nivel actual.
- Pepe (Corredor recurrente): le molesta salir a correr y que empiece a llover o haga un calor extremo. Le gustaría saber el pronóstico antes y adaptar el tipo de sesión o el horario. 

## Objetivos
Luego de que un usuario suba sus métricas de salud, historial y objetivos, se valoren los datos para realizar un plan de entrenamiento semanal y adaptativo. Advertir de condiciones climáticas y reprogramar o sugerir otros horarios.

## Requerimientos Funcionales
- RF-01: El sistema debe registrar nuevos usuarios mediante email y contraseña.
- RF-02: El sistema debe autenticar usuarios mediante correo electrónico y contraseña emitiendo un token de sesión.
- RF-03a: El sistema debe permitir cargar el nivel del corredor.
- RF-03b: El sistema debe permitir cargar la frecuencia de entrenamiento deseada.
- RF-03c: El sistema debe permitir cargar la distancia objetivo del corredor.
- RF-03d: El sistema debe permitir cargar el ritmo actual del corredor.
- RF-04: El sistema debe validar que los datos obligatorios del perfil existan antes de ejecutar cualquier cálculo de entrenamiento, considerando que el ritmo actual no es obligatorio para nivel novato.
- RF-05: El sistema debe consultar el pronóstico meteorológico a la API de clima.
- RF-06: El sistema debe generar una estructura de entrenamiento progresiva según el nivel del usuario.
- RF-07: Si las condiciones climáticas son desfavorables (probabilidad de lluvia mayor a 60% o la temperatura supera los 32°C, o ambas), el sistema debe modificar la sesión diaria.
- RF-08: El sistema debe emitir un mensaje diario conciso con el entrenamiento del día a través de un bot de Telegram.
- RF-09: El sistema debe permitir al usuario registrar el resultado de la sesión planificada para el día, seleccionando un estado (Completada, Parcial/Cortada, Salteada) y un indicador subjetivo de esfuerzo/sensación (RPE o escala de fatiga 1-5, más notas opcionales).
- RF-10: El sistema debe permitir al usuario visualizar su cronograma de entrenamiento de la semana en curso (sesiones pasadas, día actual y proyectadas).
- RF-11: El sistema debe permitir al usuario acceder a un historial básico de sesiones anteriores con su respectivo estado y feedback.
- RF-12: El sistema debe reclasificar a intensidad "suave/recuperación" las sesiones de los dos días subsiguientes cuando el usuario registre una sesión como Salteada con esfuerzo RPE ≥ 4.
- RF-13: El sistema debe asignar automáticamente el estado "Pospuesta por clima" a una sesión cuando la condición climática sea extrema (temperatura >38°C, tormenta eléctrica, o probabilidad de lluvia >80%) tanto en el día original como en su alternativa de reprogramación, sin ofrecerla como ejecutable, sin recuperar su volumen en otro día del plan y sin disparar la reclasificación de RF-12 (este estado es asignado por el sistema, no seleccionado por el usuario, y es distinto de los estados de RF-09).

## Requerimientos No Funcionales
- RNF-01: La ejecución del mensaje hacia el usuario no debe superar los 10 segundos en el percentil 95 (p95).
- RNF-02: El sistema debe registrar en formato estructurado (JSON) el 100% de las ejecuciones intermedias, herramientas invocadas y decisiones tomadas por los agentes.
- RNF-03: Las contraseñas deben almacenarse con Argon2id, con parámetros mínimos m=19456 KiB, t=2, p=1 (recomendación OWASP).
- RNF-04: La API key del modelo no debe estar en el código; se lee de la variable de entorno GOOGLE_API_KEY.

## Criterios de Aceptación
- AC-01 (RF-01): Dado un correo no registrado y una contraseña válida, cuando el usuario solicita registrarse, entonces el sistema crea el registro en base de datos y devuelve código HTTP 201.
- AC-02 (RF-02): Dado un usuario registrado que envía credenciales correctas al endpoint de login, cuando el sistema las valida, entonces responde con un token de sesión JWT y código HTTP 200.
- AC-03 (RF-02): Dado un usuario que envía un correo o contraseña incorrectos al endpoint de login, cuando el sistema intenta autenticarlo, entonces responde con código HTTP 401 Unauthorized sin emitir token.
- AC-04a (RF-03a, RF-03b, RF-03c, RF-03d, RF-04): Dado un usuario con nivel no-novato sin ritmo actual cargado, cuando solicita la generación de un plan, entonces el sistema rechaza la solicitud e indica que falta el dato, sin generar datos ficticios.
- AC-04b (RF-03a, RF-03b, RF-03c, RF-03d, RF-04): Dado un usuario con nivel novato sin ritmo actual cargado pero con nivel, frecuencia y distancia objetivo completos, cuando solicita la generación de un plan, entonces el sistema genera el plan sin exigir ritmo actual.
- AC-04c (RF-03a, RF-03b, RF-03c, RF-03d): Dado un usuario autenticado, cuando envía nivel, frecuencia de entrenamiento y distancia objetivo válidos (y ritmo actual si su nivel no es novato), entonces el sistema persiste los datos en el perfil y responde con código HTTP 200/201.
- AC-05 (RF-06): Dado un perfil de corredora novata, cuando el sistema genera el plan de entrenamiento, entonces el plan explicita, para cada sesión de la primera semana, bloques alternados de caminata y trote con la duración en minutos de cada bloque.
- AC-06 (RF-06): Dado un perfil de corredor avanzado, cuando el sistema genera el plan de entrenamiento, entonces el plan explicita, para cada sesión de ritmo, el tiempo objetivo por tramo y el tiempo de recuperación entre tramos.
- AC-07a (RF-05, RF-07): Dado un reporte meteorológico con probabilidad de lluvia >60% o temperatura >32°C, cuando el sistema evalúa la sesión, entonces el mensaje incluye una alerta con el motivo climático específico.
- AC-07b (RF-05, RF-07): Dado un reporte meteorológico con probabilidad de lluvia >60% o temperatura >32°C, cuando el sistema evalúa la sesión, entonces el mensaje incluye una alternativa de horario o día.
- AC-07c (RF-05, RF-07): Dado un reporte meteorológico con probabilidad de lluvia >60% o temperatura >32°C, cuando el sistema evalúa la sesión, entonces la sesión original permanece disponible y ejecutable si el usuario decide continuar.
- AC-08a (RF-08): Dado un plan diario generado, cuando el sistema emite el mensaje final, entonces el texto tiene una extensión menor o igual a 150 palabras.
- AC-08b (RF-08): Dado un plan diario generado, cuando el sistema emite el mensaje final, entonces el texto contiene tipo de sesión, ritmo objetivo y rango horario sugerido.
- AC-09a (RF-05, RF-07): Dado un error de la API de clima (timeout, rate limit o 5xx), cuando el sistema intenta consultar el pronóstico para generar la sesión diaria, entonces registra el fallo en logs en formato estructurado.
- AC-09b (RF-05, RF-07): Dado un error de la API de clima (timeout, rate limit o 5xx), cuando el sistema intenta consultar el pronóstico para generar la sesión diaria, entonces entrega el plan diario sin ajuste climático y adjunta un disclaimer en el mensaje (ej. "Clima no disponible: seguí las pautas base de ritmo/hidratación").
- AC-10 (RF-10, RF-11): Dado un usuario autenticado con ID X, cuando intenta acceder vía interfaz o API a un perfil, plan, historial o endpoint con ID Y (Y≠X), entonces el sistema deniega el acceso retornando código 403 Forbidden o 404 Not Found, impidiendo cualquier fuga de información.
- AC-11 (RF-09, RF-12): Dado un usuario que registra una sesión como Salteada con fatiga alta (RPE ≥4), cuando el sistema genera las sesiones de los dos días subsiguientes, entonces las reclasifica a intensidad "suave/recuperación", eliminando cualquier sesión de ritmo o velocidad planificada en ese rango.
- AC-12 (RF-09): Dado un usuario autenticado con una sesión planificada para el día, cuando registra el resultado seleccionando un estado (Completada, Parcial/Cortada, Salteada) y un indicador de esfuerzo (RPE 1-5), entonces el sistema guarda el registro asociado a esa sesión y lo refleja en el historial del usuario.
- AC-13 (RF-10): Dado un usuario autenticado con un plan semanal activo, cuando solicita ver su cronograma, entonces el sistema muestra las sesiones pasadas, el día actual y las sesiones proyectadas de la semana en curso.
- AC-14 (RF-11): Dado un usuario autenticado con al menos una sesión de entrenamiento pasada registrada, cuando solicita ver su historial, entonces el sistema muestra la lista de sesiones anteriores con su estado (Completada, Parcial/Cortada, Salteada) y el indicador de esfuerzo/notas asociado a cada una.
- AC-15 (RF-06): Dado un plan generado por el agente que incrementa el volumen semanal total en más de 10% respecto a la semana anterior, cuando el sistema aplica el guardarraíl de carga, entonces recorta el plan al tope de 10% antes de entregarlo al usuario.
- AC-16 (RF-06): Dado un perfil novato con volumen semanal base menor a 10 km, cuando el sistema calcula la progresión semanal, entonces aplica un piso mínimo de +1 km en la sesión más larga además del tope del 10%.
- AC-17 (RF-06): Dado un plan activo que alcanza su cuarta semana consecutiva, cuando el sistema genera la sesión de esa semana, entonces reduce el volumen total entre 20% y 30% respecto a la semana anterior (semana de descarga), sin excepción.
- AC-18 (RF-06): Dado un perfil novato que no completó al menos 3 semanas consecutivas de plan aeróbico sin ninguna sesión Salteada con RPE≥4, cuando el sistema genera el plan semanal, entonces no incluye ninguna sesión de ritmo o velocidad.
- AC-19 (RF-06): Dado un perfil intermedio o avanzado, cuando el sistema genera el plan semanal, entonces incluye como máximo una sesión de intensidad/velocidad por semana.
- AC-20 (RF-06): Dado un ritmo actual cargado por el usuario, cuando el sistema genera una sesión de calidad/ritmo, entonces el ritmo objetivo sugerido no es más de 10% más rápido que el ritmo actual cargado.
- AC-21 (RF-13): Dado un día de sesión con condición climática extrema (temperatura >38°C, tormenta eléctrica o lluvia >80%) cuya alternativa de reprogramación también cae en condición extrema, cuando el sistema evalúa la sesión, entonces la marca con estado "Pospuesta por clima" y no la ofrece como ejecutable.
- AC-22 (RF-13, RF-12): Dado una sesión con estado "Pospuesta por clima", cuando el sistema evalúa si dispara la reclasificación de RF-12, entonces no reclasifica las sesiones subsiguientes a intensidad suave/recuperación, dado que ese estado no equivale a Salteada.
- AC-23 (RF-13, RF-06): Dado una sesión con estado "Pospuesta por clima", cuando el sistema genera el plan de la semana siguiente, entonces no inserta ni recupera el volumen de esa sesión en otro día, y la progresión continúa desde el último volumen efectivamente ejecutado.
- AC-24 (RF-13): Dado que se acumulan 2 o más sesiones con estado "Pospuesta por clima" en la misma semana, cuando el sistema emite el mensaje diario, entonces informa explícitamente al usuario la cantidad de sesiones pospuestas de esa semana.

## Fuera de Alcance
Integración nativa directa con hardware vía Bluetooth. · Diagnóstico médico, tratamiento de lesiones o prescripción kinesiológica. · Desarrollo de aplicaciones móviles. · Elaboración de planes de nutrición o dietas personalizadas. · Pasarelas de pago o cobro recurrente de suscripciones.

## Riesgos y Dependencias
- Dependencia: API meteorológica · OpenWeatherMap.
- Dependencia: API de Gemini (`gemini-3.5-flash-lite`) · base de conocimiento kb.md · SQLite.
- Riesgo: Que el LLM sugiera cargas de entrenamiento excesivas para novatos -> mitigación: agregar guardarraíles duros previos a la entrega del plan.
- Riesgo: Consumo excesivo de tokens al pasar historiales largos de entrenamientos -> mitigación: almacenando solo métricas agregadas y resúmenes semanales en el estado compartido del agente.