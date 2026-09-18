# API de la plataforma Prosper (El Turno)

Referencia de trabajo sacada de `openapi.json` (versión 0.1.0). Los nombres de campos son literales. Lo marcado como "por confirmar" necesita una respuesta real con nuestra clave.

Para reglas de negocio, scoring y los 18 problemas ver `docs/prosper-track-reference.md` (sacado de la web de docs, no del schema). Esta hoja es la referencia de campos; esa es la de reglas. Discrepancia conocida: la web dice que las coordenadas de sede para el problema 15 están en `/availability`, pero ese schema no las trae — están en `/locations` (ver más abajo).

## Generalidades

- Base: la URL que da la organización en el registro. Todas las rutas cuelgan de `/api/v1`.
- Autenticación: cabecera `X-Api-Key` en todo salvo `/health` y el propio esquema. Clave ausente, inválida o revocada: `403 {"detail":"Invalid API key"}`.
- Todo lo de la clínica es GET y de solo lectura. Lo único que se escribe es `/submit/*`.
- JSON en snake_case. El camelCase solo aparece en los mensajes del WebSocket.
- Los ids se comparan exactos. Las fechas con hora llevan zona horaria explícita.

## 1. Consultas sobre quien llama

### GET /directory

Busca pacientes. Todos los parámetros son opcionales y se combinan.

| Parámetro | Tipo | Comportamiento |
|---|---|---|
| `name` | texto | Nombre completo o parcial, comparado tras normalizar. Es el único campo aproximado |
| `national_id` | texto | DNI o NIE tal como se dicta. Filtro exacto |
| `phone` | texto | Tal como llega. Se comparan los nueve dígitos nacionales, así que `+34…`, `0034…` y sin prefijo son la misma consulta. Filtro exacto |
| `date_of_birth` | fecha ISO | Para separar tocayos. Filtro exacto |

Un campo exacto que no coincide excluye al paciente, no lo baja en el ranking.

Respuesta: `{"matches": [ … ]}`, cada elemento con:

| Campo | Tipo | Para qué sirve |
|---|---|---|
| `patient_id` | texto (`P00042`) | El id que se envía en `book` |
| `given_name`, `first_surname`, `second_surname` | texto | Nombre legal de la ficha |
| `national_id` | texto | Para comparar en local con lo dictado. Nunca se lee en voz alta |
| `date_of_birth` | fecha | Edad, y separar tocayos |
| `phone` | texto | Igual que el DNI: se compara, no se lee |
| `sex` | texto | |
| `has_visited_before` | booleano | Decide el tipo de cita junto con la especialidad |
| `insurer` | texto | La aseguradora de la ficha. Es la `policy_id` por defecto |
| `referrals` | lista de texto | Volantes que tiene el paciente (por confirmar si son ids de especialidad) |
| `note` | texto | Nota de recepción. Material para el jurado, nunca una preferencia de agenda |
| `match_score` | número | Puntuación de la coincidencia |
| `matched_fields` | lista de texto | Qué campos coincidieron |

### GET /patients/{patient_id}/appointments

| Parámetro | Valores | |
|---|---|---|
| `when` | `upcoming` (por defecto), `past`, `all` | Solo una cita `upcoming` se puede cancelar o mover |

Respuesta: `{"appointments": [ … ]}`, de la más temprana a la más tardía, cada una con `appointment_id`, `patient_id`, `provider_id`, `location_id`, `appointment_type_id`, `start_time`, `duration_minutes`.

Es la única fuente de un `appointment_id`. Las pasadas (2024 y 2025, hasta ocho) sirven para hablar con el paciente, no para actuar sobre ellas.

### GET /availability

| Parámetro | Obligatorio | Comportamiento |
|---|---|---|
| `date_from` | sí | Primer día, incluido |
| `date_to` | sí | Último día, incluido. Más de 14 días de ventana o fuera del calendario: `422` |
| `provider_id` | uno de los dos | Solo los huecos de ese profesional |
| `specialty_id` | uno de los dos | Solo profesionales de esa especialidad |
| `location_id` | no | Solo esa sede |
| `patient_id` | no, pero pasarlo siempre | Aplica edad, historial y aseguradoras del paciente. Un hueco que sale aquí es uno que puede coger |
| `insurer` | no, repetible | Solo huecos que cubren esas aseguradoras. Sin él, se usa la única aseguradora de la ficha. Es la forma de consultar una segunda póliza |

Respuesta:

| Campo | Contenido |
|---|---|
| `providers` | Profesionales considerados: `id`, `name`, `specialty_id`, `languages`, `accepted_insurers`, `locations`, `on_leave_until` |
| `appointment_type` | El tipo correcto para ese paciente y especialidad: `id`, `name`, `duration_minutes`, `new_patient_requirement`, `guidance` |
| `slots` | Huecos: `provider_id`, `provider_name`, `specialty_id`, `location_id`, `appointment_type_id`, `start_time`, `duration_minutes`, `payable_with` (aseguradoras con las que se puede pagar ese hueco) |
| `blocked` | Profesionales frenados por una regla: `provider_id`, `restriction` |

Lectura: `slots` vacío con `blocked` vacío es agenda llena. `slots` vacío con `blocked` relleno es una regla. La respuesta lista también huecos de hoy, que nunca valen: el filtro desde mañana es nuestro.

