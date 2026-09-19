# Clínica Arenal voice receptionist

A phone receptionist for the HackSpain "El Turno" challenge, built with
[Pipecat](https://docs.pipecat.ai/). It answers a call, identifies the patient, finds the
earliest matching appointment through the clinic API, and submits the booking.

- **Pipeline**: cascade — Deepgram STT → LLM → Deepgram TTS
- **LLM**: `helmcode` gateway by default; `gemini` and `openai` selectable with `LLM_PROVIDER`
- **Conversation**: Pipecat Flows graph in `server/flow/` (identify → slot → confirm → goodbye)
- **Transports**: Twilio-shaped WebSocket (what the challenge harness dials), WebRTC for the
  browser, and a headless `eval` transport

Requirements and the platform contract live in [docs/](docs/INDEX.md). This file is only about
getting the bot running and testing it.

## What you need

| Thing | Where it comes from |
|---|---|
| [uv](https://docs.astral.sh/uv/) | `brew install uv`. It installs the right Python (3.12) for you. |
| Team API key (`pk-…`) and dashboard login | The organisers' desk. One key per team — ask a teammate, don't request a new one: rotating it breaks everyone else. |
| Deepgram API key | [console.deepgram.com](https://console.deepgram.com/) |
| An LLM key | `HELMCODE_API_KEY` for the default provider, or a Gemini / OpenAI key |
| [ngrok](https://ngrok.com/) account | Only for real calls from the dashboard. Free tier is enough. |

## 1. Set up

```bash
cd server
uv sync
cp .env.example .env    # then fill in CLINIC_API_KEY, DEEPGRAM_API_KEY and your LLM key
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

# terminal 2 (from server/; UTF-8 + judge module so Windows can load accented YAML)
cd server
uv run clinic-eval evals/ -v
# one file: uv run clinic-eval evals/PR-01/simple_booking_chloe.yaml -v
# `pipecat eval run evals/` does not recurse; use clinic-eval or pass a PR-* folder.
```

Restart terminal 1 before the next scenario: the bot keeps its conversation between runs.
(`make run-eval` is listed in `make help` but has no recipe yet — use the command above.)

## 5. Real calls from the dashboard

This is the path that gets scored. The harness dials your machine over a WebSocket, so your
laptop has to be reachable from the internet for the whole run.

### 5.1 Start the bot

```bash
make run-twilio         # from the repo root; keep this terminal open
```

Wait for `Uvicorn running on http://localhost:7860`. If you want a log file to search later:

```bash
make run-twilio 2>&1 | tee /tmp/bot.log
```

### 5.2 Open the tunnel

One-time setup: `brew install ngrok`, then `ngrok config add-authtoken <token>`. Reserve a free
static domain at dashboard.ngrok.com → Domains, so your endpoint survives restarts. Without one
the URL changes every time and you have to update the dashboard again.

```bash
make tunnel NGROK_DOMAIN=your-reserved-domain.ngrok-free.dev    # second terminal, keep it open
```

It prints the endpoint ready to paste:

```
Dashboard endpoint (Settings -> Integration):
  wss://your-reserved-domain.ngrok-free.dev/ws
```

<http://127.0.0.1:4040> shows the tunnel status and every incoming request.

### 5.3 Point the dashboard at it

Log in at <https://hackspain.getprosperapp.com> with the team credentials, then
**Settings → Integration**:

- **Endpoint**: the `wss://…/ws` line from the previous step. The scheme and the `/ws` path matter.
- **Headers**: leave empty.

Saving replaces the whole configuration, and the new endpoint applies to the *next* run — a run
that is already queued keeps the old one.

### 5.4 Make one practice call first

Press **Call** beside any published case. Practice calls score nothing and have a 30-second
cooldown. Watch the bot's terminal:

| You should see | Meaning |
|---|---|
| `Generating TTS [Clínica Arenal, how can I help you? ]` | The call connected and the bot greeted |
| `User started speaking` | The bot can hear the caller |
| `Submitted BOOK for call …` | The booking was posted |

If a practice call does not end in `Submitted BOOK`, fix that before spending a scored run.

### 5.5 Run All

**Run All** is the scored run: private cases across every open problem, points on the
leaderboard. Before pressing it:

- It opens **ten calls at the same time** and takes around 18 minutes. Keep the bot and the
  tunnel up throughout — a dropped connection fails that case.
- Every call is capped at three minutes. The bot force-submits at 150 seconds.
- After it finishes there is a **15-minute cooldown** before the next one. Cancelling is safe.
- Close anything heavy on your machine. Ten concurrent pipelines each run VAD and a
  turn-detection model locally.

Afterwards:

```bash
grep -c "stalled" /tmp/bot.log                                  # calls the stall guard rescued
grep -E "Submitted|forcing submission|ERROR" /tmp/bot.log       # outcome of every call
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `make: *** No rule to make target 'run-twilio'` | You are in `server/`. The Makefile is in the repo root. |
| Your code change has no effect | The bot does not hot-reload. Stop it with Ctrl+C and start it again. |
| Dashboard shows "Connection lost" within seconds | The tunnel is down, or the endpoint is missing `wss://` or `/ws`. Check <http://127.0.0.1:4040> for a `GET /ws → 101`. |
| Bot greets, then never reacts; no `User started speaking` in the log | Audio input died. This happened with `RNNoiseFilter` (pyrnnoise 0.4.3 crashes against av 17 on the first frame), which is why noise suppression is off in `bot()`. Test any audio filter on a real call before relying on it. |
| Bot goes silent mid-call | The LLM gateway opened a response stream and then stopped sending. `server/gateway_llm.py` cuts a stream that is silent for 4 seconds and retries once; look for `LLM stream stalled` in the log. |
| `Submitted NO_ACTION … patient_not_found` | The caller was never identified: either the call ended first (see the two rows above), or three lookups failed. |
| `forcing submission` in the log | The call hit 150 seconds. Usually slow turn-taking rather than a crash. |
| Several calls fail only during Run All | Suspect LLM concurrency: the `helmcode` gateway allows 5 concurrent requests per key and Run All holds 10 calls open. Not yet confirmed as a cause; switching `LLM_PROVIDER` to `gemini` or `openai` rules it out. |

## Project structure

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
    ├── .env.example         # every environment variable the bot reads
    ├── Dockerfile           # container image for Pipecat Cloud
    └── pcc-deploy.toml      # Pipecat Cloud deployment config
```

## Deploying to Pipecat Cloud

This project is configured for deployment to Pipecat Cloud. You can learn how to deploy to Pipecat Cloud in the [Pipecat Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart#step-2-deploy-to-production).

Refer to the [Pipecat Cloud Documentation](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction) to learn more about configuring, deploying, and managing your agents in Pipecat Cloud.

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
