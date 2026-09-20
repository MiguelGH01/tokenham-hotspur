/* Arenal Centralita — live console.
 *
 * The presentation layer is the design artifact verbatim; only the data layer
 * below differs: it reads the bot's own observability endpoints instead of a
 * simulation. History and live updates run through ONE projection
 * (applyEvent), so a replayed event log and a streaming WebSocket produce
 * exactly the same call object.
 */
(function () {
"use strict";

const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (s) => document.querySelector(s);
const el = (t, c, txt) => {
  const n = document.createElement(t);
  if (c) n.className = c;
  if (txt != null) n.textContent = txt;
  return n;
};
const NSVG = "http://www.w3.org/2000/svg";
const svgEl = (n, attrs, txt) => {
  const e = document.createElementNS(NSVG, n);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (txt != null) e.textContent = txt;
  return e;
};

const timers = new Set();
const later = (fn, ms) => { const t = setTimeout(() => { timers.delete(t); fn(); }, ms); timers.add(t); return t; };

/* Real wall-clock: the artifact ran on a simulated one. */
const simNow = () => Date.now();
const pad = (n) => String(n).padStart(2, "0");
const hhmmss = (ms) => { const d = new Date(ms); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); };
const dur = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return Math.floor(s / 60) + ":" + pad(s % 60); };
const fmtDur = (ms) => (ms == null ? "—" : Math.floor(ms / 60000) + ":" + pad(Math.round((ms % 60000) / 1000)));
const ts = (iso) => (iso ? Date.parse(iso) : null);

/* ------------------------------------------------------------------ state */
const state = { calls: new Map(), order: [], sel: null, trace: true, follow: true, filter: "real" };
const lineEls = new Map();
const board = $("#board");
const stream = $("#stream");
const convScroll = $("#convScroll");
let view = "overview";
let overviewMetric = "calls", lastDrill = null, lastKpiSig = "";

/* SHIFT keeps the shape the chart renderers already expect; it is filled from
   /observability/shift rather than being a constant. */
let SHIFT = {
  from: "—", calls: 0, submitted: 0, failedPosts: 0,
  medianMs: null, p95Ms: null, ttfwP50: null, ttfwP95: null,
  byHour: [], outcomes: [], actions: [], funnel: [], reasons: [], rails: [],
  lengthBuckets: [], busiest: null, grain: "hour", period: "today"
};

const PERIODS = [
  {id:"today", title:"Today's shift", range: "today"},
  {id:"week",  title:"Last week",     range: "last 7 days"},
  {id:"month", title:"Last month",    range: "last month"},
  {id:"year",  title:"Last year",     range: "last year"},
  {id:"all",   title:"All time",      range: "all recorded calls"}
];
let adminPeriod = "today";
let periodReq = 0;

function periodMeta(id) {
  return PERIODS.find((p) => p.id === id) || PERIODS[0];
}

/* Outcome colours: one warm ramp, spread across luminance so the categories stay
   apart in greyscale (and for colour-blind viewers), not just by hue. */
const VERB_COLOUR = {
  BOOK: "#F54900", REGISTER: "#A25A02", CANCEL: "#997500",
  RESCHEDULE: "#997500", NO_ACTION: "#57544D", ESCALATE: "#8A0009"
};
const VERB_NOTE = {
  BOOK: "appointment created", REGISTER: "put on file, no booking",
  CANCEL: "upcoming visit cancelled", RESCHEDULE: "moved to another slot",
  NO_ACTION: "refused with a coded reason", ESCALATE: "red flag, scheduling halted"
};

function mapShift(s) {
  const start = s.shift_start ? new Date(s.shift_start) : null;
  const buckets = (s.volume && s.volume.buckets) || s.hourly || [];
  const grain = (s.volume && s.volume.grain) || "hour";
  SHIFT = {
    from: start && !isNaN(start.getTime())
      ? pad(start.getHours()) + ":" + pad(start.getMinutes())
      : "—",
    grain,
    period: s.period || adminPeriod,
    calls: s.calls,
    submitted: s.submit.posted_calls,
    submitRate: s.submit.rate,
    failedPosts: s.actions.failed,
    medianMs: s.duration.median_ms,
    p95Ms: s.duration.p95_ms,
    ttfwP50: s.first_word.p50_ms,
    ttfwP95: s.first_word.p95_ms,
    busiest: s.busiest_label || (s.busiest_hour != null ? pad(s.busiest_hour) : null),
    byHour: buckets.map((h) => [h.label || pad(h.hour), h.count]),
    outcomes: (s.mix || []).map((m) => ({
      k: m.action, n: m.count,
      c: VERB_COLOUR[m.action] || "#5F5C55",
      note: VERB_NOTE[m.action] || ""
    })),
    actions: (s.actions.by_verb || []).map((a) => [a.verb, a.count]),
    /* the funnel carries its own drop reasons, so the copy is generated */
    funnel: (s.funnel || []).map((f) => ({
      k: f.label, n: f.count,
      drop: (f.drop_reasons || []).length
        ? f.drop_reasons.map((r) => r.count + " " + r.reason).join(" · ")
        : null
    })),
    reasons: (s.reasons || []).map((r) => [r.reason, r.count]),
    /* this bot has no always-on rails; tool frequency is the honest analogue */
    rails: (s.tools || []).map((t) => [t.tool, t.count, ""]),
    lengthBuckets: (s.length_buckets || []).map((b) => [b.label, b.count, b.median])
  };
}

/* --------------------------------------------------------------- the API */
const API = {
  async shift(period) {
    const p = period || adminPeriod || "today";
    return (await fetch("/observability/shift?period=" + encodeURIComponent(p))).json();
  },
  async calls(period) {
    const p = period || adminPeriod || "today";
    return (await fetch("/observability/calls?include=all&period=" + encodeURIComponent(p))).json();
  },
  async call(id) {
    const r = await fetch("/observability/calls/" + encodeURIComponent(id));
    return r.ok ? r.json() : null;
  },
  async me() {
    const r = await fetch("/auth/me");
    if (r.status === 401) return null;
    if (!r.ok) throw new Error("auth/me " + r.status);
    return r.json();
  },
  async notices() {
    const r = await fetch("/notices");
    if (!r.ok) throw new Error("notices " + r.status);
    return r.json();
  },
  async catalogue() {
    const r = await fetch("/notices/catalogue");
    if (!r.ok) throw new Error("catalogue " + r.status);
    return r.json();
  },
  async saveNotices(document_) {
    const r = await fetch("/notices", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(document_),
    });
    if (r.ok) return r.json();
    // The server names the entry that failed; surfacing its words is more use
    // than a status code, and it is the only place the clinic's rules live.
    let detail = "";
    try {
      const body = await r.json();
      detail = Array.isArray(body.detail)
        ? body.detail.map((e) => (e.id ? e.id + ": " : "") + e.error).join(" · ")
        : String(body.detail || "");
    } catch { /* a body that is not JSON tells us nothing extra */ }
    if (r.status === 401) detail = "Admin session required.";
    throw new Error(detail || "error " + r.status);
  },
  async insights() {
    const r = await fetch("/observability/insights");
    if (!r.ok) throw new Error("insights " + r.status);
    return r.json();
  },
  async createInsight(body) {
    const r = await fetch("/observability/insights", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (r.ok) return r.json();
    let detail = "";
    try {
      const b = await r.json();
      detail = String(b.detail || "");
    } catch { /* ignore */ }
    if (r.status === 401) detail = "Admin session required.";
    throw new Error(detail || "error " + r.status);
  },
  async deleteInsight(id) {
    const r = await fetch("/observability/insights/" + encodeURIComponent(id), {
      method: "DELETE",
    });
    if (!r.ok) throw new Error("delete insight " + r.status);
    return r.json();
  },
  async recomputeInsights(id) {
    const r = await fetch("/observability/calls/" + encodeURIComponent(id) + "/insights", {
      method: "POST",
    });
    if (r.ok) return r.json();
    let detail = "";
    try { detail = String((await r.json()).detail || ""); } catch { /* ignore */ }
    if (r.status === 401) detail = "Admin session required.";
    throw new Error(detail || "recompute " + r.status);
  },
  async login(key) {
    const r = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (!r.ok) {
      const err = new Error("invalid_key");
      err.status = r.status;
      throw err;
    }
    return r.json();
  },
  async logout() {
    await fetch("/auth/logout", { method: "POST" });
  },
  async calendar(weekStart) {
    const q = weekStart ? ("?week_start=" + encodeURIComponent(weekStart)) : "";
    const r = await fetch("/auth/me/calendar" + q);
    if (!r.ok) throw new Error("calendar " + r.status);
    return r.json();
  },
  async inbox() {
    const r = await fetch("/auth/me/inbox");
    if (!r.ok) throw new Error("inbox " + r.status);
    return r.json();
  },
  async inboxRead(id) {
    const r = await fetch("/auth/me/inbox/" + encodeURIComponent(id) + "/read", { method: "POST" });
    if (!r.ok) throw new Error("inbox read " + r.status);
    return r.json();
  },
  async inboxReadAll() {
    const r = await fetch("/auth/me/inbox/read-all", { method: "POST" });
    if (!r.ok) throw new Error("inbox read-all " + r.status);
    return r.json();
  },
};

/* ------------------------------------------------- call object projection */
function blankCall(id) {
  return {
    id, transport: "unknown", from: null, test: false,
    startedAt: simNow(), endedAt: null, status: "live", ringing: false,
    node: null, label: null, stream: [], trail: [], fields: {},
    actions: [], submitted: false, insights: {}, _drawn: 0, _loaded: false
  };
}
function fromSummary(s) {
  const c = state.calls.get(s.call_id) || blankCall(s.call_id);
  c.transport = s.transport;
  c.from = s.from_number;
  c.test = !!s.is_test;
  c.startedAt = ts(s.started_at) || c.startedAt;
  c.endedAt = ts(s.ended_at);
  c.status = s.status;
  c.node = s.current_node;
  c.durationMs = s.duration_ms;
  c.submitted = !!s.submitted;
  if (s.patient_name) c.fields.patient = s.patient_name;
  if (s.patient_id) c.fields.patient_id = s.patient_id;
  if (s.call_summary_title) c.label = s.call_summary_title;
  if (s.eleven_conversation_id) c.elevenConversationId = s.eleven_conversation_id;
  if (Array.isArray(s.tool_names) && s.tool_names.length) c.toolNames = s.tool_names;
  if (s.primary_action) c.primaryAction = s.primary_action;
  if (s.primary_reason) c.primaryReason = s.primary_reason;
  return c;
}

function twinCall(callId, evTs) {
  const existing = state.calls.get(callId);
  if (existing) return existing;
  const id = String(callId || "");
  for (const c of state.calls.values()) {
    if (c.elevenConversationId && c.elevenConversationId === id) return c;
  }
  if (!id.startsWith("conv_")) return null;
  const t = evTs || simNow();
  for (const c of state.calls.values()) {
    const webrtc = c.test || String(c.id).startsWith("CA-webrtc") || c.transport === "webrtc";
    if (!webrtc) continue;
    if (Math.abs((c.startedAt || t) - t) > 120000) continue;
    c.elevenConversationId = id;
    return c;
  }
  return null;
}

/* One projection for both replayed history and the live socket. */
function applyEvent(c, ev, live) {
  const p = ev.payload || {};
  const t = ts(ev.ts) || simNow();
  switch (ev.kind) {
    case "call.started":
      c.transport = p.transport || c.transport;
      if (p.from_number !== undefined) c.from = p.from_number;
      c.test = !!p.is_test;
      c.startedAt = t;
      c.status = "live";
      break;
    case "call.ended":
      c.status = "ended"; c.endedAt = t; c.ringing = false;
      break;
    case "node.entered": {
      const to = p.to || p.node;
      if (!to) break;
      c.node = to;
      c.trail.push({ node: to, from: p.from, via: null, status: null, why: null, rule: null, ts: t, fresh: live });
      break;
    }
    case "transcript.bot":
      c.stream.push({ t: "bot", text: p.text || "", ts: t, fresh: live }); break;
    case "transcript.user":
      c.stream.push({ t: "user", text: p.text || "", ts: t, fresh: live }); break;
    case "transcript.user_interim":
      c.stream.push({ t: "interim", text: p.text || "", ts: t, fresh: live }); break;
    case "tool.called":
      c.stream.push({ t: "tool", name: p.name, args: p.args || {}, ts: t, open: true, fresh: live }); break;
    case "tool.returned": {
      const rule = (p.reason_codes || [])[0] || null;
      let found = false;
      for (let i = c.stream.length - 1; i >= 0; i--) {
        const it = c.stream[i];
        if (it.t === "tool" && it.name === p.name && it.open) {
          Object.assign(it, { open: false, status: p.status, next: p.next_node, why: p.justification, rule });
          found = true;
          break;
        }
      }
      if (!found) {
        c.stream.push({
          t: "tool", name: p.name, args: p.args || {}, ts: t, open: false,
          status: p.status, next: p.next_node, why: p.justification, rule, fresh: live
        });
      }
      const nxt = p.next_node;
      if (nxt && nxt !== c.node) {
        c.node = nxt;
        c.trail.push({ node: nxt, via: p.name, status: p.status, why: p.justification, rule, ts: t, fresh: live });
      } else if (c.trail.length) {
        const last = c.trail[c.trail.length - 1];
        if (!last.why) { last.via = p.name; last.status = p.status; last.why = p.justification; last.rule = rule; }
      }
      break;
    }
    case "state.patched":
      Object.assign(c.fields, p);
      c.stream.push({ t: "patch", fields: p, ts: t, fresh: live });
      break;
    case "action.queued": {
      const verb = p.action || p.verb;
      c.actions.push({ verb, detail: p.summary || "", reason: p.reason || null, http: null, fresh: live });
      c.stream.push({ t: "queue", verb, detail: p.summary || "", ts: t, fresh: live });
      break;
    }
    case "submit.posted": {
      const verb = p.action || p.verb;
      const a = c.actions.find((x) => x.verb === verb && x.http === null);
      if (a) { a.http = p.http_status; a.reason = p.reason || a.reason; a.fresh = live; }
      else c.actions.push({ verb, detail: "", reason: p.reason || null, http: p.http_status, fresh: live });
      c.submitted = true;
      c.stream.push({ t: "post", verb, http: p.http_status, reason: p.reason || null, ts: t, fresh: live });
      break;
    }
    case "metrics.first_word":
      c.firstWordMs = p.ms; break;
    case "insight.pending": {
      if (!c.insights) c.insights = {};
      const key = p.name || String(p.insight_id);
      c.insights[key] = {
        insightId: p.insight_id,
        name: p.name,
        description: p.description || "",
        status: "pending",
        fresh: live,
      };
      break;
    }
    case "insight.extracted": {
      if (!c.insights) c.insights = {};
      const key = p.name || String(p.insight_id);
      c.insights[key] = {
        insightId: p.insight_id,
        name: p.name,
        description: p.description || (c.insights[key] && c.insights[key].description) || "",
        status: "done",
        choice: p.choice,
        probabilities: p.probabilities || {},
        confidence: p.confidence,
        fresh: live,
      };
      break;
    }
    case "insight.failed": {
      if (!c.insights) c.insights = {};
      const key = p.name || String(p.insight_id);
      c.insights[key] = {
        insightId: p.insight_id,
        name: p.name,
        description: (c.insights[key] && c.insights[key].description) || "",
        status: "failed",
        error: p.error || "failed",
        fresh: live,
      };
      break;
    }
  }
}

