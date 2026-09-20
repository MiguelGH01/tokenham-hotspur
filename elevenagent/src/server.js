import "dotenv/config";
import http from "node:http";
import express from "express";
import WebSocket, { WebSocketServer } from "ws";
import { registerCall, unregisterCall } from "./calls.js";
import { loadConfig } from "./config.js";
import { createObserver } from "./observe.js";
import { buildToolRouter } from "./proxy.js";
import { createBargeInGate, elevenInitiation, parseProsperStart, prosperClear, prosperMedia } from "./protocol.js";

const config = loadConfig();
const observer = createObserver(config);
const app = express();
app.disable("x-powered-by");
app.get("/health", (_req, res) => res.json({ ok: true }));
app.use("/tools", buildToolRouter(config, fetch, observer));
const server = http.createServer(app);
const wss = new WebSocketServer({ noServer: true });

async function signedConversationUrl() {
  const url = new URL("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url");
  url.searchParams.set("agent_id", config.agentId);
  const response = await fetch(url, { headers: { "xi-api-key": config.elevenLabsApiKey }, signal: AbortSignal.timeout(10_000) });
  if (!response.ok) throw new Error(`ElevenLabs signed URL failed (${response.status}): ${await response.text()}`);
  const data = await response.json();
  if (!data.signed_url) throw new Error("ElevenLabs did not return signed_url");
  return data.signed_url;
}

function safeSend(socket, data) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(data);
}

wss.on("connection", (prosper) => {
  let eleven = null;
  let streamSid = null;
  let callId = null;
  let callerPhone = null;
  let isTest = false;
  let started = false;
  let closing = false;
  let callObserved = false;
  let firstWordEmitted = false;
  let startedAtMs = Date.now();
  let elevenConversationId = null;
  const pendingAudio = [];
  const bargeIn = createBargeInGate();

  const closeBoth = (code = 1000, reason = "call ended") => {
    if (closing) return;
    closing = true;
    if (callObserved && callId) {
      observer.callEnded(callId, { conversationId: elevenConversationId });
      callObserved = false;
    }
    if (callId) unregisterCall(callId);
    if (eleven && (eleven.readyState === WebSocket.OPEN || eleven.readyState === WebSocket.CONNECTING)) eleven.close(code, reason);
    if (prosper.readyState === WebSocket.OPEN) prosper.close(code, reason);
  };

  prosper.on("message", async (raw) => {
    let message;
    try { message = JSON.parse(raw.toString()); }
    catch { return closeBoth(1003, "invalid JSON"); }

    if (message.event === "connected") return;
    if (message.event === "start") {
      if (started) return closeBoth(1008, "duplicate start");
      started = true;
      try {
        const start = parseProsperStart(message);
        ({ streamSid, callId, callerPhone, isTest } = start);
        registerCall(callId, { isTest, callerPhone });
        startedAtMs = Date.now();
        const signedUrl = await signedConversationUrl();
        if (closing) return;
        eleven = new WebSocket(signedUrl);
        eleven.on("open", () => {
          safeSend(eleven, JSON.stringify(elevenInitiation(start)));
          for (const audio of pendingAudio.splice(0)) safeSend(eleven, JSON.stringify({ user_audio_chunk: audio }));
          console.log(JSON.stringify({ type: "call_started", call_id: callId, stream_sid: streamSid, is_test: isTest }));
          observer.callStarted(callId, { fromNumber: callerPhone, isTest });
          callObserved = true;
        });
        eleven.on("message", (data) => {
          let event;
          try { event = JSON.parse(data.toString()); } catch { return; }
          if (event.type === "conversation_initiation_metadata") {
            const meta = event.conversation_initiation_metadata_event || {};
            if (meta.user_input_audio_format !== "ulaw_8000" || meta.agent_output_audio_format !== "ulaw_8000") {
              console.error(JSON.stringify({ type: "audio_format_error", call_id: callId, input: meta.user_input_audio_format, output: meta.agent_output_audio_format }));
              return closeBoth(1011, "ElevenLabs agent audio must be ulaw_8000 in both directions");
            }
            const bound = observer.handleElevenEvent(callId, event);
            if (bound.conversationId) elevenConversationId = bound.conversationId;
          } else if (event.type === "audio" && event.audio_event?.audio_base_64) {
            if (bargeIn.shouldForwardAudio(event.audio_event.event_id)) {
              if (!firstWordEmitted) {
                const result = observer.handleElevenEvent(callId, event, {
                  firstWordEmitted,
                  startedAtMs,
                });
                if (result.firstWordMs != null) firstWordEmitted = true;
              }
              safeSend(prosper, prosperMedia(streamSid, event.audio_event.audio_base_64));
            } else {
              console.log(JSON.stringify({ type: "audio_dropped_after_interruption", call_id: callId, event_id: event.audio_event.event_id ?? null }));
            }
          } else if (event.type === "interruption") {
            const interruptedEventId = event.interruption_event?.event_id;
            if (bargeIn.interruption(interruptedEventId)) {
              safeSend(prosper, prosperClear(streamSid));
              console.log(JSON.stringify({ type: "interruption", call_id: callId, event_id: interruptedEventId ?? null }));
            }
          } else if (event.type === "agent_response") {
            bargeIn.newTurn();
            observer.handleElevenEvent(callId, event);
          } else if (event.type === "ping" && event.ping_event?.event_id != null) {
            safeSend(eleven, JSON.stringify({ type: "pong", event_id: event.ping_event.event_id }));
          } else {
            observer.handleElevenEvent(callId, event, { firstWordEmitted, startedAtMs });
          }
        });
        eleven.on("error", (error) => {
          console.error(JSON.stringify({ type: "elevenlabs_error", call_id: callId, error: error.message }));
          closeBoth(1011, "ElevenLabs connection error");
        });
        eleven.on("close", () => closeBoth());
      } catch (error) {
        console.error(JSON.stringify({ type: "start_error", call_id: callId, error: error.message }));
        closeBoth(1011, "could not start conversation");
      }
      return;
    }

    if (message.event === "media" && message.media?.payload) {
      if (!started) return closeBoth(1008, "media before start");
      if (eleven?.readyState === WebSocket.OPEN) safeSend(eleven, JSON.stringify({ user_audio_chunk: message.media.payload }));
      else if (pendingAudio.length < 250) pendingAudio.push(message.media.payload);
      return;
    }
    if (message.event === "stop") closeBoth();
  });
  prosper.on("error", (error) => console.error(JSON.stringify({ type: "prosper_error", call_id: callId, error: error.message })));
  prosper.on("close", () => closeBoth());
});

server.on("upgrade", (request, socket, head) => {
  const pathname = new URL(request.url, "http://localhost").pathname;
  if (pathname !== "/ws") return socket.destroy();
  wss.handleUpgrade(request, socket, head, (ws) => wss.emit("connection", ws, request));
});

server.listen(config.port, "0.0.0.0", () => {
  console.log(`Prosper bridge listening on http://0.0.0.0:${config.port}`);
  console.log(`WebSocket endpoint: ws://localhost:${config.port}/ws`);
  if (config.obsIngestUrl) {
    console.log(`Observability ingest: ${config.obsIngestUrl}`);
  }
});
