# Plan: motor del agent builder

**Status**: implemented (motor, tareas 1-7) — pendiente de verificar. Tarea 8 (página "Avisos") a la espera de Miguel.

## Goal

Hoy el agente solo conoce lo que publica la clínica. Cuando esto esté hecho, recepción
podrá dejar avisos en un fichero y la siguiente llamada los respetará: un médico que no
viene, un día que se cierra, un médico que deja un seguro, un aviso que el agente dice
en voz alta al ofrecer la cita, y el tono. Cada aviso tiene fecha de fin y, cuando
influye en una llamada, queda apuntado en el registro de esa llamada. No hay pantalla:
el fichero se escribe a mano hasta que exista frontend. Sin fichero, el agente es
exactamente el de hoy.

## Findings

Heredados del `/explore` del 19-sep, más lo leído al abrir el plan.

- **El catálogo es la respuesta de la API guardada en disco.** `clinic.json` es un
  snapshot de `GET /api/v1/clinic` (commit `cae97ac`); la API dice que no cambia durante
  el evento (`API-clinic-cache`, `docs/api/02-clinic.md:55`). Se carga una vez por
  proceso: `server/clinic/clinic_catalog.py:10`, `@lru_cache(maxsize=1)`.
- **La baja de un médico se mira contra HOY, no contra el día de la cita.**
  `server/rules.py:365-386`: `_on_leave(leave, today)`. El agente nunca ofrece citas
  para hoy (`server/booking.py:60`, `start.date() <= call_day`). Consecuencia: un aviso
  "no viene mañana" escrito hoy no haría nada, y durante la baja se rechaza al médico
  también para fechas posteriores a su vuelta. Único dato real hoy: PR02 Dr. Pablo
  Requena, 2026-09-14 → 2026-09-30.
- **El día cerrado sí se mira contra el día de la cita.** `server/booking.py:45` y `:62`:
  `pick_offer` descarta los huecos cuya fecha está en `closure_days` (hoy solo
  `2026-10-12`). `server/dates.py:58` usa la misma lista para resolver "el viernes".
  Cierra la clínica entera, no un centro.
- **Seguro rechazado por un médico:** `server/rules.py:163` lee `refused_insurers` del
  médico y devuelve `provider_not_in_network`, con redirección a otro médico.
- **Los huecos se piden a la API en dos sitios:** `server/flows/booking.py:241` (la
  conversación) y `server/resolution.py:139` (el cierre de llamada). Las cinco llamadas a
  `pick_offer` beben de esos dos resultados.
- **El resumen que se lee al paciente** se arma en `server/flows/booking.py:393-398` y
  viaja como resultado de la herramienta; ya admite un campo extra (`note`, `:399-403`).
- **El tono vive en una constante:** `ROLE_MESSAGE` en `server/flows/common.py:9`, usada
  en `server/flows/identification.py:106` y `server/flows/reception.py:25`, y reexportada
  en `server/handlers.py:12`.
- **Registro por llamada:** `audit.audit(call_id, event, **fields)`,
  `server/audit.py:56`, a `audit-logs/audit-<call_id>.ndjson`. Nunca lanza.
- **El marcador:** no puntúa la conversación (`SC-conversation-unscored`) y calcula lo
  esperado con `/availability` (`SC-expected-via-api`), que no conoce nuestros avisos.
  Nada en `server/bot.py:737-747` distingue una llamada puntuada de una de demo. El muro
  se congela el domingo 20-sep a las 06:00 (`SC-freeze`).
- **Frontend:** no hay en ninguna rama (comprobado tras `git fetch`). Lo único web es el
  dashboard de evals, solo lectura, en `origin/merge/flows-agentic-improvements`.
