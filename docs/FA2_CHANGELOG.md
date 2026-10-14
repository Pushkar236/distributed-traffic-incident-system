# FA-2 Changelog

Running record of every FA-2 change. FA-1 code is extended, never replaced.

## 0. Baseline (before any FA-2 change) — 2026-10-14

* Branch `main`, last commit `98417d2`. Working tree had one **uncommitted user change**:
  `proto/traffic.proto` (header comment removed). It is preserved: FA-2 does **not** touch
  `proto/traffic.proto` or the stubs generated from it. All FA-2 RPCs live in a separate
  contract `proto/fa2_sync.proto` (service `SyncNode`).
* `python tools/test_system.py` against `python run.py` → **40 passed, 0 failed**.
* Tests were run with `DATA_DIR` pointing to a scratch copy of `data/*.db`, so the
  user's demo databases were not reset by the test's `/api/reset` call.

### FA-1 inventory (what must keep working)

| Area | Entry point |
|---|---|
| Node processes (gRPC `TrafficNode`, SQLite per node) | `backend/node.py` |
| Gateway: REST, `/ws`, static dashboard | `backend/main.py` |
| Coordination middleware, failover, store-and-forward | `backend/coordinator.py` |
| Broker (pub/sub), WebSocket hub, P2P client | `backend/communication/` |
| Heartbeat failure detector | `backend/services/fault_tolerance.py` |
| Dashboard | `frontend/index.html`, `app.js`, `style.css` |
| Tests / tooling | `tools/test_system.py`, `capture_screenshots.py`, `build_ppt.py` |

## 1. Plan

All FA-2 code goes in a new package `backend/fa2/`; the dashboard gets new files
`frontend/fa2.js` + `frontend/fa2.css` and an additional FA-2 section in `index.html`.

Integration points in existing files (smallest possible, backward compatible):

| File | Change |
|---|---|
| `backend/node.py` | attach the FA-2 runtime to the existing gRPC server; two one-line hooks so incident create/update events get logical timestamps and are causally broadcast |
| `backend/main.py` | `include_router(fa2 router)` + one start call in the lifespan |
| `frontend/index.html` | FA-2 section appended after the FA-1 panels, one log-filter chip, two asset tags |
| `README.md` | FA-2 section appended (existing text kept) |

Feature order (each verified before the next): runtime skeleton → clocks / logical clocks /
causal order → beacons + election → global snapshot → DFS → mutual exclusion → deadlock
detection → distributed objects → serverless → ledger → web caching → UI → docs → PPT.

## 2. Change log

(entries are appended below as each step is completed and verified)

### Step 1 — FA-2 contract and runtime skeleton (verified)
* NEW `proto/fa2_sync.proto` (service `SyncNode`: Deliver, GetTime, Command, Invoke, StoreChunk, ReadChunk)
* NEW `tools/gen_fa2_proto.py` → generated `backend/rpc/fa2_sync_pb2.py`, `fa2_sync_pb2_grpc.py`
  (separate generator: FA-1 stubs are never regenerated)
* NEW `backend/fa2/` package: `settings.py`, `errors.py`, `rpc.py` (SyncPool), `clocks.py`,
  `transport.py` (FIFO channels), `runtime.py` (inbox processor, event log, commands, servicer)
* MODIFIED `backend/node.py` (+7 lines, nothing removed): `self.fa2 = None`; `attach_fa2(node, server)`
  in `serve()`; two hooks `self.fa2.incident_event(...)` after INCIDENT_CREATED / INCIDENT_UPDATED.
  `attach()` catches every exception so an FA-2 fault can never stop an FA-1 node.
* MODIFIED `backend/main.py` (+5 lines, nothing removed): import, two `include_router`, start/stop in lifespan.

### Step 2 — Unit III algorithms (verified with tools/test_fa2.py)
* `clock_sync.py` Cristian (min-RTT of 4 samples + NTP offset/delay) and Berkeley (leader = time daemon)
  - fix during testing: a single Cristian sample had ~500 ms RTT on Windows (localhost IPv6 fallback
    on a fresh connection) → asymmetric delay → ~250 ms error. Now 4 samples on one connection, min RTT.
* `causal.py` Birman–Schiper–Stephenson causal broadcast; FA-1 incident events are causally broadcast
* `beacon.py` beacons every 1 s: liveness (SUSPECTED after 3.5 s) + sender-receiver and RBS offset estimates
* `election.py` Bully + Ring (Tanenbaum list variant); start-up, beacon suspicion and recovery trigger Bully
* `mutex.py` Lamport, Ricart–Agrawala, Lodha–Kshemkalyani (Fig. 2 of the IEEE TPDS 2000 paper), NONE
* `snapshot.py` Chandy–Lamport with conserved "patrol vehicle" transfers
* `deadlock.py` lock manager on units (FA-2 only, FA-1 unit status untouched) + CMH edge-chasing probes

### Step 3 — Unit IV (verified with tools/test_fa2.py)
* `dfs_store.py` chunk server (data/dfs/<NODE>/), `gateway/dfs_master.py` metadata/placement/failover/repair
  - fix during testing: all chunks of one file landed on the same 2 nodes (load not updated within a
    write) → placement now counts the chunks of the write in progress.
