/** Per-call metadata shared between the Prosper WS handler and /tools proxy. */

const calls = new Map();

export function registerCall(callId, meta = {}) {
  if (!callId) return;
  calls.set(String(callId), { isTest: false, ...meta });
}

export function unregisterCall(callId) {
  if (!callId) return;
  calls.delete(String(callId));
}

export function getCall(callId) {
  if (!callId) return null;
  return calls.get(String(callId)) || null;
}

export function isTestCall(callId) {
  return Boolean(getCall(callId)?.isTest);
}
