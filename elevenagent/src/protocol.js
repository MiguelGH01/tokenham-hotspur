export function madridNow(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Madrid", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    timeZoneName: "shortOffset",
  }).formatToParts(date);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  const rawOffset = get("timeZoneName") || "GMT+0";
  const match = rawOffset.match(/GMT([+-])(\d{1,2})(?::(\d{2}))?/);
  const offset = match ? `${match[1]}${match[2].padStart(2, "0")}:${match[3] || "00"}` : "+00:00";
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}:${get("second")}${offset}`;
}

export function parseProsperStart(message) {
  const start = message?.start || {};
  const custom = start.customParameters || start.custom_parameters || {};
  const callId = start.callSid || start.call_id || custom.call_id || custom.callSid;
  const callerPhone = start.from_number || start.from || custom.from_number || custom.from || "hidden";
  const streamSid = message.streamSid || start.streamSid || start.stream_sid;
  const isTestRaw = custom.is_test ?? custom.isTest ?? start.is_test;
  const isTest = isTestRaw === true || isTestRaw === "true" || isTestRaw === "1";
  if (!callId) throw new Error("start message is missing start.callSid");
  if (!streamSid) throw new Error("start message is missing streamSid");
  return {
    callId: String(callId),
    callerPhone: String(callerPhone),
    streamSid: String(streamSid),
    isTest,
  };
}

export function elevenInitiation({ callId, callerPhone, now = new Date() }) {
  return {
    type: "conversation_initiation_client_data",
    dynamic_variables: {
      current_datetime_madrid: madridNow(now),
      caller_phone: callerPhone,
      call_id: callId,
    },
  };
}

export function prosperMedia(streamSid, audioBase64) {
  return JSON.stringify({ event: "media", streamSid, media: { payload: audioBase64 } });
}

export function prosperClear(streamSid) {
  return JSON.stringify({ event: "clear", streamSid });
}

/**
 * Barge-in gate for the ElevenLabs -> Prosper audio path.
 *
 * ElevenLabs emits {"type":"interruption","interruption_event":{"event_id":N}}
 * when the caller talks over the agent. N is the event_id of the last agent
 * audio chunk of the interrupted response. Per the ElevenLabs WebSocket
 * contract, every audio event with event_id <= N belongs to the cancelled
 * response and must be dropped; the bridge also forwards a single Prosper
 * "clear" so audio already buffered on the Prosper side is discarded.
 */
export function createBargeInGate() {
  let lastInterruptedEventId = -1;
  let idlessCancelPending = false;

  const hasId = (value) => typeof value === "number" && Number.isFinite(value);

  return {
    /**
     * Registers an interruption event. Returns true exactly once per
     * interruption, so the caller emits a single Prosper "clear".
     */
    interruption(eventId) {
      if (hasId(eventId)) {
        if (eventId > lastInterruptedEventId) {
          lastInterruptedEventId = eventId;
          idlessCancelPending = false;
          return true;
        }
        return false; // duplicate interruption for an already-cancelled response
      }
      if (!idlessCancelPending) {
        idlessCancelPending = true;
        return true;
      }
      return false;
    },

    /**
     * True when an agent audio chunk must be forwarded to Prosper; false when
     * it is a late chunk of a cancelled response and must be dropped.
     */
    shouldForwardAudio(eventId) {
      if (hasId(eventId)) {
        if (eventId <= lastInterruptedEventId) return false;
        idlessCancelPending = false;
        return true;
      }
      return !idlessCancelPending;
    },

    /** A new agent turn started (agent_response): lifts an id-less cancel. */
    newTurn() {
      idlessCancelPending = false;
    },
  };
}
