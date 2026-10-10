/* Threadline operator console.
 *
 * Everything the console shows comes from two server objects:
 *   conv   – snapshot of one conversation: raw append-only log, in-flight messages
 *   result – output of POST /recover: decisions, recovered context, metrics
 * The "Animate replay" option reveals result.decisions one log entry at a time
 * so viewers can watch the context being rebuilt.
 */

const $ = (sel) => document.querySelector(sel);
const LANES = ["web-chat", "planner-agent", "tool-agent", "gateway"];
const LANE_COLOR = { "web-chat": "var(--web)", "planner-agent": "var(--planner)", "tool-agent": "var(--tool)", gateway: "var(--sig)" };
const STEP_MS = 420;

const state = {
  catalogue: null,
  scenario: "chaos",
  faults: new Set(),
  conv: null,
  result: null,
  reveal: Infinity,
  view: "sequence",
  selected: null,
  busy: false,
  timer: null,
  finishReplay: null,
  confirmReset: false,
};

// ---------------------------------------------------------------- helpers
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const short = (id, n = 10) => (id ? (id.length > n + 4 ? id.slice(0, n + 4) + "…" : id) : "—");

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data;
  try { data = await res.json(); } catch { data = { error: { message: `HTTP ${res.status}` } }; }
  if (!res.ok) {
    const err = new Error(data.error?.message || `HTTP ${res.status}`);
    err.details = data.error?.details;
    throw err;
  }
  return data;
}

function notify(html, isError = false) {
  const el = $("#notice");
  el.innerHTML = html;
  el.className = "notice" + (isError ? " err" : "");
  el.hidden = !html;
}

function failed(err) {
  const details = Array.isArray(err.details) ? `<br>• ${err.details.map(esc).join("<br>• ")}` : "";
  notify(`<b>${esc(err.message)}</b>${details}`, true);
}

const STATUS_TONE = {
  consistent: "ok", recovered: "ok", recovered_with_conflicts: "warn", incomplete: "bad",
  interrupted: "bad", awaiting_recovery: "info", resumed: "info", active: "info", empty: "neutral",
};
const pill = (status) => `<span class="pill ${STATUS_TONE[status] || "neutral"}">${esc(status.replace(/_/g, " "))}</span>`;

function verdictOf(decision) {
  if (!decision) return { cls: "pending", text: "not replayed" };
  if (decision.verdict === "accepted" && decision.rule === "REORDERED") return { cls: "reordered", text: "reordered" };
  return { cls: decision.verdict, text: decision.verdict };
}

// --------------------------------------------- derived view (animation aware)
function derived() {
  const r = state.result;
  if (!r) return null;
  const full = state.reveal >= r.decisions.length;
  const decisions = r.decisions.slice(0, state.reveal);
  const byId = Object.fromEntries(decisions.map((d) => [d.event_id, d]));
  const context = r.context.filter((c) => byId[c.event_id]);
  const seqs = new Set(context.map((c) => c.sequence));
  const maxSeq = Math.max(0, ...seqs);
  const gaps = full ? r.gaps : Array.from({ length: maxSeq }, (_, i) => i + 1).filter((n) => !seqs.has(n));
  const count = (pred) => decisions.filter(pred).length;
  const metrics = full ? r.metrics : {
    total_events: decisions.length,
    accepted_events: context.length,
    duplicates_removed: count((d) => d.verdict === "duplicate"),
    rejected_events: count((d) => d.verdict === "rejected"),
    conflicts: r.conflicts.filter((c) => c.losers.some((id) => byId[id])).length,
    reordered_events: count((d) => d.rule === "REORDERED"),
    sequence_gaps: gaps.length,
    signals: count((d) => d.verdict === "signal"),
  };
  return { full, decisions, byId, context, gaps, maxSeq, metrics, cursor: decisions.at(-1)?.event_id };
}

