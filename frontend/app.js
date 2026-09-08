/* Dashboard logic. All live data arrives over the WebSocket (/ws) - no polling, no refresh.
 *
 * Sections:  1. WebSocket   2. rendering   3. diagram animation   4. buttons/forms   5. WebRTC
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const NAME = { "PUNE-TR-01": "Pune", "MUM-TR-01": "Mumbai", "NSK-TR-01": "Nashik",
               "COORDINATOR": "Coordinator", "DASHBOARD": "Dashboard", "ALL": "All" };
const DEFAULT_LOCATION = { "PUNE-TR-01": "Pune - Mumbai-Bengaluru Hwy, km 42",
                           "MUM-TR-01": "Mumbai - Western Express Hwy, Andheri",
                           "NSK-TR-01": "Nashik - Mumbai Naka Junction" };

let nodes = [];        // latest node status from the gateway
let incidents = [];    // latest merged incident list
let events = [];       // communication log entries
let filter = "ALL";    // log filter: ALL / RPC / P2P / BROKER / SYSTEM
let ws;

const short = (id) => NAME[id] || id || "";
const timeOf = (iso) => (iso ? iso.slice(11, 19) : "--:--:--");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(text, isError) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 4000);
}

async function api(method, url, body) {
  const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body && JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(JSON.stringify(data.detail || res.statusText));
  return data;
}

/* ------------------------------------------------------------- 1. WebSocket */
function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";   // wss when deployed behind HTTPS
  ws = new WebSocket(`${scheme}://${location.host}/ws`);
  ws.onopen = () => { $("#ws-status").className = "pill ok"; $("#ws-status").innerHTML = '<i class="dot"></i> LIVE · WebSocket stream'; };
  ws.onclose = () => { $("#ws-status").className = "pill bad"; $("#ws-status").innerHTML = '<i class="dot"></i> disconnected · retrying'; setTimeout(connect, 1500); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.kind === "hello") {                 // first frame: full state + recent history
      nodes = msg.nodes; incidents = msg.incidents; events = msg.events;
      renderNodes(); renderIncidents(); renderTraffic(); renderLog();
    } else if (msg.kind === "snapshot") {       // every 2 s
      nodes = msg.nodes; incidents = msg.incidents;
      renderNodes(); renderIncidents(); renderTraffic();
    } else if (msg.kind === "event") {          // one broker message
      events.push(msg.event);
      appendLog(msg.event); animate(msg.event);
    } else if (msg.kind === "signal") {         // WebRTC signalling from the other tab
      rtcOnSignal(msg);
    }
  };
}

/* -------------------------------------------------------------- 2. rendering */
function renderNodes() {
  $("#node-table tbody").innerHTML = nodes.map((n) => `
    <tr>
      <td class="name">${esc(n.name)}<small class="mono muted" style="display:block;font-weight:400">${n.node_id}</small></td>
      <td class="mono">${esc(n.address)}</td>
      <td><span class="status ${n.status}">${n.status}</span></td>
      <td class="mono">${n.last_heartbeat ? timeOf(n.last_heartbeat) : "—"}</td>
      <td>${n.active_incidents}</td>
      <td>${n.available_units} / ${n.total_units}</td>
      <td class="mono">${esc(n.db_path)}</td>
    </tr>`).join("");
  for (const n of nodes) {
    const g = document.getElementById("dn-" + n.node_id);
    g.classList.remove("ONLINE", "OFFLINE", "UNKNOWN");
    g.classList.add(n.status);
  }
  $("#fault-status").innerHTML = nodes.map((n) => `<span class="${n.status}">${short(n.node_id)}: ${n.status}</span>`).join("");
  for (const btn of document.querySelectorAll("[data-fail]")) btn.disabled = nodes.find((n) => n.node_id === btn.dataset.fail)?.status !== "ONLINE";
  for (const btn of document.querySelectorAll("[data-recover]")) btn.disabled = nodes.find((n) => n.node_id === btn.dataset.recover)?.status === "ONLINE";
}

