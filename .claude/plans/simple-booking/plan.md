# Plan: simple-booking

**Status**: approved

## Goal

Sustituir el bot de pedidos de pizza/sushi del scaffold de Pipecat por el flujo de
`PR-01 Simple Booking` de "El Turno": un agente de voz que identifica a un paciente ya
en ficha, le ofrece el primer hueco disponible en una especialidad (con sede/día/franja
opcionales), lo confirma, y garantiza un `POST /api/v1/submit/book` correcto al colgar —
en cualquier punto de la llamada, no solo en el camino feliz. Al terminar, los 4 casos
de práctica de `simple_booking.md` producen exactamente el JSON aceptado, verificado sin
necesidad de una llamada de voz real.

## Findings

- `server/bot.py`, `server/handlers.py`, `server/menu.py` son el scaffold de ejemplo de
  Pipecat sin modificar (bot de pizza/sushi con Pipecat Flows) — confirmado leyendo los
  tres ficheros completos.
- `server/.env` no tenía ninguna variable para la API de la clínica al empezar esta
  sesión (solo Daily/Cartesia/Deepgram/OpenAI, vacías). Ya se han añadido
  `CLINIC_API_BASE_URL` y `CLINIC_API_KEY` con el valor real entregado por el usuario.
  `.env` está gitignoreado (`.gitignore:151`) y no trackeado — confirmado con
  `git ls-files`.
- `clinic.json` (raíz del repo) tiene exactamente las claves de nivel superior que
  describe `CL-clinic-bundle` (`providers`, `specialties`, `appointment_types`,
  `locations`, `plans`, más `calendar`/`restrictions`) — es el bundle estático, idéntico
  para todo el evento.
- El paquete Pipecat instalado (`server/.venv/.../pipecat/services/google/llm.py:158`)
  confirma `GoogleLLMService(LLMService[GeminiLLMAdapter])` — misma familia base que
  `OpenAIResponsesLLMService`, así que el intercambio de proveedor de LLM no necesita
  ninguna librería nueva.
- No existe `server/evals/` ni el transport `eval` en `bot.py` — el arnés de
  verificación de `AGENTS.md` §6 no está montado.
- `docs/agent/flow.yaml` (grafo completo de los 18 problemas) y
  `docs/requirements/scenarios/simple_booking_step_by_step.md` (5 nodos, 3 tools, solo
  problema 1) son **dos diseños distintos**, no dos capas del mismo diseño — tools con
  nombres y vocabularios de estado incompatibles entre sí. Ver Decisiones.

## Decisions

1. **Diseño de referencia: `simple_booking_step_by_step.md`, no `flow.yaml`.**
   Alternativas: (a) el grafo completo de `flow.yaml` con handlers-stub para los ~15
   nodos fuera de alcance, (b) el diseño corto de 5 nodos/3 tools. Elegido (b).
   Motivo: es el único contrastado contra los 4 casos reales de aceptación; evita
   construir nodos sin ningún caso de test que los ejercite; Pipecat Flows valida el
   grafo entero al construirlo, así que menos nodos = menos superficie de fallo justo
   cuando el objetivo es una llamada real de punta a punta. `flow.yaml` queda como
   referencia de hacia dónde escalar cuando toque cada problema — no se toca en este
   plan.
2. **Swap de LLM sin LangChain.** Se mantiene `LLMContext`/`LLMContextAggregatorPair`
   (universal aggregators, agnósticos de proveedor) y se añade una env var
   `LLM_PROVIDER=openai|gemini` que decide qué `*LLMService` instanciar en `bot.py`.
   Motivo: Pipecat ya resuelve el intercambio de proveedor con su propia abstracción de
   servicio; meter LangChain sería una segunda abstracción compitiendo por lo mismo.
3. **Deepgram y Cartesia se mantienen tal cual del scaffold** por ahora (Cartesia se
   cambiará más adelante, fuera de este plan).
4. **`clinic.json` como caché del catálogo estático**, cargado una vez al arrancar en
   vez de pedir `GET /api/v1/clinic` cada sesión — confirmado con el usuario que el
   contenido coincide y que este uso es correcto.