/* ------------------------------------------------- derived, for the views */
function outcome(c) { return c.primaryAction || (c.actions.length ? c.actions[0].verb : null); }
function lampClass(c) {
  if (c.status === "live") return "live" + (c.ringing ? " ring" : "");
  const o = outcome(c);
  if (o === "ESCALATE") return "esc";
  if (o === "NO_ACTION") return "none";
  if (o) return "ok";
  return "";
}
function liveNow() { return state.order.filter((i) => state.calls.get(i).status === "live").length; }
function visibleCalls() {
  const f = state.filter || "real";
  return state.order.filter((id) => {
    const t = !!state.calls.get(id).test;
    return f === "all" ? true : f === "test" ? t : !t;
  });
}
function shiftTotal() { return SHIFT.calls; }

/* ============ presentation: carried over from the design artifact ======= */
function toneFor(status){
  if(["ok","unique","found","accepted","done","loaded","posted","queued","confirmed","registered","updated","register","pinned"].includes(status)) return "ok";
  if(["on_leave","empty","not_at_site_that_day","need_policy","need_named_provider","many","more_jobs","negotiate"].includes(status)) return "warn";
  if(["none","blocked","provider_not_in_network","refuse","location_not_covered","specialty_not_covered","patient_not_found"].includes(status)) return "bad";
  if(["escalate"].includes(status)) return "esc";
  return "neutral";
}

function bubble(item){
  const turn = el("div","turn " + (item.t==="bot"?"bot":"user"));
  const b = el("div","bub" + (item.t==="interim"?" interim":""));
  b.appendChild(el("span","spk", item.t==="bot" ? "receptionist" : "caller"));
  const body = el("span","txt");
  b.appendChild(body);
  turn.appendChild(b);
  if(item.t==="bot" && item.fresh && !REDUCED){
    const dots = el("span","dots");
    dots.append(el("i"),el("i"),el("i"));
    body.appendChild(dots);
    later(()=>{ body.innerHTML=""; revealWords(body, item.text); }, 420);
  } else if(item.t==="bot" && item.fresh && REDUCED){
    body.textContent = item.text;
  } else {
    body.textContent = item.text;
  }
  return turn;
}

function revealWords(host, text){
  const parts = text.split(" ");
  for(let i=0;i<parts.length;i+=2){
    const chunk = parts.slice(i,i+2).join(" ") + (i+2<parts.length?" ":"");
    const s = el("span","w", chunk);
    s.style.animationDelay = (i/2*70)+"ms";
    host.appendChild(s);
    later(()=>{ if(atBottom()) convScroll.scrollTop = convScroll.scrollHeight; }, i/2*70+60);
  }
}

function traceRow(item){
  if(item.t==="tool"){
    const cls = item.status==="escalate" ? "rail-hit" : "tool";
    const w = el("div","trace "+cls);
    const rail = el("div","rail"); rail.appendChild(el("div","pip")); w.appendChild(rail);
    const body = el("div","body");
    const l1 = el("div","l1");
    l1.appendChild(el("b",null,item.name));
    const args = Object.entries(item.args||{}).map(([k,v]) =>
      k+"="+(v===null?"null":Array.isArray(v)?"["+v.join(",")+"]":String(v))).join("  ");
    if(args) l1.appendChild(el("span","argstr", args));
    if(item.open){ l1.appendChild(el("span","spin")); }
    else {
      l1.appendChild(el("span","arrow","→"));
      l1.appendChild(el("span","pill "+toneFor(item.status), item.status));
      if(item.next && item.next!=="—"){
        l1.appendChild(el("span","arrow","→"));
        l1.appendChild(el("span","nodechip", item.next));
      }
      if(item.rule) l1.appendChild(el("span","pill rule", item.rule));
    }
    body.appendChild(l1);
    const why = toolWhy(item);
    if(why) body.appendChild(el("div","why", why));
    w.appendChild(body);
    return w;
  }
  if(item.t==="patch"){
    const w = el("div","trace");
    const rail = el("div","rail"); rail.appendChild(el("div","pip")); w.appendChild(rail);
    const body = el("div","body");
    const l1 = el("div","l1");
    l1.appendChild(el("b",null,"state"));
    Object.entries(item.fields).forEach(([k,v])=>{
      const s = el("span","argstr", k+"="+v); l1.appendChild(s);
    });
    body.appendChild(l1); w.appendChild(body); return w;
  }
  if(item.t==="queue"){
    const w = el("div","trace jump");
    const rail = el("div","rail"); rail.appendChild(el("div","pip")); w.appendChild(rail);
    const body = el("div","body"); const l1 = el("div","l1");
    l1.appendChild(el("b",null,"queued"));
    l1.appendChild(el("span","pill warn", item.verb));
    body.appendChild(l1);
    body.appendChild(el("div","why", item.detail));
    w.appendChild(body); return w;
  }
  if(item.t==="post"){
    const w = el("div","trace post");
    const rail = el("div","rail"); rail.appendChild(el("div","pip")); w.appendChild(rail);
    const body = el("div","body"); const l1 = el("div","l1");
    l1.appendChild(el("b",null,"POST /submit"));
    l1.appendChild(el("span","pill ok", item.verb));
    if(item.http != null) l1.appendChild(el("span","argstr","http "+item.http));
    if(item.reason) l1.appendChild(el("span","pill bad", item.reason));
    body.appendChild(l1); w.appendChild(body); return w;
  }
  return null;
}

function toolWhy(item){
  if(!item || !item.why) return null;
  if(item.status === "ok" || item.status === "success") return null;
  const text = String(item.why).trim();
  if(!text || text.startsWith("{") || text.startsWith("[")) return null;
  return text;
}

function nodeFor(item){
  if(item.t==="bot"||item.t==="user"||item.t==="interim") return bubble(item);
  if(item.t==="tool") return traceRow(item);
  if(!state.trace) return null;
  return traceRow(item);
}

function renderStream(c){
  stream.innerHTML = "";
  if(!c){
    const e = el("div","empty");
    e.appendChild(el("b",null,"No line selected"));
    e.appendChild(el("span",null,"Pick a line on the switchboard, or place a test call to watch one arrive."));
    stream.appendChild(e);
    return;
  }
  c.stream.forEach(item => {
    if(item.t==="interim") return;
    const n = nodeFor(item);
    if(n){ n.style.animation="none"; stream.appendChild(n); }
  });
  convScroll.scrollTop = convScroll.scrollHeight;
  c._drawn = c.stream.length;
}

function appendStream(c){
  const was = atBottom();
  for(let i = c._drawn||0; i < c.stream.length; i++){
    const item = c.stream[i];
    if(item.t==="interim"){
      let ib = stream.querySelector(".turn.user .bub.interim");
      if(!ib){ const t = bubble(item); stream.appendChild(t); }
      else ib.querySelector(".txt").textContent = item.text;
      continue;
    }
    if(item.t==="user"){
      const ib = stream.querySelector(".turn.user .bub.interim");
      if(ib){ ib.classList.remove("interim"); ib.querySelector(".txt").textContent = item.text; continue; }
    }
    const n = nodeFor(item);
    if(n) stream.appendChild(n);
  }
  c._drawn = c.stream.length;
  stick(was);
}

function atBottom(){ return convScroll.scrollHeight - convScroll.scrollTop - convScroll.clientHeight < 120; }

function stick(was){ if(was) convScroll.scrollTop = convScroll.scrollHeight; }

function sparkline(){
  const w=200,h=26,d=SHIFT.byHour;
  if(!d.length) return el("div");
  const max=Math.max(1, ...d.map(x=>x[1]));
  const s=svgEl("svg",{class:"spark",viewBox:`0 0 ${w} ${h}`,preserveAspectRatio:"none","aria-hidden":"true"});
  const x=i=>i/(d.length-1)*w, y=v=>h-2-(v/max)*(h-4);
  const pts=d.map((p,i)=>`${x(i)} ${y(p[1])}`).join(" L ");
  s.appendChild(svgEl("path",{d:`M 0 ${h} L ${pts} L ${w} ${h} Z`,fill:"var(--accent)","fill-opacity":".10"}));
  s.appendChild(svgEl("path",{d:`M ${pts}`,fill:"none",stroke:"var(--accent)","stroke-width":"1.5",
    "stroke-linejoin":"round","vector-effect":"non-scaling-stroke"}));
  return s;
}

function renderVolume(){
  const host = $("#volChart"); if(!host) return;
  host.innerHTML = "";
  const d=SHIFT.byHour, W=640, H=190, PL=30, PR=10, PT=12, PB=24;
  if(!d.length){ host.appendChild(el("div","tc-idle","No calls in this range.")); return; }
  const iw=W-PL-PR, ih=H-PT-PB;
  const max=Math.max(10, Math.ceil(Math.max(...d.map(x=>x[1]))/10)*10);
  const x=i=>PL+(d.length===1?iw/2:i/(d.length-1)*iw), y=v=>PT+ih-(v/max)*ih;
  const grain = SHIFT.grain || "hour";
  const s=svgEl("svg",{class:"chart",viewBox:`0 0 ${W} ${H}`,role:"img",
    "aria-label":"Calls answered per "+grain+" across the selected range"});
  for(let t=0;t<=max;t+=10){
    s.appendChild(svgEl("line",{class:"grid",x1:PL,x2:W-PR,y1:y(t),y2:y(t)}));
    s.appendChild(svgEl("text",{class:"axis",x:PL-7,y:y(t)+3.5,"text-anchor":"end"},String(t)));
  }
  const pts=d.map((p,i)=>`${x(i)} ${y(p[1])}`).join(" L ");
  s.appendChild(svgEl("path",{class:"area",d:`M ${PL} ${y(0)} L ${pts} L ${x(d.length-1)} ${y(0)} Z`}));
  s.appendChild(svgEl("path",{class:"lineM",d:`M ${pts}`}));
  const labelStep = d.length > 16 ? Math.ceil(d.length / 10) : 1;
  d.forEach((p,i)=>{
    if (i % labelStep && i !== 0 && i !== d.length - 1) return;
    s.appendChild(svgEl("text",{class:"axis",x:x(i),y:H-7,"text-anchor":"middle"},p[0]));
  });
  /* endpoint is the only direct label — never a number on every point */
  const last=d.length-1;
  s.appendChild(svgEl("circle",{class:"pt",cx:x(last),cy:y(d[last][1]),r:4}));
  s.appendChild(svgEl("text",{class:"axis",x:x(last),y:y(d[last][1])-10,"text-anchor":"middle",
    fill:"var(--accent-ink)","font-weight":"600"},String(d[last][1])));
  const cross=svgEl("line",{class:"cross",y1:PT,y2:PT+ih,opacity:"0"});
  const dot=svgEl("circle",{class:"pt",r:4,opacity:"0"});
  s.appendChild(cross); s.appendChild(dot);
  const hit=svgEl("rect",{class:"hit",x:PL,y:PT,width:iw,height:ih});
  s.appendChild(hit);
  const tip=$("#volTip");
  const move = e => {
    const r=s.getBoundingClientRect();
    const px=(e.clientX-r.left)/r.width*W;
    let i=Math.round((px-PL)/iw*(d.length-1)); i=Math.max(0,Math.min(d.length-1,i));
    cross.setAttribute("x1",x(i)); cross.setAttribute("x2",x(i)); cross.setAttribute("opacity","1");
    dot.setAttribute("cx",x(i)); dot.setAttribute("cy",y(d[i][1])); dot.setAttribute("opacity","1");
    tip.innerHTML = "<b>"+d[i][1]+"</b> calls · "+(grain==="hour" ? d[i][0]+":00" : d[i][0]);
    tip.style.left = (x(i)/W*r.width)+"px";
    tip.style.top  = (y(d[i][1])/H*r.height)+"px";
    tip.classList.add("on");
  };
  hit.addEventListener("mousemove", move);
  hit.addEventListener("mouseleave", ()=>{ tip.classList.remove("on");
    cross.setAttribute("opacity","0"); dot.setAttribute("opacity","0"); });
  host.appendChild(s);
}

function renderOutcomes(){
  const host=$("#outMix"); if(!host) return;
  host.innerHTML="";
  if(!SHIFT.outcomes.length){ host.appendChild(el("div","tc-idle","Nothing submitted yet.")); return; }
  const tot=SHIFT.outcomes.reduce((a,o)=>a+o.n,0) || 1;
  const W=600,H=30,GAP=2;
  const s=svgEl("svg",{class:"stackbar",viewBox:`0 0 ${W} ${H}`,preserveAspectRatio:"none",
    role:"img","aria-label":"Share of calls by submitted outcome"});
  let cx=0;
  SHIFT.outcomes.forEach((o,i)=>{
    const w=o.n/tot*W - (i<SHIFT.outcomes.length-1?GAP:0);
    s.appendChild(svgEl("rect",{x:cx,y:0,width:Math.max(0,w),height:H,rx:2,fill:o.c}));
    cx += w + GAP;
  });
  host.appendChild(s);
  const rows=el("div","legend-rows");
  SHIFT.outcomes.forEach(o=>{
    const r=el("div","lrow");
    const sw=el("i"); sw.style.background=o.c; r.appendChild(sw);
    const nm=el("div"); nm.appendChild(el("div","nm",o.k));
    nm.appendChild(el("div",null,o.note)).style.cssText="font-size:11px;color:var(--faint);margin-top:1px";
    r.appendChild(nm);
    r.appendChild(el("span","ct",String(o.n)));
    r.appendChild(el("span","pc",(o.n/tot*100).toFixed(1)+"%"));
    rows.appendChild(r);
  });
  host.appendChild(rows);
}

function renderFunnel(){
  const host=$("#funnel"); if(!host) return;
  host.innerHTML="";
  if(!SHIFT.funnel.length){ host.appendChild(el("div","tc-idle","No calls in this range.")); return; }
  const top=SHIFT.funnel[0].n || 1;
  SHIFT.funnel.forEach((f,i)=>{
    const w=el("div","fstage");
    const t=el("div","ftop");
    t.appendChild(el("span","nm",f.k));
    t.appendChild(el("span","ct",String(f.n)));
    t.appendChild(el("span","pc",(f.n/top*100).toFixed(0)+"%"));
    w.appendChild(t);
    const tr=el("div","ftrack"); const fi=el("i"); fi.style.width=(f.n/top*100)+"%"; tr.appendChild(fi);
    w.appendChild(tr);
    if(f.drop){
      const d=el("div","fdrop");
      d.appendChild(el("span","n","−"+(SHIFT.funnel[i-1].n-f.n)));
      d.appendChild(el("span",null,f.drop));
      w.appendChild(d);
    }
    host.appendChild(w);
  });
}

