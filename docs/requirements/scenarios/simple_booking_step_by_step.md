Feature plan: problema 1, The Simple Booking

Base del agente. Todo lo que se construya aquí (estado, tools, envío) lo reutilizan el resto de problemas; no se reescribe, se amplía.

1. El caso

Paciente ya en ficha. Pide la primera cita disponible en una especialidad. Puede añadir sede, día de la semana o franja ("por la mañana" es antes de las 14:00, "por la tarde" desde las 14:00). "Lo antes posible" es desde el día después de la llamada, nunca el mismo día. El tipo de cita lo decide la ficha (has_visited_before), nunca lo que pida el llamante. Si varios profesionales empatan en el primer hueco, cualquiera de ellos es correcto.

Respuesta aceptada: BOOK.

Fuera de alcance en este plan, aunque el mismo flujo los soportará más adelante: negociar cuando no hay hueco (problema 7), aseguradora que no cubre (problema 6), paciente no encontrado más allá del reintento simple (problema 4), llamante que no es el paciente (problema 9). Aquí se construye el camino feliz y los puntos donde esos problemas van a enganchar.

2. Estado de la llamada

Un objeto por call_id, vivo en el proceso del servidor, no dentro del contexto del LLM. Es lo único que lee el manejador de envío al colgar.

Campo	Contenido	Cuándo se rellena
call_id	start.callSid	Al abrir el socket
from_number	E.164 o ausente	Al abrir el socket
connected_at	hora en Europe/Madrid	Al abrir el socket
patient	patient_id, has_visited_before, insurer	Al identificar
offers	diccionario de propuestas de hueco con todos sus ids	Al consultar disponibilidad
pending_submission	la acción que se enviaría ahora mismo	Desde el principio (con un valor por defecto) y actualizado en cada paso

pending_submission empieza en NO_ACTION(patient_not_found). Es la garantía de que, si la llamada se corta en cualquier punto, sale algo coherente con lo que se sabe hasta ese momento.

3. Tools que ve el LLM

Tres, con datos suficientes para conversar y ninguno de los que exige /submit. Ningún DNI, teléfono ni id de la API aparece en lo que el LLM lee o escribe.

search_patient(stated_name: string, id_type: "national_id" | "phone", id_value: string)
  -> {status: "found", patient_summary: string}
   | {status: "not_found"}
   | {status: "misheard_id"}      # la letra del DNI no cuadra con los dígitos

get_earliest_slot(specialty: enum[general_practice, paediatrics, dermatology,
                                   orthopaedics, gynaecology, physiotherapy],
                   site: enum[centro, norte, sur] | null,
                   weekday: enum[monday..sunday] | null,
                   part_of_day: "morning" | "afternoon" | null)
  -> {status: "offer", offer_id: string, summary: string}   # "Dr. Sáez, Sur, sábado 19 a las 9:15"
   | {status: "no_slots"}

confirm_offer(offer_id: string)
  -> {status: "confirmed"}
   | {status: "expired"}          # el llamante tardó y hay que volver a consultar

El LLM elige la especialidad a partir de lo que diga el llamante ("me duele la espalda" no es este caso, pero "quiero una cita con el médico de cabecera" sí). Ese mapeo de lenguaje a uno de los seis valores del enum es tarea del LLM, porque es exactamente lo que un modelo de lenguaje hace bien y un if/elif de sinónimos haría mal. Fechas, ids, horas y reglas nunca llegan al LLM en bruto.

4. Arquitectura de conversación: Pipecat Flows desde ahora

Decisión, con la razón: aunque el problema 1 cabría en un único nodo con function calling plano, se construye ya como un grafo de Pipecat Flows con nodos separados, porque los problemas siguientes van a necesitar ramas (rechazo, triaje, tercero, corrección) que cuelgan de estos mismos puntos. Reescribir esto en el problema 6 o el 9 sale más caro que montarlo bien ahora.

Nodos para este problema:

Saludo -> Identificar -> Consultar_hueco -> Confirmar -> Despedida

Identificar y Consultar_hueco son los puntos donde colgarán, respectivamente, el problema 4 (paciente nuevo, varias coincidencias) y los problemas 5, 6, 7, 15, 17 (fechas vagas, reglas, agenda llena, sede cercana, segunda póliza). Confirmar es donde cuelga el problema 13 (cambia de opinión antes de colgar).

5. Flujo paso a paso

Marca entre corchetes quién ejecuta cada paso: [INFRA] código de red o del transport, [DET] código determinista nuestro sin LLM, [STT]/[TTS] los proveedores de voz, [LLM] el modelo con sus tools.