// ------------------------------------------------------------------ setup
function renderSetup() {
  const cat = state.catalogue;
  if (!cat) return;
  $("#scenarios").innerHTML = cat.scenarios.map((s) =>
    `<button class="chip ${s.id === state.scenario ? "on" : ""}" data-scenario="${s.id}" title="${esc(s.description)}">${esc(s.label)}</button>`
  ).join("");
  $("#faults").innerHTML = cat.faults.map((f) =>
    `<label class="fault"><input type="checkbox" data-fault="${f.id}" ${state.faults.has(f.id) ? "checked" : ""}>
       <span><b>${esc(f.label)}</b><small>${esc(f.description)}</small></span></label>`
  ).join("");
}

function pickScenario(id) {
  const s = state.catalogue.scenarios.find((x) => x.id === id);
  state.scenario = id;
  state.faults = new Set(s.faults);
  renderSetup();
}

function toggleFault(id, on) {
  on ? state.faults.add(id) : state.faults.delete(id);
  const match = state.catalogue.scenarios.find((s) =>
    s.faults.length === state.faults.size && s.faults.every((f) => state.faults.has(f)));
  state.scenario = match ? match.id : null;
  renderSetup();
}

async function loadHistory() {
  try {
    const { conversations } = await api("GET", "/api/conversations");
    const cid = state.conv?.conversation.conversation_id;
    $("#history").innerHTML = conversations.length ? conversations.map((c) =>
      `<li><button class="${c.conversation_id === cid ? "on" : ""}" data-cid="${c.conversation_id}">
         <span class="h-name">${esc(c.scenario.replace(/_/g, " "))}</span>${pill(c.status)}
         <span class="h-id">${c.conversation_id} · ${c.event_count} events</span></button></li>`
    ).join("") : `<li class="empty">None yet.</li>`;
  } catch (err) { /* history is non-essential */ }
}

// ---------------------------------------------------------------- workflow
function renderWorkflow() {
  const conv = state.conv;
  const d = derived();
  const animating = d && !d.full;
  $("#cid").textContent = conv ? conv.conversation.conversation_id : "—";
  $("#scenario-name").textContent = conv ? `· ${conv.conversation.scenario.replace(/_/g, " ")}` : "";
  $("#status").outerHTML = conv
    ? `<span id="status">${animating ? pill("replaying…") : pill(conv.conversation.status)}</span>`
    : `<span id="status" class="pill neutral">no experiment</span>`;
  const stale = conv && state.result && conv.needs_recovery;
  $("#stale").textContent = stale ? "Log changed since the last recovery — replay again." : "";
  const pending = conv ? conv.in_flight.length : 0;
  $("#recover").disabled = !conv || state.busy;
  $("#deliver").disabled = !conv || !pending || state.busy;
  $("#deliver").textContent = pending ? `Deliver delayed (${pending})` : "Deliver delayed";
  $("#resume").disabled = !conv || state.busy || !d?.full || stale || !state.result.resume.resumable;
  $("#run").disabled = state.busy;
}

