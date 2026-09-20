import test from "node:test";
import assert from "node:assert/strict";
import { createBargeInGate, prosperClear, prosperMedia } from "../src/protocol.js";

test("drops audio chunks of the interrupted response that arrive after the interruption", () => {
  const gate = createBargeInGate();
  assert.equal(gate.shouldForwardAudio(1), true);
  assert.equal(gate.shouldForwardAudio(2), true);
  assert.equal(gate.interruption(2), true);
  // Late chunks of the cancelled response (event_id <= interruption id) are dropped.
  assert.equal(gate.shouldForwardAudio(1), false);
  assert.equal(gate.shouldForwardAudio(2), false);
});

test("clear is emitted exactly once per interruption", () => {
  const gate = createBargeInGate();
  assert.equal(gate.interruption(3), true); // first interruption -> one clear
  assert.equal(gate.interruption(3), false); // duplicate -> no second clear
  assert.equal(gate.interruption(2), false); // stale duplicate -> no clear
  assert.equal(gate.interruption(8), true); // a new interruption clears again
  assert.equal(gate.interruption(8), false);
});

test("audio from the next turn flows normally after an interruption", () => {
  const gate = createBargeInGate();
  assert.equal(gate.interruption(2), true);
  assert.equal(gate.shouldForwardAudio(2), false); // stale chunk dropped
  gate.newTurn(); // agent_response of the next turn
  assert.equal(gate.shouldForwardAudio(3), true);
  assert.equal(gate.shouldForwardAudio(4), true);
});

test("interruption without an event id falls back to dropping until the next turn, clearing once", () => {
  const gate = createBargeInGate();
  assert.equal(gate.interruption(undefined), true);
  assert.equal(gate.interruption(undefined), false); // still a single clear
  assert.equal(gate.shouldForwardAudio(undefined), false);
  gate.newTurn();
  assert.equal(gate.shouldForwardAudio(undefined), true);
});

test("clear matches the exact Twilio Media Streams format", () => {
  const parsed = JSON.parse(prosperClear("MZ_test_123"));
  assert.deepEqual(parsed, { event: "clear", streamSid: "MZ_test_123" });
  assert.deepEqual(Object.keys(parsed).sort(), ["event", "streamSid"]);
});

test("media forwarding format is unchanged", () => {
  const parsed = JSON.parse(prosperMedia("MZ_test_123", "QUJD"));
  assert.deepEqual(parsed, { event: "media", streamSid: "MZ_test_123", media: { payload: "QUJD" } });
});
