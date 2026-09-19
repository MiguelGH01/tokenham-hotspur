# Review — simple-booking (todo el agente)

Fecha: 2026-09-19. Alcance: **todo el agente**, no solo el último cambio. Base del diff:
`86a768d` (último commit antes de que existiera el agente, scaffold de pizzas) → árbol de
trabajo actual. 27 archivos, 1563 líneas. Revisor: un `code-reviewer` a ciegas (plan sin
`## Notes` + diff; sin acceso a esta conversación ni a logs). Árbol idéntico al baseline tras
la revisión: el revisor no tocó nada.

## Resumen

El diseño es sólido: estado por llamada sin globales, los ids/fechas/POSTs los decide Python y
nunca el modelo, la garantía de envío está aislada y testeada. Los fallos están en los bordes.
**Lo primero que arreglar: `submission.py:29` marca `_flushed = True` ANTES del POST**, así
que un fallo de red al colgar deja la llamada sin envío y el segundo `flush()` ya no hace nada.

## Antes de revisar: tests y evals

**Tests: 24/24 pasan** (`uv run pytest`, 0,7 s).

**Evals: 6/24 pasan** en bruto. Corridos contra un bot de eval propio en el puerto 7861 (el
bot de telefonía del usuario seguía vivo en el 7860 y `make evals` habría jugado contra él).

| Problema | Pasan | Nota |
|---|---|---|
| PR-01 (lo único que cubre el plan) | 3/5 sin reloj → **4/5 con `CALL_CLOCK_OVERRIDE=2026-09-18`** | `josefa` fallaba solo por la fecha |
| PR-03 | 1/5 | fuera de alcance del plan: no hay búsqueda por médico |
| PR-04 | 0/4 | fuera de alcance: no hay registro de paciente nuevo |
| PR-05 | 2/5 | 1 de los 3 fallos es de fecha (mismo motivo que `josefa`) |
| PR-06 | 0/5 | fuera de alcance: no hay reglas de póliza/derivación |

`simple_booking_identify_fail` falla en las dos pasadas: el tercer `search_patient` nunca
llega y no hay respuesta en 60 s. En el log del bot: `DeepgramSTTService error: 1011 Deepgram
did not receive audio data` (en modo texto no entra audio y Deepgram cierra por inactividad),
un `ErrorFrame`, y después silencio. **Causa no aislada**: puede ser artefacto del modo texto
o un fallo real del tercer intento. En una repetición el cliente de eval se quedó colgado
>18 min sin dar timeout.

## Hallazgos

Filtro adversarial aplicado: cada hallazgo se intentó refutar contra el código, los docs y,
donde los había, datos reales de las llamadas.

### ALTO

**H1 — Un POST fallido no se reintenta nunca** · `submission.py:29-31` · CONFIRMADO
`self._flushed = True` va antes de `post_submission`. Escenario: el llamante confirma, la
plataforma cierra el socket, `on_client_disconnected → flush()`, `ClinicClient` agota sus 2
intentos por un corte de red, la excepción se traga en la línea 42. El `finally: flush()` de
`bot.py` vuelve sin hacer nada. Resultado: cero envíos con ~28 s de ventana sin usar
(`CR-window`), y sin envío el caso falla siempre (`CR-silence-fails`).
Refutación: la Decisión 5 del plan acepta rendirse, pero dice "se deja `pending` como
estaba", lo que solo tiene sentido si algo puede enviarlo después; nada puede. El test de
idempotencia solo cubre el caso de éxito. Un POST duplicado devuelve `409`, que
`clinic_client.py` ya trata como éxito, así que mover la marca después del POST es seguro.

**H2 — `NO_ACTION` lleva siempre `patient_not_found`, también con el paciente encontrado**
`submission.py:5,25` + `flow/tools.py:106-107` · CONFIRMADO
`set_no_action()` no tiene ningún llamante en producción (solo un test). `get_earliest_slot`
devuelve `no_slots` sin anotar nada. No cuesta puntos en PR-01 (lo correcto es un `BOOK`), sí
en cuanto la respuesta correcta sea una negativa: `SC-membership` compara la acción entera,
motivo incluido.

### MEDIO