// ----------------------------------------------------------------- metrics
function renderMetrics() {
  const conv = state.conv;
  const r = state.result;
  const d = derived();
  const tile = (k, v, cls = "", s = "", small = false) =>
    `<div class="metric"><div class="v ${cls} ${small ? "sm" : ""}">${v}</div><div class="k">${k}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;
  if (!conv) { $("#metrics").innerHTML = `<div class="empty" style="grid-column:1/-1">No experiment yet.</div>`; $("#idem").textContent = ""; return; }
  const inFlight = conv.in_flight.length ? `${conv.in_flight.length} still in transit` : "in the append-only log";
  if (!d) {
    $("#metrics").innerHTML = tile("Total events", conv.events.length, "", inFlight) +
      ["Accepted events", "Duplicates removed", "Sequence gaps", "Conflicts", "Reordered"].map((k) => tile(k, "—")).join("") +
      tile("Recovery status", "not run", "", "click Replay & recover", true) + tile("Recovery duration", "—");
    $("#idem").textContent = "";
    return;
  }
  const m = d.metrics;
  const status = d.full ? r.status : "replaying…";
  const tone = { ok: "ok", warn: "warn", bad: "bad", info: "info" }[STATUS_TONE[status]] || "";
  const ruleList = r.conflicts.map((c) => `seq ${c.sequence}: ${c.rule}`).join(", ");
  $("#metrics").innerHTML =
    tile("Total events", d.full ? m.total_events : `${m.total_events}/${r.metrics.total_events}`, "", inFlight) +
    tile("Accepted events", m.accepted_events, "ok", "form the recovered context") +
    tile("Duplicates removed", m.duplicates_removed, m.duplicates_removed ? "warn" : "", "redeliveries + retries") +
    tile("Sequence gaps", m.sequence_gaps, m.sequence_gaps ? "bad" : "", d.gaps.length ? `missing: ${d.gaps.join(", ")}` : "none") +
    tile("Conflicts", m.conflicts, m.conflicts ? "warn" : "", d.full && ruleList ? ruleList : "same slot, different step") +
    tile("Reordered", m.reordered_events, m.reordered_events ? "info" : "", "placed by sequence") +
    tile("Recovery status", esc(status.replace(/_/g, " ")), tone, d.full ? "dialog: " + esc(r.dialog_state.replace(/_/g, " ")) : "", true) +
    tile("Recovery duration", `${r.duration_ms.toFixed(2)}<span class="muted"> ms</span>`, "", "server-side replay") +
    tile("Context hash", `<code>${r.context_hash}</code>`, "", esc(r.idempotency_check.verdict.replace(/_/g, " ")), true);
  const ic = r.idempotency_check;
  $("#idem").innerHTML = ic.verdict === "identical"
    ? `✓ idempotent: run #${ic.previous_runs + 1} produced the same hash as the previous run`
    : ic.verdict === "log_changed" ? `log grew by ${ic.new_events} event(s) since the previous recovery`
    : ic.verdict === "first_run" ? "first recovery for this conversation" : `⚠ ${esc(ic.verdict)}`;
}

// ---------------------------------------------------------------- timeline
function laneOf(e) { return e.kind === "signal" ? "gateway" : e.adapter; }