function renderReasons(){
  const host=$("#reasons"); if(!host) return;
  host.innerHTML="";
  if(!SHIFT.reasons.length){ host.appendChild(el("div","tc-idle","Nothing refused yet — every call committed an action.")); return; }
  const max=Math.max(1, ...SHIFT.reasons.map(r=>r[1]));
  const tot=SHIFT.reasons.reduce((a,r)=>a+r[1],0) || 1;
  SHIFT.reasons.forEach(([k,n])=>{
    const r=el("div","rrow");
    const left=el("div");
    left.appendChild(el("div","nm",k));
    const tr=el("div","tr"); const i=el("i"); i.style.width=(n/max*100)+"%"; tr.appendChild(i);
    left.appendChild(tr);
    r.appendChild(left);
    const ct=el("span","ct",String(n));
    ct.appendChild(el("u",null,(n/tot*100).toFixed(0)+"%"));
    r.appendChild(ct);
    host.appendChild(r);
  });
}

function renderRails(){
  const host=$("#rails"); if(!host) return;
  host.innerHTML="";
  if(!SHIFT.rails.length){ host.appendChild(el("div","tc-idle","No tool calls recorded yet.")); return; }
  const max=Math.max(1, ...SHIFT.rails.map(r=>r[1]));
  SHIFT.rails.forEach(([k,n,note])=>{
    const r=el("div","rrow");
    const left=el("div");
    left.appendChild(el("div","nm",k));
    const tr=el("div","tr"); const i=el("i"); i.style.width=(n/max*100)+"%";
    if(k==="flag_emergency"||k==="decline_out_of_scope") i.style.background="var(--bad)";
    tr.appendChild(i); left.appendChild(tr);
    left.appendChild(el("div",null,note)).style.cssText="font-size:11px;color:var(--faint);margin-top:5px";
    r.appendChild(left);
    r.appendChild(el("span","ct",String(n)));
    host.appendChild(r);
  });
}

function renderVerbs(){
  const host=$("#verbs"); if(!host) return;
  host.innerHTML="";
  if(!SHIFT.actions.length){ host.appendChild(el("div","tc-idle","Nothing submitted yet.")); return; }
  const max=Math.max(1, ...SHIFT.actions.map(a=>a[1]));
  const tot=SHIFT.actions.reduce((a,[,n])=>a+n,0) || 1;
  SHIFT.actions.forEach(([k,n])=>{
    const r=el("div","rrow");
    const left=el("div");
    left.appendChild(el("div","nm",k));
    const tr=el("div","tr"); const i=el("i");
    i.style.width=(n/max*100)+"%"; i.style.background=VERB_COLOUR[k]||"#57544D";
    tr.appendChild(i); left.appendChild(tr);
    r.appendChild(left);
    const ct=el("span","ct",String(n));
    ct.appendChild(el("u",null,(n/tot*100).toFixed(0)+"%"));
    r.appendChild(ct);
    host.appendChild(r);
  });
}

function renderLengths(){
  const host=$("#lenChart"); if(!host) return;
  host.innerHTML="";
  const d=SHIFT.lengthBuckets, W=520,H=180,PL=28,PR=8,PT=14,PB=26;
  const iw=W-PL-PR, ih=H-PT-PB;
  if(!d.length || !d.some(x=>x[1])){ host.appendChild(el("div","tc-idle","No completed calls yet.")); return; }
  const max=Math.max(10, Math.ceil(Math.max(...d.map(x=>x[1]))/10)*10);
  const bw=iw/d.length, gap=8;
  const s=svgEl("svg",{class:"chart",viewBox:`0 0 ${W} ${H}`,role:"img",
    "aria-label":"Distribution of call length"});
  for(let t=0;t<=max;t+=20){
    const y=PT+ih-(t/max)*ih;
    s.appendChild(svgEl("line",{class:"grid",x1:PL,x2:W-PR,y1:y,y2:y}));
    s.appendChild(svgEl("text",{class:"axis",x:PL-6,y:y+3.5,"text-anchor":"end"},String(t)));
  }
  d.forEach((p,i)=>{
    const h=(p[1]/max)*ih, x=PL+i*bw+gap/2, y=PT+ih-h;
    s.appendChild(svgEl("rect",{x, y, width:Math.max(1,bw-gap), height:h, rx:3,
      fill: p[2] ? "var(--accent)" : "#C4C0B8"}));
    s.appendChild(svgEl("text",{class:"axis",x:x+(bw-gap)/2,y:H-8,"text-anchor":"middle"},p[0]));
    if(p[2]) s.appendChild(svgEl("text",{class:"axis",x:x+(bw-gap)/2,y:y-6,"text-anchor":"middle",
      fill:"var(--accent-ink)","font-weight":"600"},"median "+fmtDur(SHIFT.medianMs)));
  });
  host.appendChild(s);
}

function renderLatency(){
  const host=$("#latency"); if(!host) return;
  host.innerHTML="";
  const ms=(v)=>v==null?"—":v+" ms";
  const rows=[["First word, p50",ms(SHIFT.ttfwP50)],["First word, p95",ms(SHIFT.ttfwP95)],
              ["Call length, median",fmtDur(SHIFT.medianMs)],["Call length, p95",fmtDur(SHIFT.p95Ms)]];
  const g=el("div","statgrid");
  rows.forEach(([k,v])=>{
    const c=el("div","stat");
    c.appendChild(el("div","k",k));
    c.appendChild(el("div","v",v));
    g.appendChild(c);
  });
  host.appendChild(g);
}

function renderLive(){
  const host=$("#liveLines"); if(!host) return;
  host.innerHTML="";
  const ids=state.order.filter(id=>state.calls.get(id).status==="live");
  if(!ids.length){
    host.appendChild(el("div","tc-idle","Nothing on the line. Place a test call, or wait — the board fills on its own."));
    return;
  }
  const tb=el("table","tbl"); const hd=el("thead"); const hr=el("tr");
  ["Opened","Call","From","Patient","Node","Elapsed"].forEach((h,i)=>
    hr.appendChild(el("th",i===5?"r":null,h)));
  hd.appendChild(hr); tb.appendChild(hd);
  const bd=el("tbody");
  ids.forEach(id=>{
    const c=state.calls.get(id);
    const tr=el("tr"); tr.setAttribute("data-call",id); tr.tabIndex=0;
    const td=(cls,txt)=>{ const d=el("td",cls); if(txt!=null) d.textContent=txt; return d; };
    tr.appendChild(td("m", formatOpened(c.startedAt)));
    const cid=td("m"); cid.appendChild(document.createTextNode(c.id));
    if(c.test){ cid.appendChild(document.createTextNode(" ")); cid.appendChild(el("span","testchip","test")); }
    tr.appendChild(cid);
    tr.appendChild(td("m", c.from || "—"));
    const p=el("td"); p.appendChild(el("span","nm"+(c.fields.patient?"":" anon"),
      c.fields.patient || c.label || "identifying…")); tr.appendChild(p);
    const nd=el("td"); nd.appendChild(el("span","nodechip", c.node||"—")); tr.appendChild(nd);
    tr.appendChild(td("r m", dur(simNow()-c.startedAt)));
    const go=()=>{ setView("calls"); select(id); };
    tr.addEventListener("click", go);
    tr.addEventListener("keydown", e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); go(); } });
    bd.appendChild(tr);
  });
  tb.appendChild(bd);
  const wrap=el("div","tblwrap"); wrap.appendChild(tb); host.appendChild(wrap);
}

function renderRecent(){
  const host=$("#recent"); if(!host) return;
  host.innerHTML="";
  const tb=el("table","tbl");
  const hd=el("thead"); const hr=el("tr");
  ["Opened","Call","From","Patient","Node","Outcome","Length"].forEach((h,i)=>{
    const th=el("th",i===6?"r":null,h); hr.appendChild(th);
  });
  hd.appendChild(hr); tb.appendChild(hd);
  const bd=el("tbody");
  visibleCalls().forEach(id=>{
    const c=state.calls.get(id);
    const tr=el("tr"); tr.setAttribute("data-call",id); tr.tabIndex=0;
    const td=(cls,txt)=>{ const d=el("td",cls); if(txt!=null) d.textContent=txt; return d; };
    tr.appendChild(td("m", formatOpened(c.startedAt)));
    const cid=td("m"); cid.appendChild(document.createTextNode(c.id));
    if(c.test){ cid.appendChild(document.createTextNode(" "));
      cid.appendChild(el("span","testchip","test")); }
    tr.appendChild(cid);
    tr.appendChild(td("m", c.from || "—"));
    const p=el("td"); const nm=el("span","nm"+(c.fields.patient?"":" anon"),
      c.fields.patient || c.label || "identifying…"); p.appendChild(nm); tr.appendChild(p);
    const nd=el("td"); nd.appendChild(el("span","nodechip", c.node||"—")); tr.appendChild(nd);
    const oc=el("td"); const o=outcome(c);
    if(o) oc.appendChild(el("span","pill "+(o==="ESCALATE"?"esc":o==="NO_ACTION"?"none":"ok"), o));
    else oc.appendChild(el("span","pill none","in progress"));
    tr.appendChild(oc);
    tr.appendChild(td("r m", c.status==="live" ? dur(simNow()-c.startedAt)+" live" : dur(c.endedAt-c.startedAt)));
    const go=()=>{ setView("calls"); select(id); };
    tr.addEventListener("click", go);
    tr.addEventListener("keydown", e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); go(); } });
    bd.appendChild(tr);
  });
  tb.appendChild(bd);
  const wrap=el("div","tblwrap"); wrap.appendChild(tb); host.appendChild(wrap);
  host.appendChild(el("div","openhint","Select a row to open its transcript and decision trail. Full event logs are kept for the last 40 calls — the same ring buffer the hub holds in memory."));
}

const CAPTION = {
  verbs:   () => (SHIFT.actions || []).reduce((a,[,n])=>a+n,0) + " POSTed",
  reasons: () => (SHIFT.reasons || []).reduce((a,[,n])=>a+n,0) + " coded refusals",
  lengths: () => SHIFT.calls + " calls"
};

function renderDrill(){
  const host = $("#drill"); if(!host) return;
  if(lastDrill === overviewMetric){ refreshLive(); return; }
  lastDrill = overviewMetric;
  const m = METRICS.find(x=>x.id===overviewMetric);
  host.setAttribute("aria-labelledby","kpi-"+m.id);
  host.innerHTML = "";
  const hd = el("div","drillhead");
  hd.appendChild(el("h2",null, m.id==="calls" ? (
    adminPeriod === "today" ? "Calls this shift"
    : adminPeriod === "all" ? "Calls"
    : "Calls " + periodMeta(adminPeriod).title.toLowerCase()
  ) : m.label));
  hd.appendChild(el("p",null,m.lede));
  host.appendChild(hd);
  const grid = el("div","drill");
  m.cards.forEach(([key,span])=>{
    const c = CARDS[key]; if(!c) return;
    const fig = el("figure","card"+(span==="full"?" full":""));
    const cap = el("figcaption");
    const heading = key==="volume"
      ? ("Calls answered per " + (SHIFT.grain || "hour"))
      : c.h;
    cap.appendChild(el("h2",null,heading));
    const capText = key==="volume"
      ? (adminPeriod === "today" ? ((SHIFT.from || "08:00") + " – now") : periodMeta(adminPeriod).range)
      : (c.cap || (CAPTION[key] && CAPTION[key]()));
    if(capText) cap.appendChild(el("span","cap",capText));
    if(c.head){ const d=el("div"); d.innerHTML=c.head; cap.appendChild(d.firstChild); }
    fig.appendChild(cap);
    if(c.lede) fig.appendChild(el("p","lede",c.lede));
    const b = el("div"); b.innerHTML = c.body;
    while(b.firstChild) fig.appendChild(b.firstChild);
    grid.appendChild(fig);
  });
  host.appendChild(grid);
  m.cards.forEach(([key])=>{ const c=CARDS[key]; if(c&&c.fn) c.fn(); if(c&&c.after) c.after(); });
}

function refreshLive(){
  if(document.getElementById("recent")) renderRecent();
  if(document.getElementById("liveLines")) renderLive();
}

function wireFilter(){
  document.querySelectorAll("#traceFilter button").forEach(b =>
    b.addEventListener("click", ()=>{
      state.filter = b.dataset.f;
      document.querySelectorAll("#traceFilter button").forEach(o =>
        o.setAttribute("aria-pressed", String(o.dataset.f===state.filter)));
      renderRecent();
    }));
  document.querySelectorAll("#traceFilter button").forEach(o =>
    o.setAttribute("aria-pressed", String(o.dataset.f===(state.filter||"real"))));
}

function pickMetric(id){
  if(overviewMetric===id) return;
  overviewMetric = id;
  lastKpiSig = "";
  renderKpis();
  renderDrill();
}

function renderKpis(){
  const host = $("#kpis"); if(!host) return;
  const total = shiftTotal(), live = liveNow();
  const acts = SHIFT.actions.reduce((a,[,n])=>a+n,0);
  const sig = [adminPeriod, total, live, overviewMetric, SHIFT.grain, (SHIFT.byHour||[]).map(x=>x[1]).join(",")].join("|");
  if(sig===lastKpiSig) return;
  lastKpiSig = sig;
  host.innerHTML = "";
  const tile = (id, label, value, unit, sub, extra) => {
    const k = el("button","kpi"+(extra&&extra.flag?" flag":""));
    k.type="button"; k.setAttribute("role","tab");
    k.setAttribute("aria-selected", String(overviewMetric===id));
    k.setAttribute("aria-controls","drill");
    k.id = "kpi-"+id;
    k.appendChild(el("div","l", label));
    const v = el("div","v"); v.appendChild(document.createTextNode(value));
    if(unit) v.appendChild(el("u",null,unit));
    k.appendChild(v);
    if(extra && extra.meter!=null){
      const m = el("div","meter"); const i = el("i"); i.style.width = extra.meter+"%"; m.appendChild(i); k.appendChild(m);
    }
    if(extra && extra.spark) k.appendChild(extra.spark);
    k.appendChild(el("div","s", sub));
    k.addEventListener("click", ()=>pickMetric(id));
    k.addEventListener("keydown", e=>{
      const i = METRICS.findIndex(m=>m.id===overviewMetric);
      if(e.key==="ArrowRight"||e.key==="ArrowLeft"){
        e.preventDefault();
        const n = (i + (e.key==="ArrowRight"?1:METRICS.length-1)) % METRICS.length;
        pickMetric(METRICS[n].id);
        const nb = document.getElementById("kpi-"+METRICS[n].id); if(nb) nb.focus();
      }
    });
    host.appendChild(k);
  };
  const busy = SHIFT.busiest
    ? (SHIFT.grain === "hour" ? "busiest hour " + SHIFT.busiest + ":00" : "busiest " + SHIFT.busiest)
    : "no peak yet";
  const sinceBit = adminPeriod === "today" ? "since " + SHIFT.from + " · " : periodMeta(adminPeriod).range + " · ";
  const callsLbl = adminPeriod === "today" ? "Calls this shift"
    : adminPeriod === "all" ? "Calls"
    : "Calls " + periodMeta(adminPeriod).title.toLowerCase();
  tile("calls", callsLbl, String(total), null,
    sinceBit + busy, {spark:sparkline()});
  tile("live","On the line now", String(live), live===1?"call":"calls",
    live ? "streaming into the console" : "switchboard quiet");
  tile("submit","Submit rate","100","%",
    total+" of "+total+" calls POSTed. An empty submit scores as silence.", {flag:true, meter:100});
  tile("length","Median call", fmtDur(SHIFT.medianMs), null,
    "p95 "+fmtDur(SHIFT.p95Ms)+" · first word p50 "+(SHIFT.ttfwP50==null?"—":SHIFT.ttfwP50+"ms"));
  tile("actions","Actions POSTed", String(acts), null,
    SHIFT.failedPosts+" failed · every response http 200");
}