**M1 — Los evals no fijan el reloj que sus propias cabeceras dan por hecho** · CONFIRMADO CON DATOS
`bot.py:76-78`, cabeceras de `evals/PR-05/*`, `Makefile`. `CALL_CLOCK_OVERRIDE` no está en
`.env`, ni en el Makefile, y en `.env.example` está comentado. Demostrado en esta revisión:
`josefa` falla sin el reloj ("does not contain 'Ortiz'") y pasa con él;
`when_exactly_josefa_tomorrow` falla porque el bot ofrece el lunes 21 en vez del sábado 19.
Riesgo espejo: si alguien lo exporta para los evals y se le olvida, todas las llamadas
puntuadas calculan desde el 18-sep y reservan en el pasado. Es una variable "solo tests" con
alcance de producción y sin guarda.

**M2 — La imagen Docker no puede cargar `clinic.json`** · `Dockerfile:19-21` + `clinic/clinic_catalog.py:7` · CONFIRMADO
En la imagen el módulo queda en `/app/clinic/`, la ruta resuelve a `/clinic.json`, y el
archivo está en la raíz del repo, fuera del contexto de build. `import bot` lanza
`FileNotFoundError`. Medio y no alto porque el camino puntuado es `make tunnel` + bot local;
`pcc-deploy.toml` sigue llamándose `pipecat-quickstart`.

**M3 — El camino puntuado depende de que el harness se detecte como `twilio`, sin guarda ni log** · `bot.py:62-66, 217-218, 239-248`
Falta de guarda: CONFIRMADA. Disparo: PLAUSIBLE. Si la detección devolviera otra cosa,
fallarían tres cosas a la vez y en silencio: `start_flow()` no se llama (el bot no habla y la
plataforma corta), las frecuencias se quedan en 16 kHz, y `_call_id` inventa un `local-…`.
Refutación: en los dos Run All todas las llamadas registraron `Parsed - Type: twilio`, y el
contrato está congelado para el evento. Por eso baja de ALTO (lo que propuso el revisor) a
MEDIO. El arreglo son tres líneas.

**M4 — `pick_offer` se fía de cualquier hueco que liste la API** · `booking.py:38-43` · PLAUSIBLE
`LIVE-01` (docs) dice que el 12-oct la API lista huecos con `payable_with: []`, y
`FR-closed-day-roll` es *must*: no reservar día cerrado aunque aparezca. Inalcanzable desde
una llamada del 19-sep (ventana de 14 días) y PR-05/06 están fuera del plan. Una línea lo
cubre: descartar huecos cuyo `payable_with` no incluya la aseguradora del paciente.

**M5 — El desempate elige al profesional MÁS cargado, y el test afirma lo contrario** · `booking.py:36,47` + `test_booking_compile.py:60` · CONFIRMADO
El diseño (`simple_booking_step_by_step.md` §7) pide "menor proporción de huecos ocupados".
`load` cuenta huecos **libres** y `min()` elige al que menos tiene, o sea el más ocupado.
`test_tie_prefers_less_loaded_provider` codifica la inversión. No cuesta puntos (cualquier
empatado es válido, `SC-multi-ok`); lo mira el jurado.

**M6 — Los tests no llegan al código que más puntos puede perder** · CONFIRMADO
Sin tests: `search_window`, `ClinicClient._request` (409, reintento 5xx, no-reintento 4xx),
y ningún handler de `flow/tools.py` (contador de 3 intentos, filtro `exact`, rama
`misheard_id`). `test_booking_compile.py` usa diccionarios de 2-4 huecos hechos a mano, no las
respuestas grabadas que pedía el contrato.

**M7 — Un `httpx.AsyncClient` nuevo por petición** · `clinic/clinic_client.py:30-32` · CONFIRMADO (sobrecoste, no bug)
Handshake TLS completo en cada `/directory` y `/availability`, en el camino donde el
llamante oye silencio, ×10 llamadas concurrentes. Un cliente por `ClinicClient` (uno por
llamada) lo evita sin compartir entre event loops.

**M8 — Consultar `/directory` con el nombre transcrito Y el id puede excluir al paciente** · `flow/tools.py:61,65` · PLAUSIBLE
Contra-evidencia real: en el Run All #1, `"Chloe Robert Smith"` (STT se comió la "s") + NIE
correcto sí encontró a la paciente. No se ha visto el caso "apellido destrozado + id
correcto". El handler ya re-verifica el id en local, así que quitar `name` de la consulta no
pierde nada.

### BAJO