function svgShell(cols, colW, body, labels) {
  const left = 128, top = 26, laneH = 46;
  const width = left + cols * colW + 30;
  const height = top + LANES.length * laneH + 22;
  const lanes = LANES.map((l, i) =>
    `<line class="lane" x1="${left - 8}" x2="${width - 10}" y1="${top + i * laneH}" y2="${top + i * laneH}"/>
     <text class="lane-label" x="8" y="${top + i * laneH + 4}">${l}</text>`).join("");
  return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Adapter timeline">
    ${lanes}${body}${labels}</svg>`;
}

function node(e, x, y, opts = {}) {
  const color = LANE_COLOR[laneOf(e)];
  const sel = state.selected === e.event_id ? "sel" : "";
  const title = `<title>seq ${e.sequence ?? "—"} · ${e.adapter} · ${e.kind}\n${esc(e.content)}</title>`;
  let shape;
  if (opts.hollow) {
    shape = `<circle cx="${x}" cy="${y}" r="7" style="fill:none;stroke:${color};stroke-width:2;stroke-dasharray:3 2" class="${sel}"/>`;
  } else if (e.kind === "handoff") {
    shape = `<rect x="${x - 8}" y="${y - 8}" width="16" height="16" transform="rotate(45 ${x} ${y})" style="fill:${color}" class="${sel}"/>`;
  } else {
    shape = `<circle cx="${x}" cy="${y}" r="${opts.small ? 6 : 9}" style="fill:${color}" class="${sel}"/>`;
  }
  let deco = "";
  if (opts.reordered) deco += `<circle cx="${x}" cy="${y}" r="13" style="fill:none;stroke:var(--reord);stroke-width:2"/>`;
  if (opts.duplicate) deco += `<circle cx="${x}" cy="${y}" r="11" style="fill:none;stroke:var(--warn);stroke-width:2;stroke-dasharray:3 2"/>`;
  if (opts.rejected) deco += `<path d="M${x - 7} ${y - 7}L${x + 7} ${y + 7}M${x + 7} ${y - 7}L${x - 7} ${y + 7}" style="stroke:var(--bad);stroke-width:3"/>`;
  const ly = e.kind === "signal" ? y + 22 : y - 14; // signal labels go below so they don't sit on the link line
  const label = opts.label ? `<text class="col-label" x="${x}" y="${ly}">${esc(opts.label)}</text>` : "";
  return `<g class="node" data-eid="${e.event_id}">${title}${shape}${deco}${label}</g>`;
}

function renderTimeline() {
  const conv = state.conv;
  const el = $("#timeline");
  document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.view === state.view));
  if (!conv) { el.innerHTML = `<div class="empty">Run an experiment to see the conversation move between adapters.</div>`; return; }
  const d = derived();
  const top = 26, laneH = 46, left = 128;
  const y = (lane) => top + LANES.indexOf(lane) * laneH;

  if (state.view === "arrival" || !d) {
    const colW = 52;
    const items = [...conv.events, ...conv.in_flight.map((f) => ({ ...f.envelope, event_id: `flight_${f.id}`, inFlight: true }))];
    const x = (i) => left + i * colW + colW / 2;
    let body = "", labels = "";
    let prev = null;
    items.forEach((e, i) => {
      const yy = y(laneOf(e));
      if (prev) body += `<line x1="${prev[0]}" y1="${prev[1]}" x2="${x(i)}" y2="${yy}" style="stroke:rgba(230,237,243,.12);stroke-width:1.5"/>`;
      prev = [x(i), yy];
      const dec = d?.byId[e.event_id];
      labels += node(e, x(i), yy, {
        hollow: e.inFlight, label: e.kind === "signal" ? "timeout" : `#${e.sequence}`,
        reordered: dec?.rule === "REORDERED", duplicate: dec?.verdict === "duplicate", rejected: dec?.verdict === "rejected",
      });
      labels += `<text class="col-label" x="${x(i)}" y="${top + LANES.length * laneH + 4}">${e.inFlight ? "transit" : "@" + e.log_offset}</text>`;
    });
    const note = d ? "" : `<div class="muted" style="padding:4px 8px 8px">Raw arrival order. Replay &amp; recover to see the reconstructed thread.</div>`;
    el.innerHTML = note + svgShell(items.length, colW, body, labels);
    return;
  }

  // Sequence (recovered) view: columns are sequence slots, the line is the dialog thread.
  const resume = d.full && state.result.resume.resumable ? state.result.resume : null;
  const cols = Math.max(d.maxSeq, 1) + (resume ? 1 : 0);
  const colW = 68;
  const x = (seq) => left + (seq - 1) * colW + colW / 2;
  const bottom = top + (LANES.length - 1) * laneH;
  let body = "", labels = "";
  for (const g of d.gaps) {
    body += `<rect class="gap" x="${x(g) - 26}" y="${top - 16}" width="52" height="${bottom - top + 32}" rx="6"/>
             <text class="gap-label" x="${x(g)}" y="${bottom + 30}">#${g} missing</text>`;
  }
  const ctx = d.context;
  for (let i = 1; i < ctx.length; i++) {
    const a = ctx[i - 1], b = ctx[i];
    if (b.sequence !== a.sequence + 1) continue; // thread is broken by a gap
    const cls = a.kind === "handoff" ? "handoff-link" : "thread";
    body += `<line class="${cls}" x1="${x(a.sequence)}" y1="${y(a.adapter)}" x2="${x(b.sequence)}" y2="${y(b.adapter)}"/>`;
  }
  for (const c of ctx) {
    labels += node(c, x(c.sequence), y(c.adapter), { reordered: c.reordered });
    labels += `<text class="col-label" x="${x(c.sequence)}" y="${bottom + 30}">#${c.sequence}</text>`;
  }
  const events = Object.fromEntries(conv.events.map((e) => [e.event_id, e]));
  for (const dec of d.decisions) {
    const e = events[dec.event_id];
    if (!e) continue;
    if (dec.verdict === "duplicate" && e.sequence) {
      labels += node(e, x(e.sequence) + 18, y(e.adapter) + 12, { small: true, duplicate: true });
    } else if (dec.verdict === "rejected" && e.sequence) {
      labels += node(e, x(e.sequence), y(e.adapter), { small: true, rejected: true });
    } else if (dec.verdict === "signal") {
      const cause = ctx.find((c) => c.message_id === e.causation_id);
      const sx = cause ? x(cause.sequence) + colW / 2 : x(Math.max(d.maxSeq, 1));
      labels += node(e, sx, y("gateway"), { label: "timeout" });
      if (cause) body += `<line x1="${x(cause.sequence)}" y1="${y(cause.adapter)}" x2="${sx}" y2="${y("gateway")}" style="stroke:var(--sig);stroke-width:1.5;stroke-dasharray:2 3"/>`;
    }
  }
  if (resume) {
    const rx = x(resume.next_sequence), ry = y(resume.resume_adapter);
    body += `<circle class="resume" cx="${rx}" cy="${ry}" r="11"/><text class="resume-label" x="${rx}" y="${ry - 16}">resume</text>`;
    const last = ctx.at(-1);
    if (last) body += `<line class="handoff-link" style="stroke:var(--ok)" x1="${x(last.sequence)}" y1="${y(last.adapter)}" x2="${rx}" y2="${ry}"/>`;
  }
  el.innerHTML = svgShell(cols, colW, body, labels);
}