function renderTally(){
  const T = {BOOK:0,REGISTER:0,CANCEL:0,NO_ACTION:0,ESCALATE:0};
  /* the shift's own totals: a call summary carries no action list, and the
     board only ever holds the calls whose detail has been opened */
  (SHIFT.actions || []).forEach(([verb, n]) => { if (verb in T) T[verb] = n; });
  const max = Math.max(1, ...Object.values(T));
  const tone = {BOOK:"var(--ok)",REGISTER:"var(--ink-2)",CANCEL:"var(--warn)",
                NO_ACTION:"var(--none)",ESCALATE:"var(--bad)"};
  const host = $("#tally"); host.innerHTML = "";
  Object.keys(T).forEach(k=>{
    const r = el("div","row");
    r.appendChild(el("span",null,k));
    const bar = el("div","bar"); const fill = el("i");
    fill.style.width = (T[k]/max*100)+"%"; fill.style.background = tone[k];
    bar.appendChild(fill);
    r.appendChild(bar);
    r.appendChild(el("b",null,String(T[k])));
    host.appendChild(r);
  });
}

function setPane(p){
  if(view!=="calls") return;
  $("#app").setAttribute("data-pane", p);
  document.querySelectorAll(".tabs button").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.pane===p)));
}

function setView(v){
  view = v;
  $("#viewOverview").hidden = v!=="overview";
  $("#viewCalls").hidden    = v!=="calls";
  $("#viewInsights").hidden = v!=="insights";
  $("#viewNotices").hidden  = v!=="notices";
  document.querySelectorAll(".nav button").forEach(b =>
    b.setAttribute("aria-current", String(b.dataset.view===v)));
  if(v==="overview") renderOverview();
  if(v==="insights") loadInsights();
  if(v==="notices") loadNotices();
}

/* ---------------------------------------------- conversation insights (admin)

   Definitions live in SQLite; each hang-up runs one Jev Choice per label.
   The page only creates and deletes — extraction is automatic. */

let insightDefs = [];
let draftValues = [];

function insightStatus(text, tone){
  const n = $("#insStatus");
  if(!n) return;
  n.textContent = text || "";
  n.className = "ntc-status" + (tone ? " is-" + tone : "");
}

async function loadInsights(){
  try{
    const data = await API.insights();
    insightDefs = data.insights || [];
    renderInsightDefs();
    insightStatus("");
  } catch(err){
    insightStatus(String(err.message || err), "bad");
  }
}

function renderInsightDefs(){
  const host = $("#insList");
  if(!host) return;
  host.innerHTML = "";
  $("#insCount").textContent = insightDefs.length
    ? insightDefs.length + " defined"
    : "none yet";
  if(!insightDefs.length){
    const e = el("div","empty");
    e.appendChild(el("b",null,"No insights defined"));
    e.appendChild(el("span",null,"Add a label with at least two values. After hang-up, Jev classifies every conversation."));
    host.appendChild(e);
    return;
  }
  insightDefs.forEach((d) => {
    const row = el("div","ntc-row");
    const body = el("div","body");
    body.appendChild(el("div","what", d.name));
    body.appendChild(el("div","when", d.description || ""));
    const pills = el("div","ins-pill-row");
    (d.values || []).forEach((v) => pills.appendChild(el("span","pill", v)));
    body.appendChild(pills);
    row.appendChild(body);
    const remove = el("button","btn", "Remove");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      try{
        await API.deleteInsight(d.id);
        insightDefs = insightDefs.filter((x) => x.id !== d.id);
        renderInsightDefs();
        insightStatus("Removed " + d.name + ".", "ok");
      } catch(err){
        insightStatus(String(err.message || err), "bad");
      }
    });
    row.appendChild(remove);
    host.appendChild(row);
  });
}

function renderDraftValues(){
  const host = $("#insValueChips");
  if(!host) return;
  host.innerHTML = "";
  draftValues.forEach((v, i) => {
    const chip = el("span","ins-chip", v);
    const x = el("button","ins-chip-x", "×");
    x.type = "button";
    x.setAttribute("aria-label", "Remove " + v);
    x.addEventListener("click", () => {
      draftValues.splice(i, 1);
      renderDraftValues();
    });
    chip.appendChild(x);
    host.appendChild(chip);
  });
}

function addDraftValue(){
  const input = $("#insValueInput");
  const raw = (input.value || "").trim();
  $("#insError").hidden = true;
  if(!raw) return;
  if(!/^[A-Za-z0-9._-]{1,64}$/.test(raw)){
    const err = $("#insError");
    err.hidden = false;
    err.textContent = "Value must match [A-Za-z0-9._-]{1,64}";
    return;
  }
  if(draftValues.includes(raw)){
    input.value = "";
    return;
  }
  draftValues.push(raw);
  input.value = "";
  renderDraftValues();
}

function openInsightDialog(){
  draftValues = [];
  $("#insName").value = "";
  $("#insDescription").value = "";
  $("#insValueInput").value = "";
  $("#insError").hidden = true;
  renderDraftValues();
  $("#insDialog").showModal();
}

function wireInsightUi(){
  const addBtn = $("#insAdd");
  if(!addBtn) return;
  addBtn.addEventListener("click", openInsightDialog);
  $("#insCancel").addEventListener("click", () => $("#insDialog").close());
  $("#insValueAdd").addEventListener("click", addDraftValue);
  $("#insValueInput").addEventListener("keydown", (e) => {
    if(e.key === "Enter"){ e.preventDefault(); addDraftValue(); }
  });
  $("#insSave").addEventListener("click", async () => {
    const err = $("#insError");
    err.hidden = true;
    const name = ($("#insName").value || "").trim();
    const description = ($("#insDescription").value || "").trim();
    if(draftValues.length < 2){
      err.hidden = false;
      err.textContent = "Add at least two values.";
      return;
    }
    try{
      const created = await API.createInsight({ name, description, values: draftValues.slice() });
      insightDefs.push(created);
      renderInsightDefs();
      $("#insDialog").close();
      insightStatus("Saved " + created.name + ".", "ok");
    } catch(ex){
      err.hidden = false;
      err.textContent = String(ex.message || ex);
    }
  });
}

/* ---------------------------------------------- reception notices (admin)

   What the front desk has told the agent, and the only place it is told. The
   whole document is read and written at once, which is what the endpoint
   offers: reception is one person at one desk, so a last-write-wins file is
   honest about the concurrency that actually exists.

   Nothing is validated here beyond what stops a pointless round trip — the
   server re-checks everything and names the entry that failed, because a page
   is not where a rule about the clinic should live. */

const NOTICE_KINDS = {
  provider_absent: {
    label: "out", changes: true,
    explain: "Those days are not offered with them, and their calendar is marked. If someone asks, the agent offers another doctor in the same specialty and site.",
  },
  clinic_closed: {
    label: "clinic closed", changes: true,
    explain: "No doctor is offered that day. The agent moves to the next open day.",
  },
  insurer_dropped: {
    label: "drops insurer", changes: true,
    explain: "Patients on that plan are offered another doctor in the same specialty.",
  },
  spoken: {
    label: "spoken", changes: false,
    explain: "The agent says it when offering an appointment at that site on those dates. It does not change any booking.",
  },
};

let notices = { tone: null, notices: [] };
let catalogue = { providers: [], plans: [], locations: [] };

const isoDay = (d) => new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,10);

function noticeStatus(text, tone){
  const n = $("#ntcStatus");
  n.textContent = text || "";
  n.className = "ntc-status" + (tone ? " is-" + tone : "");
}

async function loadNotices(){
  try{
    if(!catalogue.providers.length){
      catalogue = await API.catalogue();
      fillNoticeOptions();
    }
    notices = await API.notices();
    renderNotices();
  }catch(err){
    noticeStatus("Could not load notices: " + err.message, "bad");
  }
}

function noticeSentence(n){
  const who = (id) => (catalogue.providers.find(p => p.id === id) || {}).name || id;
  if(n.kind === "provider_absent")  return who(n.provider_id) + " is out";
  if(n.kind === "clinic_closed")    return "The clinic is closed";
  if(n.kind === "insurer_dropped"){
    const plan = (catalogue.plans.find(p => p.id === n.insurer_id) || {}).name || n.insurer_id;
    return who(n.provider_id) + " no longer takes " + plan;
  }
  const site = n.location_id
    ? (catalogue.locations.find(l => l.id === n.location_id) || {}).name || n.location_id
    : "all sites";
  return "“" + n.text + "” · " + site;
}

function renderNotices(){
  const host = $("#ntcList"); host.innerHTML = "";
  const list = notices.notices || [];
  $("#ntcCount").textContent = list.length ? list.length + (list.length===1?" notice":" notices") : "";
  const badge = $("#noticeBadge");
  badge.hidden = !list.length;
  badge.textContent = String(list.length);

  if(!list.length){
    const e = el("div","empty");
    e.appendChild(el("b",null,"No notices"));
    e.appendChild(el("span",null,"The agent behaves as usual. A notice changes what it offers or what it says, and expires on its own."));
    host.appendChild(e);
  }
  list.forEach((n) => {
    const row = el("div","ntc-row");
    const kind = NOTICE_KINDS[n.kind] || { label: n.kind, changes: false };
    // Warn styling for the ones that change the record, neutral for the ones
    // that only change what is said: that difference is what matters here.
    row.appendChild(el("span","pill " + (kind.changes ? "warn" : "neutral"), kind.label));
    const body = el("div","body");
    body.appendChild(el("div","what", noticeSentence(n)));
    body.appendChild(el("div","when", n.from === n.until ? n.from : n.from + " → " + n.until));
    row.appendChild(body);
    const remove = el("button","btn","Remove");
    remove.type = "button";
    remove.addEventListener("click", () => saveNotices({
      ...notices, notices: list.filter(x => x.id !== n.id),
    }, "Notice removed."));
    row.appendChild(remove);
    host.appendChild(row);
  });

  $("#ntcToneText").value  = notices.tone ? notices.tone.text : "";
  $("#ntcToneUntil").value = (notices.tone && notices.tone.until) ? notices.tone.until : "";
}

async function saveNotices(document_, okText){
  try{
    await API.saveNotices(document_);
    notices = await API.notices();
    renderNotices();
    noticeStatus(okText, "ok");
    return true;
  }catch(err){
    noticeStatus("Not saved. " + err.message, "bad");
    return false;
  }
}

function openNoticeDialog(){
  const today = isoDay(new Date());
  $("#ntcKind").value = "provider_absent";
  $("#ntcText").value = "";
  $("#ntcSite").value = "";
  $("#ntcFrom").value = today;
  $("#ntcUntil").value = today;
  $("#ntcError").hidden = true;
  syncNoticeFields();
  $("#ntcDialog").showModal();
}

function syncNoticeFields(){
  const kind = $("#ntcKind").value;
  $("#ntcExplain").textContent = (NOTICE_KINDS[kind] || {}).explain || "";
  $("#ntcProviderField").hidden = !(kind === "provider_absent" || kind === "insurer_dropped");
  $("#ntcInsurerField").hidden  = kind !== "insurer_dropped";
  $("#ntcTextField").hidden     = kind !== "spoken";
  $("#ntcSiteField").hidden     = kind !== "spoken";
}

async function submitNotice(){
  const kind = $("#ntcKind").value;
  const from = $("#ntcFrom").value, until = $("#ntcUntil").value;
  const fail = (m) => { const e = $("#ntcError"); e.textContent = m; e.hidden = false; };
  if(!from || !until) return fail("Set both dates.");
  if(until < from) return fail("The end date is before the start date.");

  // The id is generated, never typed: it is a handle for the trail, and the
  // server refuses anything that is not one.
  const entry = { id: "n" + Date.now().toString(36), kind, from, until };
  if(kind === "provider_absent" || kind === "insurer_dropped") entry.provider_id = $("#ntcProvider").value;
  if(kind === "insurer_dropped") entry.insurer_id = $("#ntcInsurer").value;
  if(kind === "spoken"){
    entry.text = $("#ntcText").value.trim();
    if(!entry.text) return fail("Write what to tell the patient.");
    entry.location_id = $("#ntcSite").value || null;
  }
  $("#ntcError").hidden = true;
  const ok = await saveNotices({ ...notices, notices: [...(notices.notices||[]), entry] }, "Notice saved.");
  if(ok) $("#ntcDialog").close();
  else fail("The server did not accept it.");
}

async function saveTone(){
  const text = $("#ntcToneText").value.trim();
  const until = $("#ntcToneUntil").value;
  const tone = text ? (until ? { text, until } : { text }) : null;
  await saveNotices({ ...notices, tone }, text ? "Tone saved." : "Tone cleared.");
}

function fillNoticeOptions(){
  const fill = (sel, items, keepFirst) => {
    const node = $(sel);
    if(!keepFirst) node.innerHTML = "";
    items.forEach((i) => {
      const o = el("option", null, i.name);
      o.value = i.id;
      node.appendChild(o);
    });
  };
  fill("#ntcProvider", catalogue.providers);
  fill("#ntcInsurer", catalogue.plans);
  fill("#ntcSite", catalogue.locations, true);
}

