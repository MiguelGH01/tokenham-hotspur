# pipecat-quickstart

A Pipecat AI voice agent built with a cascade pipeline (STT → LLM → TTS).

- **Pipeline**: cascade — Soniox STT → LLM → ElevenLabs TTS (Deepgram via `STT_PROVIDER` / `TTS_PROVIDER`)
- **LLM**: `helmcode` gateway by default; `gemini` and `openai` selectable with `LLM_PROVIDER`
- **Conversation**: Pipecat Flows graph in `server/flow/` (identify → slot → confirm → goodbye)
- **Transports**: Twilio-shaped WebSocket (what the challenge harness dials), WebRTC for the
  browser, and a headless `eval` transport

- **Bot Type**: Web
- **Transport(s)**: SmallWebRTC, Daily (WebRTC)
- **Pipeline**: Cascade
  - **STT**: Deepgram
  - **LLM**: OpenAI Responses
  - **TTS**: Cartesia

## Setup

| Thing | Where it comes from |
|---|---|
| [uv](https://docs.astral.sh/uv/) | `brew install uv`. It installs the right Python (3.12) for you. |
| Team API key (`pk-…`) and dashboard login | The organisers' desk. One key per team — ask a teammate, don't request a new one: rotating it breaks everyone else. |
| Soniox API key | [console.soniox.com](https://console.soniox.com/) (`SONIOX_API_KEY`) |
| ElevenLabs API key + voice ID | [elevenlabs.io](https://elevenlabs.io/) (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`). `ELEVENLABS_MODEL=eleven_v3` or `eleven_v3_conversational` uses Text-to-Dialogue; Flash/Turbo use the normal TTS WebSocket. |
| Deepgram API key | Only if you set `STT_PROVIDER=deepgram` or `TTS_PROVIDER=deepgram` |
| An LLM key | `HELMCODE_API_KEY` for the default provider, or a Gemini / OpenAI key |
| [ngrok](https://ngrok.com/) account | Only for real calls from the dashboard. Free tier is enough. |

1. **Navigate to server directory**:

```bash
cd server
uv sync
cp .env.example .env    # then fill in CLINIC_API_KEY, SONIOX_API_KEY, ELEVENLABS_* and your LLM key
```

`.env` is git-ignored. Never commit keys.

## 2. Run the tests (no keys, no network)

```bash
cd server
uv run pytest
```

These cover the pure logic: slot picking, national-id validation, submission, and the LLM
stall guard. They should all pass on a fresh clone.

## 3. Talk to it in the browser

The quickest way to hear the bot, with no tunnel or dashboard involved.

```bash
make run-webrtc         # from the repo root
```

Open <http://localhost:7860>, allow the microphone, and connect.

## 4. Headless evals

Scripted conversations played against the running bot in text mode — fast, and no audio needed.
They call the real clinic API, so `.env` must be filled in.

Scenarios live in one folder per challenge problem: `server/evals/PR-01/`, `PR-03/`, … Each
file is one published practice case, and passes when the bot makes the expected tool calls with
the expected arguments.

Run all of them, with a fresh bot before each one so no conversation state leaks between
scenarios:

```bash
make evals              # from the repo root; logs in server/eval-runs/
```

`make evals` starts its own bot on port 7860, so stop `make run-twilio` / `make run-webrtc`
first — otherwise the scenarios are played against whatever is already listening there.

Run a single scenario by hand:

```bash
# terminal 1
cd server
uv run bot.py -t eval

# terminal 2
cd server
PYTHONPATH=. uv run pipecat eval run evals/PR-01/simple_booking_chloe.yaml -v
```

Restart terminal 1 before the next scenario: the bot keeps its conversation between runs.
(`make run-eval` is listed in `make help` but has no recipe yet — use the command above.)

## 5. Real calls from the dashboard

2. **Install dependencies**:

   ```bash
   uv sync
   ```

3. **Configure environment variables**:

   ```bash
   cp .env.example .env
   # Edit .env and add your API keys
   ```

4. **Run the bot**:

   ```bash
   uv run bot.py
   ```

   The runner serves every transport; the caller selects which one (a web/mobile
   client picks its transport when it connects; a telephony provider connects to
   `/ws`).

## Project Structure

```
├── Makefile                 # run-webrtc, run-twilio, tunnel
├── scripts/tunnel.sh        # starts ngrok and prints the wss:// endpoint
├── docs/                    # requirements, platform API, agent process map
└── server/
    ├── bot.py               # entry point: pipeline, transports, per-call wiring
    ├── flow/                # conversation graph: prompts, tools, nodes
    ├── clinic/              # clinic API client and static catalogue
    ├── booking.py           # slot selection rules
    ├── national_id.py       # DNI/NIE validation
    ├── submission.py        # one submission per call, guaranteed
    ├── gateway_llm.py       # LLM service that survives a stalled stream
    ├── evals/               # scripted scenarios for `pipecat eval`
    ├── tests/               # pytest suite
    └── .env.example         # every environment variable the bot reads
```

## Deployment

The bot runs on your machine behind the tunnel (section 5); that is the only path the challenge
needs. The scaffold's Pipecat Cloud files (`Dockerfile`, `pcc-deploy.toml`) were removed: the
image could not load `clinic.json`, which lives in the repo root outside the `server/` build
context. To deploy to the cloud, restore them from git history and move `clinic.json` into
`server/` first.

## Building with an AI coding agent

Extending this bot with Claude Code, Codex, or another AI coding assistant? Give it live, accurate Pipecat context instead of stale training data with the **Pipecat Context Hub** — a local index of Pipecat docs, examples, and API source your agent queries over MCP:

```bash
# The Context Hub ships with the CLI
uv tool install "pipecat-ai[cli]"
pipecat context-hub install
```

`install` registers the MCP server with each coding agent it finds and builds the index — a few minutes and about 900 MB the first time. MCP servers load at session start, so do this before opening your coding session, and note the server won't start against an empty index. See the [Pipecat Context Hub docs](https://docs.pipecat.ai/api-reference/context-hub) for the full setup.

## Learn More

- [Pipecat Documentation](https://docs.pipecat.ai/)
- [Pipecat GitHub](https://github.com/pipecat-ai/pipecat)
- [Pipecat Examples](https://github.com/pipecat-ai/pipecat-examples)
- [Discord Community](https://discord.gg/pipecat)