function renderIncidents() {
  $("#incident-count").textContent = incidents.length;
  $("#incident-table tbody").innerHTML = incidents.map((i) => {
    const units = i.assigned_units.map((u) =>
      `<span class="unit-tag ${u.node_id !== i.handled_by ? "remote" : ""}" title="owned by ${u.node_id}">${u.unit_id}</span>`).join("") || '<span class="muted">—</span>';
    const active = i.status !== "RESOLVED";
    return `<tr class="${active ? "" : "resolved"}">
      <td class="mono">${i.incident_id}</td><td>${esc(i.type)}</td><td>${esc(i.location)}</td>
      <td><span class="sev ${i.severity}">${i.severity}</span></td>
      <td class="mono">${i.detected_by}</td>
      <td class="mono">${i.handled_by}${i.handled_by !== i.detected_by ? " ↪" : ""}</td>
      <td><span class="st ${i.status}">${i.status.replace("_", " ")}</span></td>
      <td>${units}</td>
      <td>${active ? `<button class="btn-link" data-resolve="${i.incident_id}">resolve</button>` : ""}</td>
    </tr>`;
  }).join("");
}

function renderTraffic() {
  $("#traffic-bars").innerHTML = nodes.map((n) => {
    const online = n.status === "ONLINE";
    const d = Math.round(n.traffic_density);
    const level = !online ? "off" : d >= 75 ? "high" : d >= 55 ? "warn" : "";
    return `<div class="tbar">
      <div class="region">${short(n.node_id)}<small>${n.node_id}</small></div>
      <div class="meter ${level}"><i style="width:${online ? d : 0}%"></i></div>
      <div class="pct">${online ? d + "%" : "n/a"}</div>
    </div>`;
  }).join("");
}

function logRow(ev) {
  const route = ev.target && ev.target !== "ALL" ? `${short(ev.source)} → ${short(ev.target)}` : `${short(ev.source)}${ev.target === "ALL" ? " → ALL" : ""}`;
  return `<div class="log-row ${ev.type}">
    <span class="log-ts">${timeOf(ev.ts)}</span>
    <span class="log-ch ${ev.channel}">${ev.channel}</span>
    <span class="log-body"><span class="route">${route.toUpperCase()}</span><span class="type">${ev.type}</span>
      <span class="summary">${esc(ev.summary)}</span></span>
  </div>`;
}
const visible = (ev) => filter === "ALL" || ev.channel === filter;
function renderLog() {
  const log = $("#log");
  log.innerHTML = events.filter(visible).map(logRow).join("");
  log.scrollTop = log.scrollHeight;
}
function appendLog(ev) {
  if (!visible(ev)) return;
  const log = $("#log");
  log.insertAdjacentHTML("beforeend", logRow(ev));
  log.scrollTop = log.scrollHeight;
}
$("#log-filters").onclick = (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c === chip));
  filter = chip.dataset.filter;
  renderLog();
};

/* ---------------------------------------------------- 3. diagram animation */
const POS = { "COORDINATOR": [280, 70], "PUNE-TR-01": [100, 200], "MUM-TR-01": [280, 200], "NSK-TR-01": [460, 200] };

function animate(ev) {
  const src = ev.source === "DASHBOARD" ? "COORDINATOR" : ev.source;
  const targets = ev.target === "ALL" ? Object.keys(POS).filter((k) => k !== src && k !== "COORDINATOR")
                : POS[ev.target] ? [ev.target] : [];
  if (!POS[src] || !targets.length) return;
  const failed = ev.type === "NODE_UNREACHABLE" || ev.type === "NODE_FAILED";
  $("#diagram-caption").textContent = `${timeOf(ev.ts)}  ${short(src)} → ${targets.map(short).join(", ")}  [${ev.channel}] ${ev.type}`;
  for (const tgt of targets) {
    const link = document.getElementById(`link-${src}-${tgt}`) || document.getElementById(`link-${tgt}-${src}`);
    flash(document.getElementById("dn-" + src), "busy");
    if (!failed) flash(document.getElementById("dn-" + tgt), "busy");
    flash(link, "active");
    movingDot(POS[src], POS[tgt], failed);
  }
}
function flash(el, cls) { if (!el) return; el.classList.add(cls); setTimeout(() => el.classList.remove(cls), 900); }
function movingDot([x1, y1], [x2, y2], failed) {
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  dot.setAttribute("r", 6); dot.setAttribute("class", "pulse");
  if (failed) dot.style.fill = "#d13b32";
  $("#pulses").appendChild(dot);
  const start = performance.now();
  (function step(now) {
    const p = Math.min(1, (now - start) / 800);
    const q = failed ? Math.min(p, 0.55) : p;          // a failed call never reaches the target
    dot.setAttribute("cx", x1 + (x2 - x1) * q); dot.setAttribute("cy", y1 + (y2 - y1) * q);
    p < 1 ? requestAnimationFrame(step) : dot.remove();
  })(start);
}