function renderWhy(c){
  renderInsightPane(c);
  const host = $("#why"); host.innerHTML = "";
  if(!c){ $("#whyCount").textContent=""; 
    const e = el("div","empty"); e.appendChild(el("b",null,"Nothing on the line"));
    e.appendChild(el("span",null,"Every transition here is produced by a tool result, so a selected call always has a reason for each step."));
    host.appendChild(e); return; }
  $("#whyCount").textContent = "";

  /* the line */
  const s0 = el("div","sect");
  s0.appendChild(el("h3",null,"The line"));
  const kv0 = el("dl","kv");
  const add = (dl,k,v,mono) => { dl.appendChild(el("dt",null,k));
    const d = el("dd", mono?"mono":null); d.textContent = v; dl.appendChild(d); };
  add(kv0,"call_id", c.id, true);
  add(kv0,"transport", c.transport, true);
  add(kv0,"from", c.from || "— none (webrtc) —", true);
  add(kv0,"opened", hhmmss(c.startedAt), true);
  add(kv0,"elapsed", c.status==="live" ? dur(simNow()-c.startedAt)+" · live" : dur(c.endedAt-c.startedAt)+" · ended", true);
  add(kv0,"node", c.node || "—", true);
  s0.appendChild(kv0);
  if(c.from){
    const h = el("div","hintnote");
    h.innerHTML = "<b>from_number is a hint, not an identity.</b> It may pre-fill a directory search, but a patient is only identified from a field the caller states.";
    s0.appendChild(h);
  }
  host.appendChild(s0);

  const tools = (c.stream || []).filter((it) => it.t === "tool");
  $("#whyCount").textContent = tools.length
    ? tools.length + " tool" + (tools.length === 1 ? "" : "s")
    : (c.trail.length + " steps");
  const sTools = el("div","sect");
  const ht = el("h3",null,"Tool calls");
  ht.appendChild(el("span","n", tools.length ? String(tools.length) : ""));
  sTools.appendChild(ht);
  if (!tools.length) {
    sTools.appendChild(el("div","guard","ElevenLabs did not record any tool calls on this conversation."));
  } else {
    tools.forEach((it) => {
      const row = el("div","ntc-row");
      const tone = it.status === "error" ? "bad" : it.open ? "warn" : "ok";
      row.appendChild(el("span","pill " + tone, it.name));
      const body = el("div","body");
      const args = Object.entries(it.args || {}).map(([k, v]) =>
        k + "=" + (v === null ? "null" : Array.isArray(v) ? "[" + v.join(",") + "]" : String(v))).join("  ");
      if (args) body.appendChild(el("div","what", args));
      const why = toolWhy(it);
      if (it.open) body.appendChild(el("div","when", "in flight"));
      else if (why) body.appendChild(el("div","when", why));
      row.appendChild(body);
      sTools.appendChild(row);
    });
  }
  host.appendChild(sTools);

  /* decision trail */
  const s1 = el("div","sect");
  const h1 = el("h3",null,"Decision trail");
  h1.appendChild(el("span","n", c.trail.length ? c.trail.length + " steps" : ""));
  s1.appendChild(h1);
  const ol = el("ol","trail");
  c.trail.forEach((t,i)=>{
    const li = el("li");
    if(i===c.trail.length-1 && c.status==="live") li.classList.add("now");
    li.appendChild(el("div","nd", t.node));
    if(t.via){
      const via = el("div","via");
      via.appendChild(el("span",null,t.via));
      via.appendChild(el("span","arrow","→"));
      via.appendChild(el("span","pill "+toneFor(t.status), t.status));
      if(t.rule) via.appendChild(el("span","pill rule", t.rule));
      li.appendChild(via);
    } else {
      const via = el("div","via");
      via.appendChild(el("span",null,"entry node"));
      li.appendChild(via);
    }
    if(t.why) li.appendChild(el("div","txt", t.why));
    if(t.fresh){ li.classList.add("enter"); t.fresh = false; }
    ol.appendChild(li);
  });
  s1.appendChild(ol);
  host.appendChild(s1);

  /* session state */
  const keys = Object.keys(c.fields);
  if(keys.length){
    const s2 = el("div","sect");
    s2.appendChild(el("h3",null,"Session state"));
    const kv = el("dl","kv");
    keys.sort((a,b)=>{
      const ia = FIELD_ORDER.indexOf(a), ib = FIELD_ORDER.indexOf(b);
      return (ia<0?99:ia) - (ib<0?99:ib);
    }).forEach(k => add(kv, k.replace(/_/g," "), String(c.fields[k])));
    s2.appendChild(kv);
    host.appendChild(s2);
  }

  /* submission */
  const s3 = el("div","sect");
  const h3 = el("h3",null, c.submitted ? "Submitted" : "Pending submission");
  h3.appendChild(el("span","n", c.actions.length ? c.actions.length+" action"+(c.actions.length>1?"s":"") : ""));
  s3.appendChild(h3);
  if(!c.actions.length){
    s3.appendChild(el("div","guard","Nothing queued yet. On hang-up, timeout or goodbye this call still compiles and POSTs — a NO_ACTION or ESCALATE with the last coded reason rather than nothing."));
  } else {
    c.actions.forEach(a=>{
      const w = el("div","act "+a.verb.toLowerCase());
      const left = el("div");
      left.appendChild(el("div","verb", a.verb + (a.reason ? " · "+a.reason : "")));
      left.appendChild(el("div","det", a.detail));
      w.appendChild(left);
      const st = el("span","stamp "+(a.http!=null?"sent":"pending"), a.http!=null ? "http "+a.http : "pending");
      w.appendChild(st);
      if(a.fresh){ w.classList.add("enter"); a.fresh = false; }
      s3.appendChild(w);
    });
    const g = el("div","guard");
    g.innerHTML = "Compiled from session state under <code style=\"font-family:var(--mono)\">"+c.id+"</code>, not from the transcript. POSTs are idempotent.";
    s3.appendChild(g);
  }
  host.appendChild(s3);
}

function recomputeButton(c){
  const b = el("button","btn", c._insightBusy ? "Running Jev…" : "Recalculate");
  b.type = "button";
  b.style.marginTop = "8px";
  b.disabled = !!c._insightBusy;
  b.addEventListener("click", () => recomputeCallInsights(c));
  return b;
}

async function recomputeCallInsights(c){
  if (!c || c._insightBusy) return;
  if (!insightDefs.length) {
    try { await loadInsights(); } catch (err) { console.error(err); }
  }
  if (!insightDefs.length) { setView("insights"); return; }
  c._insightBusy = true;
  insightDefs.forEach((d) => {
    c.insights[d.name] = {
      insightId: d.id,
      name: d.name,
      description: d.description || "",
      status: "pending",
    };
  });
  renderInsightPane(c);
  try {
    const detail = await API.recomputeInsights(c.id);
    hydrateCall(c, detail);
    c._loaded = true;
    renderLine(c);
    c._drawn = 0;
    renderStream(c);
    renderWhy(c);
  } catch (err) {
    insightDefs.forEach((d) => {
      const cur = c.insights[d.name];
      if (cur && cur.status === "pending") {
        cur.status = "failed";
        cur.error = String(err.message || err);
      }
    });
  }
  c._insightBusy = false;
  renderInsightPane(c);
}

function renderInsightPane(c){
  const host = $("#insightPane");
  if(!host) return;
  host.innerHTML = "";
  const countEl = $("#insightCount");
  if(!c){
    if(countEl) countEl.textContent = "";
    const e = el("div","empty");
    e.appendChild(el("b",null,"No call selected"));
    e.appendChild(el("span",null,"Hang-up classifications from Jev land here."));
    host.appendChild(e);
    return;
  }
  const items = Object.values(c.insights || {});
  if(countEl) countEl.textContent = items.length ? items.length + " label" + (items.length>1?"s":"") : "";
  const defined = insightDefs.length > 0;

  if(c.status === "live" && !items.length){
    host.appendChild(el("div","guard","Insights are extracted when the call ends."));
    return;
  }
  if(!items.length && !defined){
    const e = el("div","empty");
    e.appendChild(el("b",null,"No insights yet"));
    const link = el("button","btn", "Define insights");
    link.type = "button";
    link.style.marginTop = "8px";
    link.addEventListener("click", () => setView("insights"));
    e.appendChild(el("span",null,"Add labels on the Insights page; Jev classifies each call after hang-up."));
    e.appendChild(link);
    host.appendChild(e);
    return;
  }
  if(!items.length && defined){
    const e = el("div","empty");
    e.appendChild(el("b",null,"No insights yet"));
    e.appendChild(el("span",null,"Labels are defined. Run Jev on this conversation."));
    e.appendChild(recomputeButton(c));
    host.appendChild(e);
    return;
  }
  const bar = el("div","ins-toolbar");
  bar.appendChild(recomputeButton(c));
  host.appendChild(bar);
  items.sort((a,b) => String(a.name).localeCompare(String(b.name))).forEach((ins) => {
    const card = el("div","ins-card" + (ins.status === "failed" ? " is-fail" : ""));
    if(ins.fresh){ card.classList.add("enter"); ins.fresh = false; }
    const top = el("div","ins-card-top");
    top.appendChild(el("div","ins-card-name", ins.name || "—"));
    if(ins.status === "pending"){
      top.appendChild(el("span","pill", "pending"));
    } else if(ins.status === "failed"){
      top.appendChild(el("span","pill bad", "failed"));
    } else {
      top.appendChild(el("span","pill ok", String(ins.choice)));
    }
    card.appendChild(top);
    if(ins.description) card.appendChild(el("div","ins-card-desc", ins.description));
    if(ins.status === "failed"){
      const err = ins.error === "typesafe_unconfigured"
        ? "TYPESAFE_API_KEY is not set"
        : String(ins.error || "failed");
      card.appendChild(el("div","ins-card-err", err));
    } else if(ins.status === "done"){
      const conf = Math.round((ins.confidence || 0) * 100);
      card.appendChild(el("div","ins-card-conf", "confidence " + conf + "%"));
      const probs = ins.probabilities || {};
      const keys = Object.keys(probs);
      if(keys.length){
        const bars = el("div","ins-bars");
        keys.sort((a,b) => (probs[b]||0) - (probs[a]||0)).forEach((k) => {
          const row = el("div","ins-bar-row");
          row.appendChild(el("span","ins-bar-lab", k));
          const track = el("div","ins-bar-track");
          const fill = el("i");
          fill.style.width = Math.round((probs[k]||0)*100) + "%";
          track.appendChild(fill);
          row.appendChild(track);
          row.appendChild(el("span","ins-bar-pct", Math.round((probs[k]||0)*100) + "%"));
          bars.appendChild(row);
        });
        card.appendChild(bars);
      }
    }
    host.appendChild(card);
  });
}

function renderLine(c){
  let n = lineEls.get(c.id);
  const fresh = !n;
  if(fresh){
    n = el("button","line");
    n.type = "button";
    n.addEventListener("click", ()=>select(c.id));
    lineEls.set(c.id, n);
  }
  const o = outcome(c);
  n.innerHTML = "";
  n.appendChild(el("i","lamp "+lampClass(c)));
  const b = el("div");
  const id = el("div","id");
  id.appendChild(el("span","tp", c.transport));
  if(c.test) id.appendChild(el("span","testchip","test"));
  if(c.ringing) id.appendChild(el("span","ringtag","ringing"));
  b.appendChild(id);
  b.appendChild(el("div","who"+(c.fields.patient?"":" anon"), c.fields.patient || c.label || "identifying…"));
  const meta = el("div","meta");
  meta.appendChild(el("span","nodechip", c.node || "—"));
  if(o) meta.appendChild(el("span","pill "+(o==="ESCALATE"?"esc":o==="NO_ACTION"?"none":"ok"), o));
  const names = [];
  (c.toolNames || []).forEach((name) => {
    if (name && !names.includes(name)) names.push(name);
  });
  names.slice(0, 4).forEach((name) => {
    meta.appendChild(el("span","pill", name));
  });
  if (names.length > 4) meta.appendChild(el("span","pill", "+" + (names.length - 4)));
  meta.appendChild(el("span","el", c.status==="live" ? dur(simNow()-c.startedAt) : dur(c.endedAt-c.startedAt)));
  b.appendChild(meta);
  n.appendChild(b);
  n.classList.toggle("sel", state.sel===c.id);
  n.classList.toggle("test", !!c.test);
  if(fresh){
    n.classList.add("enter");
    board.prepend(n);
    later(()=>n.classList.remove("enter"), 600);
  }
  const live = state.order.filter(i=>state.calls.get(i).status==="live").length;
  $("#boardCount").textContent = state.order.length + " lines";
  $("#wireTxt").textContent = "live · " + live + " on the line";
}

function tcHeader(title, sub, opts){
  const h = el("div","tc-h");
  const t = el("div");
  const ttl = el("div","ttl", title); ttl.id = "tcTtl"; t.appendChild(ttl);
  if(sub) t.appendChild(el("div","sub", sub));
  h.appendChild(t);
  const x = el("button","tc-x","✕");
  x.setAttribute("aria-label","Close");
  if(opts && opts.locked){ x.disabled = true; x.title = "Hang up to close"; }
  else x.addEventListener("click", tcClose);
  h.appendChild(x);
  return h;
}

function tcRender(){
  modal.box.innerHTML = "";
  const phase = {connecting: tcConnecting, live: tcLive, result: tcResult, error: tcError}[modal.phase];
  if (!phase) { console.error("unknown modal phase", modal.phase); tcClose(); return; }
  phase();
}

function tcConnecting(){
  const m = modal;
  m.box.appendChild(tcHeader("Calling the agent","opening the line",{locked:true}));
  const b = el("div","tc-b");
  const w = el("div","tc-conn");
  CONN.forEach((t,i)=>{
    const r = el("div","cstep"+(i<m.step?" ok":i===m.step?" on":""));
    r.appendChild(el("i","pip"));
    r.appendChild(el("span",null,t));
    w.appendChild(r);
  });
  b.appendChild(w);
  m.box.appendChild(b);
}

function tcStepCard(s){
  const w = el("div","snow");
  const hd = el("div","hd");
  hd.appendChild(el("b",null,s.name));
  const ms = el("span","ms"); ms.id="tcMs";
  ms.textContent = s.open ? "…" : "done";
  hd.appendChild(ms);
  w.appendChild(hd);
  const args = Object.entries(s.args||{}).map(([k,v]) =>
    k+"="+(v===null?"null":Array.isArray(v)?"["+v.join(",")+"]":JSON.stringify(v))).join("   ");
  if(args) w.appendChild(el("div","args", args));
  if(s.open){
    const wt = el("div","waiting");
    wt.appendChild(el("span","spin"));
    wt.appendChild(el("span",null,"waiting on the tool"));
    w.appendChild(wt);
  } else {
    const res = el("div","res");
    const pills = el("div","pills");
    pills.appendChild(el("span","pill "+toneFor(s.status), s.status));
    if(s.rule) pills.appendChild(el("span","pill rule", s.rule));
    res.appendChild(pills);
    if(s.why) res.appendChild(el("p","why", s.why));
    if(s.next && s.next!=="—"){
      const n = el("div","nxt");
      n.appendChild(document.createTextNode("next"));
      n.appendChild(el("span","arrow","→"));
      n.appendChild(el("span","nodechip", s.next));
      res.appendChild(n);
    }
    w.appendChild(res);
  }
  return w;
}

function tcKeys(e){
  if(!modal) return;
  if(e.key==="Escape"){ if(tcClosable()){ e.preventDefault(); tcClose(); } return; }
  if(e.key!=="Tab") return;
  const f = [...modal.box.querySelectorAll('button:not([disabled])')].filter(n => n.offsetParent !== null);
  if(!f.length) return;
  const first=f[0], last=f[f.length-1];
  if(e.shiftKey && document.activeElement===first){ e.preventDefault(); last.focus(); }
  else if(!e.shiftKey && document.activeElement===last){ e.preventDefault(); first.focus(); }
}

function tcClosable(){ return modal && modal.phase==="result"; }

