# Distributed Real-Time Traffic Incident Detection, Coordination and Response System

**Distributed Systems — Formative Assessment 1 mini-project**

**Live demo:** https://distributed-traffic-incident-system.onrender.com — hosted on Render's free tier,
so the first visit after idle takes ~30–60 s to wake up. All four processes (3 gRPC nodes + gateway) run
inside one container; databases reset to fresh demo data on each restart (use *Reset demo data* any time).

An educational distributed system in which three independent regional traffic-control nodes
(**Pune**, **Mumbai**, **Nashik**) detect, share and respond to traffic incidents. Every major feature
maps to a Unit 1 / Unit 2 concept: RPC (gRPC), message-oriented communication (publish/subscribe
broker), stream-oriented communication (WebSocket), peer-to-peer messaging (node → node gRPC),
WebRTC, names vs. addresses, middleware, per-node storage, virtualization and fault tolerance.

> Everything described here runs on one laptop with `pip install -r requirements.txt` and `python run.py`.

---

## 1. Project overview

| Node | Node ID | Address (local mode) | Local database | Response units |
|---|---|---|---|---|
| Pune Traffic Node | `PUNE-TR-01` | `localhost:5001` | `data/pune.db` | PT-01, PT-02 |
| Mumbai Traffic Node | `MUM-TR-01` | `localhost:5002` | `data/mumbai.db` | MT-01, MT-02, MT-03 |
| Nashik Traffic Node | `NSK-TR-01` | `localhost:5003` | `data/nashik.db` | NT-01, NT-02 |

Dashboard + API gateway + coordination middleware + message broker: `http://localhost:8000`.

Incident types: Accident, Road Blockage, Vehicle Breakdown, Heavy Congestion, Traffic Signal Failure.
Severity → units required: **High = 2, Medium = 1, Low = 0 (monitoring only)**.

## 2. Architecture

```
                 ┌──────────────────────────────┐
                 │   Traffic Control Dashboard  │  HTML / CSS / JS (frontend/)
                 └──────────────┬───────────────┘
                     REST  +  WebSocket /ws  (+ WebRTC signalling relay)
                 ┌──────────────┴───────────────┐
                 │  API Gateway  (FastAPI)      │  backend/main.py
                 │  Coordination Middleware     │  backend/coordinator.py
                 │  Message Broker (pub/sub)    │  backend/communication/messaging.py
                 │  Heartbeat failure detector  │  backend/services/fault_tolerance.py
                 └───────┬──────────┬───────────┘
                gRPC     │          │          │   gRPC
        ┌────────────────┴┐  ┌──────┴────────┐ ┌┴────────────────┐
        │ PUNE-TR-01 :5001│  │MUM-TR-01 :5002│ │ NSK-TR-01 :5003 │   backend/node.py (one process each)
        └────────┬────────┘  └──────┬────────┘ └────────┬────────┘
                 │ ◄──── P2P gRPC ──►│◄──── P2P gRPC ───►│
            data/pune.db        data/mumbai.db      data/nashik.db   (SQLite, never shared)
```

**Request flow — `POST /api/incidents`**

1. Coordinator allocates a global identifier (`INC-1004`).
2. RPC `CreateIncident` on the reporting node (with failover to the nearest online node if it is down).
3. The node stores the record in **its own** SQLite file and publishes `INCIDENT_CREATED` to the broker.
4. Broker consumer (coordinator) broadcasts the incident to the other nodes via RPC `NotifyIncident`
   (queued for nodes that are offline → delivered on `NODE_RECOVERED`).
5. RPC `RequestResponseSupport` on the node → local unit search → if short, **P2P**
   `SearchResponseUnits` / `ReserveUnit` on the nearest peer(s).
6. Every step is published as a message and streamed to the dashboard over the WebSocket.

## 3. Distributed Systems concepts