/* ------------------------------------------------------- 4. buttons / forms */
$("#f-node").onchange = (e) => { $("#f-location").value = DEFAULT_LOCATION[e.target.value]; };

$("#incident-form").onsubmit = async (e) => {
  e.preventDefault();
  const body = { detected_by: $("#f-node").value, type: $("#f-type").value, location: $("#f-location").value,
                 severity: $("#f-severity").value, description: $("#f-description").value };
  try {
    const res = await api("POST", "/api/incidents", body);
    const inc = res.incident;
    const lines = [`Coordinator allocated identifier ${inc.incident_id}`];
    if (res.redirected) lines.push(`<b>${body.detected_by} unavailable → request redirected to ${res.served_by}</b>`);
    lines.push(`RPC CreateIncident → ${res.served_by} recorded ${inc.incident_id} in its local DB`,
               `Broker: INCIDENT_CREATED published, broadcast to other nodes`,
               `RPC RequestResponseSupport → ${res.served_by}`, ...res.trace.map(esc));
    $("#trace-list").innerHTML = lines.map((l) => `<li class="${l.startsWith("<b>") ? "redirect" : ""}">${l}</li>`).join("");
    toast(`${inc.incident_id} ${inc.status.replace("_", " ")} — handled by ${short(res.served_by)}`);
  } catch (err) {
    toast("Report failed: " + err.message, true);
  }
};

document.onclick = async (e) => {
  const fail = e.target.closest("[data-fail]"), recover = e.target.closest("[data-recover]"), resolve = e.target.closest("[data-resolve]");
  try {
    if (fail) { await api("POST", `/api/nodes/${fail.dataset.fail}/fail`); toast(`${short(fail.dataset.fail)} node failed (simulated)`); }
    if (recover) { await api("POST", `/api/nodes/${recover.dataset.recover}/recover`); toast(`${short(recover.dataset.recover)} node recovered`); }
    if (resolve) { await api("PATCH", `/api/incidents/${resolve.dataset.resolve}`, { status: "RESOLVED" }); toast(`${resolve.dataset.resolve} resolved, units released`); }
  } catch (err) { toast(err.message, true); }
};
$("#btn-reset").onclick = async () => { const r = await api("POST", "/api/reset"); toast(`Demo data reset on ${r.reset.join(", ")}`); };
setInterval(() => { $("#clock").textContent = new Date().toTimeString().slice(0, 8); }, 1000);

/* ---------------------------------------------------------------- 5. WebRTC
 * Tab that clicks "Connect Peer" = caller (Traffic Control Center); the other tab answers
 * (Emergency Response Team). Offer/answer/ICE go through the WebSocket; after that the
 * data channel (and optional audio/video) flows directly browser <-> browser.
 */
let pc = null, channel = null, role = null, localStream = null, waitingIce = [];

const sendSignal = (data) => ws.send(JSON.stringify({ kind: "signal", ...data }));
function rtcLog(cls, text) {
  const box = $("#rtc-log");
  if (box.firstElementChild?.classList.contains("muted")) box.innerHTML = "";
  box.insertAdjacentHTML("beforeend", `<div class="${cls}">[${new Date().toTimeString().slice(0, 8)}] ${esc(text)}</div>`);
  box.scrollTop = box.scrollHeight;
}
function rtcState(text, ok) { $("#rtc-state").className = "pill" + (ok ? " ok" : ""); $("#rtc-state").innerHTML = `<i class="dot"></i> ${text}`; }