function tcResult(){
  const m = modal, c = state.calls.get(m.callId);
  m.box.appendChild(tcHeader("Call ended", c.id+" · "+dur((c.endedAt||simNow())-c.startedAt)));
  const b = el("div","tc-b");
  const w = el("div","tc-res");
  if(!c.actions.length){
    w.appendChild(el("div","none","The call ended before anything was compiled. In the running bot this path still POSTs — a NO_ACTION carrying the last coded reason — because an empty submit scores as silence."));
  } else {
    c.actions.forEach(a=>{
      const t = el("div","top");
      const k = el("span","k", a.verb + (a.reason ? " · "+a.reason : ""));
      k.style.color = a.verb==="ESCALATE" ? "var(--bad)" : a.verb==="NO_ACTION" ? "var(--none)" : "var(--ok)";
      t.appendChild(k);
      t.appendChild(el("span","d", a.http ? "http "+a.http : "not sent"));
      w.appendChild(t);
      w.appendChild(el("div","none", a.detail));
      w.appendChild(el("div",null,"")).style.height="10px";
    });
  }
  b.appendChild(w);
  m.box.appendChild(b);
  const foot = el("div","tc-f");
  foot.appendChild(el("span","note","The full transcript and decision trail are kept in Conversations."));
  const again = el("button","btn","Call again");
  again.addEventListener("click", ()=>{ tcClose(); tcOpen(); });
  foot.appendChild(again);
  const go = el("button","btn primary","View the full trace");
  go.addEventListener("click", ()=>{ const id=m.callId; tcClose(); setView("calls"); select(id); });
  foot.appendChild(go);
  m.box.appendChild(foot);
}

function tcLive(){
  const m = modal, c = state.calls.get(m.callId);
  m.box.appendChild(tcHeader("Test call", c.id+" · "+(c.from||"no caller id"),{locked:true}));

  const bar = el("div","tc-bar");
  const on = el("div","on"); on.appendChild(el("i")); on.appendChild(el("span",null,"On the line"));
  bar.appendChild(on);
  const elp = el("span","el","0:00"); elp.id="tcEl"; bar.appendChild(elp);
  const mic = el("div","barmic"+(m.muted?" muted":"")); mic.appendChild(el("i"));
  mic.id="tcBarMic"; bar.appendChild(mic);
  const mute = el("button","btn", m.muted ? "Unmute" : "Mute");
  mute.setAttribute("aria-pressed", String(m.muted));
  mute.addEventListener("click", ()=>{ m.muted = !m.muted; tcRender(); });
  bar.appendChild(mute);
  const hang = el("button","btn danger","Hang up");
  hang.addEventListener("click", tcHangUp);
  bar.appendChild(hang);
  m.box.appendChild(bar);

  const b = el("div","tc-b tc-live");
  const nn = el("div","nodenow");
  nn.appendChild(document.createTextNode("Current node"));
  nn.appendChild(el("b",null, c.node || "—"));
  b.appendChild(nn);

  const steps = tcSteps(c);
  const stack = el("div","stack");
  if(!steps.length){
    stack.appendChild(el("div","tc-idle","Connected. Nothing decided yet — the first tool call appears here the moment it fires."));
  } else {
    steps.slice(Math.max(0,steps.length-3), steps.length-1).forEach(s=>{
      const p = el("div","spast");
      p.appendChild(el("span",null,s.name));
      p.appendChild(el("span","arrow","→"));
      p.appendChild(el("span","pill "+toneFor(s.status), s.status||"…"));
      if(s.next && s.next!=="—"){ p.appendChild(el("span","arrow","→")); p.appendChild(el("span",null,s.next)); }
      stack.appendChild(p);
    });
    stack.appendChild(tcStepCard(steps[steps.length-1]));
  }
  b.appendChild(stack);
  m.box.appendChild(b);
  m.lastSteps = steps.length;
}

function toast(c){
  /* you are on the phone; a banner you cannot act on is only noise */
  if(modal && (modal.phase==="live" || modal.phase==="connecting")) return;
  const t = el("div","toast");
  t.appendChild(el("i","lamp live ring"));
  const mid = el("div");
  mid.appendChild(el("div","t1","incoming"));
  mid.appendChild(el("div","t2", c.from || "no caller id"));
  mid.appendChild(el("div","t3", c.id + " · " + c.transport));
  t.appendChild(mid);
  const b = el("button","btn","Watch");
  b.addEventListener("click", ()=>{ setView("calls"); select(c.id); kill(); });
  t.appendChild(b);
  $("#toasts").appendChild(t);
  const kill = ()=>{ if(!t.parentNode) return; t.classList.add("out"); later(()=>t.remove(), 320); };
  later(kill, 5200);
}

const METRICS = [
  {id:"calls",  label:"Calls this shift",
   lede:"Load across the shift, and the calls whose full event log is still in the buffer.",
   cards:[["volume","full"],["recent","full"]]},
  {id:"live",   label:"On the line now",
   lede:"What the switchboard is holding this minute.",
   cards:[["livelines","full"],["volume","full"]]},
  {id:"submit", label:"Submit rate",
   lede:"Every call compiles and POSTs. These three say what was submitted, how far each call got, and why the rest did not book.",
   cards:[["outcome",""],["funnel",""],["reasons","full"]]},
  {id:"length", label:"Median call",
   lede:"How long a call runs, and how fast the agent gets its first word out.",
   cards:[["lengths",""],["latency",""]]},
  {id:"actions",label:"Actions POSTed",
   lede:"One call can POST more than one action, so this outruns the call count.",
   cards:[["verbs",""],["outcome",""],["rails","full"]]}
];

const CARDS = {
  volume:{h:"Calls answered per hour", cap:"08:00 – now",
    lede:"Load, not quality. Each bar is one bucket of the selected range.",
    body:'<div id="volChart"></div><div class="tip" id="volTip"></div>', fn:renderVolume},
  recent:{h:"Recent traces", cap:"open one", lede:"",
    head:'<div class="filters" id="traceFilter"><button data-f="real" aria-pressed="true">Real</button>'+
         '<button data-f="test" aria-pressed="false">Test</button><button data-f="all" aria-pressed="false">All</button></div>',
    body:'<div id="recent"></div>', fn:renderRecent, after:wireFilter},
  livelines:{h:"Lines up right now", cap:"live", lede:"Select one to watch it unfold.",
    body:'<div id="liveLines"></div>', fn:renderLive},
  outcome:{h:"What the shift submitted", cap:"one row per call",
    lede:"Primary outcome per call. A refusal is still a submission — an empty one would score as silence.",
    body:'<div id="outMix"></div>', fn:renderOutcomes},
  funnel:{h:"How far calls got", cap:"by stage",
    lede:"Each drop-off reconciles against the coded reasons — no call leaves the funnel unexplained.",
    body:'<div class="fun" id="funnel"></div>', fn:renderFunnel},
  reasons:{h:"Coded reasons", cap:"",
    lede:"The reason each NO_ACTION or ESCALATE carried. Set by a tool, never by the model.",
    body:'<div class="ranked" id="reasons"></div>', fn:renderReasons},
  rails:{h:"Tools called", cap:"across the shift",
    lede:"How often each tool ran. This graph has no always-on rails, so tool frequency is what shows where the work goes.",
    body:'<div class="ranked" id="rails"></div>', fn:renderRails},
  verbs:{h:"Actions by verb", cap:"",
    lede:"More than one action can come off a single call.",
    body:'<div class="ranked" id="verbs"></div>', fn:renderVerbs},
  lengths:{h:"Call length", cap:"",
    lede:"The bucket holding the median is picked out.",
    body:'<div id="lenChart"></div><div class="tip" id="lenTip"></div>', fn:renderLengths},
  latency:{h:"Time to first word", cap:"call start → first bot audio",
    lede:"A caller hears silence until this clears. The bot speaks a holding line rather than let it run long.",
    body:'<div id="latency"></div>', fn:renderLatency}
};

const FIELD_ORDER = ["caller","patient","patient_id","age","plan","specialty","provider","site","window","referral","rail","reason","directory","booking","scheduling","lookup"];

const MACHINE = new Set(["tool","patch","queue","post"]);

const CONN = ["Requesting microphone","Opening the audio channel","Bot answered"];

/* =================================================================== views */
function hydrateCall(c, detail) {
  c.stream = []; c.trail = []; c.actions = []; c.fields = c.fields || {};
  c.insights = {};
  (detail.events || []).forEach((ev) => applyEvent(c, ev, false));
  const call = detail.call || {};
  c.node = call.node || c.node;
  if (call.patient_name) c.fields.patient = call.patient_name;
  if (call.patient_id) c.fields.patient_id = call.patient_id;
  c.primaryAction = call.primary_action || c.primaryAction;
  c.primaryReason = call.primary_reason || c.primaryReason;
  const fromEvents = (detail.events || [])
    .filter((ev) => ev.kind === "tool.called" && ev.payload && ev.payload.name)
    .map((ev) => ev.payload.name);
  if (fromEvents.length) c.toolNames = fromEvents;
}

async function select(id) {
  state.sel = id;
  const c = state.calls.get(id);
  lineEls.forEach((n, k) => n.classList.toggle("sel", k === id));
  $("#convTtl").textContent = c ? (c.fields.patient || c.label || "identifying…") : "No line selected";
  $("#convSub").textContent = c
    ? c.transport + " · " + (c.from || "no caller id") + " · " + (c.status === "live" ? "live" : "ended")
    : "pick a line on the switchboard";
  if (!c) return;
  const reload = !c._loaded || c.status !== "live";
  if (reload) {
    const detail = await API.call(id);
    if (detail) {
      hydrateCall(c, detail);
      c._loaded = true;
      renderLine(c);
    }
  }
  c._drawn = 0;
  renderStream(c);
  renderWhy(c);
  if (window.innerWidth <= 900) setPane("conv");
}

function formatOpened(ms) {
  const d = new Date(ms);
  const hm = hhmmss(ms).slice(0, 5);
  if (adminPeriod === "today") return hm;
  return d.getDate() + " " + "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ")[d.getMonth()] + " " + hm;
}

function renderOverview() {
  renderKpis(); renderDrill();
  syncPeriodHead();
}

function syncPeriodButtons() {
  document.querySelectorAll("#periodPicker button[data-period]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.period === adminPeriod)));
}

function syncPeriodHead() {
  const meta = periodMeta(adminPeriod);
  const title = $("#shiftTitle");
  if (title) title.textContent = meta.title;
  const live = liveNow();
  const liveTxt = live ? live + (live === 1 ? " call live" : " calls live") : "switchboard quiet";
  const l = $("#shiftLive");
  if (l) l.textContent = liveTxt;
  const range = $("#shiftRange");
  if (!range) return;
  if (adminPeriod === "today") {
    range.innerHTML = "";
    range.appendChild(document.createTextNode((SHIFT.from || "08:00") + "–"));
    const nowEl = el("span", null, hhmmss(simNow()).slice(0, 5));
    nowEl.id = "shiftNow";
    range.appendChild(nowEl);
    range.appendChild(document.createTextNode(" Europe/Madrid"));
  } else {
    range.textContent = meta.range + " · Europe/Madrid";
  }
}

function replaceCalls(summaries) {
  const next = new Map();
  const order = [];
  (summaries || []).forEach((s) => {
    const c = fromSummary(s);
    next.set(c.id, c);
    order.push(c.id);
  });
  state.calls = next;
  state.order = order;
  if (state.sel && !state.calls.has(state.sel)) {
    state.sel = null;
    if (stream) stream.innerHTML = "";
    renderWhy(null);
  }
  if (board) board.innerHTML = "";
  lineEls.clear();
  state.order.slice().reverse().forEach((id) => renderLine(state.calls.get(id)));
  if (!state.order.length && $("#boardCount")) $("#boardCount").textContent = "0 lines";
}

async function setAdminPeriod(period) {
  if (!period || period === adminPeriod) {
    syncPeriodButtons();
    return;
  }
  const req = ++periodReq;
  adminPeriod = period;
  syncPeriodButtons();
  syncPeriodHead();
  try {
    const [shift, calls] = await Promise.all([API.shift(period), API.calls(period)]);
    if (req !== periodReq) return;
    replaceCalls(calls.calls || []);
    mapShift(shift);
    lastKpiSig = ""; lastDrill = null;
    refreshAll();
  } catch (err) {
    console.error("Could not load period", period, err);
  }
}

function refreshAll() {
  renderTally();
  if (view === "overview") renderOverview();
}

/* ============================================================ the live wire */
let ws = null, wsRetry = 0;
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + "/observability/live");
  ws.onopen = () => { wsRetry = 0; setWire(true); };
  ws.onclose = () => {
    setWire(false);
    wsRetry = Math.min(wsRetry + 1, 6);
    later(connect, 500 * Math.pow(2, wsRetry - 1));
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
  ws.onmessage = (m) => {
    let msg; try { msg = JSON.parse(m.data); } catch (_) { return; }
    if (msg.kind === "snapshot") { onSnapshot(msg); return; }
    onEvent(msg);
  };
}
function setWire(ok) {
  const w = $("#wire"); if (!w) return;
  w.classList.toggle("is-down", !ok);
  const live = liveNow();
  $("#wireTxt").textContent = ok
    ? "live · " + live + " on the line"
    : "reconnecting…";
}
function onSnapshot(msg) {
  if (msg.shift && adminPeriod === "today") mapShift(msg.shift);
  (msg.calls || []).forEach((s) => {
    const c = fromSummary(s);
    if (!state.calls.has(c.id)) { state.calls.set(c.id, c); state.order.push(c.id); }
  });
  state.order.sort((a, b) => state.calls.get(b).startedAt - state.calls.get(a).startedAt);
  board.innerHTML = ""; lineEls.clear();
  state.order.slice().reverse().forEach((id) => renderLine(state.calls.get(id)));
  lastKpiSig = ""; lastDrill = null;
  if (adminPeriod !== "today") {
    API.shift(adminPeriod).then((s) => {
      mapShift(s); lastKpiSig = ""; lastDrill = null; refreshAll();
    }).catch(() => refreshAll());
  } else {
    refreshAll();
  }
  if (state.sel && state.calls.has(state.sel)) select(state.sel);
}
function onEvent(ev) {
  if (!ev || !ev.call_id) return;
  let c = twinCall(ev.call_id, ts(ev.ts));
  if (!c) {
    c = blankCall(ev.call_id);
    c.ringing = true;
    state.calls.set(c.id, c);
    state.order.unshift(c.id);
    c._loaded = true; /* born live: every event arrives on this socket */
  }
  applyEvent(c, ev, true);
  if (ev.kind === "call.started") {
    toast(c);
    /* A test dial sets awaitingCall before /api/offer; any new call that
       arrives while we wait is the one we placed — bind even if is_test was
       missing on an older bot build. */
    if (modal && modal.awaitingCall && (c.test || c.transport === "webrtc" || c.transport === "unknown"))
      bindTestCall(c.id);
    else if (state.follow && view === "calls") select(c.id);
  }
  if (c.ringing && (ev.kind === "transcript.bot" || ev.kind === "transcript.user")) c.ringing = false;
  renderLine(c);
  if (state.sel === c.id) { appendStream(c); renderWhy(c); }
  if (ev.kind === "call.ended" || ev.kind === "submit.posted" || ev.kind === "call.started") {
    API.shift().then((s) => { mapShift(s); lastKpiSig = ""; lastDrill = null; refreshAll(); }).catch(() => {});
    if (ev.kind === "call.ended") {
      API.calls().then((d) => {
        const keepSel = state.sel;
        replaceCalls(d.calls || []);
        if (keepSel && state.calls.has(keepSel)) select(keepSel);
        else if (keepSel) {
          const still = [...state.calls.values()].find((c) =>
            c.elevenConversationId === keepSel || c.id === keepSel);
          if (still) select(still.id);
        }
      }).catch(() => {});
    }
  } else {
    refreshAll();
  }
  tcOnEvent(c);
}