5. **Fallos de nuestra propia llamada a la API de la clínica**: reintento corto (2
   intentos, ~2s en total); si sigue sin responder, se deja `pending_submission` tal
   como estaba antes del fallo y se cuelga. Alternativa descartada: `ESCALATE` con un
   motivo que no describiría lo que pasó realmente — el vocabulario cerrado de `reason`
   no tiene nada para "nuestro proveedor se cayó", así que inventar uno sería peor que
   dejar el último estado conocido con certeza.
6. **Reintentos de identificación: 3 intentos en total** antes de fijar
   `NO_ACTION(patient_not_found)`. Ajustable tras ver cómo se comporta el reconocimiento
   de voz con DNIs en las primeras llamadas de práctica.
7. **Estado por llamada: `flow_manager.state`**, sin diccionario global indexado por
   `call_id`. Motivo: cada llamada entrante ya crea una invocación nueva de
   `bot(runner_args)` con su propio `flow_manager.state`, aislado de fábrica — satisface
   `FR-concurrency`/`CR-concurrency` sin código extra.
8. **Arnés de eval en paralelo a `handlers.py`**, no después. Motivo:
   `AGENTS.md` §3 golden rule 3 ("make it verifiable before you make it fancy") — cada
   handler se comprueba contra los 4 casos reales en el momento, no al final.
9. **Credenciales de la clínica ya resueltas**: `CLINIC_API_BASE_URL` y
   `CLINIC_API_KEY` ya están en `server/.env` local. El cliente HTTP apunta a la API
   real desde el principio — no hace falta stub para `/directory`/`/availability`.
10. **(añadida en la relectura, pendiente de OK)** El envío vive en un módulo propio
    `submission.py` (`CallSubmission`), no inline en `bot.py`. Motivo: la garantía
    "toda llamada termina con un POST" es la que más puntos protege
    (`CR-silence-fails`), y solo se puede probar sin levantar el pipeline si está
    separada del runner. Alternativa descartada: meterlo en `on_client_disconnected`
    directamente — funciona, pero no se puede testear la idempotencia ni el temporizador.
11. **(añadida en la relectura, pendiente de OK)** La hora de conexión es inyectable
    (`CALL_CLOCK_OVERRIDE`, solo tests). Motivo: los JSON aceptados de
    `simple_booking.md` están calculados para una llamada el 18-09-2026; sin reloj
    fijable, el test de composición exacta solo pasaría ese día.

## Context

Ficheros a leer antes de implementar:

- `server/bot.py` (completo, 183 líneas) — pipeline de audio, wiring de
  `FlowManager`, hooks `on_client_connected`/`on_client_disconnected`. El pipeline y el
  transporte Twilio (`FastAPIWebsocketParams`) ya cumplen `CR-endpoint`/`CR-twilio-shape`
  — no se tocan, solo se sustituye qué LLM se instancia y qué pasa al desconectar.
- `server/handlers.py:120-163` — patrón a reutilizar tal cual: `FlowsFunctionSchema`
  con `enum` construido en el momento desde `flow_manager.state`, más el handler que
  **vuelve a validar** esa misma lista antes de aceptar (doble cerrojo, no solo el
  schema). Este es el patrón para `get_earliest_slot` (enum de `specialty`/`site` desde
  el catálogo) y para `search_patient` (revalidación de la letra del DNI).
- `server/menu.py` (47 líneas) — patrón a reemplazar por `clinic_catalog.py`: cargar
  datos estáticos en `flow_manager.state` antes de que arranque la conversación.
- `docs/requirements/scenarios/simple_booking_step_by_step.md` — el diseño completo:
  estado de la llamada (§2), firmas exactas de las 3 tools (§3), flujo paso a paso por
  capa `[INFRA]/[DET]/[STT]/[TTS]/[LLM]` (§5), reintentos (§6), reparto de carga en
  empates (§7).
