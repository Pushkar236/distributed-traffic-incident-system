# FA-1 Concept Mapping — Concept → Where implemented → How demonstrated

Every row below is backed by code that runs in the demo. Nothing here is a slide-only claim.

| # | Concept (syllabus) | Where implemented (file / component) | How demonstrated in the running system |
|---|---|---|---|
| 1 | **Definition of Distributed Systems** | Three independent node processes (`backend/node.py`, ports 5001-5003) + gateway (`backend/main.py`) | `python run.py` starts 4 OS processes; the dashboard shows one coherent incident network built from separate nodes with separate databases |
| 2 | **Goals — Resource sharing** | `backend/services/matching_service.py`, `backend/communication/p2p.py` | Pune reserves Mumbai's unit MT-03 for INC-1004 (visible in *Assigned Response* with the remote unit tag) |
| 3 | **Goals — Transparency** | `backend/coordinator.py` | Operator posts one form; which node stores the record and which node lends units is hidden (location/access/failure transparency) |
| 4 | **Goals — Scalability / Openness** | `backend/config.py` registry + `proto/traffic.proto` contract | Adding a region = one registry entry + one process implementing the same proto service |
| 5 | **Goals — Fault handling** | `backend/services/fault_tolerance.py` | Heartbeat failure detector, redirection, queued broadcasts (see row 15) |
| 6 | **Types of DS** (distributed information system; real-time/pervasive traits) | Per-node SQLite records; traffic ticker in `node.py` (`traffic_ticker`) | Incident/unit records per node; traffic density streams every 3 s; nodes join/leave via fail/recover |
| 7 | **Distributed System Architecture** (layered + peer-to-peer hybrid) | Dashboard → Gateway/Coordinator → Nodes → Local DBs; node ↔ node P2P | Slide 6 diagram = actual process layout; dashboard *Network Diagram* animates the same topology |
| 8 | **Design Issues** (heterogeneity, openness, scalability, failure handling, concurrency, transparency) | Common gRPC contract; registry; per-node processes; failover; atomic `reserve_unit` UPDATE in `database/db.py`; coordinator | Slide 7 table; concurrency = `UPDATE … WHERE status='AVAILABLE'` prevents double-reserving one unit |
| 9 | **Middleware** | `backend/coordinator.py` + `rpc/client.py` (stub pool, id→address resolution) + broker + failure detector | Dashboard only calls `POST /api/incidents`; the middleware does id allocation, RPC, broadcast, matching, failover |
| 10 | **Distributed Multimedia Systems** | `frontend/app.js` (`rtc` object) — WebRTC data channel + optional audio/video | Emergency Communication panel: two tabs exchange messages and (optionally) live video peer-to-peer |
| 11 | **Model of Distributed Computation** (message passing, no shared memory/clock) | Separate processes, gRPC messages, broker events, per-node timestamps | No shared DB or memory; all interaction is visible as messages in the *Distributed Communication Log* |
| 12 | **Role of Virtualization** | `docker/Dockerfile.node`, `docker/Dockerfile.gateway`, `docker/docker-compose.yml` | Each node as its own container with its own volume; same code also runs as plain processes |
| 13 | **Communication Fundamentals** (sync/async, transient/persistent) | gRPC calls = synchronous request/reply; broker = asynchronous; queued broadcasts = persistent until node recovers | Trace panel shows synchronous RPC steps; log shows async broker messages; recovery delivers queued broadcast |
| 14 | **Remote Procedure Call** | `proto/traffic.proto`, generated `backend/rpc/traffic_pb2*.py`, `backend/node.py` servicer, `backend/rpc/client.py` | `CreateIncident`, `RequestResponseSupport`, `GetNodeStatus`, `SearchResponseUnits` … (13 procedures); logged as channel **RPC** |
| 15 | **Fault Tolerance** | `HealthMonitor` (heartbeat every 2 s), failover loop in `Coordinator.report_incident()`, `_check_up()` in `node.py` (simulated failure), store-and-forward in `coordinator.py` | *Fail Pune Node* → OFFLINE → new Pune incident redirected to Mumbai → Mumbai borrows Nashik units → *Recover Pune* → queued broadcast delivered |
| 16 | **Message-Oriented Communication** | `backend/communication/messaging.py` (`MessageBroker`, `BrokerPublisher`), `POST /internal/events` | Message types `INCIDENT_CREATED`, `INCIDENT_UPDATED`, `RESPONSE_REQUESTED`, `RESPONSE_AVAILABLE`, `NODE_FAILED`, `NODE_RECOVERED`, `REQUEST_REDIRECTED` in the log; `GET /api/events` history |
| 17 | **Stream-Oriented Communication** | `backend/communication/websocket.py` (`/ws`), `snapshot_stream` in `main.py` | Traffic meters, heartbeat timestamps, incident table and log update continuously without refresh |
| 18 | **Peer-to-Peer Messaging** | `backend/communication/p2p.py` (`PeerClient`), `SearchResponseUnits` / `ReserveUnit` / `ReleaseUnit` in `node.py` | Log entries `PUNE → MUMBAI RESPONSE_REQUESTED` / `MUMBAI → PUNE RESPONSE_AVAILABLE` (channel **P2P**); the coordinator is not on that path |
| 19 | **WebRTC** | `frontend/app.js` `rtc` (RTCPeerConnection, data channel `emergency`, getUserMedia), signalling relay in `websocket.py` | *Connect Peer* between two tabs → data channel OPEN → *Send WebRTC Message*; *Start Audio/Video (optional)* |
| 20 | **Names, Identifiers & Addresses** | `backend/config.py` (`NODES` ids vs `address()`, env override `NODE_ADDR_*`), incident ids `INC-xxxx`, unit ids `PT-01` … | Network Status table shows *Node ID* and *Address* columns separately; Docker resolves the same ids to different addresses |

## What is simulated vs. real

| Aspect | Real | Simulated |
|---|---|---|
| Separate node processes, ports, databases | ✔ | |
| gRPC calls coordinator→node and node→node | ✔ | |
| WebSocket stream to the browser | ✔ | |
| WebRTC peer connection between browser tabs | ✔ | |
| Node failure | gRPC `UNAVAILABLE` really returned by the node; killing the process also works | The *button* puts the node into a refuse-all mode instead of crashing it |
| Traffic density | | Random-walk values per road segment (no real sensors) |
| Geography / distances | | Static nearest-first peer order (Pune→Mumbai→Nashik) |
| Message broker | Real pub/sub semantics in Python | Not an external broker (RabbitMQ not used) |
| Incident / unit data | | Fictional demo data |