/* ========================================================== the test call */
let modal = null;
const MIC_BARS = 24;

function tcOpen() {
  if (modal) return;
  modal = { phase: "connecting", step: 0, callId: null, lastSteps: -1, muted: false,
            tickTimer: null, awaitingCall: false, pc: null, mic: null, audio: null,
            analyser: null, level: 0 };
  const scrim = el("div", "scrim");
  const box = el("div", "tc");
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-labelledby", "tcTtl");
  scrim.appendChild(box);
  document.body.appendChild(scrim);
  document.body.style.overflow = "hidden";
  modal.scrim = scrim; modal.box = box;
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim && tcClosable()) tcClose(); });
  document.addEventListener("keydown", tcKeys, true);
  tcRender();
  dial();
}
function tcClose() {
  if (!modal) return;
  clearInterval(modal.tickTimer);
  document.removeEventListener("keydown", tcKeys, true);
  teardownCall();
  modal.scrim.remove();
  document.body.style.overflow = "";
  modal = null;
}
function teardownCall() {
  if (!modal) return;
  try { if (modal.pc) modal.pc.close(); } catch (_) {}
  try { if (modal.mic) modal.mic.getTracks().forEach((t) => t.stop()); } catch (_) {}
  try { if (modal.audioCtx) modal.audioCtx.close(); } catch (_) {}
  if (modal.audio) { modal.audio.srcObject = null; modal.audio.remove(); }
  modal.pc = null; modal.mic = null; modal.audio = null; modal.analyser = null;
}

function tcFail(title, detail, hint) {
  if (!modal) return;
  modal.phase = "error";
  modal.error = { title, detail, hint };
  teardownCall();
  tcRender();
}
function tcError() {
  const m = modal;
  m.box.appendChild(tcHeader("Could not place the call", ""));
  const b = el("div", "tc-b");
  const w = el("div", "tc-res");
  const t = el("div", "top");
  const k = el("span", "k", m.error.title); k.style.color = "var(--bad)";
  t.appendChild(k);
  w.appendChild(t);
  w.appendChild(el("div", "none", m.error.detail));
  if (m.error.hint) {
    const h = el("div", "none", m.error.hint);
    h.style.cssText = "margin-top:10px;font-family:var(--mono);font-size:11.5px;color:var(--faint)";
    w.appendChild(h);
  }
  b.appendChild(w);
  m.box.appendChild(b);
  const foot = el("div", "tc-f");
  foot.appendChild(el("span", "note", ""));
  const retry = el("button", "btn primary", "Try again");
  retry.addEventListener("click", () => { tcClose(); tcOpen(); });
  foot.appendChild(retry);
  m.box.appendChild(foot);
}

async function dial() {
  const m = modal;
  /* 1 — the microphone */
  try {
    m.mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    tcFail("The microphone was blocked",
      "The browser refused access to the microphone, so there is nothing to send down the line.",
      "Allow it for this site and try again.");
    return;
  }
  if (!modal) return;
  m.step = 1; tcRender();
  meterFrom(m.mic);

  /* 2 — the audio channel */
  try {
    const pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
    m.pc = pc;
    m.mic.getTracks().forEach((t) => pc.addTrack(t, m.mic));
    pc.addTransceiver("audio", { direction: "recvonly" });
    const audio = document.createElement("audio");
    audio.autoplay = true;
    document.body.appendChild(audio);
    m.audio = audio;
    pc.ontrack = (e) => { audio.srcObject = e.streams[0]; };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await iceSettled(pc, 2500);

    m.awaitingCall = true;
    const res = await fetch("/api/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
    });
    if (!res.ok) {
      tcFail("The bot did not answer",
        res.status === 404
          ? "There is no WebRTC endpoint on this server."
          : "The server refused the offer (http " + res.status + ").",
        "Start the bot with:  uv run bot.py -t webrtc");
      return;
    }
    const answer = await res.json();
    if (!modal) return;
    await pc.setRemoteDescription(answer);
  } catch (err) {
    tcFail("The audio channel did not open",
      "The offer was sent but the connection could not be negotiated.",
      String(err && err.message ? err.message : err));
    return;
  }
  if (!modal) return;
  m.step = 2; tcRender();
  /* 3 — the bot answers: the call appears on the observability socket, and
     bindTestCall takes the modal live. If it never does, say so. */
  later(() => {
    if (modal && modal.phase === "connecting") {
      tcFail("The line opened but no call started",
        "Audio is connected, yet the bot never registered a call.",
        "Check the bot's log for a startup error.");
    }
  }, 12000);
}
function iceSettled(pc, timeoutMs) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { pc.removeEventListener("icegatheringstatechange", check); resolve(); };
    const check = () => { if (pc.iceGatheringState === "complete") done(); };
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(done, timeoutMs);
  });
}
function meterFrom(streamIn) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const src = ctx.createMediaStreamSource(streamIn);
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    src.connect(an);
    modal.audioCtx = ctx;
    modal.analyser = an;
    modal.buf = new Uint8Array(an.frequencyBinCount);
  } catch (_) { /* meter is a nicety; the call works without it */ }
}
function micLevel() {
  if (!modal || !modal.analyser || modal.muted) return 0;
  modal.analyser.getByteTimeDomainData(modal.buf);
  let peak = 0;
  for (let i = 0; i < modal.buf.length; i++) peak = Math.max(peak, Math.abs(modal.buf[i] - 128));
  return Math.min(1, peak / 90);
}

function bindTestCall(id) {
  const m = modal;
  m.awaitingCall = false;
  m.callId = id;
  m.phase = "live";
  m.lastSteps = -1;
  tcRender();
  clearInterval(m.tickTimer);
  m.tickTimer = setInterval(tcTick, 100);
}
function tcSteps(c) { return c.stream.filter((i) => i.t === "tool"); }
function tcTick() {
  if (!modal || modal.phase !== "live") return;
  const c = state.calls.get(modal.callId); if (!c) return;
  const e = document.getElementById("tcEl");
  if (e) e.textContent = dur(simNow() - c.startedAt);
  const bm = document.getElementById("tcBarMic");
  if (bm) bm.querySelector("i").style.width = (micLevel() * 100) + "%";
  const steps = tcSteps(c);
  const last = steps[steps.length - 1];
  if (last && last.open) {
    const ms = document.getElementById("tcMs");
    if (ms) { const d = simNow() - last.ts; ms.textContent = d < 1000 ? Math.round(d) + "ms" : (d / 1000).toFixed(1) + "s"; }
  }
}
function tcOnEvent(c) {
  if (!modal || modal.phase !== "live" || modal.callId !== c.id) return;
  if (c.status === "ended") { tcShowResult(); return; }
  const steps = tcSteps(c);
  const last = steps[steps.length - 1];
  if (steps.length !== modal.lastSteps || (last && !last.open && !modal.box.querySelector(".snow .res"))) {
    tcRender();
  } else {
    const nb = modal.box.querySelector(".nodenow b");
    if (nb) nb.textContent = c.node || "—";
  }
}
function tcHangUp() {
  teardownCall();
  tcShowResult();
}
function tcShowResult() {
  clearInterval(modal.tickTimer);
  teardownCall();
  modal.phase = "result";
  tcRender();
}

/* ===================================================================== auth */
const WEEKDAYS = [
  ["monday", "Lun"],
  ["tuesday", "Mar"],
  ["wednesday", "Mié"],
  ["thursday", "Jue"],
  ["friday", "Vie"],
  ["saturday", "Sáb"],
];
const WEEKDAY_LABEL = Object.fromEntries(WEEKDAYS);
let calWeekStart = null; // ISO Monday of the visible week
let calProviderId = null;
let calPollTimer = null;
let calBusy = false;
let calLastSig = "";
let calBuiltWeek = null;
const CAL_POLL_MS = 2000;
const CAL_START = 8 * 60;
const CAL_END = 20 * 60;
const CAL_PX = 1.1;

function showOnly(which) {
  $("#viewLogin").hidden = which !== "login";
  $("#shellAdmin").hidden = which !== "admin";
  $("#shellDoctor").hidden = which !== "doctor";
  document.title = which === "admin" ? "Arenal Centralita"
    : which === "doctor" ? "Mi horario · Clínica Arenal"
    : "Clínica Arenal";
  if (which !== "doctor") {
    stopCalPoll();
    inboxOpen = false;
    const panel = $("#inboxPanel");
    if (panel) panel.hidden = true;
  }
}

function insurerName(ref) {
  if (!ref) return "—";
  if (typeof ref === "string") return ref;
  return ref.name || ref.id || "—";
}

function chipGroup(title, items, cls) {
  const g = el("div", "chip-group");
  g.appendChild(el("h3", null, title));
  const row = el("div", "chips");
  if (!items || !items.length) {
    row.appendChild(el("span", "chip", "—"));
  } else {
    items.forEach((t) => row.appendChild(el("span", "chip" + (cls ? " " + cls : ""), t)));
  }
  g.appendChild(row);
  return g;
}

function mondayOf(isoDate) {
  const d = new Date(isoDate + "T12:00:00");
  const day = (d.getDay() + 6) % 7; // Mon=0
  d.setDate(d.getDate() - day);
  return d.toISOString().slice(0, 10);
}

function shiftWeek(isoMonday, deltaWeeks) {
  const d = new Date(isoMonday + "T12:00:00");
  d.setDate(d.getDate() + deltaWeeks * 7);
  return d.toISOString().slice(0, 10);
}

function fmtDayLabel(iso) {
  const d = new Date(iso + "T12:00:00");
  return pad(d.getDate()) + "/" + pad(d.getMonth() + 1);
}

function minutesOf(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

function blockKey(date, b) {
  return [date, b.start, b.end, b.kind, b.source || "", b.label || ""].join("|");
}

function isCitaBlock(b) {
  return b.kind === "booked" || b.kind === "emergency";
}

function calSignature(data) {
  // The reason a day is empty is part of what is drawn, so a day that becomes
  // closed — or stops being — has to count as a change worth redrawing.
  return JSON.stringify((data.days || []).map((d) => [d.date, d.note || "", d.blocks || []]));
}

function stopCalPoll() {
  if (calPollTimer != null) {
    clearInterval(calPollTimer);
    calPollTimer = null;
  }
}

function startCalPoll() {
  stopCalPoll();
  calPollTimer = setInterval(() => {
    if ($("#shellDoctor").hidden) return;
    loadDoctorCalendar(calWeekStart, { silent: true });
    loadDoctorInbox({ silent: true });
  }, CAL_POLL_MS);
}

/* ----------------------------------------------------------------- inbox */
let inboxOpen = false;
let inboxLastSig = "";
let inboxKnownIds = new Set();
let inboxBootstrapped = false;

function doctorToast(kind, title, body) {
  const t = el("div", "toast " + (kind === "emergency" ? "urgency" : "quiet"));
  t.appendChild(el("i", "lamp " + (kind === "emergency" ? "live ring" : "")));
  const mid = el("div");
  mid.appendChild(el("div", "t1", kind === "emergency" ? "urgencia" : "aviso"));
  mid.appendChild(el("div", "t2", title));
  if (body) mid.appendChild(el("div", "t3", body));
  t.appendChild(mid);
  const b = el("button", "btn", "Buzón");
  b.addEventListener("click", () => { openInbox(true); kill(); });
  t.appendChild(b);
  $("#toasts").appendChild(t);
  const kill = () => { if (!t.parentNode) return; t.classList.add("out"); later(() => t.remove(), 320); };
  later(kill, kind === "emergency" ? 7000 : 4500);
}

function setInboxBadge(n) {
  const badge = $("#inboxBadge");
  if (!badge) return;
  if (n > 0) {
    badge.hidden = false;
    badge.textContent = n > 99 ? "99+" : String(n);
  } else {
    badge.hidden = true;
    badge.textContent = "0";
  }
}

function renderInboxList(notes) {
  const list = $("#inboxList");
  if (!list) return;
  list.textContent = "";
  if (!notes || !notes.length) {
    list.appendChild(el("div", "inbox-idle", "Sin avisos."));
    return;
  }
  notes.forEach((n) => {
    const item = el("button", "inbox-item kind-" + (n.kind || "cancel") + (n.unread ? " unread" : ""));
    item.type = "button";
    item.appendChild(el("div", "ik", n.kind === "emergency" ? "Urgencia" : "Cancelación"));
    item.appendChild(el("div", "it", n.title || "Aviso"));
    item.appendChild(el("div", "ib", n.body || ""));
    const when = n.created_at ? new Date(n.created_at).toLocaleString("es-ES", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"
    }) : "";
    item.appendChild(el("div", "iw", when));
    item.addEventListener("click", async () => {
      if (n.unread) {
        try {
          const res = await API.inboxRead(n.id);
          setInboxBadge(res.unread || 0);
          n.unread = false;
          item.classList.remove("unread");
        } catch (err) { console.error(err); }
      }
    });
    list.appendChild(item);
  });
}

function openInbox(forceOpen) {
  const panel = $("#inboxPanel");
  const btn = $("#btnInbox");
  if (!panel || !btn) return;
  if (forceOpen === true) inboxOpen = true;
  else if (forceOpen === false) inboxOpen = false;
  else inboxOpen = !inboxOpen;
  panel.hidden = !inboxOpen;
  btn.setAttribute("aria-expanded", String(inboxOpen));
  if (inboxOpen) loadDoctorInbox({ silent: false });
}

async function loadDoctorInbox(opts) {
  const silent = !!(opts && opts.silent);
  try {
    const data = await API.inbox();
    const notes = data.notifications || [];
    const sig = JSON.stringify(notes.map((n) => [n.id, n.unread, n.kind]));
    if (silent && sig === inboxLastSig) return;
    inboxLastSig = sig;
    setInboxBadge(data.unread || 0);
    if (inboxOpen || !silent) renderInboxList(notes);

    const ids = new Set(notes.map((n) => n.id));
    if (inboxBootstrapped) {
      notes.forEach((n) => {
        if (inboxKnownIds.has(n.id)) return;
        if (n.kind === "emergency") {
          doctorToast("emergency", n.title || "Urgencia", n.body);
        } else if (n.kind === "cancel") {
          doctorToast("cancel", n.title || "Cita cancelada", n.body);
        }
      });
    }
    inboxKnownIds = ids;
    inboxBootstrapped = true;
  } catch (err) {
    console.error(err);
  }
}

