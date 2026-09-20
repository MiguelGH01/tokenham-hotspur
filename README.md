<p align="center">
  <img src="server/observability/console/healthcheck.svg" alt="healthcheck" width="280">
</p>

<h1 align="center">healthcheck</h1>

<p align="center">
  <strong>Tokenham Hotspur</strong> · HackSpain 2026<br>
  Recepcionista de voz para Clínica Arenal
</p>

---

**healthcheck** es el agente de voz de [Tokenham Hotspur](https://github.com/MiguelGH01/tokenham-hotspur) para el hackathon [HackSpain](https://hackspain.getprosperapp.com/). Atiende las llamadas de citas de **Clínica Arenal**: identifica a quien llama, consulta la ficha y la agenda reales, y reserva, mueve o cancela — o rechaza y escala cuando no toca citar.

La voz es una pieza de un sistema más grande: reglas de la clínica en código, un envío por llamada, y una centralita web para ver el turno, las conversaciones y por qué el agente hizo lo que hizo.

```
Llamada → STT → agente (Flows) → TTS
                 ↘ clinic API (solo lectura)
                 ↘ submit de la acción
                 ↘ consola /console/
```

## Cómo usarlo

Todo se lanza desde la **raíz del repo**. `make help` lista los mismos comandos.

### 1. Requisitos

| Qué | Dónde sale |
|---|---|
| [uv](https://docs.astral.sh/uv/) | `brew install uv` — instala Python 3.12 |
| Clave de equipo (`pk-…`) y login del dashboard | Mesa de organización. Una clave por equipo: si la rotas, se cae el resto |
| LLM | Cloudflare (por defecto), o Helmcode / Gemini / OpenAI con `LLM_PROVIDER` |
| STT | Soniox (`SONIOX_API_KEY`) o Deepgram |
| TTS | ElevenLabs (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`) o Deepgram |
| [ngrok](https://ngrok.com/) | Solo para llamadas reales desde el dashboard. El plan gratis basta |
| Node.js 20+ | Solo si `VOICE_AGENT=elevenagent` |

### 2. Entorno

```bash
cd server
uv sync
cp .env.example .env
```

Rellena al menos:

- `CLINIC_API_KEY` — la `pk-…` del equipo
- la clave del LLM que uses (`CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`, o la del proveedor que pongas en `LLM_PROVIDER`)
- `SONIOX_API_KEY` si el STT es Soniox
- `ELEVENLABS_API_KEY` y `ELEVENLABS_VOICE_ID` si el TTS es ElevenLabs

`.env` está en `.gitignore`. No lo subas.

Opcional:

- `VOICE_AGENT=carloslabs` (defecto, pipeline Pipecat) o `elevenagent` (puente ConvAI en `elevenagent/`)
- `TYPESAFE_API_KEY` — banderas rojas en llamada y la pestaña Insights de la consola
- `NGROK_DOMAIN` — dominio reservado para que el `wss://` no cambie al reiniciar

### 3. Tests (sin red, sin claves)

```bash
make test          # desde la raíz; equivale a cd server && uv run pytest tests/
```

Cubren la lógica pura: huecos, DNI/NIE, envío, consola, reglas de la clínica.

### 4. Consola web (la app)

La centralita vive en [http://localhost:7860/console/](http://localhost:7860/console/). Mismo origen que el bot: Overview, Conversations, Insights, avisos de recepción, y **Place test call**.

```bash
make console-seed  # turno sintético, para que Overview no esté vacío
make console       # arranca el bot y abre la consola
```

En el login:

- `admin` — centralita
- `PR01` … `PR12` — horario de ese médico

Otras utilidades:

```bash
make console-reset                         # borra la SQLite de demo
make console CONSOLE_DB=data/centralita.sqlite   # lee la base “de verdad”
```

Ctrl-C para el bot.

### 5. Hablar con el agente en el navegador

Sin túnel ni dashboard. El cliente WebRTC de Pipecat queda en [http://localhost:7860](http://localhost:7860).

```bash
make run-webrtc    # micrófono en el navegador
```

Permite el micro y conecta. El log va a `server/bot.log` y a `server/run-logs/webrtc-<timestamp>/`.

### 6. Evals (conversaciones scriptadas)

Escenarios en texto contra el bot, sin audio. Llamán a la API de la clínica: `.env` tiene que estar completo.

Los YAML están en `server/evals/` (un fichero por caso). Cada run levanta un bot fresco en el puerto 7861 para que no se mezcle el estado entre escenarios.

```bash
make eval                          # todos los escenarios GREEN
make eval-one S=simple_booking_chloe
make evals-parallel JOBS=4         # los mismos, en paralelo
```

Un escenario a mano:

```bash
# terminal 1
make run-eval                      # bot headless en :7860

# terminal 2
cd server
PYTHONPATH=. uv run pipecat eval run evals/simple_booking_chloe.yaml -v
```

Reinicia el terminal 1 antes del siguiente escenario: el bot conserva la conversación.

`make evals` es un alias de `make eval`. No lo lances a la vez que `make run` / `make console`: compiten por el puerto.

### 7. Llamadas reales (lo que puntúa)

El harness de Prosper marca tu máquina por WebSocket. Tiene que ser alcanzable desde internet durante todo el run (Run All abre **10** sockets a la vez).

#### Atajo: todo a la vez

```bash
make run
```

Un proceso en el puerto 7860 (Prosper `/ws`, WebRTC de Place-test-call, y la consola) más el túnel ngrok. Ctrl-C para el bot y el túnel. Copia la línea `wss://…/ws` que imprime ngrok en **Settings → Integration** del dashboard.

Por defecto el Makefile usa el dominio reservado `NGROK_DOMAIN`. Para otro:

```bash
make run NGROK_DOMAIN=tu-dominio.ngrok-free.dev
```

#### A mano

```bash
make run-twilio     # deja este terminal abierto; espera "Uvicorn running on http://localhost:7860"
make tunnel         # en otro terminal; pega el wss:// en el dashboard
```

El log de arranque dice `VOICE_AGENT=carloslabs` o `elevenagent`. Para guardar log:

```bash
make run-twilio 2>&1 | tee /tmp/bot.log
```

Antes de reiniciar el endpoint en medio de un scored run:

```bash
make guard          # espera / se niega si hay un run en curso
make guard FORCE=1  # salta la comprobación
```

#### ElevenLabs ConvAI

En `server/.env`:

```bash
VOICE_AGENT=elevenagent
ELEVEN_AGENT_ID=tu_agent_id
```

Python arranca `elevenagent/` en el puerto 3000, proxifica `/ws` y `/tools/*` por 7860, y alimenta la misma consola. Con elevenagent, la consola lee transcripts y tools de la API ConvAI de ElevenLabs (poll en vivo y un batch al abrir), no solo de la SQLite local. Place-test-call también puentea a ese agente. Las tools de ElevenLabs deben apuntar a `https://tu-host-ngrok/tools/…` (el mismo host que Prosper, no `:3000`). Detalle: [elevenagent/README.md](elevenagent/README.md).

Los evals usan siempre carloslabs, da igual `VOICE_AGENT`.

### 8. Otros comandos

```bash
make cost            # € y segundos por llamada grabada (p50/p95)
make oracle          # cobertura offline de los casos publicados
make oracle-fetch    # refresca el roster de organización
make oracle-check    # el juez no debe rechazar ninguna respuesta publicada
make concurrency     # N sockets Twilio locales (N=20 por defecto; Run All abre 10)
make transform-logs  # normaliza logs sueltos en server/run-logs/
```

## Estructura

```
├── Makefile                      # run, console, eval, tunnel, …
├── scripts/tunnel.sh             # ngrok → wss://…/ws listo para pegar
├── clinic.json                   # catálogo estático de la clínica
├── docs/                         # requisitos, API, mapa del agente
├── elevenagent/                  # puente ConvAI (opcional)
└── server/
    ├── bot.py                    # pipeline, transports, arranque
    ├── flows/                    # grafo de la conversación
    ├── observability/console/    # centralita (esta es la web app)
    ├── evals/                    # escenarios de `make eval`
    ├── tests/
    └── .env.example
```

## Docs

- [Índice](docs/INDEX.md)
- [Qué construimos](docs/requirements/01-product.md)
- [Operaciones (claves, túnel, dashboard)](docs/requirements/08-operations.md)
- [Mapa del agente](docs/agent/README.md)