* `objects.py` servants + skeleton + RemoteProxy, `gateway/naming.py` naming service
* `gateway/faas.py` + `faas_functions.py` local FaaS (process instances, cold/warm, scale to zero, triggers)
* `ledger.py` hash-chained replica (data/<region>_ledger.db, a NEW file per node), `gateway/ledger_service.py`
* `gateway/web.py` ETag / 304 web caching demo; `gateway/api.py` REST API `/api/fa2/*`, `/internal/fa2/*`

### Verification after steps 1-3
* `python tools/test_fa2.py` → **69 passed, 0 failed**
* `python tools/test_system.py` (FA-1) → 40 passed in 3 of 4 runs; one run failed
  *"traffic density changes over time"*. Investigated: node sensors tick every 3 s, the gateway heartbeat
  samples every ~2 s, so observed changes are alternately ~2 s and ~4.1 s apart (measured), while the test
  waits 3.5 s → timing-dependent (~8 %) independent of FA-2. The assertion was NOT weakened; see
  `docs/FA2_TESTING.md` for the baseline comparison.

### Step 4 — Dashboard (verified in headless Chromium)
* NEW `frontend/fa2.js`, `frontend/fa2.css` — FA-2 section driven only by `/api/fa2/*` (polling 2-4 s).
  `fa2.js` uses `addEventListener` because `app.js` assigns `document.onclick` (overwriting it would break
  FA-1's Fail/Recover/Resolve buttons).
* MODIFIED `frontend/index.html` (+368 lines, nothing removed): FA-2 section after the FA-1 `<main>`,
  "FA-2 ↓" header link, "FA-2" log-filter chip, two asset tags, one footer entry.
* Click-through of every FA-2 control: 0 console errors; FA-1 Fail/Recover still work.
* fix: two-digit numbers of ordered lists were clipped (list padding).

### Step 5 — Documentation and presentation
* NEW `docs/FA2_IMPLEMENTATION.md`, `FA2_CONCEPT_MAPPING.md`, `FA2_TESTING.md`, `FA2_VIVA_PREPARATION.md`
* MODIFIED `README.md` (+54 lines, nothing removed): FA-2 section 16 + a one-line pointer under the old
  "FA-2 future scope (not implemented)" heading (which would otherwise be misleading).
* MODIFIED `.gitignore` (+4 lines): `data/dfs/`, `data/fa2_dfs_master.json` (FA-2 runtime data).
* NEW `tools/capture_fa2_screenshots.py` → 24 real captures in `docs/screenshots/fa2/` (FA-1 shots untouched)
* NEW `tools/build_fa2_ppt.py` → `Distributed_Traffic_Incident_FA2.pptx` (16 slides) + previews in
  `docs/slides_preview_fa2/`. `tools/build_ppt.py` and `Distributed_Traffic_Incident_FA1.pptx` unchanged.

### Step 6 — Final verification (fresh scratch copy of the databases)
* FA-1 `tools/test_system.py`: **40 / 40** in three consecutive runs (before and after the FA-2 suite)
* FA-2 `tools/test_fa2.py`: **69 / 69**
* No exception / traceback in the combined node + gateway log.
* The user's `data/pune.db`, `mumbai.db`, `nashik.db` are byte-identical to before (MD5 checked): all
  development runs used `DATA_DIR=<scratch copy>`.

## 3. Complete file list

Modified (additive only): `backend/node.py`, `backend/main.py`, `frontend/index.html`, `README.md`, `.gitignore`.
Not touched by FA-2: `proto/traffic.proto` (its uncommitted change is the user's own), every other FA-1 file.

New:
```
proto/fa2_sync.proto
backend/rpc/fa2_sync_pb2.py, backend/rpc/fa2_sync_pb2_grpc.py          (generated)
backend/fa2/__init__.py, settings.py, errors.py, rpc.py, clocks.py, transport.py, runtime.py,
            clock_sync.py, causal.py, beacon.py, election.py, mutex.py, snapshot.py, deadlock.py,
            dfs_store.py, objects.py, ledger.py
backend/fa2/gateway/__init__.py, api.py, dfs_master.py, naming.py, faas.py, faas_functions.py,
                    ledger_service.py, web.py
frontend/fa2.js, frontend/fa2.css
tools/gen_fa2_proto.py, tools/test_fa2.py, tools/capture_fa2_screenshots.py, tools/build_fa2_ppt.py
docs/FA2_CHANGELOG.md, FA2_IMPLEMENTATION.md, FA2_CONCEPT_MAPPING.md, FA2_TESTING.md, FA2_VIVA_PREPARATION.md
docs/screenshots/fa2/*.png (24), docs/slides_preview_fa2/Slide1-16.PNG
Distributed_Traffic_Incident_FA2.pptx
```

## 4. Open points for the user
* "Beacon protocol" is ambiguous in the syllabus; implemented as liveness + clock-offset beacons (both meanings).
  Confirm which one your course expects.
* FA-1's "traffic density changes over time" test is timing-dependent in the original code (~8 % failures);
  it was left unchanged. Raising its 3.5 s wait to ≥ 4.5 s would make it deterministic — your call.
* Nothing was committed; review with `git status` / `git diff` and commit when satisfied.
* Fill in team names / roll numbers / college on slide 1 of the FA-2 deck (placeholders, as in FA-1).