See [`docs/CONCEPT_MAPPING.md`](docs/CONCEPT_MAPPING.md) for the full *concept → file → demonstration* table
and [`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md) for a file-by-file explanation of the code.
Summary:

| Concept | Implementation | Demonstration |
|---|---|---|
| RPC | Python gRPC, `proto/traffic.proto` (13 procedures) | `CreateIncident`, `RequestResponseSupport`, `GetNodeStatus` … |
| Message-oriented | Python pub/sub `MessageBroker` (+ store-and-forward) | `INCIDENT_CREATED`, `RESPONSE_AVAILABLE`, `NODE_FAILED` … in the log |
| Stream-oriented | WebSocket `/ws` | Traffic density, heartbeats, incident table update live |
| P2P | Node → node gRPC (`PeerClient`) | `PUNE → MUMBAI` request, `MUMBAI → PUNE` reply |
| WebRTC | Browser `RTCPeerConnection` + data channel (+ optional A/V) | Emergency Communication panel between two tabs |
| Names / addresses | `config.NODES` ids vs `config.address()` | Network Status shows both; Docker changes addresses only |
| Middleware | `Coordinator` + stub pool + broker + failure detector | Dashboard calls one endpoint; node details hidden |
| Fault tolerance | Heartbeat detector, failover loop in `report_incident()`, queued broadcasts | Fail Pune → request redirected → Recover → catch-up |
| Per-node storage | One SQLite file per node | `data/pune.db`, `mumbai.db`, `nashik.db` |
| Virtualization | Docker images + compose | One container per node |

## 4. Technology stack

Python 3.11 · FastAPI · Uvicorn · grpcio / grpcio-tools / protobuf · httpx · SQLite (stdlib) ·
HTML/CSS/Vanilla JS · WebSocket · WebRTC · Docker (optional) · python-pptx & Playwright (tooling only).

## 5. Folder structure

```
DS/
├── backend/
│   ├── main.py                  FastAPI gateway: REST, /ws, static frontend, lifespan wiring
│   ├── coordinator.py           Coordination middleware (id allocation, RPC, broadcast, failover)
│   ├── node.py                  Regional node: gRPC server + local SQLite + P2P + failure mode
│   ├── config.py                Node registry: ids, names, addresses, peers, constants
│   ├── rpc/                     traffic_pb2.py / traffic_pb2_grpc.py (generated), client.py (stub pool)
│   ├── communication/           messaging.py (broker), websocket.py (stream hub), p2p.py (peer client)
│   ├── services/                incident_service.py, matching_service.py, fault_tolerance.py
│   ├── models/schemas.py        Pydantic request models
│   └── database/                db.py (SQLite access), seed.py (fictional demo data)
├── frontend/                    index.html, style.css, app.js
├── proto/traffic.proto          gRPC contract
├── data/                        pune.db, mumbai.db, nashik.db (created on first run)
├── docker/                      Dockerfile.node, Dockerfile.gateway, docker-compose.yml
├── tools/                       gen_proto.py, test_system.py, capture_screenshots.py, build_ppt.py
├── docs/                        CONCEPT_MAPPING.md, FINAL_REPORT.md, screenshots/
├── run.py                       One-command local launcher
├── requirements.txt
└── Distributed_Traffic_Incident_FA1.pptx
```

## 6. Installation

```bash
git clone <your-repo>   # or unzip the project
cd DS
python -m venv .venv
# Windows:  .venv\Scripts\activate      Linux/macOS:  source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+ is required. (The gRPC stubs are already generated; regenerate with
`python tools/gen_proto.py` after editing `proto/traffic.proto`.)

## 7. How to run

**Local mode (recommended for the viva)**

```bash
python run.py            # starts PUNE, MUM, NSK node processes + gateway
# open http://localhost:8000
```

Options: `python run.py --reset` (fresh demo data), `--nodes-only`, `--gateway-only`, `--node PUNE-TR-01`.

**Docker mode**

```bash
cd docker
docker compose up --build      # 3 node containers + gateway container
# open http://localhost:8000
docker compose stop pune-node  # real container failure
docker compose start pune-node # recovery
```
The compose file is validated (`docker compose config`); building it needs the Docker daemon running.

## 8. How to start multiple nodes manually

Each node is an ordinary process; start them in separate terminals:

```bash
python -m backend.node --node-id PUNE-TR-01     # gRPC on :5001, data/pune.db
python -m backend.node --node-id MUM-TR-01      # gRPC on :5002, data/mumbai.db
python -m backend.node --node-id NSK-TR-01      # gRPC on :5003, data/nashik.db
python -m uvicorn backend.main:app --port 8000  # gateway / dashboard
```

Ctrl+C in a node terminal is a *real* crash: the dashboard shows it OFFLINE within ~2 s.

## 9. How to test

**Automated end-to-end check (40 assertions)**

```bash
python run.py                    # terminal 1
python tools/test_system.py      # terminal 2
```

**RPC** — call a node directly with the generated stub:

```bash
python -c "import grpc; from backend.rpc import traffic_pb2 as pb, traffic_pb2_grpc as g; \
s=g.TrafficNodeStub(grpc.insecure_channel('localhost:5002')); print(s.GetNodeStatus(pb.Empty()))"
```
or watch the *Distributed Communication Log* with the **RPC** filter while reporting an incident.

**Messaging** — publish a message to the broker and read the history:

```bash
curl -X POST http://localhost:8000/internal/events -H "Content-Type: application/json" \
     -d "{\"type\":\"SYSTEM\",\"source\":\"TEST\",\"summary\":\"hello broker\"}"
curl http://localhost:8000/api/events?limit=5
```
The dashboard log (filter **Messages**) shows it instantly — that is the broker → WebSocket path.

**WebSockets** — open the dashboard; the header pill shows *LIVE · WebSocket stream*; traffic bars and
heartbeat timestamps change without any refresh. Or from Python:
`python -c "import asyncio,websockets; asyncio.run((lambda: websockets.connect('ws://127.0.0.1:8000/ws'))().__aenter__())"`
(the test script does this properly).

**P2P** — report a **High** incident from Pune: Pune has one free unit, so it asks Mumbai directly.
Log entries `PUNE → MUMBAI RESPONSE_REQUESTED` and `MUMBAI → PUNE RESPONSE_AVAILABLE` carry channel **P2P**;
the *Last request trace* shows `PUNE-TR-01 -> MUM-TR-01: P2P SearchResponseUnits`.

**WebRTC** — open the dashboard in **two tabs**. In tab 1 click **Connect Peer** (it becomes the Traffic
Control Center); tab 2 auto-answers (Emergency Response Team). When the pill shows *data channel OPEN*,
type a message and click **Send WebRTC Message** — it appears in the other tab. **Start Audio/Video (optional)**
adds camera/microphone (browser permission required). Open exactly two tabs for the demo.

## 10. How to simulate node failure / recovery

* Click **Fail Pune Node** → the node refuses every RPC with gRPC `UNAVAILABLE`; the heartbeat fails,
  the node turns **OFFLINE** (red in the diagram) and `NODE_FAILED` is logged.
* Report another incident with *Reporting node = Pune*: the trace shows
  `PUNE-TR-01 unavailable → request redirected to MUM-TR-01`; Mumbai (no free units) borrows Nashik's
  units by P2P; the P2P attempt to Pune is logged as `NODE_UNREACHABLE`.
* Click **Recover Pune** → `NODE_RECOVERED`, and the broadcast Pune missed is delivered
  (`Delivered 1 queued broadcast(s) to PUNE-TR-01 after recovery`).
* API equivalents: `POST /api/nodes/PUNE-TR-01/fail`, `POST /api/nodes/PUNE-TR-01/recover`.
* Real crash: kill the node process (or `docker compose stop pune-node`); recovery = restart it.

## 11. Demo scenario (viva script, ~5 minutes)

1. Open `http://localhost:8000` — all three nodes ONLINE, 3 seed incidents, traffic bars moving.
2. Report **Accident · Pune · High** → trace: `INC-1004` recorded in `pune.db`, broadcast, local `PT-02`,
   P2P to Mumbai, `MT-03` reserved → **UNITS ASSIGNED**. Watch the diagram pulses and the log.
3. Filter the log by **P2P**, then **RPC**, then **Messages** to point at each communication style.
4. **Fail Pune Node** → red node, `NODE_FAILED`.
5. Report **Road Blockage · Pune · High** → redirected to Mumbai → P2P Pune UNAVAILABLE → Nashik
   `NT-01, NT-02` → completed. The system kept working.
6. **Recover Pune** → `NODE_RECOVERED` + queued broadcast delivered.
7. Open a second tab → **Connect Peer** → exchange a WebRTC message (optionally start video).
8. **Reset demo data** to return to the initial state.

## 12. API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/nodes` | Node status snapshot + registry (ids, addresses, peers) |
| GET | `/api/nodes/{node_id}` | One node: status, units, incidents held, peers |
| POST | `/api/nodes/{node_id}/fail` / `/recover` | Fault-tolerance controls |
| GET | `/api/incidents` | Incidents merged from every reachable node |
| POST | `/api/incidents` | Report an incident (`type, location, severity, description, detected_by`) |
| GET | `/api/incidents/{id}` | One incident |
| PATCH | `/api/incidents/{id}` | `{"status":"RESOLVED"}` — releases local and borrowed units |
| GET | `/api/traffic` | Traffic density per region with road segments |
| GET | `/api/events?limit=&type=` | Broker history |
| POST | `/internal/events` | Producer endpoint used by nodes |
| POST | `/api/reset` | Reload fictional demo data on every node |
| WS | `/ws` | Live stream: `hello`, `event`, `snapshot`, `signal` frames |

Interactive docs: `http://localhost:8000/docs`.

## 13. FA-1 concept mapping