- `docs/requirements/scenarios/simple_booking.md` — los 4 casos de aceptación con el
  JSON `BOOK` exacto esperado.
- `docs/requirements/03-call-and-submission.md` — forma de `POST /submit/book`, códigos
  de estado (`200`/`404`/`409`/`410`/`422`), vocabulario cerrado de `reason`.
- `docs/requirements/04-clinic-domain.md` — `CL-type-rule` (tipo de cita por
  especialidad+`has_visited_before`), `CL-slot-range`/`CL-fiesta` (trampa del 12 de
  octubre), `CL-saturday` (solo Centro abre sábado).
- `docs/api/README.md` y `docs/api/openapi.snapshot.json` — forma exacta de los campos
  de `/directory`, `/availability`, `/submit/book` antes de escribir `clinic_client.py`.
- `clinic.json` — catálogo estático a precargar.
- `AGENTS.md` §6 — cómo añadir el transport `eval` a mano a un bot existente (la ruta
  que aplica aquí, no un re-scaffold).

Patrón de proyecto a seguir: el "cerrojo doble" de `handlers.py:57-78` (validar en el
schema Y en el handler) — aplica igual a `select_pizza_order`/`select_sushi_order` como
modelo para `search_patient`/`get_earliest_slot`.

Advertencias:

- No usar `docs/agent/flow.yaml` en este plan (Decisión 1).
- No usar LangChain (Decisión 2).
- `clinic.json` solo sirve para el catálogo estático — `/directory`, `/availability` y
  `/submit/book` son siempre llamadas en vivo, nunca desde el fichero.
- `CLINIC_API_KEY` ya vive en `server/.env` (gitignorado) — nunca ponerla en
  `.env.example`, en el código, ni en ningún fichero versionado.

## Acceptance contract

Todos los comandos se ejecutan desde `server/`.

- [ ] Con la hora de conexión fijada a `2026-09-18` (Europe/Madrid) y las respuestas
      de `/directory` y `/availability` de cada caso grabadas como fixture, la
      composición del envío produce exactamente el JSON `BOOK` aceptado de
      `simple_booking.md` para los 4 casos — checked by:
      `uv run pytest tests/test_booking_compile.py`
- [ ] Los 4 casos completan un diálogo scripted contra el bot real (transport `eval`,
      modo texto, API viva) hasta el nodo `Despedida`, y `confirm_offer` se llama con
      una oferta cuyo `patient_id`, `appointment_type_id` y `policy_id` coinciden con
      el caso — checked by: `uv run pipecat eval run evals/simple_booking_<caso>.yaml -v`
      (uno por caso: josefa, amelia, ignacio, chloe)
- [ ] Al desconectar el socket en cualquier punto de la llamada (incluida una
      desconexión a mitad de flujo, sin confirmar nada), se dispara exactamente un
      `POST /submit/book` si hay oferta confirmada, o `NO_ACTION(patient_not_found)` si
      no se llegó a identificar al paciente — checked by:
      `uv run pytest tests/test_submission.py`
- [ ] El temporizador de 2m30s fuerza el envío de `pending_submission` tal como esté en
      ese momento, sin esperar al cierre del socket — checked by:
      `uv run pytest tests/test_submission.py`
- [ ] Tras 3 intentos fallidos de identificación, `pending_submission` queda en
      `NO_ACTION(patient_not_found)` y la llamada cierra — checked by: escenario scripted
      `evals/simple_booking_identify_fail.yaml`

Nota sobre el reloj: el slot aceptado depende del día de la llamada (`FR-earliest`:
"lo antes posible" = desde el día siguiente). Los JSON de `simple_booking.md` están
calculados para una llamada el 18-09-2026. Por eso la hora de conexión tiene que ser
inyectable (env var `CALL_CLOCK_OVERRIDE`, solo para tests) — sin ella, el primer
criterio solo pasaría ese día, y el segundo solo comprueba forma, no el slot exacto.

Escenarios con nombre:

- **josefa / amelia / ignacio / chloe** — cada uno prueba que, con los datos exactos del
  caso (identificador, especialidad, sede/día/franja si aplica), el flujo produce el
  `BOOK` aceptado, incluido el tipo de cita correcto (`review` vs `first_visit` vs
  `orthopaedic_review`) y el `policy_id` correcto de la ficha.
- **identify_fail** — prueba que el flujo no se cuelga esperando indefinidamente cuando
  el identificador no cuadra tres veces seguidas, y que el envío por defecto sigue
  siendo válido (nunca vacío).
- **disconnect mid-flow** — prueba la garantía de "toda llamada termina con un envío",
  incluso cuando el que llama cuelga antes de confirmar.

Gate commands (lo que correrá `/verify`):

```bash
cd server
uv run pytest
uv run pipecat eval run evals/simple_booking_josefa.yaml -v
uv run pipecat eval run evals/simple_booking_amelia.yaml -v
uv run pipecat eval run evals/simple_booking_ignacio.yaml -v
uv run pipecat eval run evals/simple_booking_chloe.yaml -v
uv run pipecat eval run evals/simple_booking_identify_fail.yaml -v
```

## Out of scope

- Cualquier rama de `flow.yaml` fuera del camino feliz: desambiguar varias
  coincidencias más allá del reintento simple (`PR-04`), llamante distinto del paciente
  (`PR-09`), aseguradora que no cubre / segunda póliza (`PR-06`/`PR-17`), negociar
  cuando no hay hueco (`PR-07`), cancelar/reprogramar, escalar por emergencia médica.
- Idiomas distintos de inglés/castellano (`PR-11` completo).
- El cambio de proveedor de TTS (Cartesia → otro) — mencionado por el usuario pero
  fuera de esta pasada.
- Probar contra una llamada de teléfono real / número de Twilio — se verifica vía
  transport `eval`; la llamada real se prueba cuando toque contra el dashboard del
  evento.

## Interfaces

Las 3 tools que ve el LLM (contrato entre `handlers.py` y los escenarios de eval que se
escriben en paralelo). Las firmas de abajo son el **schema que ve el modelo** (nombre,
argumentos, forma del resultado); los handlers Python siguen la convención del scaffold
`(args: FlowArgs, flow_manager: FlowManager)` de `handlers.py:57`.

```python
async def search_patient(
    stated_name: str, id_type: Literal["national_id", "phone"], id_value: str,
) -> dict:
    """-> {"status": "found", "patient_summary": str}
        | {"status": "not_found"}
        | {"status": "misheard_id"}"""

async def get_earliest_slot(
    specialty: Literal["general_practice", "paediatrics", "dermatology",
                        "orthopaedics", "gynaecology", "physiotherapy"],
    site: Literal["centro", "norte", "sur"] | None,
    weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday",
                      "saturday", "sunday"] | None,
    part_of_day: Literal["morning", "afternoon"] | None,
) -> dict:
    """-> {"status": "offer", "offer_id": str, "summary": str}
        | {"status": "no_slots"}"""

async def confirm_offer(offer_id: str) -> dict:
    """-> {"status": "confirmed"} | {"status": "expired"}"""
```

Funciones puras que prueban los tests de aceptación sin red ni pipeline:

```python
# booking.py — elige el hueco a partir de una respuesta real de /availability
def pick_offer(
    availability: dict,        # AvailabilityResponse tal cual (slots[], appointment_type, providers[])
    patient: dict,             # PatientMatchOut tal cual (patient_id, insurer, ...)
    connected_at: datetime,    # hora de conexión, Europe/Madrid
    weekday: str | None = None,        # "monday".."sunday"
    part_of_day: str | None = None,    # "morning" (<14:00) | "afternoon" (>=14:00)
) -> dict | None:
    """{"patient_id","provider_id","location_id","appointment_type_id","slot","policy_id"}
    o None si no hay hueco. `slot` = ISO con offset, p.ej. "2026-09-19T11:00:00+02:00".
    Nunca el mismo día de connected_at. Empate en la primera hora → provider con menos
    slots ocupados (menor proporción de sus slots en la respuesta)."""

# national_id.py
def is_valid_national_id(value: str) -> bool: ...   # DNI 8 dígitos + letra, NIE X/Y/Z

# submission.py
class CallSubmission:
    def __init__(self, call_id: str, client): ...   # client.post_submission(action: dict) -> awaitable
    pending: dict            # empieza en {"action": "NO_ACTION", "reason": "patient_not_found"}
    def set_book(self, offer: dict) -> None: ...    # offer = salida de pick_offer
    async def flush(self) -> None: ...               # idempotente: el 2º flush no repite el POST
```