// ----------------------------------------------------------------- context
function renderContext() {
  const el = $("#context");
  const d = derived();
  if (!d) { el.innerHTML = `<div class="empty">Recovery has not run yet. Click <b>Replay &amp; recover</b>.</div>`; return; }
  const bySeq = Object.fromEntries(d.context.map((c) => [c.sequence, c]));
  const transit = new Set((state.conv?.in_flight || []).map((f) => f.envelope.sequence));
  let html = "";
  for (let s = 1; s <= d.maxSeq; s++) {
    const c = bySeq[s];
    if (!c) {
      const why = transit.has(s) ? "held in transit (delayed). Use <b>Deliver delayed</b>, then replay again." : "never received. Surfaced, not invented: the context is incomplete.";
      html += `<div class="gap-row">⚠ seq ${s} missing: ${why}</div>`;
      continue;
    }
    const tags = (c.kind === "handoff" ? `<span class="tag ho">hand-off → ${esc(c.target_adapter)}</span>` : "") +
      (c.reordered ? `<span class="tag reord">reordered</span>` : "");
    html += `<div class="msg ${c.adapter} ${state.selected === c.event_id ? "sel" : ""}" data-eid="${c.event_id}">
      <div class="m-head"><b>#${c.sequence} ${esc(c.role)}</b><span class="adapter-${c.adapter}">${c.adapter}</span>${tags}<code>${short(c.message_id)}</code></div>
      <div class="m-body ${c.kind === "handoff" ? "handoff" : ""}">${esc(c.content)}</div>
      ${c.warnings.map((w) => `<div class="m-warn">⚠ ${esc(w)}</div>`).join("")}</div>`;
  }
  if (d.full) {
    const r = state.result;
    if (r.resume.resumable) {
      html += `<div class="resume-row">⟳ Checkpoint: resume with <b>${esc(r.resume.resume_adapter)}</b> at seq ${r.resume.next_sequence}, caused by <code>${short(r.resume.causation_id)}</code>.</div>`;
    } else if (r.dialog_state === "complete" && !r.gaps.length) {
      html += `<div class="resume-row">✓ Dialog complete: nothing to resume.</div>`;
    } else {
      html += `<div class="blocked-row">Resume blocked: ${r.resume.blockers.map(esc).join("; ")}</div>`;
    }
  }
  el.innerHTML = html || `<div class="empty">No accepted events yet…</div>`;
  if (d && !d.full) el.scrollTop = el.scrollHeight;
}