function createPeer() {
  // STUN lets two different devices find each other through the deployed site;
  // two tabs on one machine work even without it
  pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
  pc.onicecandidate = (e) => e.candidate && sendSignal({ type: "ice", candidate: e.candidate });
  pc.oniceconnectionstatechange = () => { $("#rtc-ice").textContent = "ice: " + pc.iceConnectionState; };
  pc.ontrack = (e) => { $("#remote-video").srcObject = e.streams[0]; rtcLog("sys", "remote media track received"); };
  pc.ondatachannel = (e) => bindChannel(e.channel);
  if (localStream) localStream.getTracks().forEach((t) => pc.addTrack(t, localStream));
  $("#btn-rtc-hangup").disabled = false;
}
function bindChannel(dc) {
  channel = dc;
  dc.onopen = () => { rtcState("data channel OPEN · peer-to-peer", true); $("#rtc-input").disabled = false; $("#btn-rtc-send").disabled = false;
                      rtcLog("sys", `data channel "${dc.label}" open — messages now flow browser ↔ browser`); };
  dc.onclose = () => { rtcState("data channel closed", false); $("#rtc-input").disabled = true; $("#btn-rtc-send").disabled = true; };
  dc.onmessage = (e) => rtcLog("peer", `${role === "control" ? "Response Team" : "Traffic Control"}: ${e.data}`);
}
async function rtcConnect() {
  if (pc) return toast("Already connected");
  role = "control"; $("#rtc-role").textContent = "role: Traffic Control Center (caller)";
  createPeer();
  bindChannel(pc.createDataChannel("emergency"));
  await pc.setLocalDescription(await pc.createOffer());
  sendSignal({ type: "offer", sdp: pc.localDescription });
  rtcState("offer sent · waiting for peer tab", false);
  rtcLog("sys", "offer sent through WebSocket signalling");
}
async function rtcOnSignal(msg) {
  if (msg.type === "offer") {
    if (!pc) { role = "team"; $("#rtc-role").textContent = "role: Emergency Response Team (answerer)"; createPeer(); }
    await pc.setRemoteDescription(msg.sdp);
    for (const c of waitingIce) await pc.addIceCandidate(c);
    waitingIce = [];
    await pc.setLocalDescription(await pc.createAnswer());
    sendSignal({ type: "answer", sdp: pc.localDescription });
    rtcLog("sys", "offer received → answer sent");
  } else if (msg.type === "answer" && pc) {
    await pc.setRemoteDescription(msg.sdp);
    rtcLog("sys", "answer received");
  } else if (msg.type === "ice") {
    if (pc && pc.remoteDescription) await pc.addIceCandidate(msg.candidate); else waitingIce.push(msg.candidate);
  } else if (msg.type === "bye" && pc) {
    rtcHangup(false); rtcLog("sys", "peer hung up");
  }
}
async function rtcStartMedia() {
  try {
    localStream = await navigator.mediaDevices.getUserMedia({ video: { width: 320, height: 240 }, audio: true });
    $("#local-video").srcObject = localStream;
    rtcLog("sys", "local camera/microphone started");
    if (pc) {                                                   // already connected: add tracks and renegotiate
      localStream.getTracks().forEach((t) => pc.addTrack(t, localStream));
      await pc.setLocalDescription(await pc.createOffer());
      sendSignal({ type: "offer", sdp: pc.localDescription });
    }
  } catch (err) { toast("Media not available: " + err.message, true); }
}
function rtcHangup(notify = true) {
  if (notify) sendSignal({ type: "bye" });
  channel?.close(); pc?.close();
  pc = channel = role = null;
  $("#rtc-role").textContent = "role: —"; rtcState("not connected", false); $("#rtc-ice").textContent = "ice: new";
  $("#rtc-input").disabled = true; $("#btn-rtc-send").disabled = true; $("#btn-rtc-hangup").disabled = true;
  $("#remote-video").srcObject = null;
}
$("#btn-rtc-connect").onclick = rtcConnect;
$("#btn-rtc-media").onclick = rtcStartMedia;
$("#btn-rtc-hangup").onclick = () => rtcHangup();
$("#rtc-form").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#rtc-input").value.trim();
  if (!text || channel?.readyState !== "open") return;
  channel.send(text);
  rtcLog("me", `${role === "control" ? "Traffic Control" : "Response Team"} (me): ${text}`);
  $("#rtc-input").value = "";
};

connect();