- **L1** `bot.py:177` — `submission._client`: se mete en un atributo privado para compartir el cliente.
- **L2** `flow/tools.py:71-73` — un `lookup_failed` no cuenta como intento: con la API caída el nodo identify gira hasta el tope de 3 min.
- **L3** `bot.py:192-196` — la re-pregunta no tiene tope: un llamante mudo oye la misma frase cada ~16 s durante 3 min.
- **L4** `booking.py:13-15` — ventana fija de +13 días sin recortar al fin de calendario (`2026-10-16`, que está en `clinic.json` y no se lee). Irrelevante este fin de semana.
- **L5** `bot.py:77-78` — un override sin offset da un datetime naive y asume la zona del host.
- **L6** `Makefile:26-41` — `|| true` en cada escenario: `make evals` siempre sale con 0, no sirve de gate. Usa el puerto 7860 fijo y mata por patrón (`pkill -f`), que alcanza a evals de otras sesiones. `make run-eval` sale en `help` y no tiene receta.
- **L7** `bot.py:1` — el docstring aún remite a `handlers.py`; el grafo está en `flow/`.
- **L8** `flow/tools.py:109-114` — `next(...)` sin default; hoy inalcanzable.
- **L9** Idioma: `ROLE_MESSAGE` dice "answer in the caller's language", pero saludo, voz TTS (`aura-2-helena-en`), re-pregunta y fechas (`strftime` en inglés) son solo inglés. No puntúa (`SC-conversation-unscored`); castellano sí está en alcance.

### Revisado y limpio

Secretos (ninguno en archivos versionados; `.gitignore` cubre `.env` y logs de eval) ·
inyección (DNI validado por regex+checksum, teléfono reducido a dígitos, todo va como query
params codificados; el LLM nunca aporta un id que llegue a un POST) · doble acceso a
`call_data` (`CallData` admite atributo y clave) · `gateway_llm.py` (sigue el mismo patrón que
usa Pipecat en `services/nvidia/llm.py`) · aislamiento por llamada (`CR-concurrency`) · forma
de los cuerpos de envío y rutas contra el snapshot de OpenAPI · **tests de aceptación: creados
en un solo commit (`d4c7640`) y nunca editados después — ningún test debilitado.**

## Inventario: por qué está cada cosa y si sobra

| Pieza | Para qué | Por qué existe | Quién la usa | Veredicto |
|---|---|---|---|---|
| `handlers.py` (shim 5 líneas) | reexporta desde `flow` | compatibilidad tras partir el `handlers.py` viejo | **nadie** (verificado por grep) | **QUITAR** + su palabra en el `Dockerfile` |
| `clinic_catalog.py` (shim 5 líneas) | reexporta desde `clinic/` | misma partición | solo `flow/tools.py:15` | **SIMPLIFICAR**: cambiar ese import y borrar |
| extra `rnnoise` en `pyproject.toml` | — | resto del filtro que dejaba sordo al bot | **nadie** (verificado) | **QUITAR** |
| `Dockerfile` + `pcc-deploy.toml` + dep `pipecatcloud` | deploy a Pipecat Cloud | venía del scaffold | nada del camino puntuado | **QUITAR o arreglar**: hoy está roto (M2), y un deploy roto es peor que ninguno |
| `DailyParams` try/except + extra `daily` | saltarse Daily si no importa | un compañero en Windows | `bot()`; el harness nunca marca por Daily | **PREGUNTA**: quitarlo borra ~8 líneas y un modo de fallo, pero rompe `-t daily` a quien lo use |
| `build_llm`: ramas `gemini` / `openai` | proveedor alternativo | Decisión 2 del plan (pedía solo esas dos) | solo se usa `helmcode` | **SIMPLIFICAR**: `helmcode` + un respaldo. Ojo: el respaldo es lo que descarta el gateway si falla |
| `CALL_CLOCK_OVERRIDE` / `_connected_at` | reloj fijable | Decisión 11 del plan | `run_bot`; **no se fija en ningún sitio** | **SIMPLIFICAR**: o se cablea en los evals y se limita al transport `eval`, o se borra (M1) |
| `set_no_action()` | fijar motivo de negativa | interfaz del plan | **nadie** en producción | **CABLEAR**, no quitar (H2) |
| envío de oferta sin confirmar (`set_offer`/`clear_offer`) | si la llamada muere tras ofrecer, enviar esa cita | equivocarse cuesta lo mismo que callar | `flow/tools.py`; 4 tests | **PREGUNTA**: correcto para PR-01. Se invierte cuando abra PR-06: a quien hay que **negar**, ofrecer y auto-enviar convierte un `NO_ACTION` ganable en un `BOOK` seguro-fallido |
| comentarios con medidas ("~8%", "mediana 11s", "9 de 28", "16 de 21") | justifican tres constantes | medidas de los Run All | — | **REDACTAR**: no son verificables desde el repo; el log del Run All #2 ya no existe. Decir de dónde salen |
| temporizador de envío forzado (quitado) | — | tiraba los últimos 30 s | — | **MANTENER quitado**, por un motivo más fuerte que el medido: `CR-record` dice que el registro es *toda* acción aceptada; un `NO_ACTION` temprano + un `BOOK` después da dos acciones y falla |
| `submission.py` | garantía "toda llamada acaba en un POST" | Decisión 10 | `bot.py`, `flow/tools.py`, 2 tests | MANTENER (arreglar H1, H2) |
| `gateway_llm.py` | corta a 4 s un stream mudo y reintenta si aún no hubo salida | el gateway se cuelga a media respuesta | rama `helmcode` | MANTENER: es un workaround de un fallo concreto de un proveedor, no abstracción especulativa |
| `_reprompt` + `user_idle_timeout=16s` | repite el último mensaje tras 16 s de silencio | la plataforma corta a ~30-35 s sin audio del agente | handler `on_user_turn_idle`; 4 tests | MANTENER (valorar L3) |
| `_telephony_transport` | transport Twilio con `auto_hang_up=False` | `create_transport` revienta sin credenciales de Twilio | `bot()` | MANTENER (arreglar M3) |
| `start_flow` + handler RTVI | arrancar el flujo una sola vez | `client-ready` descarta lo encolado antes | sí mismo | MANTENER |
| `booking.py`, `national_id.py`, `clinic/*`, `flow/*`, `eval_judge.py`, `tzdata` | lógica pura, validación, API, grafo, juez de evals | tareas del plan | verificado | MANTENER |

