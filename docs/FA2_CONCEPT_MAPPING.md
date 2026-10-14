# FA-2 Concept Mapping — Concept → Where implemented → How demonstrated

Companion to the FA-1 table in [`CONCEPT_MAPPING.md`](CONCEPT_MAPPING.md). Every row is backed by code
that runs and by an assertion in `tools/test_fa2.py` unless marked *documentation*.

## Unit III — Synchronization

| # | Concept | Where implemented | How demonstrated (dashboard → FA-2 section / API) | Test |
|---|---|---|---|---|
| 1 | **Clock synchronization — physical clocks, skew, drift** | `backend/fa2/clocks.py` `PhysicalClock`; `settings.CLOCK_SKEW` | *FA-2 Node State* + *Clock Synchronization* bars: offset vs reference grows with drift | ✔ |
| 2 | **Cristian's algorithm** (+ NTP offset/delay) | `clock_sync.py` `cristian()`, time server `GET /internal/fa2/time` | *Cristian (all nodes)* → RTT samples, adjustment, error before → after (< 1 ms) | ✔ |
| 3 | **Berkeley algorithm** | `clock_sync.py` `berkeley()` run by the leader; `CLOCK_ADJUST` messages | *Berkeley (leader = daemon)* → per-node readings, average, adjustments, skew ≈ 1 ms | ✔ |
| 4 | **Logical clocks / Lamport's algorithm** | `clocks.py` `LogicalClocks.tick/merge`; stamped in `runtime.send()` / `_processor()` | *Logical Clocks* table ordered by (L, node); "Lamport violations = 0" over all send/receive pairs | ✔ |
| 5 | **Vector algorithm (vector clocks)** | `clocks.py`, `compare_vectors()`; `GET /api/fa2/events/compare` | Vector column; *Compare (happened-before?)* → before / after / concurrent | ✔ |
| 6 | **Why logical time** (physical-time anomalies) | `gateway/api.py` `fa2_events()` | Red counter: pairs where skewed physical time says *receive before send* | ✔ |
| 7 | **Causal ordering (vector timestamps)** | `causal.py` (Birman–Schiper–Stephenson); FA-1 hook `runtime.incident_event()` | *Run causal-order demo*: Nashik receive order ACK, ALERT → delivery order ALERT, ACK (← from buffer) | ✔ |
| 8 | **Global state** | `snapshot.py` (Chandy–Lamport markers, channel recording) | *Take snapshot*: recorded local + in-transit = 30, naive reading ≠ 30 | ✔ |
| 9 | **Election — Bully** | `election.py` ELECTION / OK / COORDINATOR, timeouts | *Start Bully*, or *Fail Nashik Node* (FA-1 button) → automatic re-election of Mumbai; *Recover* → Nashik wins again | ✔ |
| 10 | **Election — Ring** | `election.py` RING_ELECTION / RING_COORDINATOR, dead successors skipped | *Start Ring* → participant list in the trace | ✔ |
| 11 | **Mutual exclusion — Lamport** | `mutex.py` `Lamport` (request queue, REPLY, RELEASE) | *All 3 nodes request concurrently* → 18 messages, 0 lost updates | ✔ |
| 12 | **Mutual exclusion — Ricart–Agrawala** | `mutex.py` `RicartAgrawala` (deferred replies) | → 12 messages, 0 lost updates | ✔ |
| 13 | **Why mutual exclusion** | `mutex.py` `NoCoordination` + DFS register read-modify-write | Algorithm *NONE* → red overlapping bars, lost updates in the register | ✔ |
| 14 | **Beacon protocol** | `beacon.py` (liveness + sender-receiver + RBS offset estimation) | *Beacon Protocol* table; *Fail Mumbai Node* → peers mark it SUSPECTED within 3.5 s | ✔ |
| 15 | **Self-study: Lodha–Kshemkalyani fair ME** | `mutex.py` `LodhaKshemkalyani` (Fig. 2 of the paper: RV, LRQ, FLUSH) | → 8 messages (6 REQUEST + 2 FLUSH) vs 12 (RA) vs 18 (Lamport); LRQ / RV visible in *Per-node algorithm state* | ✔ |
| 16 | **Self-study: Knapp's classification** | `deadlock.py` (edge-chasing CMH probes), `api.py` global-state detection on a snapshot | *Create circular wait* → *Detect: edge-chasing* / *Detect: global state* → *Resolve*; path-pushing & diffusion: documentation | ✔ (2 of 4 classes) |

## Unit IV — Emerging distributed paradigms

| # | Concept | Where implemented | How demonstrated | Test |
|---|---|---|---|---|
| 17 | **Distributed object-based systems** | `objects.py` (servants, interface table, skeleton, `RemoteProxy`), `gateway/naming.py` | *Distributed Objects*: pick `fa2obj://…` reference, invoke; `sync_with` passes a remote reference | ✔ |
| 18 | **Distributed web-based systems** | whole dashboard (browser ↔ REST/WebSocket ↔ gateway ↔ gRPC); `gateway/web.py` | *HTTP caching*: GET → 200 + ETag; *Revalidate* → 304, bytes saved | ✔ |
| 19 | **Distributed file system** | `dfs_store.py` (chunk servers), `gateway/dfs_master.py` (metadata, placement, failover, repair) | *Distributed File System*: chunks → replicas per file, *read* shows serving replica, *corrupt #0* → failover, *Repair* | ✔ |
| 20 | **Serverless architectures** | `gateway/faas.py`, `gateway/faas_functions.py` | *Serverless Functions*: cold vs warm (PID), scale to zero, event triggers from incidents, report → DFS | ✔ |
| 21 | **Blockchain / distributed ledger (self-study)** | `ledger.py` (replica, hash chain, PoW, majority commit, sync), `gateway/ledger_service.py` | *Audit Ledger*: replicas' height/tip, blocks of real events, *Tamper* → broken at #1, *Repair* | ✔ |
| 22 | **Case study: Megaport** | documentation (`FA2_IMPLEMENTATION.md` §3.6, dashboard table) | case-study table | doc |
| 23 | **Case study: Cloudflare** | documentation | case-study table; related demo: HTTP caching, FaaS | doc |
| 24 | **Case study: AWS** | documentation | case-study table; related demo: vector clocks, replication, FaaS metering | doc |
| 25 | **Case study: Apache Hadoop** | documentation | case-study table; related demo: DFS (NameNode/DataNode roles) | doc |
| 26 | **Case study: Kubernetes** | documentation | case-study table; related demo: leader election, health monitoring | doc |

## FA-1 features re-used by FA-2

| FA-1 feature | Re-used for |
|---|---|
| Node processes + gRPC server | host the SyncNode service (same port) |
| Registry `config.NODES` / `config.address()` | FA-2 peers and addresses (names vs addresses) |
| Simulated failure (`node.available`) | crash model for every FA-2 algorithm |
| Broker + WebSocket log | FA-2 events (channel FA2), ledger input, FaaS triggers |
| Heartbeat monitor `HealthMonitor.is_online()` | DFS placement / repair, mutex & deadlock pre-checks |
| Coordinator `get_incident()` / `traffic()` | inputs of FaaS functions and DFS reports |
| Per-node SQLite (`NodeDatabase`) | `ResponseUnit` remote objects, snapshot local state (read-only) |
