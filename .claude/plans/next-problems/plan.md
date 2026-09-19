# PR-06, PR-05, PR-04 — qué piden, qué falta y en qué orden

Estado a 19 sep: PR-01 y PR-03 hechos (evals + llamada de práctica de Josefa OK).
Abiertos y sin hacer: PR-04, PR-05, PR-06. PR-02 no puntúa (prueba de 5/10/20 llamadas a la vez).
PR-07 a PR-18 aún no están abiertos.

## Orden: PR-06 → PR-05 → PR-04

| PR | Peso | Coste | Por qué en ese puesto |
|---|---|---|---|
| PR-06 | **3** | Bajo | El que más puntúa y el más barato: la API ya dice qué regla aplica. 2 ramas de código. |
| PR-05 | 2 | Medio | 3 de 5 casos ya pasan. Faltan 2 piezas (fecha concreta + saltar al siguiente día abierto). |
| PR-04 | 2 | Alto | Verbo nuevo (REGISTER), nodo nuevo, 8 campos dictados por voz. El más frágil al oído (STT). |

---

## PR-06 — Las Reglas (peso 3)

**Qué pide.** El paciente pide algo que una regla de la clínica impide, y no sabe cuál. Hay dos
respuestas correctas según el caso: o no reservar diciendo la regla exacta, o reservar otra cosa.

| Caso | Pide | Regla | Respuesta correcta |
|---|---|---|---|
| Teresa | Dermatología | No tiene volante | `NO_ACTION referral_required` |
| Josefa S. | Ginecología | Adeslas no la cubre | `NO_ACTION specialty_not_covered` |
| Gloria | Dra. Iglesias (derma) | Iglesias no acepta DKV | **Redirigir**: `BOOK` con Dr. Vilar |
| Sonia | "Médico de cabecera" para su hija de 9 años | Cabecera es desde 14 años | **Redirigir**: `BOOK` en pediatría |
| Ignacio | Dermatología, con volante | Ninguna (caso control) | `BOOK` normal |

**Hecho clave, sondeado en vivo el 19 sep:** `/availability` devuelve en `blocked` la regla exacta:

```
Teresa    slots=0   blocked = referral_required        (PR05, PR12)
Josefa S. slots=0   blocked = specialty_not_covered    (PR11)
Gloria    slots=PR12 blocked = provider_not_in_network (PR05)   <- Vilar sí tiene huecos
Sonia     slots=0   blocked = not_eligible_age         (los 4 de cabecera)
Sonia + paediatrics -> PR08 norte paediatric_review, el aceptado
```

**Qué ya funciona.** Teresa y Josefa S. ya envían el motivo correcto (tools.py lee `blocked`).
Ignacio ya pasa. Falta que el bot lo *explique* de palabra, que es lo que mira el juez del eval.

**Qué falta.**

1. `tools.py` `get_earliest_slot`: si el médico nombrado aparece en `blocked` → soltar el médico,
   mantener la especialidad (igual que la rama `on_leave`). `note = "not_in_network"`. (Gloria)
2. `tools.py`: si no hay huecos y todo `blocked` es `not_eligible_age` y pidió `general_practice`
   → repetir la búsqueda con `paediatrics`. `note = "age_redirect"`. (Sonia). Una llamada API más.
3. `tools.py`: en `no_slots`, devolver también `reason` al LLM para que pueda explicarlo.
4. `nodes.py`: dos entradas nuevas en `OFFER_NOTES`; en `find_slot`, qué decir por cada `reason`
   (volante, plan no cubre) y ofrecer `end_without_booking`.
5. Tests: una por rama en `test_named_doctor.py` o archivo nuevo. Evals: los 5 de `evals/PR-06/`.

**Ojo.** Sonia habla de "mi hija": la paciente del expediente es la niña, P00009. Con el DNI que
dicte la madre se identifica a la niña directamente; no hay que construir nada de "terceros".

---

## PR-05 — Cuándo Exactamente (peso 2)

**Qué pide.** El paciente dice la fecha de forma coloquial y hay que acertar el día exacto contando
desde el momento de la llamada. Vocabulario cerrado: `tomorrow`, `the day after tomorrow`,
`a week from today`, `in a fortnight`, `on Saturday morning`, `this coming <día>`,
`first thing <día>` (= mañana), `<día> afternoon`, y una fecha con nombre (`Monday the twelfth of October`).

**Trampas.** Solo Centro abre sábado. Nada abre domingo. 12 oct cerrado aunque la API liste huecos.
Si el día pedido está cerrado → **el siguiente día abierto** que cumpla el resto (sede, mañana/tarde).
Nunca `NO_ACTION`, nunca el día cerrado. "Tomorrow" puede ser sábado.