## Tasks

Todas las rutas son relativas a `server/` y los comandos se ejecutan desde ahí.

1. **UPDATE** `pyproject.toml` — añade el extra `evals` a `pipecat-ai[...]` (el
   transport `eval` y `pipecat eval` viven ahí) y `google` para `GoogleLLMService`;
   `uv sync`.
   VALIDATE: `uv run python -c "from pipecat.evals.transport import EvalTransportParams; from pipecat.services.google.llm import GoogleLLMService"`
2. **CREATE** `clinic_catalog.py` — carga `clinic.json` una vez al arrancar; expone
   specialties/locations/appointment_types para construir los `enum` dinámicos.
   VALIDATE: `uv run python -c "from clinic_catalog import load_catalog; c=load_catalog(); assert c['providers']"`
3. **DELETE** `menu.py` — sustituido por lo anterior.
   VALIDATE: `grep -rn "menu" bot.py handlers.py` no devuelve nada
4. **CREATE** `clinic_client.py` — cliente HTTP async para `/directory`,
   `/availability`, `/submit/book`, con `X-Api-Key` desde env y el retry corto (2
   intentos, ~2s) de la Decisión 5.
   VALIDATE: `uv run python -c "..."` que llama `GET /directory?name=Josefa Domínguez Navarro`
   y comprueba que devuelve `patient_id == "P00001"`
5. **CREATE** `national_id.py` — validación local de la letra del DNI/NIE (caso
   `misheard_id`), función pura.
   VALIDATE: `uv run pytest tests/test_national_id.py`
6. **CREATE** `submission.py` — `CallSubmission` de `## Interfaces`: guarda
   `pending`, lo actualiza en cada paso, y `flush()` hace el POST con reintentos,
   idempotente. El reloj se inyecta (`CALL_CLOCK_OVERRIDE` en tests).
   VALIDATE: `uv run pytest tests/test_submission.py`
7. **UPDATE** `handlers.py` — sustituye los nodos de pizza por
   `Saludo → Identificar → Consultar_hueco → Confirmar → Despedida`, con las 3 tools de
   `## Interfaces`, usando `clinic_client` + `national_id` + `submission` + el
   tie-break de carga (§7 del doc corto).
   VALIDATE: `uv run python -c "import handlers"` sin excepciones
8. **UPDATE** `bot.py` — cambia `menu` por `clinic_catalog`; añade el switch
   `LLM_PROVIDER=openai|gemini`; crea el `CallSubmission` por llamada y lo cuelga de
   `on_client_disconnected` y del temporizador de 2m30s; añade la entrada `eval` a
   `transport_params`.
   VALIDATE: `uv run bot.py -t eval` arranca sin excepción
9. **UPDATE** `.env.example` — quita variables de pizza (`RESTAURANT_NAME`,
   `PIZZA_*`, `SUSHI_*`); añade `CLINIC_API_BASE_URL`, `CLINIC_API_KEY` (vacío),
   `LLM_PROVIDER`, `CALL_CLOCK_OVERRIDE` (comentado, solo tests).
10. **ADD** `evals/simple_booking_{josefa,amelia,ignacio,chloe,identify_fail}.yaml`.
    VALIDATE: cada `uv run pipecat eval run evals/<nombre>.yaml -v` pasa
11. **CREATE** `tests/test_booking_compile.py` — composición pura (fecha, tie-break,
    `policy_id`, tipo de cita) con reloj fijado y fixtures de `/directory` y
    `/availability` de los 4 casos.
    VALIDATE: `uv run pytest tests/test_booking_compile.py`

