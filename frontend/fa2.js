/* FA-2 dashboard section (Unit III synchronization + Unit IV paradigms).
 *
 * Loaded after app.js and re-uses its helpers: $, esc, api, toast, short.
 * Everything shown comes from the backend (/api/fa2/*): node state is polled every 2 s,
 * the merged event log every 3 s, the ledger every 4 s. Buttons call the real algorithms.
 * Event listeners are added with addEventListener so FA-1's handlers stay untouched.
 */
"use strict";

(() => {
  const NODES = ["PUNE-TR-01", "MUM-TR-01", "NSK-TR-01"];
  const ALGO_SHORT = { LODHA_KSHEMKALYANI: "LK", RICART_AGRAWALA: "RA", LAMPORT: "Lamport", NONE: "none" };
  let st = null;              // last /api/fa2/state
  let meRuns = [];            // mutual-exclusion runs of this session (for comparison)
  let etag = null;            // web caching demo
  let objectsKey = "";

  const ms = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${Number(v).toFixed(1)} ms`);
  const clockTime = (epoch) => { const d = new Date(epoch * 1000); return d.toTimeString().slice(0, 8) + "." + String(d.getMilliseconds()).padStart(3, "0"); };
  const vec = (v) => (v ? `[${NODES.map((n) => v[n] ?? 0).join(", ")}]` : "—");
  const stat = (label, value, cls = "") => `<div class="fa2-stat ${cls}"><b>${esc(value)}</b>${esc(label)}</div>`;
  const node = (id) => st && st.nodes[id];
  const up = (id) => node(id) && node(id).available;

  /* ------------------------------------------------------------------ polling */
  async function refresh() {
    if (document.hidden) return;
    try { st = await api("GET", "/api/fa2/state"); } catch { return; }
    renderNodes(); renderClock(); renderBeacons(); renderCausal(); renderElection(); renderSnapshot();
    renderMutex(); renderDeadlock(); renderObjects(); renderDFS(); renderFaaS(); renderLedgerReplicas(); renderWeb();
  }
  async function refreshEvents() {
    if (document.hidden) return;
    try { renderEvents(await api("GET", "/api/fa2/events?limit=45")); } catch { /* gateway restarting */ }
  }
  async function refreshLedger() {
    if (document.hidden) return;
    try { renderLedger(await api("GET", "/api/fa2/ledger?limit=8")); } catch { /* ignore */ }
  }
  async function refreshIncidents() {
    try {
      const { incidents } = await api("GET", "/api/incidents");
      const sel = $("#fa2-dfs-incident"), keep = sel.value;
      sel.innerHTML = incidents.map((i) => `<option value="${i.incident_id}">${i.incident_id} · ${esc(i.type)} · ${i.status}</option>`).join("");
      if (keep) sel.value = keep;
    } catch { /* ignore */ }
  }

  /* --------------------------------------------------------------- node state */
  function renderNodes() {
    const pill = $("#fa2-leader-pill");
    pill.className = "pill" + (st.leader ? " ok" : "");
    pill.innerHTML = `<i class="dot"></i> leader: ${st.leader ? short(st.leader) + " (" + st.leader + ")" : "none"}`;
    $("#fa2-node-table tbody").innerHTML = NODES.map((id) => {
      const n = node(id);
      if (!n.available) return `<tr><td class="name">${short(id)}<small class="mono muted" style="display:block;font-weight:400">${id}</small></td>
        <td colspan="9"><span class="status OFFLINE">OFFLINE</span> <span class="muted">refuses every FA-2 RPC (simulated failure)</span></td></tr>`;
      const beacons = Object.entries(n.beacon.peers).map(([p, b]) => `<span class="tag ${b.status}">${short(p)} ${b.status}</span>`).join(" ");
      const ledger = n.ledger.valid ? `<span class="tag ok">#${n.ledger.height} valid</span>` : `<span class="tag bad">broken at #${n.ledger.broken_at}</span>`;
      const me = n.mutex;
      return `<tr>
        <td class="name">${short(id)}<small class="mono muted" style="display:block;font-weight:400">${id}</small></td>
        <td>${n.election.leader === id ? '<span class="tag leader">LEADER</span>' : esc(short(n.election.leader) || "—")}${n.election.in_progress ? ' <span class="tag UNKNOWN">electing</span>' : ""}</td>
        <td class="mono">${n.lamport}</td><td class="mono">${vec(n.vector)}</td>
        <td class="mono">${clockTime(n.physical_clock)}</td><td class="mono">${ms(n.true_offset_ms)}</td>
        <td>${beacons}</td>
        <td class="mono">${ALGO_SHORT[me.algorithm]} · ${me.status}</td>
        <td>${ledger}</td><td class="mono">${n.messages_sent}${n.messages_failed ? ` <span class="muted">(${n.messages_failed} failed)</span>` : ""}</td>
      </tr>`;
    }).join("");
  }

  /* -------------------------------------------------------------- clock sync */
  function renderClock() {
    $("#fa2-clock-bars").innerHTML = NODES.map((id) => {
      const n = node(id);
      if (!n.available) return `<div class="fa2-off"><span>${short(id)}</span><div class="track"></div><span class="val muted">offline</span></div>`;
      const off = n.true_offset_ms, pos = Math.max(-3000, Math.min(3000, off)) / 3000 * 50;
      const left = pos < 0 ? 50 + pos : 50, width = Math.max(Math.abs(pos), 0.4);
      return `<div class="fa2-off"><span>${short(id)} <small class="muted">${n.drift_ppm} ppm</small></span>
        <div class="track" title="centre = reference time, full width = ±3 s"><i style="left:${left}%;width:${width}%"></i></div>
        <span class="val">${ms(off)}</span></div>`;
    }).join("");
    const c = st.last.clock;
    if (!c) return;
    let lines;
    if (c.algorithm === "CRISTIAN") {
      lines = Object.entries(c.results).map(([id, r]) =>
        `${short(id).padEnd(7)} RTT ${r.rtt_ms} ms (samples ${r.all_rtts_ms.join("/")})  adjust ${ms(r.adjustment_ms)}  ` +
        `NTP θ=${ms(r.ntp_offset_ms)} δ=${r.ntp_delay_ms} ms  error ${ms(r.error_before_ms)} → ${ms(r.error_after_ms)}`);
      lines.unshift("CRISTIAN (external sync with the gateway time server, min-RTT sample):");
    } else {
      lines = Object.entries(c.result.readings).map(([id, r]) => r.diff_ms === undefined ? `${short(id).padEnd(7)} ${r.note}` :
        `${short(id).padEnd(7)} reading ${ms(r.diff_ms)} vs daemon  RTT ${r.rtt_ms} ms  → adjust ${ms(r.adjustment_ms)}${r.outlier ? "  (outlier, ignored)" : ""}`);
      lines.unshift(`BERKELEY (internal sync, time daemon ${c.daemon}, average ${ms(c.result.average_ms)}):`);
    }
    lines.push(`Skew between nodes now: ${c.max_skew_ms} ms`);
    $("#fa2-clock-result").textContent = lines.join("\n");
    $("#fa2-clock-result").classList.remove("muted");
  }

  /* ----------------------------------------------------------------- beacons */
  function renderBeacons() {
    $("#fa2-beacon-int").textContent = st.settings.beacon_interval;
    $("#fa2-beacon-to").textContent = st.settings.beacon_timeout;
    const rows = [];
    for (const id of NODES) {
      const n = node(id);
      if (!n.available) { rows.push(`<tr><td class="name">${short(id)}</td><td colspan="7"><span class="status OFFLINE">OFFLINE</span> sends no beacons</td></tr>`); continue; }
      for (const [p, b] of Object.entries(n.beacon.peers)) {
        const truth = up(p) ? node(p).true_offset_ms - n.true_offset_ms : null;
        rows.push(`<tr><td class="name">${short(id)}</td><td>${short(p)}</td><td><span class="tag ${b.status}">${b.status}</span></td>
          <td class="mono">${b.last_seen_s}s ago</td><td class="mono">${b.received}</td><td class="mono">${ms(b.offset_ms)}</td>
          <td class="mono">${ms(b.rbs_offset_ms)}</td><td class="mono muted">${truth === null ? "—" : ms(truth)}</td></tr>`);
      }
    }
    $("#fa2-beacon-table tbody").innerHTML = rows.join("");
  }

  /* --------------------------------------------------------- logical clocks */
  function renderEvents(ev) {
    const ok = ev.lamport_violations === 0 && ev.vector_violations === 0;
    $("#fa2-clock-check").innerHTML =
      stat("send/receive pairs checked", ev.pairs_checked) +
      stat("Lamport violations  L(send) ≥ L(recv)", ev.lamport_violations, ok ? "ok" : "bad") +
      stat("vector violations  V(send) ≮ V(recv)", ev.vector_violations, ok ? "ok" : "bad") +
      stat("pairs where skewed PHYSICAL time says recv < send", ev.physical_clock_anomalies, ev.physical_clock_anomalies ? "bad" : "");
    $("#fa2-event-table tbody").innerHTML = ev.events.slice().reverse().map((e) => `<tr>
      <td><button class="btn-link" data-evt="${e.node}:${e.seq}" title="use in compare">${short(e.node)}:${e.seq}</button></td>
      <td>${e.lamport}</td><td>${vec(e.vector)}</td><td>${short(e.node)}</td><td><span class="tag ${e.type}">${e.type}</span></td>
      <td title="${esc(e.text)}">${esc(e.kind)}</td><td>${esc(short(e.peer))}</td><td>${clockTime(e.phys)}</td></tr>`).join("");
  }

  function renderCausal() {
    const n = node("NSK-TR-01");
    if (!n.available) return;
    const item = (m) => `<li class="${m.after_buffering ? "buffered" : ""}">${esc(m.topic)} from ${short(m.from)} vt=${vec(m.vt)}${m.after_buffering ? " ← from buffer" : ""}</li>`;
    $("#fa2-causal-recv").innerHTML = n.causal.received.slice(-6).map(item).join("") || '<li class="muted">nothing yet</li>';
    $("#fa2-causal-deliv").innerHTML = n.causal.delivered.slice(-6).map(item).join("") || '<li class="muted">nothing yet</li>';
    const buf = NODES.filter(up).flatMap((id) => node(id).causal.buffered.map((b) => `${short(id)} holds ${b.topic} from ${short(b.from)}, waiting for ${b.waiting_for.join(", ")}`));
    $("#fa2-causal-buffer").textContent = buf.join("\n") || "— (nothing waiting)";
  }

  /* ---------------------------------------------------------------- election */
  function renderElection() {
    const e = st.last.election;
    $("#fa2-elect-summary").innerHTML = stat("current leader (agreed)", st.leader ? short(st.leader) : "none", st.leader ? "ok" : "bad") +
      (e ? stat(`last dashboard election: ${e.algorithm} from ${short(e.initiator)}`, `${e.messages} msgs`) +
           stat("all nodes agree", e.agreed ? "yes" : "no", e.agreed ? "ok" : "bad") : "");
    const trace = NODES.filter(up).flatMap((id) => node(id).election.trace.map((t) => ({ ...t, node: id })))
      .sort((a, b) => a.t - b.t).slice(-16);
    if (trace.length) $("#fa2-elect-trace").innerHTML = trace.map((t) => `<li>${clockTime(t.t)} ${short(t.node)}: ${esc(t.text)}</li>`).join("");
  }

  /* ---------------------------------------------------------------- snapshot */
  function renderSnapshot() {
    const s = st.last.snapshot;
    if (!s) return;
    const rows = NODES.map((id) => {
      const r = s.nodes[id];
      if (!r) return `${short(id).padEnd(7)} no state recorded (offline)`;
      const chans = Object.entries(r.channels).map(([p, v]) => `${short(p)}→${short(id)}: [${v.join(",")}]`).join("  ");
      return `${short(id).padEnd(7)} patrols ${String(r.state.patrols).padStart(2)}  L=${r.state.lamport}  in-transit ${chans}`;
    });
    $("#fa2-snap-result").innerHTML = `<div class="fa2-stats">${
      stat("recorded local + in transit", `${s.recorded_local_total} + ${s.recorded_in_transit} = ${s.recorded_total}`, s.consistent ? "ok" : "bad")}${
      stat("naive simultaneous reading", `${s.naive_total} (${s.naive_reading.join("/")})`, s.naive_total === s.expected_total ? "" : "bad")}${
      stat("consistent cut", s.consistent ? "yes" : "no", s.consistent ? "ok" : "bad")}</div>` +
      esc(`Snapshot ${s.sid} initiated by ${s.initiator}\n` + rows.join("\n"));
    $("#fa2-snap-result").classList.remove("muted");
  }

  /* -------------------------------------------------------- mutual exclusion */
  function renderMutex() {
    const r = st.last.mutex;
    $("#fa2-me-state").textContent = NODES.filter(up).map((id) => {
      const { history, messages_sent, ...m } = node(id).mutex;
      return `${id}: ${JSON.stringify(m)}`;
    }).join("\n\n");
    if (!r) return;
    $("#fa2-me-summary").innerHTML =
      stat("algorithm", ALGO_SHORT[r.algorithm]) + stat("CS executions", r.completed) +
      stat("messages (per CS)", `${r.total_messages} (${r.messages_per_cs ?? "—"})`) +
      stat("overlapping CS", r.overlapping_sections, r.overlapping_sections ? "bad" : "ok") +
      stat("lost updates", r.lost_updates, r.lost_updates ? "bad" : "ok");
    const cs = r.critical_sections;
    if (cs.length) {
      const t0 = Math.min(...cs.map((c) => c.enter)), t1 = Math.max(...cs.map((c) => c.exit)), span = Math.max(t1 - t0, 0.001);
      const overlapping = new Set();
      cs.forEach((a, i) => cs.forEach((b, j) => { if (i !== j && a.enter < b.exit && b.enter < a.exit) overlapping.add(i); }));
      $("#fa2-me-timeline").innerHTML = NODES.map((id) => `<div class="fa2-tl-row"><span>${short(id)}</span><div class="fa2-tl-lane">${
        cs.map((c, i) => c.node !== id ? "" : `<i class="${overlapping.has(i) ? "overlap" : ""}" title="request ${c.request}"
          style="left:${(c.enter - t0) / span * 100}%;width:${Math.max((c.exit - c.enter) / span * 100, 1)}%"></i>`).join("")}</div></div>`).join("") +
        `<div class="hint" style="margin-top:4px">time → (${span.toFixed(2)} s); red = two nodes inside the CS at once</div>`;
    }
    $("#fa2-me-register").textContent = r.register || "(empty)";
    if (!meRuns.length || meRuns[meRuns.length - 1].duration_s !== r.duration_s || meRuns[meRuns.length - 1].algorithm !== r.algorithm) {
      meRuns.push(r);
    }
    $("#fa2-me-history tbody").innerHTML = meRuns.slice(-8).map((x) => `<tr><td>${ALGO_SHORT[x.algorithm]}</td><td>${x.completed}</td>
      <td>${x.total_messages}</td><td>${x.messages_per_cs ?? "—"}</td><td>${x.overlapping_sections}</td><td>${x.lost_updates}</td>
      <td>${x.algorithm === "NONE" ? "n/a" : x.granted_in_priority_order ? "yes" : "no"}</td></tr>`).join("");
  }

  /* ---------------------------------------------------------------- deadlock */
  const WFG_POS = { "PUNE-TR-01": [70, 170], "MUM-TR-01": [180, 50], "NSK-TR-01": [290, 170] };
  function renderDeadlock() {
    const edges = NODES.filter(up).filter((id) => node(id).deadlock.waiting)
      .map((id) => ({ from: id, to: node(id).deadlock.waiting.holder, unit: node(id).deadlock.waiting.unit }));
    $("#fa2-wfg-edges").innerHTML = edges.map((e) => {
      const [x1, y1] = WFG_POS[e.from], [x2, y2] = WFG_POS[e.to];
      const len = Math.hypot(x2 - x1, y2 - y1), ux = (x2 - x1) / len, uy = (y2 - y1) / len;
      const off = 6;                                         // offset so A->B and B->A do not overlap
      return `<line x1="${x1 + ux * 32 - uy * off}" y1="${y1 + uy * 32 + ux * off}" x2="${x2 - ux * 34 - uy * off}" y2="${y2 - uy * 34 + ux * off}" marker-end="url(#fa2-arrow)"></line>
        <text class="edge" x="${(x1 + x2) / 2 - uy * 16}" y="${(y1 + y2) / 2 + ux * 16}">${e.unit}</text>`;
    }).join("");
    const held = NODES.filter(up).map((id) => `${short(id).padEnd(7)} holds [${node(id).deadlock.held.join(", ")}]` +
      (node(id).deadlock.waiting ? `  waits for ${node(id).deadlock.waiting.unit} (held by ${short(node(id).deadlock.waiting.holder)})` : ""));
    const d = st.last.deadlock || {};
    let verdict = "";
    if (d.detection) verdict = `\n${d.detection.method}: ${d.detection.deadlock ? "DEADLOCK cycle " + d.detection.cycle.map(short).join(" → ") : "no deadlock"}`;
    if (d.resolution) verdict += `\nresolved: victim ${short(d.resolution.victim)} released [${d.resolution.aborted.released.join(", ")}], cycle now ${d.resolution.cycle_after ? "still present" : "gone"}`;
    $("#fa2-dl-result").textContent = held.join("\n") + verdict;
    const trace = NODES.filter(up).flatMap((id) => node(id).deadlock.trace.map((t) => ({ ...t, node: id }))).sort((a, b) => a.t - b.t).slice(-12);
    $("#fa2-dl-trace").innerHTML = trace.map((t) => `<li class="${/DEADLOCK|BLOCKED/.test(t.text) ? "bad" : ""}">${clockTime(t.t)} ${short(t.node)}: ${esc(t.text)}</li>`).join("");
  }

  /* ----------------------------------------------------- distributed objects */
  function renderObjects() {
    const key = st.objects.map((o) => o.ref).join();
    if (key === objectsKey) return;
    objectsKey = key;
    const sel = $("#fa2-obj-ref");
    sel.innerHTML = st.objects.map((o) => `<option value="${esc(o.ref)}">${esc(o.ref.replace("fa2obj://", ""))}</option>`).join("");
    fillMethods();
  }
  function fillMethods() {
    const obj = st.objects.find((o) => o.ref === $("#fa2-obj-ref").value);
    if (!obj) return;
    $("#fa2-obj-method").innerHTML = Object.entries(obj.methods).map(([m, params]) => `<option value="${m}">${m}(${params.join(", ")})</option>`).join("");
    setArgs();
  }
  function setArgs() {
    const m = $("#fa2-obj-method").value;
    const examples = { set_phase: { phase: "RED" }, preempt_for: { incident_id: "INC-1001" },
                       sync_with: { ref: "fa2obj://MUM-TR-01/TrafficSignal/SIG-MUM-ANDHERI" } };
    $("#fa2-obj-args").value = JSON.stringify(examples[m] || {});
  }

  /* -------------------------------------------------------------------- DFS */
  function renderDFS() {
    $("#fa2-dfs-cfg").textContent = `${st.settings.chunk_size} B chunks × ${st.settings.replication} replicas`;
    $("#fa2-dfs-table tbody").innerHTML = st.dfs.files.map((f) => `<tr>
      <td title="written by ${esc(f.writer)}">${esc(f.path)}</td><td>${f.size} B</td><td>${f.version}</td>
      <td>${f.replicas.map((r, i) => `#${i}:${r.map((n) => short(n)[0]).join("")}`).join(" ")}${f.under_replicated ? ` <span class="tag bad">${f.under_replicated} under-replicated</span>` : ""}</td>
      <td><button class="btn-link" data-dfs-read="${esc(f.path)}">read</button> · <button class="btn-link" data-dfs-corrupt="${esc(f.path)}">corrupt #0</button></td>
    </tr>`).join("") || '<tr><td colspan="5" class="muted">no files yet</td></tr>';
  }

  /* -------------------------------------------------------------------- FaaS */
  function renderFaaS() {
    $("#fa2-faas-warm").textContent = st.faas.keep_warm_s;
    $("#fa2-faas-table tbody").innerHTML = st.faas.functions.map((f) => `<tr>
      <td>${f.name}</td><td title="${esc(f.description)}">${esc(f.trigger)}</td><td>${f.invocations}</td><td>${f.cold_starts}</td>
      <td>${f.avg_ms ?? "—"}</td><td>${f.warm_instances.map((i) => `${i.pid ?? "starting"}${i.busy ? "*" : ""} (${i.idle_s}s idle)`).join(", ") || '<span class="muted">0 — scaled to zero</span>'}</td>
      <td>${f.gb_seconds}</td><td><button class="btn-link" data-faas="${f.name}">invoke</button></td></tr>`).join("");
    $("#fa2-faas-recent").innerHTML = st.faas.recent.slice().reverse().map((r) => `<li class="${r.error ? "bad" : ""}">
      ${clockTime(r.t)} ${r.function} [${r.trigger}] ${r.cold_start ? `<b>COLD</b> init ${r.init_ms} ms +` : "warm"} run ${r.run_ms} ms · pid ${r.pid}
      ${r.error ? esc(r.error) : esc(summariseResult(r.result))}</li>`).join("") || '<li class="muted">no invocations yet — report an incident to trigger classify_incident</li>';
  }
  function summariseResult(res) {
    if (!res) return "";
    if (res.priority) return `→ ${res.incident_id} ${res.priority} (score ${res.score})`;
    if (res.path) return `→ ${res.path}`;
    if (res.advisories) return `→ ${res.advisories.length} advisory(ies)`;
    return "";
  }

  /* ------------------------------------------------------------------ ledger */
  function renderLedgerReplicas() {
    $("#fa2-ledger-replicas tbody").innerHTML = NODES.map((id) => {
      const n = node(id);
      if (!n.available) return `<tr><td>${short(id)}</td><td colspan="3"><span class="status OFFLINE">OFFLINE</span> (catches up on recovery)</td></tr>`;
      const l = n.ledger;
      return `<tr><td>${short(id)}${n.election.leader === id ? ' <span class="tag leader">proposer</span>' : ""}</td><td>${l.height}</td>
        <td title="${l.tip_hash}">${l.tip_hash.slice(0, 16)}…</td>
        <td>${l.valid ? '<span class="tag ok">all links valid</span>' : `<span class="tag bad">#${l.broken_at}: ${esc(l.reason)}</span>`}</td></tr>`;
    }).join("");
  }
  function renderLedger(L) {
    const last = L.recent_commits[L.recent_commits.length - 1];
    $("#fa2-ledger-summary").innerHTML = stat("events waiting for next block", L.pending) +
      (last ? stat(`last block #${last.index} by ${short(last.leader)}`, last.committed ? `committed ${last.stored_on.length}/3` : "NOT committed", last.committed ? "ok" : "bad") +
              stat("mining (nonce / ms)", `${last.nonce} / ${last.mining_ms}`) : "") +
      (L.last_error ? stat("ledger service", L.last_error, "bad") : "");
    $("#fa2-ledger-blocks tbody").innerHTML = L.blocks.slice().reverse().map((b) => `<tr>
      <td>${b.index}</td><td>${short(b.proposer)}</td><td title="prev ${b.prev_hash}">${b.hash.slice(0, 14)}…</td><td>${b.nonce}</td>
      <td style="white-space:normal">${b.events.slice(0, 3).map((e) => esc(e.type)).join(", ")}${b.events.length > 3 ? ` +${b.events.length - 3}` : ""}</td></tr>`).join("");
  }

  /* --------------------------------------------------------------------- web */
  function renderWeb() {
    const w = st.web;
    $("#fa2-web-stats").innerHTML = stat("requests", w.requests) + stat("200 full", w.full_responses) +
      stat("304 not modified", w.not_modified, w.not_modified ? "ok" : "") + stat("bytes saved", w.bytes_saved);
  }
  async function webFetch(revalidate) {
    const headers = revalidate && etag ? { "If-None-Match": etag } : {};
    const t0 = performance.now();
    const res = await fetch("/api/fa2/web/feed", { cache: "no-store", headers });
    const body = await res.text();
    if (res.headers.get("ETag")) etag = res.headers.get("ETag");
    $("#fa2-web-result").textContent = `HTTP ${res.status} ${res.status === 304 ? "Not Modified" : "OK"} · ${body.length} bytes body · ` +
      `${(performance.now() - t0).toFixed(1)} ms\nETag: ${etag}\nCache-Control: ${res.headers.get("Cache-Control")}` +
      (revalidate && !headers["If-None-Match"] ? "\n(no ETag yet - did a full GET)" : "");
    $("#fa2-web-result").classList.remove("muted");
  }

  /* ----------------------------------------------------------------- actions */
  const ACTIONS = {
    cristian: () => api("POST", "/api/fa2/clock/cristian", {}),
    berkeley: () => api("POST", "/api/fa2/clock/berkeley", {}),
    reskew: () => api("POST", "/api/fa2/clock/reskew", { randomize: true }),
    causal: () => api("POST", "/api/fa2/causal/demo", {}),
    bully: () => api("POST", "/api/fa2/election", { algorithm: "BULLY", initiator: $("#fa2-elect-init").value }),
    ring: () => api("POST", "/api/fa2/election", { algorithm: "RING", initiator: $("#fa2-elect-init").value }),
    snapshot: () => api("POST", "/api/fa2/snapshot", { with_traffic: true }),
    mutex: () => api("POST", "/api/fa2/mutex/run", { algorithm: $("#fa2-me-algo").value }),
    "dl-setup": () => api("POST", "/api/fa2/deadlock/setup"),
    "dl-edge": () => api("POST", "/api/fa2/deadlock/detect", { method: "EDGE_CHASING", initiator: $("#fa2-dl-init").value }),
    "dl-global": () => api("POST", "/api/fa2/deadlock/detect", { method: "GLOBAL_STATE", initiator: $("#fa2-dl-init").value }),
    "dl-resolve": () => api("POST", "/api/fa2/deadlock/resolve", {}),
    "dl-reset": () => api("POST", "/api/fa2/deadlock/reset"),
    "dfs-report": () => api("POST", `/api/fa2/dfs/report/${$("#fa2-dfs-incident").value}`),
    "dfs-repair": () => api("POST", "/api/fa2/dfs/repair"),
    "ledger-note": () => api("POST", "/api/fa2/ledger/record", { text: `Operator note at ${new Date().toTimeString().slice(0, 8)}` }),
    "ledger-verify": () => api("POST", "/api/fa2/ledger/verify"),
    "ledger-tamper": () => api("POST", "/api/fa2/ledger/tamper", { node: "NSK-TR-01", index: 1 }),
    "ledger-repair": () => api("POST", "/api/fa2/ledger/repair", { node: "NSK-TR-01" }),
    "web-get": () => webFetch(false),
    "web-reval": () => webFetch(true),
  };
  const MESSAGE = {
    cristian: () => "Cristian sync done on every node",
    berkeley: (r) => `Berkeley round by ${short(r.daemon)}: skew now ${r.max_skew_ms} ms`,
    reskew: (r) => `Clocks re-skewed (spread ${r.max_skew_ms} ms)`,
    causal: () => "Causal demo started - watch Nashik's receive vs delivery order",
    bully: (r) => `Bully: leader ${short(r.leader)} (${r.messages} messages)`,
    ring: (r) => `Ring: leader ${short(r.leader)} (${r.messages} messages)`,
    snapshot: (r) => `Snapshot ${r.sid}: total ${r.recorded_total} (${r.consistent ? "consistent" : "INCONSISTENT"})`,
    mutex: (r) => `${ALGO_SHORT[r.algorithm]}: ${r.total_messages} messages, ${r.lost_updates} lost update(s)`,
    "dl-setup": (r) => (r.cycle ? `Circular wait: ${r.cycle.map(short).join(" → ")}` : "Locks requested"),
    "dl-edge": (r) => (r.deadlock ? `Probe returned: deadlock ${r.cycle.map(short).join(" → ")}` : "Probe discarded: no deadlock"),
    "dl-global": (r) => (r.deadlock ? `Snapshot ${r.sid} wait-for graph has cycle ${r.cycle.map(short).join(" → ")}` : "No cycle in the snapshot"),
    "dl-resolve": (r) => `Victim ${short(r.victim)} aborted - cycle ${r.cycle_after ? "still present" : "broken"}`,
    "dfs-report": (r) => `${r.stored.path} stored: ${r.stored.chunks.length} chunks × 2 replicas`,
    "dfs-repair": (r) => `${r.actions.length} chunk copy(ies) re-replicated`,
    "ledger-note": (r) => (r.commit ? `Block #${r.commit.index} ${r.commit.committed ? "committed" : "not committed"}` : "Queued (no leader?)"),
    "ledger-verify": (r) => Object.entries(r).map(([n, v]) => `${short(n)}: ${v ? (v.valid ? "valid" : "BROKEN #" + v.broken_at) : "offline"}`).join(" · "),
    "ledger-tamper": (r) => `Nashik replica: ${r.reason}`,
    "ledger-repair": (r) => `Nashik replica ${r.valid ? "repaired" : "still broken"}`,
  };

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-fa2]");
    const evt = e.target.closest("[data-evt]");
    const read = e.target.closest("[data-dfs-read]");
    const corrupt = e.target.closest("[data-dfs-corrupt]");
    const faas = e.target.closest("[data-faas]");
    try {
      if (btn) {
        const name = btn.dataset.fa2;
        btn.disabled = true;
        try {
          const res = await ACTIONS[name]();
          if (MESSAGE[name]) toast(MESSAGE[name](res));
        } finally { btn.disabled = false; }
        await refresh(); refreshEvents(); refreshLedger();
      } else if (evt) {
        const a = $("#fa2-cmp-a"), b = $("#fa2-cmp-b");
        if (!a.value || b.value) { a.value = evt.dataset.evt; b.value = ""; } else b.value = evt.dataset.evt;
      } else if (read) {
        const r = await api("GET", `/api/fa2/dfs/file?path=${encodeURIComponent(read.dataset.dfsRead)}`);
        $("#fa2-dfs-read").textContent = `${r.path} v${r.version} · ${r.size} B · SHA-256 ${r.verified ? "verified" : "MISMATCH"}\n` +
          r.chunks.map((c) => `chunk #${c.index} served by ${c.served_by}` + c.skipped.map((s) => ` (skipped ${s.node}: ${s.reason})`).join("")).join("\n") +
          `\n----\n${r.content}`;
        $("#fa2-dfs-read").classList.remove("muted");
      } else if (corrupt) {
        const r = await api("POST", "/api/fa2/dfs/corrupt", { path: corrupt.dataset.dfsCorrupt, chunk_index: 0 });
        toast(`Chunk ${r.corrupted} corrupted on ${short(r.node)} - read the file to see the failover`);
      } else if (faas) {
        const body = faas.dataset.faas === "congestion_advisory" ? {} : { incident_id: $("#fa2-dfs-incident").value };
        const r = await api("POST", `/api/fa2/faas/invoke/${faas.dataset.faas}`, body);
        toast(`${r.function}: ${r.cold_start ? "COLD start " + r.init_ms + " ms init" : "warm"}, run ${r.run_ms} ms, pid ${r.pid}`);
        refresh();
      }
    } catch (err) { toast(err.message, true); }
  });

  $("#fa2-compare-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("GET", `/api/fa2/events/compare?a=${encodeURIComponent($("#fa2-cmp-a").value)}&b=${encodeURIComponent($("#fa2-cmp-b").value)}`);
      $("#fa2-cmp-result").textContent = `${r.text}   [V(a)=${vec(r.a.vector)} V(b)=${vec(r.b.vector)} · L(a)=${r.a.lamport} L(b)=${r.b.lamport}]`;
    } catch (err) { toast(err.message, true); }
  });
  $("#fa2-obj-ref").addEventListener("change", fillMethods);
  $("#fa2-obj-method").addEventListener("change", setArgs);
  $("#fa2-obj-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("POST", "/api/fa2/objects/invoke", { ref: $("#fa2-obj-ref").value, method: $("#fa2-obj-method").value,
                                                              args: JSON.parse($("#fa2-obj-args").value || "{}") });
      $("#fa2-obj-result").textContent = `${r.ok ? "OK" : "REMOTE EXCEPTION"} · executed on ${r.host} · ${r.latency_ms} ms · ` +
        `request ${r.request_bytes} B / reply ${r.reply_bytes} B (marshalled)\n` + JSON.stringify(r.ok ? r.result : r.error, null, 2);
    } catch (err) { toast(err.message, true); }
  });
  $("#fa2-dfs-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("PUT", "/api/fa2/dfs/file", { path: $("#fa2-dfs-path").value, content: $("#fa2-dfs-content").value });
      toast(`${r.path} v${r.version} written: ${r.chunks.length} chunk(s)`);
      refresh();
    } catch (err) { toast(err.message, true); }
  });

  refresh(); refreshEvents(); refreshLedger(); refreshIncidents();
  setInterval(refresh, 2000);
  setInterval(refreshEvents, 3000);
  setInterval(refreshLedger, 4000);
  setInterval(refreshIncidents, 10000);
})();