// --------------------------------------------------------------- decisions
function renderDecisions() {
  const el = $("#decisions");
  const d = derived();
  if (!d) { el.innerHTML = `<li class="empty">Every replay decision will be explained here.</li>`; return; }
  const notableOnly = $("#only-notable").checked;
  const list = d.decisions.filter((x) => !notableOnly || x.verdict !== "accepted" || x.rule !== "IN_ORDER" || x.warnings.length);
  el.innerHTML = list.length ? list.map((x) => {
    const v = verdictOf(x);
    return `<li class="decision ${state.selected === x.event_id ? "sel" : ""}" data-eid="${x.event_id}">
      <div class="d-head"><span class="verdict ${v.cls}">${v.text}</span><span class="rule">${x.rule}</span>
        <span class="muted">@${x.log_offset} · seq ${x.sequence ?? "—"} · ${x.adapter}</span></div>
      <p>${esc(x.reason)}</p>${x.warnings.map((w) => `<p class="w">⚠ ${esc(w)}</p>`).join("")}</li>`;
  }).join("") : `<li class="empty">${notableOnly ? "All events accepted in order: nothing notable." : "No decisions yet."}</li>`;
  if (!d.full) el.scrollTop = el.scrollHeight;
}

// --------------------------------------------------------------- inspector
function renderInspector() {
  const conv = state.conv;
  const tbody = $("#events");
  if (!conv) { tbody.innerHTML = `<tr><td colspan="8" class="empty">No events.</td></tr>`; $("#detail").hidden = true; return; }
  const d = derived();
  const rows = conv.events.map((e) => {
    const v = verdictOf(d?.byId[e.event_id]);
    const cls = [state.selected === e.event_id ? "sel" : "", d && !d.full && d.cursor === e.event_id ? "cursor" : ""].join(" ");
    return `<tr class="${cls}" data-eid="${e.event_id}" tabindex="0">
      <td class="mono">${e.log_offset}</td><td>${e.sequence ?? "—"}</td>
      <td class="adapter-${e.adapter}">${e.adapter}</td><td>${e.kind} / ${e.role}</td>
      <td class="mono">${short(e.message_id)}</td><td class="mono">${short(e.causation_id)}</td>
      <td class="mono">${short(e.idempotency_key, 12)}</td><td><span class="verdict ${v.cls}">${v.text}</span></td></tr>`;
  });
  const flights = conv.in_flight.map((f) =>
    `<tr class="in-flight"><td>—</td><td>${f.envelope.sequence}</td><td class="adapter-${f.envelope.adapter}">${f.envelope.adapter}</td>
      <td>${f.envelope.kind} / ${f.envelope.role}</td><td class="mono">${short(f.envelope.message_id)}</td>
      <td class="mono">${short(f.envelope.causation_id)}</td><td class="mono">${short(f.envelope.idempotency_key, 12)}</td>
      <td><span class="verdict pending">in transit</span></td></tr>`);
  tbody.innerHTML = rows.concat(flights).join("");
  renderDetail();
}

function renderDetail() {
  const el = $("#detail");
  const e = state.conv?.events.find((x) => x.event_id === state.selected);
  if (!e) { el.hidden = true; return; }
  const d = derived();
  const dec = d?.byId[e.event_id];
  const causeSeq = state.conv.events.find((x) => x.message_id === e.causation_id)?.sequence;
  const fields = [
    ["event_id", e.event_id], ["log_offset", e.log_offset], ["conversation_id", e.conversation_id],
    ["message_id", e.message_id], ["sequence", e.sequence ?? "— (out-of-band signal)"],
    ["causation_id", e.causation_id ? `${e.causation_id}${causeSeq ? `  → seq ${causeSeq}` : ""}` : "— (conversation start)"],
    ["idempotency_key", e.idempotency_key], ["adapter", e.adapter], ["protocol", e.native?.protocol || "gateway-internal"],
    ["kind / role", `${e.kind} / ${e.role}`], ["target_adapter", e.target_adapter || "—"],
    ["sent_at", e.sent_at || "—"], ["received_at", e.received_at], ["gateway note", e.note || "—"],
  ];
  const v = verdictOf(dec);
  el.hidden = false;
  el.innerHTML = `<div><h3>Correlation metadata</h3><div class="kv">${fields.map(([k, val]) => `<span>${k}</span><code>${esc(val)}</code>`).join("")}</div>
      <h3 style="margin-top:14px">Content</h3><pre>${esc(e.content)}</pre></div>
    <div><h3>Recovery decision</h3>${dec ? `<p><span class="verdict ${v.cls}">${v.text}</span> <span class="rule mono">${dec.rule}</span></p>
      <p style="font-size:13px;line-height:1.5">${esc(dec.reason)}</p>${dec.warnings.map((w) => `<p class="hint">⚠ ${esc(w)}</p>`).join("")}`
      : `<p class="muted">Not replayed yet. Run <b>Replay &amp; recover</b>.</p>`}
      <h3 style="margin-top:14px">Native wire payload (${esc(e.native?.protocol || "none")})</h3>
      <pre>${esc(e.native ? JSON.stringify(e.native.payload, null, 2) : "Raised by the gateway itself; no adapter payload.")}</pre></div>`;
}