## Notes

- 2026-09-18 — Aprobado implícitamente por `/implement simple-booking` (Adolfo no
  contestó explícitamente a las Decisiones 10 y 11; se asumen aceptadas). Instrucción
  añadida: pocos tests, no gastar tiempo ahí, priorizar implementación.

- 2026-09-19 — Tests: los 3 ficheros de aceptación (agente ciego) se escribieron en
  paralelo a los módulos puros, así que no hubo RED capturado para
  `booking`/`national_id`/`submission` — pasaron 10/10 al primer run. Instrucción de
  Adolfo: no gastar tiempo en tests.
- 2026-09-19 — `Saludo` no es un nodo propio: es el `pre_actions: tts_say` del nodo
  `identify` con `respond_immediately: False` (un nodo sin funciones no puede
  transicionar). Mismo comportamiento, un nodo menos. Saludo en inglés (asunción de
  idioma base del build-plan Q7).
- 2026-09-19 — Añadido `revise_search` en el nodo `confirm` (transición a `find_slot`)
  para que un "no" del llamante no sea un callejón sin salida. Fuera del doc corto pero
  de coste cero; el cambio de opinión completo (`PR-13`) sigue fuera de alcance.
- 2026-09-19 — `flush()` se llama en tres sitios (post_action del nodo final,
  `on_client_disconnected`, `finally` del runner) y además por el temporizador de 150s.
  Es idempotente, así que el primero que llegue gana.
- 2026-09-19 — Sin `from_number` como candidato previo (§5 paso 2 del doc corto): era
  una mejora de jurado, no cambia el resultado. Pendiente para otra pasada.
- 2026-09-19 — Escenarios eval: sin `eval:` de juez salvo en `identify_fail` (no hay
  Ollama garantizado); el slot exacto no es observable desde el harness, se verifica
  en el log del bot (`Offer offer-1: {...}`) y en `test_booking_compile.py`.
- 2026-09-19 — BLOQUEO T8/T10: `server/.env` no tiene `OPENAI_API_KEY`,
  `DEEPGRAM_API_KEY` ni `CARTESIA_API_KEY` (vacías). El bot no arranca sin ellas.
- 2026-09-19 — Decisión 3 cambiada por Adolfo: TTS pasa de Cartesia a Deepgram
  (`DeepgramTTSService`, voz `aura-2-helena-en` por defecto). LLM por defecto ahora
  `gemini` (`GEMINI_API_KEY`, con `GOOGLE_API_KEY` como alternativa).
- 2026-09-19 — BUG encontrado y corregido: `tts_say` como `pre_action` del primer nodo
  deja a Flows esperando un `ActionFinishedFrame`; si el llamante habla durante el
  saludo, la interrupción lo descarta y `_set_node` se cuelga (bot sin instrucciones ni
  tools toda la llamada). El saludo fijo se encola ahora en `bot.py` como
  `TTSSpeakFrame` después de `initialize()`. Además el flujo arranca en
  `worker.rtvi on_client_ready` para clientes RTVI (webrtc/daily/eval) y en
  `on_client_connected` solo para Twilio, como en la plantilla oficial.
- 2026-09-19 — `gemini-2.5-flash` ya no está disponible para cuentas nuevas (404 de la
  API); modelo por defecto `gemini-3.6-flash`.
- 2026-09-19 — A mitad de implementación entró un `git stash`/`pull`/`stash pop` ajeno a
  esta sesión (commits upstream: Krisp VIVA, Makefile, túnel ngrok) con conflictos en
  `bot.py`, `.env.example` y `uv.lock`. Adolfo los resolvió; `bot.py` conserva Krisp +
  la lógica de este plan.