[INFRA] Llega start. Se guardan callSid, streamSid y la hora de conexión en Europe/Madrid. Se crea el CallState con el envío pendiente por defecto.
[DET] En paralelo, sin bloquear el saludo: GET /directory?phone=from_number. Si from_number falta, se omite. El resultado queda como candidato, no como identificación; nodo Identificar lo usará si el nombre que dé el llamante coincide.
[DET] Nodo Saludo. Frase fija por [TTS]: "Clínica Arenal, ¿en qué puedo ayudarle?". Sin paso por el LLM.
[STT] Llega la voz del llamante y se transcribe.
[LLM] Nodo Identificar. El modelo extrae nombre e identificador y llama search_patient.
[DET], dentro de la tool:
Si id_type es national_id: se valida la letra contra los dígitos en local. Si no cuadra, se devuelve misheard_id sin tocar la API.
Si cuadra o id_type es phone: GET /directory?name=…&national_id=… o …&phone=….
Un resultado: se guarda en state.patient. Se devuelve patient_summary en lenguaje natural, sin ids sensibles ("le tengo localizada, Marta").
Cero resultados: not_found.
Si el candidato del paso 2 ya coincidía en nombre, se puede saltar la pregunta del identificador y confirmarlo directamente (mejora para el criterio "personal" del jurado, no cambia el resultado del caso).
[LLM] Según la respuesta:
found → pasa a pedir la especialidad y llama get_earliest_slot.
misheard_id o not_found → pide que repita el identificador y vuelve al paso 4, hasta el límite de reintentos (sección 6).
[DET], dentro de get_earliest_slot:
date_from = mañana en Europe/Madrid; date_to = date_from + 13 días.
GET /availability?date_from&date_to&specialty_id=…&patient_id=… con location_id si el llamante dio sede.
Filtro de franja en local si se pidió mañana o tarde.
Se ordena por start_time. En caso de empate en la primera hora, se elige el profesional con menor ocupación relativa (reparto de carga, sección 7).
appointment_type_id es el que ya trae ese slot en la respuesta.
Se guarda la propuesta completa en state.offers[offer_id]: provider_id, location_id, appointment_type_id, slot con zona horaria, policy_id = state.patient.insurer.
Se devuelve solo el resumen legible.
[LLM] Nodo Consultar_hueco dice la propuesta por [TTS].
[STT] El llamante confirma.
[LLM] Nodo Confirmar llama confirm_offer(offer_id).
[DET], dentro de la tool: se copia state.offers[offer_id] a state.pending_submission como BOOK completo con call_id. Todavía no se envía nada por red.
[DET] Nodo Despedida. Frase fija por [TTS].
[INFRA] Llega stop, se cierra el socket de la plataforma. Empiezan los 30 segundos de ventana.
[DET] El manejador de desconexión lee state.pending_submission (ya completo, sin pasar por el LLM) y hace POST /submit/book, con reintentos ante fallo de red; un 409 se trata como éxito.
[INFRA] Se escribe la traza completa (transcripción, cada tool con su entrada y salida, cambios de estado, envío y respuesta) y se destruye la pipeline.

El LLM interviene con efecto real solo dos veces: eligiendo qué tool llamar con qué argumentos de lenguaje, y narrando la propuesta. Nunca calcula una fecha, nunca compone el envío, nunca ve un id de la API.

6. Reintentos y salvaguardas

Decisión tomada, con la razón: dos reintentos por identificación (tres intentos en total) antes de fijar pending_submission en NO_ACTION(patient_not_found) y cerrar. Con el tope de tres minutos por llamada, más de tres intentos consume tiempo sin mejorar la tasa de acierto, y el vocabulario de motivos no permite reportar "me rendí": patient_not_found es lo más cercano a la verdad.

Además, un temporizador propio de dos minutos y medio (antes del tope de tres) fuerza el envío del pending_submission tal como esté en ese momento, sin esperar a que la plataforma corte la llamada por su cuenta. Es casi gratis de construir y protege también a los problemas siguientes, que tendrán conversaciones más largas.

7. Reparto de carga en el empate

El enunciado dice que cualquier profesional empatado en el primer hueco es una respuesta correcta, y las guías de agenda (no puntúan, pero las mira el jurado) piden no ofrecer siempre al mismo médico. Decisión: en un empate, se elige el profesional con menor proporción de huecos ocupados en su especialidad, calculada sobre la respuesta de /availability que ya se tiene, sin llamada adicional a la API. Coste de implementación bajo, y ayuda al criterio del jurado sin arriesgar el punto del leaderboard.

8. Qué falta decidir

Proveedores de voz y LLM. Para escribir el run_bot real hace falta elegir con quién se integra: reconocimiento de voz, síntesis y el modelo con function calling. ¿Ya tenéis cuentas o claves abiertas de algún proveedor (Deepgram, Cartesia, OpenAI, Google, Anthropic, u otro), o hay que decidir desde cero teniendo en cuenta el presupuesto de 100 € y que luego hará falta catalán?

Qué hacer si nuestra propia llamada a la API falla. El vocabulario de reason no tiene un motivo para "nuestro proveedor de voz se ha caído" o "la API de la clínica no responde". Propuesta: reintentar con un backoff corto (2 intentos, unos 2 segundos en total) y, si sigue sin responder, dejar el pending_submission que hubiera antes de ese fallo y colgar. ¿Te parece bien ese comportamiento, o prefieres que en ese caso se intente ESCALATE con algún motivo aunque no describa exactamente lo que pasó?

Límite de reintentos de identificación. Propongo tres intentos en total (sección 6). ¿Lo dejamos así o lo ajustamos tras ver cómo se comporta el reconocimiento de voz con DNIs en las primeras llamadas de práctica?