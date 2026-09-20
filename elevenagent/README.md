# Prosper ↔ ElevenLabs local bridge

Un único servicio Node.js que:

- acepta el WebSocket `Twilio Media Streams` del harness de Prosper en `/ws`;
- abre una conversación independiente de ElevenLabs por llamada;
- reenvía audio µ-law 8 kHz en ambas direcciones y manda `clear` al interrumpir;
- inyecta `current_datetime_madrid`, `caller_phone` y `call_id` como variables dinámicas;
- ofrece los seis endpoints `/tools/*` y añade en servidor `X-Api-Key` + el `call_id` real;
- (opcional) publica eventos al CallHub de Python vía `OBS_INGEST_URL` para la misma consola.
- con `VOICE_AGENT=elevenagent`, la consola lista y abre conversaciones desde la API ConvAI (`GET /v1/convai/conversations`), con transcript **y** tool calls/results, no solo el SQLite local.

## Uso recomendado: desde el backend Python

En `server/.env`:

```bash
VOICE_AGENT=elevenagent
ELEVEN_AGENT_ID=tu_agent_id
ELEVENLABS_API_KEY=…
CLINIC_API_KEY=…
```

Luego `make run` o `make run-twilio` (puerto **7860**). Python arranca este sidecar en `127.0.0.1:3000`,
proxifica `/ws` y `/tools/*`, y la consola recibe transcripts / tools / submits igual que con
carloslabs. **Place test call** (WebRTC) también puentea al agente ConvAI cuando
`VOICE_AGENT=elevenagent`.

El túnel ngrok sigue siendo el de 7860. En el agente de ElevenLabs apunta las tools a:

- `https://TU-HOST/tools/book`
- `https://TU-HOST/tools/reschedule`
- `https://TU-HOST/tools/cancel`
- `https://TU-HOST/tools/register`
- `https://TU-HOST/tools/no-action`
- `https://TU-HOST/tools/escalate`
- `https://TU-HOST/tools/search-patient`
- `https://TU-HOST/tools/nearest-site`

(no al puerto 3000).

## Requisitos

- Node.js 20 o superior
- ngrok (solo si lo arrancas en solitario)
- el agente de ElevenLabs configurado con **input y output `ulaw_8000`**. El puente valida ambos formatos y cierra la llamada si no coinciden, para evitar audio corrupto.

## Instalar y ejecutar (standalone)

```bash
npm install
cp .env.example .env
# Rellena ELEVENLABS_API_KEY, AGENT_ID (o ELEVEN_AGENT_ID) y CLINIC_API_KEY.
npm test
npm start
```

Comprobar:

```bash
curl http://localhost:3000/health
```

Exponerlo (solo si no usas el proxy de Python):

```bash
ngrok http 3000
```

Si ngrok muestra `https://abc123.ngrok-free.app`:

- pasa a Prosper: `wss://abc123.ngrok-free.app/ws`
- usa en ElevenLabs las URLs `/tools/…` del mismo host

## Ajuste necesario en las seis submit tools

En cada webhook de submit, añade el header **`X-Prosper-Call-Id`** y elige como valor la **Conv AI Dynamic Variable** llamada `call_id`. No es un valor que deba escribir el LLM: ElevenLabs lo toma de las variables que este puente inyectó para esa conversación. El proxy descarta cualquier `call_id` recibido en el body y usa exclusivamente el del header.

Mantén `wait for response` activo. Un `409` de Prosper significa que esa acción idéntica ya se aceptó y no debe reintentarse.

## Flujo

1. Prosper conecta y envía `start` con `start.callSid`, `streamSid` y `from_number`.
2. El puente obtiene una URL firmada de ElevenLabs y abre una conversación nueva.
3. Envía las variables dinámicas y relaya los frames `media.payload` sin transcodificar, porque ambos extremos usan µ-law 8 kHz.
4. El audio del agente vuelve como frames `media`. Un evento de interrupción de ElevenLabs genera `clear` hacia Prosper.
5. Las submit tools llaman al proxy. Este añade la API key y el `call_id` de esa conversación antes de reenviar a `/api/v1/submit/*`.
6. Si `OBS_INGEST_URL` está definido, transcripts, tools y submits se publican al CallHub.

## Seguridad y límites

- `.env` está ignorado por git. Nunca pongas secretos en las tools, URLs, logs ni en el zip.
- El servicio registra `call_id`, acción y estado HTTP, pero no audio, credenciales ni datos clínicos.
- Cada llamada mantiene sockets y buffers separados; no hay estado global de conversación.
- El proxy impone 10 s de timeout. Prosper acepta submits hasta 30 s tras cerrar el socket, pero el agente debe enviarlos en cuanto decide la acción.

## Pruebas incluidas

`npm test` comprueba el parseo de `start`, fecha/hora de Madrid con cambio horario, frames `media`/`clear`, barge-in, el mapper de observabilidad y que el proxy sustituye un `call_id` no fiable por el header de la conversación y añade la API key.

No se puede validar sin credenciales: la URL firmada, una conversación real, audio real de ElevenLabs, submissions reales ni concurrencia del plan. Haz primero una llamada de práctica antes de un scored run.

## Tools de lectura añadidas en v1.1

El bridge añade dos endpoints GET para ElevenLabs:

- `GET /tools/search-patient`: reenvía la búsqueda a `/api/v1/directory`, pero elimina `national_id` y `phone` de la respuesta antes de dársela al modelo. Acepta `name`, `national_id`, `phone` y `date_of_birth`.
- `GET /tools/nearest-site`: geocodifica la dirección del llamante, filtra sedes que puedan atender su petición y devuelve la sede viable más cercana por distancia geodésica.

Parámetros de `nearest-site`:

- obligatorio: `address`
- opcionales: `patient_id`, `specialty_id`, `provider_id`, `insurer`
- para filtrar por huecos reales: `date_from` y `date_to` juntos

Si la dirección es ambigua devuelve `needs_clarification: true` y hasta tres candidatos sin elegir por el agente. Si ninguna sede puede servir la petición devuelve `match: null` y `reason: no_viable_site`.

La geocodificación usa Nominatim/OpenStreetMap por defecto. Se puede cambiar con `GEOCODING_BASE_URL`; identifica la aplicación con `GEOCODING_USER_AGENT`. No se guardan las direcciones.
