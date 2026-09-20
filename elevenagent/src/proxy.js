import express from "express";
import { isTestCall } from "./calls.js";
import { SUBMIT_VERBS } from "./observe.js";

const ROUTES = Object.freeze({
  register: "/v1/submit/register",
  book: "/v1/submit/book",
  reschedule: "/v1/submit/reschedule",
  cancel: "/v1/submit/cancel",
  "no-action": "/v1/submit/no-action",
  escalate: "/v1/submit/escalate",
});

function callIdFrom(req) {
  return req.get("x-prosper-call-id") || req.query.call_id || null;
}

function clinicHeaders(config) {
  return { accept: "application/json", "X-Api-Key": config.clinicApiKey };
}

async function jsonOrError(response) {
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  return { data, text };
}

function publicPatient(match) {
  const {
    national_id: _nationalId,
    phone: _phone,
    ...safe
  } = match || {};
  return safe;
}

function radians(value) {
  return Number(value) * Math.PI / 180;
}

export function distanceKm(a, b) {
  const earthKm = 6371.0088;
  const dLat = radians(b.latitude - a.latitude);
  const dLon = radians(b.longitude - a.longitude);
  const lat1 = radians(a.latitude);
  const lat2 = radians(b.latitude);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * earthKm * Math.asin(Math.sqrt(h));
}