- **Ya hay frontend (llegó a `main` el 19-sep, PR #8, `feat/backend`).** Consola en
  `server/observability/console/` (`index.html`, `app.js` 1566 líneas, `styles.css`; JS
  sin build), servida por el propio proceso del bot en `/console`
  (`server/observability/routes.py:128`, `make console`). Dos vistas: Overview y
  Conversations (Switchboard / Conversation / Why). Todo es lectura salvo
  `POST /observability/fixtures/{name}/load`; no hay ninguna vista de edición ni
  autenticación. CORS solo local (`routes.py:26-37`), pero `make tunnel` expone ese mismo
  puerto 7860 por ngrok.
- **El panel "Why" se alimenta del resultado de cada herramienta.** `trace_tool`
  (`server/observability/emit.py:131`) levanta `status`, `reason_codes` y `justification`
  del diccionario que devuelve la herramienta y los publica como `tool.returned`;
  `renderWhy` (`console/app.js:815`) los pinta como "Decision trail". `get_earliest_slot`
  ya está decorada (`server/flows/booking.py:21`).
- **Baseline:** `make test` → 271 passed en 12,65 s (medido el 19-sep al cerrar este plan,
  en `adolfo/product-features`).

## Decisions

- **19-sep · Qué casos entran.** Médico ausente, día cerrado, médico que deja un seguro,
  avisos hablados, tono. Fuera: saludo editable. Para luego: preguntas frecuentes
  (riesgo de que invente lo que no está escrito). Sin decidir y fuera de este plan:
  repartir carga, ausencia de medio día.
- **19-sep · El tono es una frase añadida al system prompt** (palabras de Adolfo).
- **19-sep · La pantalla de edición va al final de la cola.** No hay frontend en el
  repo; se espera a que lo suban. Descartado: montar una página propia ahora —
  duplicaría lo que vaya a subir el equipo.
- **19-sep · "Pasar a una persona según el tema" sale de este plan** y pasa a la feature
  3 como guardarraíl único con Jev de TypeSafe AI (Adolfo tiene clave). Es el mismo
  mecanismo que escalar urgencias; hacerlo dos veces es duplicar.
- **19-sep · El médico ausente se aplica por día de cita, no con la regla de baja.** Se
  esconden los huecos de ese médico solo en los días del aviso; otro día con él, u otro
  médico ese día, siguen ofreciéndose. Descartado: reutilizar `leave` tal cual (cero
  lógica, pero mira si está de baja HOY); hacer las dos cosas y además arreglar
  `_on_leave` (lo más correcto, pero toca una regla que hoy acierta casos del marcador).
  La regla de baja existente no se toca.
- **19-sep · El interruptor es el propio fichero.** Sin fichero, comportamiento idéntico
  al de hoy; el fichero no entra en git; al arrancar, el bot deja en el log qué avisos
  que cambian citas hay. Descartado: una variable de entorno además del fichero (dos
  llaves, pero una cosa más que recordar en la demo y tampoco salva de un olvido); no
  activarlo hasta el domingo (riesgo cero, pero el vídeo no se graba hasta entonces).
  Riesgo aceptado: dejar el fichero puesto y lanzar un Run All antes del domingo 06:00.
- **19-sep · La pantalla deja de estar al final de la cola:** ya hay consola (PR #8). El
  orden sigue siendo motor primero, pantalla después. **Dónde va la pantalla está sin
  decidir:** Miguel va a crear una página nueva de médicos, aparte de Overview y
  Conversations, y hay que ver dónde encajan los avisos por UX.
- **19-sep · Reparto de la pantalla (decidido por Adolfo).** Una página "Avisos" en la
  consola, nuestra, es el único sitio donde se crean y se quitan los cinco tipos de
  aviso, con la lista de lo activo y su fecha de fin. El calendario de horarios que va a
  hacer Miguel solo lee `GET /notices` y pinta las ausencias y los días cerrados como
  franjas grises; no guarda nada. Motivo: si el calendario no enseña las ausencias,
  muestra huecos libres de un médico que el agente dice que no está. Descartado: crear
  los avisos dentro del calendario (la mejor experiencia, pero bloqueado por Miguel, cae
  en su código, y seguro/aviso hablado/tono no caben en un calendario); que el calendario
  ignore los avisos (dos pantallas contradiciéndose). Sin decidir: una línea "N avisos
  activos" en Overview.
- **19-sep · El guardado de avisos va sin protección por ahora** (decisión de Adolfo).
  Descartado de momento: aceptar solo peticiones de la propia máquina; una clave en
  variable de entorno. Riesgo aceptado: mientras `make tunnel` esté abierto, quien tenga
  la URL de ngrok puede escribir avisos que cambian qué citas se dan.
- **19-sep · El rastro se ve en la consola, no solo en el fichero.** Cuando un aviso
  influye, la herramienta lo devuelve en `justification` y el panel "Why" lo pinta sin
  tocar el frontend. El evento en `audit-<call_id>.ndjson` se mantiene.
- **19-sep · Elecciones mecánicas, sin consultar** (cuestan un re-run si están mal):
  - Los huecos del médico ausente se quitan justo después de pedirlos a la API, en los
    dos sitios, y no dentro de `pick_offer`: así la función que elige el hueco puntuado
    no cambia ni una línea.
  - Día cerrado y seguro entran como una capa sobre el catálogo, sin mutar el catálogo
    cacheado: `closure_days()` y la regla de seguros los ven sin tocarse.
  - El aviso hablado se dice con el resumen de la oferta (antes del "sí"), no después de
    confirmar: es cuando le sirve al paciente y no pasa por el código que envía la cita.
  - Texto libre (tono y avisos hablados) limitado a 200 caracteres y entregado al modelo
    como texto a decir, no como instrucción. Quien escribe el fichero es personal de la
    clínica; cuando haya pantalla, esto se revisa.
  - Una entrada mal escrita se ignora y se apunta en el log; nunca rompe una llamada.

## Context

**Ficheros que se tocan o se crean**

- CREATE `server/reception_notices.py` — leer, validar y consultar el fichero de avisos.
- CREATE `server/tests/unit/test_reception_notices.py`.
- UPDATE `server/clinic/clinic_catalog.py:10-13` — `load_catalog()` devuelve el catálogo
  base más días cerrados y seguros de los avisos; `load_base_catalog()` es el cacheado.
  Leer antes `server/dates.py:54-58` y `server/rules.py:155-170`, que lo consumen.
- UPDATE `server/clinic_catalog.py` (el re-export que todo el código importa) — añadir
  `load_base_catalog` a la lista.
- UPDATE `server/flows/booking.py:241-248` y `server/resolution.py:139-150` — quitar los
  huecos de médicos ausentes tras pedir disponibilidad, y apuntarlo en el registro.
- UPDATE `server/flows/booking.py:393-403` — añadir al resultado de la oferta los avisos
  hablados que coincidan con el centro y el día de esa oferta.
- UPDATE `server/flows/common.py:9-14` — función que devuelve `ROLE_MESSAGE` más la frase
  de tono; usarla en `server/flows/identification.py:106` y `server/flows/reception.py:25`.
  La constante `ROLE_MESSAGE` se queda (la reexporta `server/handlers.py:12`).
- UPDATE `server/bot.py` cerca de `:660` (`call_started`) — apuntar `notices_active` al
  empezar la llamada.
- UPDATE `.gitignore` — el fichero de avisos.
- UPDATE `docs/features.md` §4 — estado y guion de demo al terminar.

**Patrones a seguir**

- Reglas como funciones puras sobre datos, con el porqué en el docstring:
  `server/rules.py:1-23`.
- Filtro de huecos sin mutar la respuesta de la API (`{**availability, "slots": [...]}`):
  `server/flows/booking.py:292-299`.
- Evento de registro con campos planos: `server/flows/booking.py:383-392`.
- Tests que fijan la lógica pasando los datos a mano en vez de leer el catálogo:
  `server/booking.py:37-43` (`closed_days=frozenset()`).

**Avisos**

- `load_catalog()` se llama desde 8 módulos. La capa de avisos no puede mutar el
  diccionario cacheado: otro módulo que lo tenga cogido lo vería cambiar.
- El seguro y el tono se activan con la fecha real de Madrid. Los evals usan
  `CALL_CLOCK_OVERRIDE` (`server/bot.py:124`); como en evals no hay fichero de avisos, no
  se cruzan. Si algún día se escriben evals con avisos, hay que pasar el reloj.
- Regla del repo (`docs/features.md`, "no romper el board"): este plan toca
  `server/flows/` → `make test` y `make evals` antes y después. `make evals` mata el bot
  de eval de otras sesiones: lo lanza o lo autoriza Adolfo, nunca por iniciativa propia.
- Varias sesiones comparten rama y directorio: `git add <ficheros propios>`, nunca `-A`.

## Acceptance contract

- [ ] Sin fichero de avisos, `load_catalog()` devuelve un catálogo igual al de
  `clinic.json` y los huecos no se filtran — checked by:
  `test_no_file_changes_nothing`.
- [ ] Con un aviso de médico ausente del 22 al 24, de una lista de huecos desaparecen
  solo los de ese médico en esos tres días; los suyos del 25 y los de otros médicos el
  23 siguen — checked by: `test_absent_provider_hides_only_their_slots_on_those_days`.
- [ ] Con un aviso de día cerrado, `closure_days()` incluye ese día además del
  `2026-10-12` del catálogo, y el diccionario cacheado no ha cambiado — checked by:
  `test_closed_day_is_added_without_mutating_base`.
- [ ] Con un aviso "el médico X deja el seguro Y" activo hoy, la regla devuelve
  `provider_not_in_network` para X con Y; con el aviso caducado ayer, no — checked by:
  `test_dropped_insurer_bites_only_while_active`.
- [ ] Un aviso hablado de un centro hasta el jueves aparece en el resultado de una oferta
  de ese centro el martes, y no en una de otro centro ni en una del viernes — checked
  by: `test_spoken_notice_matches_site_and_day`.
- [ ] Con tono definido y vigente, el mensaje de rol es `ROLE_MESSAGE` más esa frase; sin
  tono o caducado, es `ROLE_MESSAGE` exacto — checked by: `test_tone_appends_one_sentence`.
- [ ] Una entrada inválida (sin fecha de fin, médico inexistente, texto de más de 200
  caracteres, JSON roto) se ignora, las válidas se aplican y no se lanza excepción —
  checked by: `test_invalid_entries_are_skipped`.
- [ ] Cuando un aviso de ausencia quita huecos, el registro de la llamada contiene un
  evento `notice_applied` con el `id` del aviso; toda llamada con fichero empieza con un
  evento `notices_active` que lista los `id` vigentes — checked by:
  `test_applied_notice_is_audited`.
- [ ] Cuando un aviso de ausencia quita huecos, el resultado de `get_earliest_slot` lleva
  una `justification` que nombra el `id` del aviso, y se publica en el evento
  `tool.returned` de esa llamada — checked by: `test_applied_notice_reaches_why_panel`.
- [ ] `GET /notices` devuelve el contenido del fichero (o la estructura vacía si no
  existe); `PUT /notices` con un cuerpo válido lo escribe y la siguiente lectura lo
  refleja; con una entrada inválida responde 422, no escribe nada y dice qué entrada
  falla — checked by: `test_notices_routes_roundtrip_and_reject_invalid`.
- [ ] La suite entera sigue en verde, con al menos los 271 tests de antes — checked by:
  `make test`.
- [ ] Los escenarios `server/evals/PR-*` que pasaban antes siguen pasando, sin fichero de
  avisos — checked by: `make evals`, lanzado o autorizado por Adolfo, antes y después.
- [ ] Una llamada real con fichero de avisos: pedir cita con el médico ausente en un día
  del aviso y en un centro con aviso hablado. Se oye el aviso, no se ofrece ese médico
  ese día, y el `audit-<call_id>.ndjson` lo refleja — checked by: Adolfo, a oído, con
  `make run-webrtc`.

**Gate commands:** `make test` · `make evals` (con permiso) · `make run-webrtc` + lectura
de `server/audit-logs/audit-<call_id>.ndjson`.

## Out of scope

- Proteger el guardado de avisos (sin nada por ahora; se revisa antes de enseñarlo con el
  túnel abierto).
- Arreglar `_on_leave` para que mire la fecha de la cita.
- Cerrar un solo centro, ausencias de medio día, repartir carga.
- Pasar a una persona según el tema (feature 3, guardarraíl con Jev).
- Preguntas frecuentes y saludo editable.
- Validar un aviso pasando los evals antes de activarlo.

## Interfaces

El fichero es el contrato entre este motor y la futura pantalla. Ruta por defecto
`server/reception_notices.json`, configurable con `RECEPTION_NOTICES_PATH`. Fechas
`YYYY-MM-DD`, inclusivas, en hora de Madrid. `until` es obligatorio en todo.

```json
{
  "tone": {"text": "Habla de tú, cercano.", "until": "2026-09-30"},
  "notices": [
    {"id": "n1", "kind": "provider_absent", "provider_id": "PR03",
     "from": "2026-09-22", "until": "2026-09-24"},
    {"id": "n2", "kind": "clinic_closed", "from": "2026-09-25", "until": "2026-09-25"},
    {"id": "n3", "kind": "insurer_dropped", "provider_id": "PR05", "insurer_id": "dkv",
     "from": "2026-09-19", "until": "2026-12-31"},
    {"id": "n4", "kind": "spoken", "text": "El ascensor está averiado.",
     "location_id": "centro", "from": "2026-09-19", "until": "2026-09-24"}
  ]
}
```

`provider_absent`, `clinic_closed` y `spoken` se comparan con el día de la cita;
`insurer_dropped` y `tone`, con el día de la llamada. En `spoken`, `location_id` nulo
significa todos los centros.

```python
# server/reception_notices.py
EMPTY: dict = {"tone": None, "notices": []}
def validate_notices(raw: dict) -> tuple[Notices, list[dict]]: ...   # (válidos, errores {id, error})
def load_notices(path: Path | None = None) -> Notices: ...           # sin fichero → vacío; inválidos fuera
def hide_absent(availability: dict, notices: Notices) -> tuple[dict, list[str]]: ...  # (nueva, ids aplicados)
def spoken_for(notices: Notices, location_id: str, day: date) -> list[str]: ...       # textos
def record_active_notices(call_id: str) -> None: ...                 # evento notices_active {ids: [...]}
def mount_notices_routes(app: FastAPI) -> None: ...                  # GET /notices, PUT /notices

# server/clinic/clinic_catalog.py, re-exportado por server/clinic_catalog.py
def load_base_catalog() -> dict: ...     # clinic.json tal cual, cacheado
def load_catalog() -> dict: ...          # base + días cerrados + seguros de los avisos; nunca muta la base
# server/dates.py — ya existe: closure_days() -> set[str], lee load_catalog()

# server/flows/common.py
def role_message(today: date) -> str: ...
```

`Notices` es inmutable. En el resultado de `get_earliest_slot`: los textos hablados van
en `spoken_notices` (lista) y la explicación en `justification` (texto que contiene el
`id` del aviso). Evento de registro: `notice_applied` con `notice_ids`. `PUT /notices`
inválido → 422 con `detail` = lista de `{id, error}`. "Activo hoy" usa la fecha real de
Madrid (`insurer_dropped`); `role_message` recibe la fecha.

## Tasks

1. CREATE `server/reception_notices.py` + sus tests: leer, validar, caducidad, y las tres
   consultas (`hide_absent`, `spoken_for`, tono). Sin tocar nada más.
   VALIDATE: `cd server && uv run pytest tests/unit/test_reception_notices.py`
2. UPDATE `server/clinic/clinic_catalog.py` (+ re-export en `server/clinic_catalog.py`): capa de días cerrados y seguros sobre el
   catálogo base, sin mutarlo.
   VALIDATE: `cd server && uv run pytest tests/unit/test_reception_notices.py tests/acceptance/test_rules.py tests/acceptance/test_dates.py`
3. UPDATE `server/flows/booking.py:241` y `server/resolution.py:139`: quitar huecos de
   ausentes y apuntar `notice_applied`.
   VALIDATE: `cd server && uv run pytest tests/acceptance/test_flows.py tests/acceptance/test_resolution.py tests/unit/test_flow_tools.py`
4. UPDATE `server/flows/booking.py:393-403`: avisos hablados en el resultado de la oferta.
   VALIDATE: `cd server && uv run pytest tests/acceptance/test_flows.py`
5. UPDATE `server/flows/common.py`, `identification.py:106`, `reception.py:25`: tono.
   VALIDATE: `cd server && uv run pytest tests/acceptance/test_flows.py`
6. UPDATE `server/bot.py`: evento `notices_active` al empezar la llamada y línea de log
   al arrancar. UPDATE `.gitignore`.
   VALIDATE: `make test`
7. CREATE rutas `GET /notices` y `PUT /notices` junto a las de la consola
   (`server/observability/routes.py:22`, mismo `app`), reutilizando el validador del
   motor. Sin autenticación (decisión del 19-sep).
   VALIDATE: `cd server && uv run pytest tests/test_observability.py tests/unit/test_reception_notices.py`
8. La página "Avisos" en la consola — después del motor; se detalla al llegar (toca
   `server/observability/console/`, que es de Miguel: avisarle antes).
9. Con permiso de Adolfo: `make evals` sin fichero de avisos. Después, llamada real con
   fichero. UPDATE `docs/features.md` §4 con estado y guion de demo.
   VALIDATE: `make evals` · `make run-webrtc`

## Notes

- **19-sep · Arreglos tras `/review`** (panel: código, seguridad, arquitectura; informe en
  `review-report.md`). Aplicados y re-verificados con los mismos ataques que los confirmaron:
  - **A-1 (ALTO):** `resolution.py:150` enviaba citas con el médico ausente. Ahora filtra con
    `hide_absent` antes de `pick_offer`. No se puso en el cliente HTTP porque los tests
    congelados usan clientes falsos que lo esquivarían; son dos puntos de llamada con una
    función común, y un tercero tendría que acordarse.
  - **C-1 (CRÍTICO):** el texto libre ya no puede forjar estructura. `_text` sustituye los
    caracteres de control por espacio; `id` limitado a `[A-Za-z0-9._-]{1,64}`; `MAX_NOTICES
    = 50`; el tono va entrecomillado, nombrado como preferencia de voz y con las reglas
    repetidas detrás; el aviso hablado viaja con una instrucción que lo declara texto de
    recepción y no una orden al modelo.
  - **A-3 (ALTO):** `load_catalog()` vuelve a cachear, con clave `(fichero, mtime, tamaño,
    día)`. Dos llamadas seguidas devuelven el mismo objeto y la edición sigue llegando a la
    llamada siguiente. `dropped_insurers` recibe ese mismo día en vez de leer el reloj.
  - **A-4 (ALTO):** `PUT` lee el cuerpo crudo y devuelve 413 por encima de 64 KB, antes de
    parsear nada.
  - **M-1 (MEDIO):** la explicación solo dice "del médico pedido" cuando se pidió uno.
  - **M-2 (MEDIO):** los errores ya no repiten el valor enviado.
  - **M-3 (MEDIO):** parseo cacheado por mtime → 1 aviso en el log por versión del fichero,
    no uno por lectura.
  - **M-4 (MEDIO):** `PUT` persiste solo los campos que su `kind` admite (`as_document`), y
    `GET` devuelve esa misma vista validada. Escritura con `mkstemp` + `os.replace`, 0600.
  - **B-1:** `.tmp` añadido al `.gitignore` (además, `mkstemp` ya no deja huérfanos).
  - Añadido de paso: `id` duplicado es un error, y `GET /notices/errors` para que la consola
    pueda enseñar lo que el fichero pide y el agente no hace.
  - **Desviación:** el informe proponía que `GET /notices` devolviera los errores en el mismo
    cuerpo. El test de aceptación congelado exige que leer devuelva exactamente lo guardado,
    así que los errores van en su propia ruta.
- **19-sep · Sin arreglar, decisión pendiente de Adolfo:**
  - **A-2 (ALTO):** una ausencia de recepción responde `provider_unavailable` sin lista de
    alternativas, mientras la baja publicada responde `provider_on_leave` con ellas. Cambiarlo
    toca el camino que el marcador puntúa.
  - **Exposición de la ruta de escritura:** sigue sin autenticación (decisión del 19-sep,
    tomada antes de saber que `make tunnel` la publica). El endurecimiento reduce el daño,
    no la alcanzabilidad.
- **19-sep · Suite tras los arreglos:** 293 passed.

- **19-sep · Baseline en `main` tras el PR #8:** 276 passed en 12,7 s. `make test` no
  termina: `tests/test_observability.py::test_live_call_counted` deja una conexión SQLite
  abierta y el proceso no sale. No es de este plan y no se toca; los VALIDATE se lanzan
  con `uv run pytest ... > fichero` y se mata el proceso al acabar.
- **19-sep · Error mío de rama, corregido.** A las 20:59 el directorio compartido pasó de
  `main` a `merge/flows-agentic-improvements` sin que lo advirtiera, y lo que encontré
  después lo atribuí a `main`. En ESA rama `server/flows/booking.py` usa `closure_days()`
  sin importarlo (merge `f61bd6f`) y pedir un médico por nombre da `NameError`; allí el
  módulo real de catálogo es `server/clinic_catalog.py`. **En `main` nada de eso ocurre**
  (comprobado a las 21:2x: 276 passed; la misma prueba con "Martín Sáez" devuelve oferta;
  el módulo real es `server/clinic/clinic_catalog.py` con re-export). Mi línea de arreglo
  quedó sin commitear, viajó a `main` en el stash de GitHub Desktop y dejó marcas de
  conflicto en `server/flows/booking.py`; restaurado a `HEAD`, idéntico a `main`. El bug
  de la otra rama sigue sin arreglar allí: es de quien trabaje en ella.
- **19-sep · Imports de los tests.** Versión 2 del agente ciego: `from clinic_catalog
  import load_base_catalog, load_catalog` y `from dates import closure_days`. Válidos en
  `main` siempre que el re-export incluya `load_base_catalog`. Ninguna comprobación cambió.
- **19-sep · El motor se implementó en `feat/backend`** (decisión de Adolfo: "arranca el
  motor aquí"), no en `main`. Son el mismo código; `main` solo tiene encima el merge del
  PR #8. Baseline medido allí: 276 passed. Después: **293 passed** (276 + 10 aceptación +
  7 andamiaje). Antes de cada tarea conviene comprobar la rama
  (`git branch --show-current`): el directorio lo comparten varias sesiones y GitHub
  Desktop mueve los cambios sin commitear al cambiar de rama.
- **19-sep · Desviación: la explicación para "Why" hacía falta en dos salidas, no en una.**
  El plan solo contemplaba el resultado de la oferta. Cuando el aviso deja al médico
  pedido sin huecos, `get_earliest_slot` sale antes por `provider_unavailable` — que es
  justo el caso típico de esta feature. La `justification` se añade en ambas, desde
  `_notice_justification`. Lo destapó el test 9, que falló por eso.
- **19-sep · Desviación: el tono necesitó `current_role_message()`.** Los nodos se
  construyen sin fecha a mano, así que `role_message(today)` (la que fijan los tests) se
  acompaña de un envoltorio sin argumentos que lee la fecha de Madrid. `ROLE_MESSAGE`
  sigue existiendo y `handlers.py` lo sigue reexportando.
- **19-sep · Elección mecánica: un seguro retirado se inyecta como `refused_insurers`**,
  con la forma exacta que publica la API, así que `accepts_plan` no sabe que existen los
  avisos. Un médico que recepción marca como que dejó DKV es indistinguible de uno que la
  clínica publica rechazándolo.
- **19-sep · Comprobado a mano, fuera de los tests** (`AUDIT_DIR` y fichero en temporal):
  `GET /notices` sin fichero devuelve la estructura vacía; `PUT` válido 200; `PUT` con
  `PR99` devuelve 422 con `{"id": "x", "error": "unknown provider 'PR99'"}` y no escribe;
  el tono se cuela en el mensaje de rol; pedir a Sáez ausente da `provider_unavailable`
  con la justificación que nombra `n1`; la siguiente búsqueda ofrece a Ortiz y lleva el
  aviso hablado; el trail contiene `notices_active` y `notice_applied`. Sin fichero,
  `load_catalog() is load_base_catalog()` y el mensaje de rol es idéntico.
- **19-sep · Desviación: el criterio del panel "Why" se prueba a medias.** El test
  comprueba que la `justification` está en el resultado de la herramienta; que llegue a
  pantalla lo hace `trace_tool` (código existente de la consola) y no se prueba aquí.
- **19-sep · Los tests de aceptación van a `server/tests/acceptance/test_reception_notices.py`**
  (lo que congela el hook), no a `tests/unit/` como decían los VALIDATE de las tareas.
