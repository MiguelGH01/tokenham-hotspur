/**
 * Fire-and-forget observability ingest into the Python CallHub.
 * When OBS_INGEST_URL is unset, every call is a no-op (standalone npm start).
 */

const TOOL_TO_NODE = Object.freeze({
  "search-patient": "identify",
  search_patient: "identify",
  "nearest-site": "find_slot",
  nearest_site: "find_slot",
  find_nearest_site: "find_slot",
  book: "confirm",
  reschedule: "confirm",
  cancel: "cancel_confirm",
  register: "registration",
  "no-action": "no_booking",
  no_action: "no_booking",
  escalate: "refused",
});

const SUBMIT_VERBS = Object.freeze({
  book: "BOOK",
  register: "REGISTER",
  reschedule: "RESCHEDULE",
  cancel: "CANCEL",
  "no-action": "NO_ACTION",
  no_action: "NO_ACTION",
  escalate: "ESCALATE",
});

export function createObserver(config = {}) {
  const url = (config.obsIngestUrl || process.env.OBS_INGEST_URL || "").replace(/\/$/, "");
  const token = config.obsIngestToken || process.env.OBS_INGEST_TOKEN || "";
  const fetchImpl = config.fetchImpl || fetch;

  async function emit(kind, callId, payload = {}) {
    if (!url || !callId) return;
    try {
      const response = await fetchImpl(url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-obs-token": token,
        },
        body: JSON.stringify({ kind, call_id: callId, payload }),
        signal: AbortSignal.timeout(5_000),
      });
      if (!response.ok) {
        console.error(JSON.stringify({
          type: "obs_ingest_error",
          call_id: callId,
          kind,
          status: response.status,
        }));
      }
    } catch (error) {
      console.error(JSON.stringify({
        type: "obs_ingest_error",
        call_id: callId,
        kind,
        error: error.message,
      }));
    }
  }

  function fire(kind, callId, payload) {
    // Never await on the call path.
    void emit(kind, callId, payload);
  }

  return {
    callStarted(callId, { fromNumber, isTest = false } = {}) {
      fire("call.started", callId, {
        transport: isTest ? "webrtc" : "twilio",
        from_number: fromNumber || null,
        is_test: Boolean(isTest),
        emit_reception: true,
      });
    },

    callEnded(callId) {
      fire("call.ended", callId, {});
    },

    userTranscript(callId, text, { interim = false } = {}) {
      const trimmed = String(text || "").trim();
      if (!trimmed) return;
      fire(interim ? "transcript.user_interim" : "transcript.user", callId, {
        text: trimmed,
        final: !interim,
      });
    },

    botTranscript(callId, text) {
      const trimmed = String(text || "").trim();
      if (!trimmed) return;
      fire("transcript.bot", callId, { text: trimmed, final: true });
    },

    firstWord(callId, ms) {
      fire("metrics.first_word", callId, { ms: Math.max(0, Number(ms) || 0) });
    },

    toolCalled(callId, name, args = {}) {
      fire("tool.called", callId, { name, args });
    },

    toolReturned(callId, name, { status = "ok", justification, nextNode } = {}) {
      const payload = {
        name,
        status,
        justification: justification || `${name} returned ${status}`,
      };
      if (nextNode) payload.next_node = nextNode;
      fire("tool.returned", callId, payload);
    },

    actionQueued(callId, action, { reason, summary, seq, payload } = {}) {
      const body = { action };
      if (reason != null) body.reason = reason;
      if (summary != null) body.summary = summary;
      if (seq != null) body.seq = seq;
      if (payload != null) body.payload = payload;
      fire("action.queued", callId, body);
    },

    submitPosted(callId, action, { httpStatus, ok, reason, error } = {}) {
      const body = { action, ok: ok !== false };
      if (httpStatus != null) body.http_status = httpStatus;
      if (reason != null) body.reason = reason;
      if (error != null) body.error = error;
      fire("submit.posted", callId, body);
    },

    /**
     * Map an ElevenLabs ConvAI server event into CallHub emits.
     * Returns { firstWordMs } when this was the first forwarded agent audio.
     */
    handleElevenEvent(callId, event, ctx = {}) {
      if (!event || !callId) return {};
      const type = event.type;
      if (type === "user_transcript") {
        const text = event.user_transcription_event?.user_transcript;
        this.userTranscript(callId, text, { interim: false });
        return {};
      }
      if (type === "tentative_user_transcript") {
        const text =
          event.tentative_user_transcription_event?.user_transcript ||
          event.user_transcription_event?.user_transcript;
        this.userTranscript(callId, text, { interim: true });
        return {};
      }
      if (type === "agent_response") {
        const text = event.agent_response_event?.agent_response;
        this.botTranscript(callId, text);
        return {};
      }
      if (type === "client_tool_call") {
        const tool = event.client_tool_call || event.client_tool_call_event || {};
        const name = tool.tool_name || tool.name;
        const params = tool.parameters || tool.tool_parameters || {};
        if (name) this.toolCalled(callId, name, params);
        return {};
      }
      if (type === "audio" && event.audio_event?.audio_base_64 && !ctx.firstWordEmitted) {
        const startedAt = ctx.startedAtMs || Date.now();
        const ms = Math.max(0, Date.now() - startedAt);
        this.firstWord(callId, ms);
        return { firstWordMs: ms };
      }
      return {};
    },
  };
}

export { TOOL_TO_NODE, SUBMIT_VERBS };
