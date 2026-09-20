import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import express from "express";
import { createObserver, SUBMIT_VERBS, TOOL_TO_NODE } from "../src/observe.js";
import { buildToolRouter } from "../src/proxy.js";

test("handleElevenEvent maps user and agent transcripts", async () => {
  const posts = [];
  const observer = createObserver({
    obsIngestUrl: "http://ingest.test/internal/obs/events",
    obsIngestToken: "tok",
    fetchImpl: async (_url, init) => {
      posts.push(JSON.parse(init.body));
      return { ok: true, status: 200 };
    },
  });

  observer.handleElevenEvent("CA-1", {
    type: "user_transcript",
    user_transcription_event: { user_transcript: "hola" },
  });
  observer.handleElevenEvent("CA-1", {
    type: "agent_response",
    agent_response_event: { agent_response: "buenos días" },
  });
  observer.handleElevenEvent("CA-1", {
    type: "tentative_user_transcript",
    tentative_user_transcription_event: { user_transcript: "ho" },
  });
  const first = observer.handleElevenEvent(
    "CA-1",
    { type: "audio", audio_event: { audio_base_64: "xx", event_id: 1 } },
    { firstWordEmitted: false, startedAtMs: Date.now() - 120 },
  );
  assert.ok(first.firstWordMs >= 0);

  await new Promise((r) => setTimeout(r, 40));

  const kinds = posts.map((p) => p.kind);
  assert.ok(kinds.includes("transcript.user"));
  assert.ok(kinds.includes("transcript.bot"));
  assert.ok(kinds.includes("transcript.user_interim"));
  assert.ok(kinds.includes("metrics.first_word"));
});

test("SUBMIT_VERBS and TOOL_TO_NODE cover the six actions", () => {
  assert.equal(SUBMIT_VERBS.book, "BOOK");
  assert.equal(SUBMIT_VERBS.escalate, "ESCALATE");
  assert.equal(TOOL_TO_NODE["search-patient"], "identify");
  assert.equal(TOOL_TO_NODE.cancel, "cancel_confirm");
});

test("submit proxy emits action.queued then submit.posted", async () => {
  const posts = [];
  const observer = createObserver({
    obsIngestUrl: "http://ingest.test/internal/obs/events",
    obsIngestToken: "tok",
    fetchImpl: async (_url, init) => {
      posts.push(JSON.parse(init.body));
      return { ok: true, status: 200 };
    },
  });

  const clinicFetch = async () => ({
    ok: true,
    status: 200,
    text: async () => JSON.stringify({ call_id: "CA-2" }),
    headers: { get: () => "application/json" },
  });

  const app = express();
  app.use(
    "/tools",
    buildToolRouter(
      { clinicApiKey: "pk-test", clinicApiBaseUrl: "https://clinic.test/api" },
      clinicFetch,
      observer,
    ),
  );

  const server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();

  const response = await fetch(`http://127.0.0.1:${port}/tools/book`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-prosper-call-id": "CA-2",
    },
    body: JSON.stringify({
      patient_id: "P1",
      provider_id: "PR01",
      location_id: "L1",
      appointment_type_id: "T1",
      slot: "2026-09-22T10:00:00+02:00",
      policy_id: "POL1",
    }),
  });
  assert.equal(response.status, 200);
  await new Promise((r) => setTimeout(r, 50));

  const kinds = posts.map((p) => p.kind);
  assert.ok(kinds.includes("tool.called"));
  assert.ok(kinds.includes("action.queued"));
  assert.ok(kinds.includes("submit.posted"));
  assert.ok(kinds.includes("tool.returned"));
  const queued = posts.find((p) => p.kind === "action.queued");
  assert.equal(queued.payload.action, "BOOK");
  const posted = posts.find((p) => p.kind === "submit.posted");
  assert.equal(posted.payload.http_status, 200);
  assert.equal(posted.payload.ok, true);

  await new Promise((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())));
});

test("observer no-ops when OBS_INGEST_URL is empty", async () => {
  let called = false;
  const observer = createObserver({
    obsIngestUrl: "",
    fetchImpl: async () => {
      called = true;
      return { ok: true, status: 200 };
    },
  });
  observer.callStarted("CA-x", { fromNumber: "+34" });
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(called, false);
});
