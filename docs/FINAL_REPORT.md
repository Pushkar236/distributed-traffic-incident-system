# Final Report — Distributed Real-Time Traffic Incident Detection, Coordination and Response System

Distributed Systems · Formative Assessment 1 · mini-project

## 1. What was implemented

| Area | Delivered |
|---|---|
| Regional nodes | Three independent gRPC server processes (`PUNE-TR-01`, `MUM-TR-01`, `NSK-TR-01`) on ports 5001–5003, each with its own SQLite database (`data/pune.db`, `mumbai.db`, `nashik.db`) holding incidents, response units, traffic segments and node metadata |
| RPC | `proto/traffic.proto` with 13 procedures (`GetNodeStatus`, `CreateIncident`, `RequestResponseSupport`, `SearchResponseUnits`, `ReserveUnit`, …); Python gRPC servers in nodes, generated stubs used by the coordinator and by peers |
| Coordination middleware | `backend/coordinator.py`: incident-id allocation, RPC orchestration, broadcast fan-out, failover, store-and-forward of missed broadcasts, merged read views |
| Message-oriented communication | In-process publish/subscribe broker (`MessageBroker`: queue + worker task + subscriber list) with history; nodes publish over HTTP (`BrokerPublisher`); message types `INCIDENT_CREATED`, `INCIDENT_UPDATED`, `RESPONSE_REQUESTED`, `RESPONSE_AVAILABLE`, `RESPONSE_UNAVAILABLE`, `NODE_FAILED`, `NODE_RECOVERED`, `NODE_UNREACHABLE`, `REQUEST_REDIRECTED`, `INCIDENT_BROADCAST`, `RPC_CALL`, `SYSTEM` |
| Stream-oriented communication | WebSocket `/ws` streaming every broker message (`event` frames) plus node/incident snapshots every 2 s; dashboard never polls or refreshes |
| P2P | `PeerClient` inside each node calls neighbours directly (nearest first) — `SearchResponseUnits`, `ReserveUnit`, `ReleaseUnit`; coordinator is not on that path |
| WebRTC | Two dashboard tabs form an `RTCPeerConnection` with a data channel (`emergency`); optional audio/video via `getUserMedia` with renegotiation; signalling relayed over the existing WebSocket |
| Names / addresses | Registry in `backend/config.py` separates `node_id` from `address` (env-overridable for Docker); global incident ids `INC-xxxx`; unit ids with owner node |
| Fault tolerance | Heartbeat failure detector (gRPC `GetNodeStatus` every 2 s), failover loop in `report_incident()` redirecting nearest-first, peers skip `UNAVAILABLE` nodes, queued broadcasts delivered on recovery, Fail/Recover buttons and endpoints |
| Frontend | Single-page dashboard: network status table, animated SVG network diagram, live incidents (with resolve), incident form with request trace, filterable communication log, traffic meters, fault-tolerance controls, WebRTC panel |
| Virtualization | `docker/Dockerfile.node`, `docker/Dockerfile.gateway`, `docker/docker-compose.yml` (3 node containers + gateway, named volumes, service-name addressing) |
| Tooling | `run.py` launcher, `tools/test_system.py` (40 end-to-end assertions), `tools/capture_screenshots.py` (Playwright), `tools/build_ppt.py` (python-pptx), `tools/gen_proto.py` |
| Documentation | `README.md`, `docs/CONCEPT_MAPPING.md`, this report, 15-slide `Distributed_Traffic_Incident_FA1.pptx` with real screenshots |

## 2. How to run

```bash
pip install -r requirements.txt
python run.py                 # 3 nodes + gateway
# open http://localhost:8000
python tools/test_system.py   # optional: end-to-end verification while it runs
```
Docker: `cd docker && docker compose up --build`.

## 3. Distributed Systems concepts demonstrated

Unit 1: definition & goals (resource sharing, transparency, scalability, openness, fault handling), types
(distributed information system with real-time/pervasive traits), layered + P2P architecture, design issues
(heterogeneity, openness, scalability, failure handling, concurrency, transparency), middleware (coordination
service), distributed multimedia (WebRTC), model of computation (message passing, no shared state),
virtualization (Docker).

Unit 2: communication fundamentals (sync RPC vs. async messages, transient vs. queued), RPC (gRPC),
message-oriented (broker), stream-oriented (WebSocket), P2P (node → node gRPC), WebRTC, names/identifiers/
addresses (registry), fault tolerance (detection, redirection, recovery).

Detailed table: `docs/CONCEPT_MAPPING.md`.

## 4. What is simulated

* **Node failure button** — puts the node into a mode where every RPC is refused with gRPC `UNAVAILABLE`
  (callers cannot distinguish it from a crash). Killing the process is also detected; recovery then requires a restart.
* **Traffic density** — random-walk per road segment, nudged by active incidents; no real sensors.
* **Geography** — static nearest-first peer order (Pune → Mumbai → Nashik) instead of real distances/maps.
* **Message broker** — real publish/subscribe semantics, but implemented in Python inside the gateway process
  (no RabbitMQ). Persistence is in-memory (bounded history + per-node pending queues).
* **Data** — all incidents, roads, units and names are fictional.

## 5. Implemented for FA-1 vs. reserved for FA-2

| FA-1 (done) | FA-2 (future) |
|---|---|
| Independent nodes, per-node storage | Replication of node data to peers; recovery of state |
| gRPC, broker messages, WebSocket, P2P, WebRTC | — |
| Heartbeat failure detection + request redirection | Election algorithm for a dynamic coordinator |
| Atomic per-node unit reservation | Distributed mutual exclusion across nodes |
| Per-node timestamps | Clock synchronization / logical clocks for global ordering |
| Broadcast copies of incidents | Distributed consistency of those copies |

## 6. Verification performed

* `tools/test_system.py`: 40/40 assertions pass — REST endpoints, direct gRPC calls, P2P matching
  (PT-02 + MT-03), broker history, WebSocket frames, broadcast copies on other nodes, traffic change over time,
  failure (`UNAVAILABLE`), redirection to Mumbai with Nashik units, `REQUEST_REDIRECTED`/`NODE_FAILED` events,
  recovery, store-and-forward delivery, resolve-and-release, validation errors.
* `tools/capture_screenshots.py`: drove the real UI with Playwright, including WebRTC between two tabs with
  data-channel messages in both directions and remote video rendering (Chromium fake camera).
* `docker compose config` validates the compose file. Container build was **not** executed in this
  environment because the Docker daemon was not running; the Dockerfiles use only the same pip packages as
  local mode.

## 7. Known limitations

* The coordinator/gateway is a single process (single point of coordination) — deliberately, since election
  and replication are FA-2 topics; nodes keep working P2P but new incidents need the gateway.
* Incident ids are allocated centrally by the coordinator; if it restarts it re-derives the next id from the nodes
  it can reach.
* Broker history and pending broadcast queues are in memory; a gateway restart loses them.
* WebRTC demo assumes exactly two dashboard tabs (any extra tab also receives the signalling messages);
  no STUN/TURN is configured, so it is intended for one machine or a LAN.
* Unit reservation is atomic per node but not globally serialized (FA-2 mutual exclusion).
* Slide 1 team/college fields are placeholders to be filled in.