Full table: [`docs/CONCEPT_MAPPING.md`](docs/CONCEPT_MAPPING.md).

## 14. FA-2 future scope (not implemented)

> **Update:** mutual exclusion, clock synchronization, logical clocks and election are now implemented
> by the FA-2 extension — see [section 16](#16-fa-2-extension--unit-iii-synchronization--unit-iv-emerging-paradigms).

Mutual exclusion for network-wide unit reservation · clock synchronization (Lamport/vector) for
cross-node event ordering · election algorithm for a dynamic coordinator · replication & recovery of
node data · distributed consistency of incident copies.

## 15. Presentation

`Distributed_Traffic_Incident_FA1.pptx` (15 slides) is generated from the implementation by
`python tools/build_ppt.py`; screenshots in `docs/screenshots/` are real captures made with
`python tools/capture_screenshots.py` against the running system. Team names, roll numbers and the
college name are placeholders on slide 1 — fill them in.

## 16. FA-2 extension — Unit III (Synchronization) & Unit IV (Emerging Paradigms)

FA-2 extends this system in place: the same three node processes and gateway, plus a second gRPC service
(`SyncNode`, `proto/fa2_sync.proto`) on each node port. FA-1 code, APIs and UI are unchanged
(additive hooks only — see [`docs/FA2_CHANGELOG.md`](docs/FA2_CHANGELOG.md)).

| Unit III — Synchronization | Unit IV — Emerging paradigms |
|---|---|
| Cristian (+NTP offset/delay) and Berkeley clock sync of skewed node clocks | Distributed objects: references, proxy, skeleton, naming service |
| Lamport and vector clocks on every FA-2 message; merged event log with clock-condition check | Distributed web-based system: ETag / 304 HTTP caching |
| Causal broadcast (Birman–Schiper–Stephenson) of incident events | Distributed file system: chunks × 2 replicas, checksums, failover, repair |
| Chandy–Lamport global snapshot | Serverless: local FaaS with cold/warm starts, scale to zero, event triggers |
| Bully + Ring election (auto re-election on leader failure) | Blockchain/DLT (self-study): hash-chained, majority-committed audit ledger |
| Mutual exclusion: Lamport, Ricart–Agrawala, **Lodha–Kshemkalyani** (self-study) | Case studies: Megaport, Cloudflare, AWS, Hadoop, Kubernetes (documentation) |
| Beacon protocol (liveness + offset estimation) | |
| Deadlock detection per **Knapp's classes** (self-study): edge-chasing + global state | |

**Run and demonstrate**

```bash
python run.py                    # same launcher as FA-1
# open http://localhost:8000 and click "FA-2 ↓" in the header
python tools/test_system.py      # FA-1 regression (40 assertions)
python tools/test_fa2.py         # FA-2 (69 assertions, includes leader failure / recovery)
```

FA-2 API: `http://localhost:8000/docs` → tag *FA-2* (`/api/fa2/state`, `/api/fa2/election`, `/api/fa2/mutex/run`,
`/api/fa2/snapshot`, `/api/fa2/deadlock/*`, `/api/fa2/dfs/*`, `/api/fa2/objects/*`, `/api/fa2/faas/*`,
`/api/fa2/ledger/*`, `/api/fa2/web/feed`, …).

**New files**

```
backend/fa2/                 node side: runtime, transport, clocks, clock_sync, causal, beacon, election,
                             mutex, snapshot, deadlock, objects, dfs_store, ledger
backend/fa2/gateway/         gateway side: api (REST), dfs_master, naming, faas (+ faas_functions),
                             ledger_service, web
backend/rpc/fa2_sync_pb2*.py generated from proto/fa2_sync.proto (python tools/gen_fa2_proto.py)
frontend/fa2.js, fa2.css     FA-2 dashboard section
tools/test_fa2.py            FA-2 end-to-end test
tools/capture_fa2_screenshots.py, tools/build_fa2_ppt.py   screenshots + presentation
docs/FA2_*.md                implementation, concept mapping, testing, viva preparation, changelog
Distributed_Traffic_Incident_FA2.pptx
```

Runtime data created by FA-2 (git-ignored): `data/*_ledger.db`, `data/dfs/`, `data/fa2_dfs_master.json`.

Documentation: [`FA2_IMPLEMENTATION.md`](docs/FA2_IMPLEMENTATION.md) ·
[`FA2_CONCEPT_MAPPING.md`](docs/FA2_CONCEPT_MAPPING.md) · [`FA2_TESTING.md`](docs/FA2_TESTING.md) ·
[`FA2_VIVA_PREPARATION.md`](docs/FA2_VIVA_PREPARATION.md).