## Adherencia al plan

| Punto | Estado |
|---|---|
| Construido sin tarea en el plan | `gateway_llm.py`, proveedor `helmcode`, `_reprompt`, `_telephony_transport`, oferta sin confirmar, `eval_judge.py`, partición en `flow/` y `clinic/`, 19 evals de PR-03..06. Justificados por lo medido en llamadas reales; no son ítems del plan |
| Construido aunque estaba **fuera de alcance** | **Cambio de TTS Cartesia → Deepgram** (la Decisión 3 decía mantener Cartesia). Defendible, pero es lo que hace morder L9 |
| Fuera de alcance que SÍ se respetó | sin registro, reprogramación, escalado ni segunda póliza; `flow.yaml` sin tocar; sin LangChain |
| Contrato: temporizador de 2m30 + su test | **no construido a propósito**, y el test nunca existió. El ítem está obsoleto: tacharlo del plan en vez de dejarlo como incumplido |
| Contrato: reloj al 18-sep + respuestas grabadas como fixture | **ninguna de las dos mitades se cumple** (M1, M6). Único aflojamiento real del contrato |
| Decisión 5 (reintentar y dejar `pending`) | cumplida en la letra, minada por H1 |
| Decisión 6 (3 intentos) | cumplida, salvo L2 |
| §7 del diseño (desempate) | implementado al revés (M5) |
| Tests de `tests/acceptance/` editados o debilitados | **ninguno** |

## Lo que el diff no puede responder

- Si algún POST real ha devuelto `200`: los evals usan ids `local-…` que dan `404`, así que ejercitan la composición pero nunca un envío aceptado.
- Si `/availability` con `patient_id` ya filtra por póliza y día cerrado (decide si M4 es teórico). Un sondeo al 12-oct lo resuelve.
- Por qué falla `identify_fail`: artefacto del modo texto (Deepgram cierra por falta de audio) o fallo real.
- Si un `ErrorFrame` del STT mata el pipeline en una llamada real (ahí el audio no para de llegar, así que el cierre por inactividad no debería darse).
- Si el LLM llama de verdad a `revise_search` cuando el llamante rechaza: es lo único que impide auto-enviar una oferta rechazada.
- **Nada de lo cambiado esta noche (vigilante de silencio, oferta pendiente, re-pregunta completa) se ha visto funcionar en una llamada real.** Solo tests e imports.