**Qué ya funciona (3 de 5 evals).** `tomorrow`, `this coming Thursday`, `Saturday morning`: salen
bien porque el filtro de día de la semana ya coge el primero *después* del día de la llamada.

**Qué falta (2 evals).**

| Caso | Hoy pasa esto | Hace falta |
|---|---|---|
| Chloe, "this coming Sunday" en Centro | domingo → 0 huecos → `no_slots` | Saltar al siguiente día abierto |
| Amelia, "Monday the twelfth of October" | No existe forma de pedir una fecha | Parámetro de fecha + ventana que llegue hasta ahí + salto |

1. `tools.py` schema: parámetro `day` con enum cerrado (`tomorrow`, `day_after_tomorrow`,
   `in_a_week`, `in_a_fortnight`) y parámetro `date` (`MM-DD`) para fechas con nombre. El LLM solo
   clasifica la frase; **la fecha la calcula el código** desde `connected_at`.
2. `booking.py`: `pick_offer(..., not_before=<fecha>)`: el primer hueco en o después de esa fecha
   que cumpla sede y mañana/tarde. El salto al siguiente día abierto sale solo: si ese día no hay
   huecos (domingo) o está en `closed_days` (12 oct), el `min()` cae en el siguiente.
3. `tools.py`: día de la semana sin huecos (domingo) → mismo salto: buscar desde esa fecha sin
   exigir el día. `note = "closed_day"`.
4. `booking.py` `search_window`: si hay fecha objetivo, la ventana de 14 días empieza en ella
   (la API no deja pedir más de 14 días; el 12 oct no entra en la ventana por defecto).
5. `nodes.py`: `OFFER_NOTES["closed_day"]` ("ese día la clínica cierra, el siguiente abierto es…").
6. Tests de `pick_offer` con `not_before`. Evals: `evals/PR-05/` (ya llevan su `# clock:`).

**Ojo.** Estos evals dependen de la fecha por definición: llevan reloj fijado y está bien así.

---

## PR-04 — El Paciente Nuevo (peso 2)

**Qué pide.** Llama alguien que no está en el fichero y solo quiere darse de alta. Respuesta:
`REGISTER` con 8 datos, todos correctos. Si además se reserva algo, el caso falla.

Campos (`POST /v1/submit/register`, cuerpo plano): `given_name`, `first_surname`,
`second_surname`, `national_id`, `date_of_birth` (`YYYY-MM-DD`), `phone`, `email`, `insurer`.

**Trampas.**
- Dos apellidos siempre. Tildes y ñ: se envía la grafía legal.
- DNI y NIE con letra de control (ya lo validamos). El email **no** tiene dígito de control:
  no "arreglarlo". Locales con puntos, guiones bajos y números.
- "Mapfre Salud" → `mapfre`. Aseguradora = id del catálogo (`clinic.json` → `plans`).
- Homónimos: hay una Natalia y un Sergio en el fichero con otro DNI. Buscar por nombre no es
  identidad. **Esto ya lo cumplimos**: `search_patient` exige coincidencia exacta de DNI/teléfono.
- Si el bot ofrece cita, el paciente la rechaza. No reservar.

**Qué ya existe.** La ruta `REGISTER` en `clinic_client.py`. La validación de DNI/NIE. El nodo `close`.

**Qué falta.**

1. `submission.py`: `set_register(fields)`.
2. `tools.py` `search_patient`: con un identificador **válido** y 0 resultados, devolver
   `not_found` con pista `may_be_new`; el prompt pregunta "¿es la primera vez que viene?".
   Nueva herramienta de transición `start_registration` → nodo `register`.
3. `tools.py` `register_patient(given_name, first_surname, second_surname, national_id,
   date_of_birth, phone, email, insurer)`: valida en código (letra del DNI, fecha ISO, 9 dígitos
   de teléfono, forma de email, aseguradora en enum), normaliza (email en minúsculas sin espacios)
   y deja el `REGISTER` pendiente. Si algo no valida, devuelve qué campo repetir.
4. `nodes.py` nodo `register`: pedir los datos de pocos en pocos, **leerlos de vuelta** antes de
   enviar (es una prueba de oído), y cerrar sin ofrecer cita. Nodo final con `flush` + colgar.
5. Tests de validación y normalización. Evals: `evals/PR-04/` (4).

**Riesgo.** Es el PR que más depende del STT (emails y apellidos dictados). Los evals de texto no
lo prueban: hace falta llamada de práctica. Por eso va el último.

---

## Pendientes que no son de ningún PR

- `identify_fail`: arreglado con prompt (19 sep). No es garantía; el contador en código se descartó
  porque choca con el dictado del DNI a trozos.
- Bot solo en inglés aunque el prompt diga lo contrario (PR-11, no abierto).
- Con Gemini no actúan el corte de stream colgado ni el log de respuestas (viven en `gateway_llm.py`).