- 2026-09-19 — Evals con reloj fijado al 18-09: **chloe pasa y su oferta coincide
  exactamente con el JSON aceptado** (P00011/PR03/sur/review/2026-09-21T09:00/mapfre).
  josefa/amelia/ignacio: inestables. Dos causas vistas en el log: (1) Gemini a veces
  contesta con texto (~40 tokens) en vez de llamar a `search_patient`; (2) el servidor
  eval reutiliza UNA sesión de `bot()` para todas las conexiones, así que el estado del
  flujo se arrastra entre escenarios — para runs aislados hay que usar
  `pipecat eval suite` (bot nuevo por run), no `eval run` en bucle.
- 2026-09-19 — El temporizador de 150s arranca ahora al empezar el flujo (no al arrancar
  el proceso).
- 2026-09-19 — CAUSA RAÍZ de los evals inestables: **429 de Gemini, clave en free tier**
  (límite 20 requests para `gemini-3.6-flash`). Todos los runs fallidos tienen el 429 en
  el log del bot; los que pasan no. Las dos hipótesis anteriores (filtro de turnos
  incompletos; Gemini respondiendo texto en vez de tool) quedan REFUTADAS — A/B con bot
  fresco: filtro ON 1/3, filtro OFF 0/3, todos los fallos con 429. El filtro se deja
  como estaba (ON).
- 2026-09-19 — Verificado con bot fresco y reloj al 18-09: josefa y chloe producen
  exactamente el JSON aceptado. `clinic.json` comparado con `GET /v1/clinic` en vivo:
  idénticos.
- 2026-09-19 — Decisión de Adolfo: LLM pasa a Helmcode (gateway compatible con OpenAI,
  `https://api.helmcode.com/v1`, modelo `deepseek-v4-flash`: tool calling + streaming,
  100 rpm, 5 requests concurrentes por clave). Proveedor `helmcode` añadido a
  `build_llm()` vía `OpenAILLMService(base_url=...)` y puesto por defecto; `gemini` y
  `openai` siguen disponibles con `LLM_PROVIDER`.
- 2026-09-19 — PRIMERA LLAMADA REAL (práctica, Ignacio): "Connection lost · Missing
  record" a los 3 s, 2 de 2. Túnel OK (ngrok registró `GET /ws -> 101`). Causa raíz:
  `create_transport()` construye `TwilioFrameSerializer` con `auto_hang_up=True` y
  credenciales Twilio vacías → `ValueError` al arrancar la sesión. Fix: `bot()` monta
  el transporte de telefonía él mismo (`_telephony_transport`) con
  `auto_hang_up=False`; la plataforma cuelga sola y no tenemos cuenta Twilio.
- 2026-09-19 — Calidad de llamada (tras la 1ª llamada real OK de Ignacio, BOOK registrado
  con los 7 campos correctos): saludo en inglés (la voz es `aura-2-helena-en`; en español
  salía ilegible), prompt "ya has saludado, no repitas" + nombre e identificador en una
  sola pregunta, títulos hablables (`Dra.`/`Dr.`→"Doctor", `D.`→"Don") solo en el texto
  hablado, y frase de relleno "One moment, please." dicha por código antes de cada
  consulta a la API.
- 2026-09-19 — Refactor ajeno a esta sesión (`094b0b8`): `handlers.py` → paquete
  `server/flow/` (`nodes`, `tools`, `prompts`), cliente y catálogo → `server/clinic/`.
  Los 4 arreglos de calidad sobrevivieron. Regresión introducida por el move y corregida
  aquí: `CATALOG_PATH` usaba `parent.parent` y pasó a apuntar a `server/clinic.json`;
  ahora `parents[2]` (raíz del repo). Síntoma: los 4 evals fallaban en `search_patient`
  con FileNotFoundError.
- 2026-09-19 — Evals tras el refactor + fix: josefa/amelia/ignacio/chloe 4/4, ofertas
  idénticas al JSON aceptado, frase de relleno 2x por llamada. Se corren en el puerto
  7861 (`--port 7861` / `--bot-url ws://localhost:7861`) para no chocar con el bot de
  Twilio del 7860. Un corte de DNS local (~01:31) tumbó un run intermedio: no era código.