async function geocodeAddress(config, address, fetchImpl) {
  const url = new URL(config.geocodingBaseUrl || "https://nominatim.openstreetmap.org/search");
  url.searchParams.set("q", address);
  url.searchParams.set("format", "jsonv2");
  url.searchParams.set("limit", "3");
  url.searchParams.set("countrycodes", "es");
  const response = await fetchImpl(url, {
    headers: { accept: "application/json", "user-agent": config.geocodingUserAgent || "prosper-elevenlabs-bridge/1.1" },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`Geocoder failed (${response.status})`);
  const results = await response.json();
  if (!Array.isArray(results) || results.length === 0) return { needs_clarification: true, candidates: [] };
  const candidates = results.map((item) => ({
    display_name: item.display_name,
    latitude: Number(item.lat),
    longitude: Number(item.lon),
    importance: Number(item.importance || 0),
  })).filter((item) => Number.isFinite(item.latitude) && Number.isFinite(item.longitude));
  if (!candidates.length) return { needs_clarification: true, candidates: [] };
  const first = candidates[0];
  const second = candidates[1];
  const ambiguous = Boolean(second && first.importance - second.importance < 0.05 && first.display_name !== second.display_name);
  return { needs_clarification: ambiguous, point: first, candidates };
}

function catalogueAllows(location, clinic, query) {
  if (query.insurer) {
    const insurer = String(query.insurer).toLowerCase();
    if ((location.not_covered_by || []).some((plan) => String(plan.id).toLowerCase() === insurer)) return false;
    if (location.covered_by?.length && !(location.covered_by || []).some((plan) => String(plan.id).toLowerCase() === insurer)) return false;
  }
  if (query.provider_id) {
    const provider = (clinic.providers || []).find((item) => item.id === query.provider_id);
    if (!provider || !(provider.location_names || []).includes(location.name)) return false;
  }
  if (query.specialty_id) {
    const providerNames = new Set(location.provider_names || []);
    const supports = (clinic.providers || []).some((provider) => provider.specialty_id === query.specialty_id && providerNames.has(provider.name));
    if (!supports) return false;
  }
  return true;
}

async function hasAvailability(config, locationId, query, fetchImpl) {
  if (!query.date_from || !query.date_to) return true;
  const url = new URL(`${config.clinicApiBaseUrl}/v1/availability`);
  for (const key of ["date_from", "date_to", "provider_id", "specialty_id", "patient_id"]) {
    if (query[key]) url.searchParams.set(key, query[key]);
  }
  if (query.insurer) url.searchParams.append("insurer", query.insurer);
  url.searchParams.set("location_id", locationId);
  const response = await fetchImpl(url, { headers: clinicHeaders(config), signal: AbortSignal.timeout(10_000) });
  if (!response.ok) throw new Error(`Availability failed (${response.status})`);
  const data = await response.json();
  return Array.isArray(data.slots) && data.slots.length > 0;
}

function queryAsArgs(query) {
  const args = {};
  for (const [key, value] of Object.entries(query || {})) {
    if (value != null && value !== "") args[key] = String(value);
  }
  return args;
}

export function buildToolRouter(config, fetchImpl = fetch, observer = null) {
  const router = express.Router();
  router.use(express.json({ limit: "64kb" }));

  router.get("/search-patient", async (req, res) => {
    const callId = callIdFrom(req);
    if (observer && callId) observer.toolCalled(callId, "search-patient", queryAsArgs(req.query));
    try {
      const target = new URL(`${config.clinicApiBaseUrl}/v1/directory`);
      for (const [key, value] of Object.entries(req.query)) {
        if (["name", "national_id", "phone", "date_of_birth"].includes(key) && value) target.searchParams.set(key, String(value));
      }
      const upstream = await fetchImpl(target, { headers: clinicHeaders(config), signal: AbortSignal.timeout(10_000) });
      const { data } = await jsonOrError(upstream);
      if (observer && callId) {
        observer.toolReturned(callId, "search-patient", {
          status: upstream.ok ? "ok" : "error",
          justification: upstream.ok
            ? `directory returned ${(data?.matches || []).length} match(es)`
            : `directory failed (${upstream.status})`,
        });
      }
      if (!upstream.ok) return res.status(upstream.status).json(data);
      return res.json({ matches: (data?.matches || []).map(publicPatient) });
    } catch (error) {
      console.error(JSON.stringify({ type: "search_patient_error", error: error.message }));
      if (observer && callId) {
        observer.toolReturned(callId, "search-patient", { status: "error", justification: error.message });
      }
      return res.status(502).json({ error: "Clinic API unavailable" });
    }
  });

  router.get("/nearest-site", async (req, res) => {
    const callId = callIdFrom(req);
    const address = String(req.query.address || "").trim();
    if (observer && callId) observer.toolCalled(callId, "nearest-site", queryAsArgs(req.query));
    if (!address) {
      if (observer && callId) {
        observer.toolReturned(callId, "nearest-site", { status: "error", justification: "address is required" });
      }
      return res.status(400).json({ error: "address is required" });
    }
    try {
      const [geocoded, clinicResponse] = await Promise.all([
        geocodeAddress(config, address, fetchImpl),
        fetchImpl(`${config.clinicApiBaseUrl}/v1/clinic`, { headers: clinicHeaders(config), signal: AbortSignal.timeout(10_000) }),
      ]);
      if (!clinicResponse.ok) {
        if (observer && callId) {
          observer.toolReturned(callId, "nearest-site", { status: "error", justification: "Clinic catalogue unavailable" });
        }
        return res.status(clinicResponse.status).json({ error: "Clinic catalogue unavailable" });
      }
      const clinic = await clinicResponse.json();
      if (geocoded.needs_clarification) {
        const body = { needs_clarification: true, candidates: geocoded.candidates.slice(0, 3).map(({ display_name }) => ({ display_name })) };
        if (observer && callId) {
          observer.toolReturned(callId, "nearest-site", { status: "needs_clarification", justification: "ambiguous address" });
        }
        return res.json(body);
      }
      const eligible = [];
      for (const location of clinic.locations || []) {
        if (!catalogueAllows(location, clinic, req.query)) continue;
        if (!await hasAvailability(config, location.id, req.query, fetchImpl)) continue;
        eligible.push({
          location_id: location.id,
          name: location.name,
          address: location.address,
          distance_km: Number(distanceKm(geocoded.point, { latitude: location.latitude, longitude: location.longitude }).toFixed(2)),
        });
      }
      eligible.sort((a, b) => a.distance_km - b.distance_km || a.location_id.localeCompare(b.location_id));
      if (!eligible.length) {
        if (observer && callId) {
          observer.toolReturned(callId, "nearest-site", { status: "no_viable_site", justification: "no viable site" });
        }
        return res.json({ needs_clarification: false, match: null, alternatives: [], reason: "no_viable_site" });
      }
      if (observer && callId) {
        observer.toolReturned(callId, "nearest-site", {
          status: "ok",
          justification: `nearest site ${eligible[0].name}`,
        });
      }
      return res.json({ needs_clarification: false, resolved_address: geocoded.point.display_name, match: eligible[0], alternatives: eligible.slice(1) });
    } catch (error) {
      console.error(JSON.stringify({ type: "nearest_site_error", error: error.message }));
      if (observer && callId) {
        observer.toolReturned(callId, "nearest-site", { status: "error", justification: error.message });
      }
      return res.status(502).json({ error: "Nearest-site lookup unavailable" });
    }
  });

  router.post("/:action", async (req, res) => {
    const path = ROUTES[req.params.action];
    if (!path) return res.status(404).json({ error: "Unknown submit action" });
    const callId = callIdFrom(req);
    if (!callId) return res.status(400).json({ error: "Missing X-Prosper-Call-Id header (bind it to the ElevenLabs call_id dynamic variable)" });
    const body = { ...(req.body || {}) };
    delete body.call_id;
    body.call_id = callId;
    const verb = SUBMIT_VERBS[req.params.action] || String(req.params.action).toUpperCase().replace(/-/g, "_");
    if (observer) {
      observer.toolCalled(callId, req.params.action, { ...body });
      observer.actionQueued(callId, verb, {
        reason: body.reason,
        payload: { ...body },
      });
    }

    // Console Place-test-call uses a synthetic call_id Prosper does not know —
    // dry-run the submit so the agent still gets a 200 and the console records it.
    if (isTestCall(callId)) {
      const fake = {
        call_id: callId,
        received_at: new Date().toISOString(),
        record: { actions: [{ action: verb, ...body }] },
        dry_run: true,
      };
      console.log(JSON.stringify({ type: "submit_dry_run", action: req.params.action, call_id: callId }));
      if (observer) {
        observer.toolReturned(callId, req.params.action, {
          status: "ok",
          justification: `dry-run submit ${verb} (Place test call)`,
        });
        observer.submitPosted(callId, verb, { httpStatus: 200, ok: true, reason: body.reason });
      }
      return res.status(200).json(fake);
    }

    const target = `${config.clinicApiBaseUrl}${path}`;
    try {
      const upstream = await fetchImpl(target, {
        method: "POST",
        headers: { "content-type": "application/json", "X-Api-Key": config.clinicApiKey },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(10_000),
      });
      const text = await upstream.text();
      console.log(JSON.stringify({ type: "submit", action: req.params.action, call_id: callId, status: upstream.status }));
      const ok = upstream.status >= 200 && upstream.status < 300;
      if (observer) {
        observer.toolReturned(callId, req.params.action, {
          status: ok ? "ok" : "error",
          justification: `submit ${verb} → HTTP ${upstream.status}`,
        });
        observer.submitPosted(callId, verb, {
          httpStatus: upstream.status,
          ok,
          reason: body.reason,
        });
      }
      res.status(upstream.status);
      const contentType = upstream.headers.get("content-type");
      if (contentType) res.set("content-type", contentType);
      return res.send(text);
    } catch (error) {
      console.error(JSON.stringify({ type: "submit_error", action: req.params.action, call_id: callId, error: error.message }));
      if (observer) {
        observer.toolReturned(callId, req.params.action, { status: "error", justification: error.message });
        observer.submitPosted(callId, verb, { httpStatus: 502, ok: false, error: error.message, reason: body.reason });
      }
      return res.status(502).json({ error: "Clinic API unavailable" });
    }
  });
  return router;
}

export { ROUTES };