function makeCalBlock(b, calStart, calEnd, px, animateIn) {
  const s = minutesOf(b.start);
  const e = minutesOf(b.end);
  if (e <= calStart || s >= calEnd) return null;
  const top = Math.max(s, calStart) - calStart;
  const blockH = Math.max(12, (Math.min(e, calEnd) - Math.max(s, calStart)) * px - 2);
  const node = el("div", "cal-block " + b.kind);
  node.style.top = (top * px) + "px";
  node.style.height = blockH + "px";
  node.appendChild(el("span", "t", b.start + "–" + b.end));
  node.appendChild(el("span", "l", b.label || b.kind));
  if (animateIn && isCitaBlock(b) && !REDUCED) {
    node.classList.add("is-enter");
    later(() => node.classList.remove("is-enter"), 700);
  }
  return node;
}

function renderCalendar(data, opts) {
  const silent = !!(opts && opts.silent);
  const wrap = $("#docCalendar");
  const note = $("#calNote");
  const range = $("#calRange");
  if (!wrap) return;
  calWeekStart = data.date_from;
  range.textContent = fmtDayLabel(data.date_from) + " – " + fmtDayLabel(data.date_to);
  if (data.source === "availability") {
    note.textContent = "En vivo · huecos de la clínica; el resto del horario aparece como cita.";
  } else if (String(data.source || "").startsWith("availability_error")) {
    note.textContent = "En vivo · sin ocupación remota; el horario se muestra como libre.";
  } else {
    note.textContent = "En vivo";
  }

  const days = (data.days || []).filter((d) => d.weekday !== "sunday");
  const sameWeek = silent && calBuiltWeek === data.date_from && wrap.querySelector(".cal-grid");
  if (!sameWeek) {
    wrap.textContent = "";
    wrap.appendChild(buildCalColumns(days, CAL_START, CAL_END, CAL_PX, !silent));
    calBuiltWeek = data.date_from;
    return;
  }
  patchCalColumns(wrap.querySelector(".cal-grid"), days, CAL_START, CAL_END, CAL_PX);
}

function buildCalColumns(days, calStart, calEnd, px, animateCitas) {
  const height = (calEnd - calStart) * px;
  const root = el("div", "cal-grid");
  root.style.gridTemplateRows = "auto " + height + "px";

  root.appendChild(el("div", "cal-corner", ""));
  days.forEach((d) => {
    const h = el("div", "cal-dayhead");
    h.appendChild(el("b", null, WEEKDAY_LABEL[d.weekday] || d.weekday));
    h.appendChild(el("span", null, fmtDayLabel(d.date)));
    // A plain day off needs no flag in the header — it is the normal case, and
    // the column says so. Only what reception or the clinic changed does.
    if (d.note && d.note_kind !== "off") {
      h.appendChild(el("span", "cal-note", d.note));
    }
    root.appendChild(h);
  });

  const hoursCol = el("div", null);
  hoursCol.style.position = "relative";
  hoursCol.style.borderRight = "1px solid var(--line-soft)";
  for (let m = calStart; m < calEnd; m += 60) {
    const lab = el("div", "cal-hour", pad(Math.floor(m / 60)) + ":00");
    lab.style.position = "absolute";
    lab.style.top = ((m - calStart) * px) + "px";
    lab.style.right = "4px";
    lab.style.left = "0";
    lab.style.border = "0";
    lab.style.background = "transparent";
    hoursCol.appendChild(lab);
  }
  root.appendChild(hoursCol);

  days.forEach((d) => {
    const col = el("div", "cal-cell");
    col.dataset.date = d.date;
    col.style.height = height + "px";
    // The reason goes in the gap itself. A greyed-out column still leaves the
    // doctor guessing whether the day is theirs off, the clinic's, or
    // reception's doing; the words are the whole point of showing it at all.
    if (d.note) {
      col.classList.add("is-off");
      const why = el("div", "cal-off " + (d.note_kind || "off"));
      why.appendChild(el("span", "l", d.note));
      if (d.note_detail) why.appendChild(el("span", "d", d.note_detail));
      col.appendChild(why);
    }
    (d.blocks || []).forEach((b) => {
      const node = makeCalBlock(b, calStart, calEnd, px, animateCitas);
      if (!node) return;
      node.dataset.key = blockKey(d.date, b);
      col.appendChild(node);
    });
    root.appendChild(col);
  });
  return root;
}

function patchCalColumns(root, days, calStart, calEnd, px) {
  if (!root) return;
  const cols = {};
  root.querySelectorAll(".cal-cell[data-date]").forEach((c) => { cols[c.dataset.date] = c; });
  days.forEach((d) => {
    const col = cols[d.date];
    if (!col) return;
    const next = new Map();
    (d.blocks || []).forEach((b) => next.set(blockKey(d.date, b), b));
    const existing = new Map();
    Array.from(col.querySelectorAll(".cal-block")).forEach((n) => {
      if (n.classList.contains("is-leave")) return;
      existing.set(n.dataset.key, n);
    });

    existing.forEach((node, key) => {
      if (next.has(key)) return;
      const wasCita = node.classList.contains("booked") || node.classList.contains("emergency");
      if (wasCita && !REDUCED) {
        node.classList.add("is-leave");
        later(() => { if (node.parentNode) node.parentNode.removeChild(node); }, 380);
      } else if (node.parentNode) {
        node.parentNode.removeChild(node);
      }
    });

    next.forEach((b, key) => {
      if (existing.has(key)) return;
      const node = makeCalBlock(b, calStart, calEnd, px, true);
      if (!node) return;
      node.dataset.key = key;
      col.appendChild(node);
    });
  });
}

async function loadDoctorCalendar(weekStart, opts) {
  const silent = !!(opts && opts.silent);
  if (calBusy) return;
  calBusy = true;
  const wrap = $("#docCalendar");
  if (!silent && wrap) wrap.innerHTML = '<div class="cal-idle">Cargando agenda…</div>';
  try {
    const data = await API.calendar(weekStart || undefined);
    const sig = calSignature(data);
    if (silent && sig === calLastSig) return;
    calLastSig = sig;
    renderCalendar(data, { silent });
  } catch (err) {
    console.error(err);
    if (!silent && wrap) wrap.innerHTML = '<div class="cal-idle">No se pudo cargar la agenda.</div>';
  } finally {
    calBusy = false;
  }
}

function renderDoctorSchedule(provider) {
  $("#docName").textContent = provider.name;
  $("#docMeta").textContent = provider.specialty_name + " · " + provider.id;
  $("#docBadge").textContent = provider.id;

  const leave = $("#docLeave");
  if (provider.leave) {
    leave.hidden = false;
    leave.textContent = "";
    const strong = el("strong", null, "De baja");
    leave.appendChild(strong);
    leave.appendChild(document.createTextNode(
      " del " + provider.leave.start + " al " + provider.leave.end
      + (provider.leave.reason ? " (" + provider.leave.reason + ")" : "") + "."
    ));
  } else {
    leave.hidden = true;
    leave.textContent = "";
  }

  const schedules = provider.schedules || [];
  const wrap = $("#docSchedule");
  wrap.textContent = "";
  const table = el("table", "schedule");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  hr.appendChild(el("th", null, "Sede"));
  WEEKDAYS.forEach(([, label]) => hr.appendChild(el("th", null, label)));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  if (!schedules.length) {
    const tr = document.createElement("tr");
    const td = el("td", "empty", "Sin horario en el catálogo");
    td.colSpan = 7;
    tr.appendChild(td);
    tbody.appendChild(tr);
  } else {
    schedules.forEach((site) => {
      const byDay = {};
      (site.days || []).forEach((d) => {
        byDay[d.weekday] = (d.intervals || []).join(", ");
      });
      const tr = document.createElement("tr");
      tr.appendChild(el("th", null, site.location_name || site.location_id));
      WEEKDAYS.forEach(([key]) => {
        const text = byDay[key];
        tr.appendChild(el("td", text ? null : "empty", text || "—"));
      });
      tbody.appendChild(tr);
    });
  }
  table.appendChild(tbody);
  wrap.appendChild(table);

  const chips = $("#docChips");
  chips.textContent = "";
  chips.appendChild(chipGroup("Idiomas", provider.languages));
  chips.appendChild(chipGroup("Tipos de cita", provider.appointment_type_names));
  chips.appendChild(chipGroup(
    "Seguros aceptados",
    (provider.accepted_insurers || []).map(insurerName),
    "ok"
  ));
  chips.appendChild(chipGroup(
    "Seguros rechazados",
    (provider.refused_insurers || []).map(insurerName),
    "bad"
  ));
}

async function doLogout() {
  stopCalPoll();
  calLastSig = "";
  calBuiltWeek = null;
  adminPeriod = "today";
  periodReq++;
  syncPeriodButtons();
  try { if (ws) ws.close(); } catch (_) {}
  ws = null;
  state.calls.clear();
  state.order = [];
  state.sel = null;
  lineEls.clear();
  if (board) board.textContent = "";
  await API.logout();
  showLogin();
}

function showLogin() {
  showOnly("login");
  const err = $("#loginError");
  err.hidden = true;
  err.textContent = "";
  $("#loginKey").value = "";
  later(() => $("#loginKey").focus(), 50);
}

async function enterSession(session) {
  if (session.role === "admin") {
    showOnly("admin");
    await bootAdmin();
    return;
  }
  if (session.role === "provider" && session.provider) {
    showOnly("doctor");
    $("#docClock").textContent = hhmmss(simNow()) + " Europe/Madrid";
    calProviderId = session.provider.id;
    calLastSig = "";
    calBuiltWeek = null;
    inboxLastSig = "";
    inboxKnownIds = new Set();
    inboxBootstrapped = false;
    inboxOpen = false;
    const panel = $("#inboxPanel");
    if (panel) panel.hidden = true;
    renderDoctorSchedule(session.provider);
    await loadDoctorCalendar(calWeekStart);
    await loadDoctorInbox({ silent: false });
    startCalPoll();
    return;
  }
  showLogin();
}

async function bootAdmin() {
  $("#clock").textContent = hhmmss(simNow()) + " Europe/Madrid";
  try {
    const [shift, calls] = await Promise.all([API.shift(), API.calls()]);
    mapShift(shift);
    (calls.calls || []).forEach((s) => {
      const c = fromSummary(s);
      state.calls.set(c.id, c);
      state.order.push(c.id);
    });
    state.order.sort((a, b) => state.calls.get(b).startedAt - state.calls.get(a).startedAt);
    state.order.slice().reverse().forEach((id) => renderLine(state.calls.get(id)));
  } catch (err) {
    console.error("Could not reach the observability API", err);
  }
  renderTally();
  setView("overview");
  connect();
  loadInsights();
}

/* ===================================================================== boot */
document.querySelectorAll(".nav button").forEach((b) =>
  b.addEventListener("click", () => setView(b.dataset.view)));
document.querySelectorAll("#periodPicker button[data-period]").forEach((b) =>
  b.addEventListener("click", () => setAdminPeriod(b.dataset.period)));

$("#ntcAdd").addEventListener("click", openNoticeDialog);
$("#ntcKind").addEventListener("change", syncNoticeFields);
$("#ntcSave").addEventListener("click", submitNotice);
$("#ntcCancel").addEventListener("click", () => $("#ntcDialog").close());
wireInsightUi();
$("#ntcToneClear").addEventListener("click", () => {
  $("#ntcToneText").value = "";
  $("#ntcToneUntil").value = "";
  saveTone();
});
// The tone saves on blur rather than behind its own button: it is one field,
// and a tone left typed but unsaved is a tone that silently does nothing.
$("#ntcToneText").addEventListener("change", saveTone);
$("#ntcToneUntil").addEventListener("change", saveTone);
document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => setPane(b.dataset.pane)));
$("#btnPlace").addEventListener("click", tcOpen);
$("#btnTrace").addEventListener("click", (e) => {
  state.trace = !state.trace;
  e.target.setAttribute("aria-pressed", String(state.trace));
  const c = state.calls.get(state.sel); if (c) { c._drawn = 0; renderStream(c); }
});
$("#btnFollow").addEventListener("click", (e) => {
  state.follow = !state.follow;
  e.target.setAttribute("aria-pressed", String(state.follow));
});
$("#btnLogoutAdmin").addEventListener("click", () => doLogout());
$("#btnLogoutDoctor").addEventListener("click", () => doLogout());
$("#btnInbox").addEventListener("click", (e) => {
  e.stopPropagation();
  openInbox();
});
$("#btnInboxClose").addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  openInbox(false);
});
$("#btnInboxReadAll").addEventListener("click", async (e) => {
  e.preventDefault();
  e.stopPropagation();
  try {
    const res = await API.inboxReadAll();
    setInboxBadge(res.unread || 0);
    inboxLastSig = "";
    await loadDoctorInbox({ silent: false });
  } catch (err) { console.error(err); }
});
$("#calPrev").addEventListener("click", () => {
  const base = calWeekStart || mondayOf(new Date().toISOString().slice(0, 10));
  calLastSig = "";
  calBuiltWeek = null;
  loadDoctorCalendar(shiftWeek(base, -1));
});
$("#calNext").addEventListener("click", () => {
  const base = calWeekStart || mondayOf(new Date().toISOString().slice(0, 10));
  calLastSig = "";
  calBuiltWeek = null;
  loadDoctorCalendar(shiftWeek(base, 1));
});
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("#loginKey").value.trim();
  const err = $("#loginError");
  err.hidden = true;
  if (!key) return;
  try {
    const session = await API.login(key);
    await enterSession(session);
  } catch (_) {
    err.textContent = "Clave no reconocida. Prueba admin o un id de médico (PR01…PR12).";
    err.hidden = false;
  }
});

setInterval(() => {
  const clock = $("#clock");
  if (clock && !$("#shellAdmin").hidden) clock.textContent = hhmmss(simNow()) + " Europe/Madrid";
  const docClock = $("#docClock");
  if (docClock && !$("#shellDoctor").hidden) docClock.textContent = hhmmss(simNow()) + " Europe/Madrid";
  state.order.forEach((id) => {
    const c = state.calls.get(id);
    if (c.status === "live") {
      const n = lineEls.get(id);
      if (n) { const e = n.querySelector(".el"); if (e) e.textContent = dur(simNow() - c.startedAt); }
    }
  });
  const nb = $("#navBadge");
  if (nb) { const l = liveNow(); nb.textContent = String(l); nb.hidden = l === 0; }
  if (view === "overview" && !$("#shellAdmin").hidden) {
    refreshLive();
    const n = $("#shiftNow");
    if (n && adminPeriod === "today") n.textContent = hhmmss(simNow()).slice(0, 5);
    const l = $("#shiftLive");
    if (l) {
      const live = liveNow();
      l.textContent = live ? live + (live === 1 ? " call live" : " calls live") : "switchboard quiet";
    }
  }
}, 1000);

(async function start() {
  try {
    const session = await API.me();
    if (session) {
      await enterSession(session);
      return;
    }
  } catch (err) {
    console.error("Could not reach auth", err);
  }
  showLogin();
})();
})();