- 2026-09-19 — 2ª LLAMADA REAL (Josefa): "Agent silence", NO_ACTION enviado (la red de
  seguridad funcionó: nunca registro vacío). Causa raíz en el log del bot:
  `_audio_task_handler unexpected exception (pyrnnoise.py:43): Graph.__init__() got an
  unexpected keyword argument 'rate'` — el `RNNoiseFilter` añadido upstream revienta en
  el primer frame (pyrnnoise 0.4.3 vs av 17.0.1), mata la tarea de audio de entrada y
  deja el bot SORDO toda la llamada. Reproducido offline al 100%. Refutadas tres
  hipótesis mías (prompt "no vuelvas a saludar", Deepgram con "Hi" corto, gateway).
  Fix: `_noise_filter_available()` prueba el filtro con un frame de 8 kHz al arrancar;
  si falla, el bot corre sin filtro y lo registra como ERROR. Pendiente para quien
  añadió RNNoise: fijar versiones compatibles de pyrnnoise/av.
- 2026-09-19 — Hallazgo de latencia: `deepseek-v4-flash` emite tokens de razonamiento
  (440 tokens para una respuesta de 7 palabras, ~3 s). Sin resolver: comprobar si el
  gateway permite desactivarlo.
- 2026-09-19 — Decisión de Adolfo: la supresión de ruido NO toca todavía (el caso de uso
  es PR-12, no PR-01). `RNNoiseFilter` y el guard de autocomprobación retirados de
  `bot.py`; queda un comentario en `_audio_kwargs()` con cuándo reintroducirlo y el
  aviso del crash pyrnnoise 0.4.3 + av 17. Verificado después: pytest 10/10 y evals
  4/4 contra la API real, ofertas idénticas al JSON aceptado, 0 errores en el bot.
- 2026-09-19 — RUN ALL #2 (28 llamadas: 4 BOOK, 24 perdidas). Dos modos de fallo, medidos
  en `/tmp/bot-runall2.log`: (1) 15-16 cortes por "Agent silence": el bot pregunta, el VAD
  no detecta voz en 37-56 s y la plataforma corta; el llamante simulado tarda mediana 11 s,
  p90 24 s, máx 46 s. (2) 8-9 llamadas donde NUESTRO temporizador de 150 s envió el
  NO_ACTION por defecto y, al ser el envío único, bloqueó el BOOK posterior (N=19 y N=25
  llegaron a despedirse después).
- 2026-09-19 — Tres arreglos (decisión de Adolfo): (#10) temporizador de 150 s ELIMINADO —
  la ventana de envío dura 30 s tras cerrar el socket, flush al desconectar basta.
  (#9) vigilante de silencio: `user_idle_timeout=16 s` (cuenta desde que el bot deja de
  hablar ≈ 20 s desde que empieza) + `on_user_turn_idle` → repite la última PREGUNTA del
  bot ("Sorry, are you still there? …"), dicho por código, sin LLM. (#11) si la llamada
  acaba con una oferta hecha y no rechazada, se envía como BOOK en vez de NO_ACTION
  (`CallSubmission.set_offer/clear_offer`; `revise_search` la borra). Riesgo asumido:
  reserva no confirmada verbalmente. También: espacio que faltaba en "at{sede}".
- 2026-09-19 — Verificación: pytest 20/20 (4 nuevos en tests/unit), evals 4/4 con ofertas
  exactas. NO verificado: que el vigilante salte en vivo — el eval de texto no genera
  "Bot stopped speaking" (no hay audio), que es lo que arma el reloj. Solo se puede ver
  en una llamada con audio: buscar "of silence, re-prompting" en el log.
- 2026-09-19 — Claude vía Cloudflare (default): `LLM_PROVIDER=cloudflare` usa
  `AnthropicLLMService` contra la API unificada
  `https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1/messages`, modelo
  `anthropic/claude-sonnet-4.6`. Requiere `CLOUDFLARE_ACCOUNT_ID` + token con
  Workers AI Read, y créditos de AI Gateway Unified Billing (Workers Paid no
  cubre tokens de Claude). `helmcode`, `gemini` y `openai` siguen como
  alternativas.
