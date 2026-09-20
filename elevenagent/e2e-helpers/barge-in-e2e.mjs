// End-to-end check of the real bridge server: a fake Prosper client and a fake
// ElevenLabs WebSocket. Verifies that audio chunks arriving after an
// interruption are dropped, that exactly one "clear" reaches Prosper, and that
// the next turn's audio flows.
import assert from "node:assert/strict";
import { once } from "node:events";
import WebSocket, { WebSocketServer } from "ws";

process.env.ELEVENLABS_API_KEY = "test-key";
process.env.AGENT_ID = "agent_test";
process.env.CLINIC_API_KEY = "test-key";
process.env.PORT = "39210";

const EL_PORT = 39211;
const fakeElevenLabs = new WebSocketServer({ port: EL_PORT });

const realFetch = globalThis.fetch;
globalThis.fetch = async (url, ...args) => {
  if (String(url).includes("get-signed-url")) {
    return { ok: true, json: async () => ({ signed_url: `ws://127.0.0.1:${EL_PORT}` }) };
  }
  return realFetch(url, ...args);
};

const received = [];
fakeElevenLabs.on("connection", (socket) => {
  socket.on("message", (raw) => {
    const msg = JSON.parse(raw.toString());
    if (msg.type !== "conversation_initiation_client_data") return;
    const send = (obj) => socket.send(JSON.stringify(obj));
    send({ type: "conversation_initiation_metadata", conversation_initiation_metadata_event: { user_input_audio_format: "ulaw_8000", agent_output_audio_format: "ulaw_8000" } });
    send({ type: "audio", audio_event: { audio_base_64: "QUJD", event_id: 1 } });
    send({ type: "audio", audio_event: { audio_base_64: "REVG", event_id: 2 } });
    // Interruption: event_id 3 is the last audio chunk of the cancelled response.
    send({ type: "interruption", interruption_event: { event_id: 3 } });
    // That chunk arrives after the interruption (in flight): must NOT reach Prosper.
    send({ type: "audio", audio_event: { audio_base_64: "U1RBTEU=", event_id: 3 } });
    send({ type: "agent_response", agent_response_event: { agent_response: "perdona, te escucho" } });
    // Next turn: must flow again.
    send({ type: "audio", audio_event: { audio_base_64: "TkVXVA==", event_id: 4 } });
  });
});

await import("../src/server.js");

const prosper = new WebSocket("ws://127.0.0.1:39210/ws");
await once(prosper, "open");
prosper.on("message", (raw) => received.push(JSON.parse(raw.toString())));
prosper.send(JSON.stringify({ event: "start", streamSid: "MZ_e2e", start: { callSid: "CA_e2e", customParameters: {} } }));

await new Promise((resolve) => setTimeout(resolve, 750));

const summary = received.map((m) => (m.event === "media" ? `media:${m.media.payload}` : m.event));
assert.deepEqual(summary, ["media:QUJD", "media:REVG", "clear", "media:TkVXVA=="], `unexpected Prosper events: ${JSON.stringify(summary)}`);

const clears = received.filter((m) => m.event === "clear");
assert.equal(clears.length, 1, "expected exactly one clear per interruption");
assert.deepEqual(clears[0], { event: "clear", streamSid: "MZ_e2e" });
assert.ok(!summary.includes("media:U1RBTEU="), "stale post-interruption chunk reached Prosper");

prosper.close();
fakeElevenLabs.close();
console.log("E2E OK:", JSON.stringify(summary));
process.exit(0);