function renderAll() {
  renderWorkflow();
  renderMetrics();
  renderTimeline();
  renderContext();
  renderDecisions();
  renderInspector();
}

function select(eid) {
  state.selected = state.selected === eid ? null : eid;
  renderAll();
  if (state.selected) $("#detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ----------------------------------------------------------------- actions
async function withBusy(fn) {
  state.busy = true; renderWorkflow();
  try { await fn(); } catch (err) { failed(err); } finally { state.busy = false; renderAll(); }
}

async function refreshConv() {
  state.conv = await api("GET", `/api/conversations/${state.conv.conversation.conversation_id}`);
}

function runExperiment() {
  return withBusy(async () => {
    stopAnimation();
    const body = state.scenario ? { scenario: state.scenario } : { faults: [...state.faults] };
    if (state.scenario) body.faults = [...state.faults];
    state.conv = await api("POST", "/api/experiments", body);
    state.result = null; state.selected = null; state.view = "arrival";
    const n = state.conv.events.length, held = state.conv.in_flight.length;
    notify(`Logged <b>${n}</b> deliveries across adapters${held ? `; <b>${held}</b> held in transit` : ""}. ` +
      `This is the raw arrival order. Next: <b>Replay &amp; recover</b>.`);
    loadHistory();
  });
}

function stopAnimation() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  state.reveal = Infinity;
  if (state.finishReplay) { const finish = state.finishReplay; state.finishReplay = null; finish(); }
}

function recover() {
  return withBusy(async () => {
    stopAnimation();
    const result = await api("POST", `/api/conversations/${state.conv.conversation.conversation_id}/recover`);
    await refreshConv();
    state.result = result;
    state.view = "sequence";
    const done = () => { notify(recoveryAdvice(result)); loadHistory(); };
    if ($("#animate").checked) {
      state.reveal = 0;
      notify("Replaying the event log one entry at a time…");
      state.busy = true;
      const finished = await new Promise((resolve) => {
        state.finishReplay = () => resolve(false); // interrupted by another action
        state.timer = setInterval(() => {
          state.reveal += 1;
          if (state.reveal >= result.decisions.length) { state.finishReplay = null; stopAnimation(); resolve(true); }
          renderAll();
        }, STEP_MS);
      });
      if (!finished) return;
    } else {
      state.reveal = Infinity;
    }
    done();
  });
}

function recoveryAdvice(r) {
  const m = r.metrics;
  const parts = [`Replayed <b>${m.total_events}</b> log entries in <b>${r.duration_ms.toFixed(2)} ms</b> → ${pill(r.status)}.`];
  if (m.duplicates_removed) parts.push(`${m.duplicates_removed} duplicate(s) removed, so no repeated replies.`);
  if (m.reordered_events) parts.push(`${m.reordered_events} late event(s) placed by sequence.`);
  if (m.conflicts) parts.push(`${m.conflicts} conflict(s) resolved by rule and flagged for review.`);
  if (r.gaps.length) parts.push(`<b>Gap at seq ${r.gaps.join(", ")}</b>: resume blocked until the context is complete.`);
  if (r.resume.resumable) parts.push(`Open hand-off detected. Next: <b>Resume from checkpoint</b> with ${esc(r.resume.resume_adapter)}.`);
  if (r.idempotency_check.verdict === "identical") parts.push(`Same hash as the previous run: recovery is idempotent.`);
  return parts.join(" ");
}

function deliver() {
  return withBusy(async () => {
    const out = await api("POST", `/api/conversations/${state.conv.conversation.conversation_id}/deliver`);
    state.conv = out.conversation;
    state.view = "arrival";
    notify(`Released <b>${out.delivered}</b> delayed message(s): live outcome ${out.outcomes.map((o) => `<b>${o.outcome}</b> (seq ${o.event.sequence})`).join(", ")}. ` +
      `They arrived <i>after</i> later sequences. Next: <b>Replay &amp; recover</b> again.`);
    loadHistory();
  });
}

function resume() {
  return withBusy(async () => {
    const out = await api("POST", `/api/conversations/${state.conv.conversation.conversation_id}/resume`);
    state.conv = out.conversation;
    notify(`<b>${esc(out.resumed_from.resume_adapter)}</b> resumed from the recovered context at seq ${out.resumed_from.next_sequence}: ` +
      `${out.outcomes.map((o) => `seq ${o.event.sequence} ${o.outcome}`).join(", ")}. Next: <b>Replay &amp; recover</b> to verify the dialog is complete.`);
    loadHistory();
  });
}

async function loadConversation(cid) {
  await withBusy(async () => {
    stopAnimation();
    state.conv = await api("GET", `/api/conversations/${cid}`);
    state.result = null; state.reveal = Infinity; state.selected = null; state.view = "arrival";
    notify(`Loaded <code>${cid}</code>. Replay &amp; recover to rebuild its context from the log.`);
    loadHistory();
  });
}

async function resetDemo() {
  const btn = $("#reset");
  if (!state.confirmReset) {
    state.confirmReset = true;
    btn.textContent = "Click again to delete all experiments";
    setTimeout(() => { state.confirmReset = false; btn.textContent = "Reset demo data"; }, 4000);
    return;
  }
  state.confirmReset = false;
  btn.textContent = "Reset demo data";
  await withBusy(async () => {
    stopAnimation();
    await api("POST", "/api/reset");
    Object.assign(state, { conv: null, result: null, selected: null, reveal: Infinity });
    notify("All experiments deleted.");
    loadHistory();
  });
}

// ------------------------------------------------------------------- wiring
document.addEventListener("click", (ev) => {
  const t = ev.target;
  const chip = t.closest("[data-scenario]");
  if (chip) return pickScenario(chip.dataset.scenario);
  const hist = t.closest("[data-cid]");
  if (hist) return loadConversation(hist.dataset.cid);
  const seg = t.closest(".seg button");
  if (seg) { state.view = seg.dataset.view; return renderTimeline(); }
  const item = t.closest("[data-eid]");
  if (item && !item.dataset.eid.startsWith("flight_")) return select(item.dataset.eid);
});
document.addEventListener("keydown", (ev) => {
  const row = ev.target.closest?.("tr[data-eid]");
  if (row && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); select(row.dataset.eid); }
});
document.addEventListener("change", (ev) => {
  if (ev.target.dataset.fault) toggleFault(ev.target.dataset.fault, ev.target.checked);
});
$("#run").onclick = runExperiment;
$("#recover").onclick = recover;
$("#deliver").onclick = deliver;
$("#resume").onclick = resume;
$("#reset").onclick = resetDemo;
$("#only-notable").onchange = renderDecisions;

(async function init() {
  try {
    const health = await api("GET", "/api/health");
    $("#health").textContent = `LOCAL DEMO · v${health.version}`;
    state.catalogue = await api("GET", "/api/scenarios");
    pickScenario(state.scenario);
  } catch (err) {
    $("#health").textContent = "SERVER OFFLINE";
    $("#health").classList.add("down");
    failed(err);
  }
  renderAll();
  loadHistory();
})();