Por confirmar → confirmado por la web de docs (página Scoring/contract): los primeros once valores de `reason` "mirror the clinic's own restrictions one-for-one" — se corresponden uno a uno con `restriction`. Sigue sin confirmar el formato exacto de `referrals` en `/directory` (¿ids de especialidad?).

## 2. Catálogo

Sin parámetros, no cambia durante el evento. Se pide una vez al arrancar.

### GET /clinic

Todo en una llamada:

| Campo | Contenido |
|---|---|
| `clinic_name`, `patient_count` | |
| `calendar` | `starts`, `ends`, `max_span_days`, `slot_minutes`, `closure_days`, `appointment_count` |
| `restrictions` | Lista de `id`, `title`, `explanation`: las reglas fijas y su motivo de rechazo |
| `providers` | Ver abajo |
| `specialties` | Ver abajo |
| `appointment_types` | Ver abajo |
| `locations` | Ver abajo |
| `plans` | Ver abajo |

### Profesional (también en GET /providers)

`id`, `name`, `specialty_id`, `specialty_name`, `languages`, `appointment_type_names`, `location_names`, `schedules` (por sede: `location_id`, `location_name`, `days` con `weekday` e `intervals`), `accepted_insurers` y `refused_insurers` (cada uno `id` y `name`), `leave` (`start`, `end`, `reason`) o nulo.

### Sede (también en GET /locations)

`id`, `name`, `address`, `latitude`, `longitude`, `hours` (`weekday`, `intervals`), `provider_names`, `covered_by`, `not_covered_by`.

Las coordenadas para el problema 15 están aquí. La documentación dice que vienen en `/availability`, pero su esquema no las trae.

### Especialidad (también en GET /specialties)

`id`, `name`, `min_age_months`, `max_age_months` (o nulo), `referral_required`, `provider_names`, `covered_by`, `not_covered_by`. Los `id` son los que acepta `availability?specialty_id=`.

### Tipo de cita (también en GET /appointment-types)

`id`, `name`, `duration_minutes`, `new_patient_requirement`, `guidance`, `provider_names`, `specialty_id` (o nulo en los universales), `specialty_name`.

### Aseguradora (también en GET /insurance-plans)

`id`, `name`, `covered_specialty_names`, `uncovered_specialty_names`, `covered_location_names`, `uncovered_location_names`, `accepted_by`, `refused_by`, `holders`.

Ids válidos: `sanitas`, `adeslas`, `dkv`, `asisa`, `mapfre`, `caser`, `cigna`, `axa`, `nueva_mutua`, `privado`.

Ojo: el catálogo cruza entidades por nombre (`provider_names`, `location_names`, `covered_specialty_names`) y no por id. La caché necesita índices de nombre a id.

## 3. Envío

Un POST por acción. Todas llevan `call_id`, que es exactamente `start.callSid`.

| Ruta | Campos además de `call_id` |
|---|---|
| `POST /submit/register` | `given_name`, `first_surname`, `second_surname`, `national_id`, `date_of_birth`, `phone`, `email`, `insurer` |
| `POST /submit/book` | `patient_id`, `provider_id`, `location_id`, `appointment_type_id`, `slot`, `policy_id` |
| `POST /submit/reschedule` | `appointment_id`, `provider_id`, `location_id`, `slot`, `policy_id` |
| `POST /submit/cancel` | `appointment_id` |
| `POST /submit/no-action` | `reason` |
| `POST /submit/escalate` | `reason` |

Todos los campos son obligatorios. `slot` es fecha y hora con zona horaria (`2026-09-24T16:30:00+02:00`) y se compara al minuto en Europe/Madrid. `insurer` y `policy_id` son del enum de aseguradoras. En `register`, un `national_id` cuya letra no cuadra con los dígitos devuelve `422`.

Valores de `reason`:

- Reglas de la clínica: `not_eligible_age`, `referral_required`, `provider_not_in_network`, `specialty_not_covered`, `location_not_covered`, `insurer_referral_required`, `allowance_exhausted`, `provider_on_leave`, `location_hours`, `type_not_offered`, `patient_history`.
- Otros finales: `no_availability`, `clinic_closed`, `patient_not_found`, `provider_not_found`, `caller_not_authorised`, `out_of_scope`, `medical_emergency`.

Respuesta `200`: `call_id`, `received_at` y `record.actions`, que es la lista de todas las acciones aceptadas para esa llamada hasta ahora. Cada una lleva su verbo (`REGISTER`, `BOOK`, `RESCHEDULE`, `CANCEL`, `NO_ACTION`, `ESCALATE`) y sus campos. Un `REGISTER` anida los suyos bajo `new_patient`.

| Código | Significado |
|---|---|
| 200 | Aceptada. No significa que el caso pase |
| 404 | `call_id` que la plataforma no abrió, o de otro equipo |
| 409 | Acción idéntica ya aceptada. Es un reintento, se trata como éxito |
| 410 | Ventana cerrada (más de 30 segundos desde que cerraron su socket) |
| 422 | Cuerpo mal formado. No se registra nada |

### GET /submissions

`limit` de 1 a 200 (50 por defecto). Devuelve `{"submissions": [ … ]}` con `call_id`, `record` y `received_at` de nuestros envíos más recientes. Sirve para verificar y para el arnés.

## 4. Otros

`GET /health`: sin clave, comprobación de vida.
