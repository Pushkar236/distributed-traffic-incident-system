# FA-2 Implementation — Unit III (Synchronization) and Unit IV (Emerging Distributed Paradigms)

FA-2 **extends** the FA-1 system (three regional node processes — Pune `PUNE-TR-01`, Mumbai
`MUM-TR-01`, Nashik `NSK-TR-01` — plus the FastAPI gateway). Nothing from FA-1 was removed or
replaced; see [`FA2_CHANGELOG.md`](FA2_CHANGELOG.md) for the exact diff of every existing file.

## 1. Architecture

```
                         Dashboard (index.html + app.js [FA-1] + fa2.js [FA-2])
                                 │ REST /api/...  /api/fa2/...   WebSocket /ws
 ┌───────────────────────────────┴────────────────────────────────────────────────────┐
 │ Gateway process (backend/main.py)                                                   │
 │   FA-1: coordinator, broker, heartbeat monitor, WebSocket hub                       │
 │   FA-2: gateway/api.py  REST API · dfs_master.py (DFS metadata) · naming.py         │
 │         faas.py (serverless runtime) · ledger_service.py · web.py (HTTP caching)    │
 │         /internal/fa2/time (Cristian time server) · /internal/fa2/dfs (file API)    │
 └──────┬───────────────────────────────┬───────────────────────────────┬─────────────┘
        │ gRPC TrafficNode (FA-1)        │ + gRPC SyncNode (FA-2)        │
 ┌──────┴───────────┐  Deliver()  ┌──────┴───────────┐  Deliver()  ┌───┴──────────────┐
 │ PUNE-TR-01 :5001 │◄───────────►│ MUM-TR-01 :5002  │◄───────────►│ NSK-TR-01 :5003  │
 │ FA-2 runtime     │   FIFO      │ FA-2 runtime     │   FIFO      │ FA-2 runtime     │
 │ pune.db (FA-1)   │  channels   │ mumbai.db        │  channels   │ nashik.db        │
 │ pune_ledger.db   │             │ mumbai_ledger.db │             │ nashik_ledger.db │
 │ dfs/PUNE-TR-01/  │             │ dfs/MUM-TR-01/   │             │ dfs/NSK-TR-01/   │
 └──────────────────┘             └──────────────────┘             └──────────────────┘
```

* **A second gRPC service on the same port.** `proto/fa2_sync.proto` defines `SyncNode`; every node
  registers it next to FA-1's `TrafficNode` (`runtime.attach()` called from `node.py`). FA-1's
  `proto/traffic.proto` is unchanged.
* **Message passing model.** All Unit III algorithms are written the textbook way: a node reacts to
  received messages (`on_REQUEST`, `on_MARKER`, …) and sends messages. `Deliver(SyncMessage)` carries
  `kind`, `sender`, the sender's **Lamport** clock, its **vector** clock, its physical clock reading and a
  JSON payload.
* **FIFO channels** (`transport.py`): one outgoing queue + sender task per peer, so messages between two
  nodes are delivered in send order (required by Lamport ME, Lodha–Kshemkalyani and Chandy–Lamport).
  A per-channel / per-message delay can be injected to create messages "in transit".
* **Atomic handlers** (`runtime.py`): received messages go into an inbox processed one at a time, so each
  "on receiving X do …" step is atomic, as the algorithms assume.
* **Failure model.** FA-1's *Fail Node* button sets `node.available = False`. The FA-2 runtime then refuses
  every SyncNode RPC with `UNAVAILABLE`, sends nothing, and drops buffered input — indistinguishable from
  a crash for the other nodes. *Recover* restores it and triggers a Bully election (recovering-node rule).
* **The gateway only triggers and observes.** It sends control `Command`s ("start an election", "request
  the CS") and reads state; the algorithms themselves run between the node processes.

Integration with FA-1:

| FA-1 feature | FA-2 integration |
|---|---|
| Incident create/update in a node | gets Lamport + vector timestamps and is **causally broadcast** (`runtime.incident_event`) |
| Node Fail / Recover buttons | beacons stop → peers suspect the node → re-election; DFS reads fail over; ledger commits with 2/3 |
| Response units (SQLite) | remote `ResponseUnit` objects read them; deadlock demo locks them (lock table is FA-2 only) |
| Incidents | FaaS `classify_incident` runs on every `INCIDENT_CREATED`; resolved incidents get a report stored in the DFS |
| Broker / communication log | every FA-2 step is published with channel **FA2** (new *FA-2* filter chip) |

## 2. Unit III — Synchronization

### 2.1 Physical clocks and clock synchronization — `backend/fa2/clocks.py`, `clock_sync.py`

All processes run on one host and would read the same OS clock, so each node runs a **software clock**
`real time + offset + drift × elapsed` with a deliberate offset and an exaggerated drift
(`settings.CLOCK_SKEW`: Pune +1.25 s / +900 ppm, Mumbai −0.8 s / −600 ppm, Nashik +2.1 s / +1500 ppm).
This is the only simulated part; the synchronization algorithms are real message exchanges.

* **Cristian's algorithm (external sync)** — the node requests the time from the gateway
  (`GET /internal/fa2/time`, the reference "UTC" source) and sets `t3 + RTT/2`. Four samples are taken
  on one connection and the minimum-RTT sample is used (Cristian's refinement — the first request on
  Windows pays a ~250 ms connection set-up, a real example of delay asymmetry). The same timestamps
  give the **NTP** offset θ = ((t2−T1)+(t3−T4))/2 and delay δ = (T4−T1)−(t3−t2), also reported.
  Measured: errors of 1.2–2.9 s reduced to < 1 ms.
* **Berkeley algorithm (internal sync)** — the **elected leader** is the time daemon. It polls every
  node with `GetTime` (RTT-compensated), discards readings > 5 s from the median (fault-tolerant
  average), and sends each node the *adjustment* (`CLOCK_ADJUST`). Measured: spread 2.1 s → ~1 ms.
* Because of drift, clocks separate again over time — visible live on the offset bars.

### 2.2 Logical clocks — Lamport's algorithm and the vector algorithm — `clocks.py`, `runtime.py`

* **Lamport clock**: `L := L+1` before every event; on receive `L := max(L, L_msg) + 1`.
* **Vector clock**: `V[i] := V[i]+1` per event; on receive `V := max(V, V_msg)` then `V[i] += 1`.
* Every FA-2 message (except beacons) is a send/receive event and carries both clocks; every node keeps
  an event log (`type, kind, peer, L, V, physical time`).
* `GET /api/fa2/events` merges the three logs, orders them by (Lamport, node) — Lamport's **total order** —
  and **checks the clock conditions on every matched send/receive pair**: `L(send) < L(recv)` and
  `V(send) < V(recv)` (0 violations in every test run) and counts the pairs where the *skewed physical*
  timestamps say the receive happened before the send (e.g. 155 of 367 pairs) — the reason logical
  clocks are needed.
* `GET /api/fa2/events/compare?a=&b=` decides happened-before vs concurrent from the vector clocks.

### 2.3 Causal ordering with vector timestamps — `causal.py`

Birman–Schiper–Stephenson causal broadcast: `VT[k]` = broadcasts delivered from node k; a message from j
with timestamp `VTm` is delivered when `VTm[j] = VT[j]+1` and `VTm[k] ≤ VT[k]` for k ≠ j, otherwise
**buffered**. Every FA-1 incident create/update is causally broadcast. The demo slows Pune→Nashik by 3 s:
Nashik receives Mumbai's acknowledgement before Pune's alert, buffers it, and delivers alert → ack.

### 2.4 Global state — `snapshot.py`

Chandy–Lamport snapshot on the FIFO channels (marker rules as in the textbook; any node can initiate,
the dashboard uses the leader). The application state is a pool of standby **patrol vehicles**
(10 per region) that nodes lend to each other while "transfer traffic" runs (0.2–1.2 s in transit).
The recorded local state also contains Lamport/vector clocks, FA-1 active incidents / free units and
the wait-for edges. **Consistency check:** recorded local + in-transit vehicles = 30 in every run, while
a naive simultaneous read of the three counters gives e.g. 21–25.

### 2.5 Election algorithms — `election.py`

* **Bully** (priority Pune 1 < Mumbai 2 < Nashik 3): ELECTION to higher nodes, OK, COORDINATOR, with
  timeouts (1.5 s for OK, 4 s for COORDINATOR). Triggered at start-up, by a **suspected leader**
  (beacons) and by a **recovered node**.
* **Ring** (Tanenbaum's variant): RING_ELECTION collects the ids of live nodes around Pune→Mumbai→Nashik;
  a dead successor is skipped (delivery failure); RING_COORDINATOR circulates once.
* The leader is used as Berkeley time daemon, default snapshot initiator and ledger block proposer.

### 2.6 Mutual exclusion — `mutex.py`

Shared resource: the regional air-ambulance register `/shared/air-ambulance-register.log` stored in the
FA-2 DFS. The critical section is a **read-modify-write** of that file with 0.6 s of work in between, so
any violation of mutual exclusion shows up as a **lost update** (fewer lines than CS executions).
Request priority `ReqID = (sequence number, PID)` with PID Pune 1, Mumbai 2, Nashik 3.

| Algorithm | Messages / CS (theory) | Measured, 3 concurrent requests |
|---|---|---|
| Lamport (REQUEST, REPLY, RELEASE; request queue; FIFO) | 3(N−1) = 6 | **18** total (6 per CS), 0 lost updates |
| Ricart–Agrawala (REQUEST, deferred REPLY) | 2(N−1) = 4 | **12** total (4 per CS), 0 lost updates |
| Lodha–Kshemkalyani (REQUEST, REPLY, FLUSH) | N−1 … 2(N−1) | **8** total (6 REQUEST + 2 FLUSH), 0 lost updates |
| NONE (no algorithm) | 0 | 0 messages, **2 overlapping CS, 2 lost updates** |

All three algorithms grant the CS in request-priority (timestamp) order — the fairness property.
The 8 messages of Lodha–Kshemkalyani match the paper's own three-process example ("only eight messages
are required, thus saving 33 percent over the RA algorithm").

### 2.7 Beacon protocol — `beacon.py`

> **Interpretation note.** "Beacon protocol" is not defined in the repository and is used with two
> meanings in the literature: (a) periodic *beacons for liveness/membership monitoring*, and
> (b) *beacon-based clock synchronization* (IEEE 802.11 TSF beacons, Reference Broadcast Synchronization).
> FA-2 implements one protocol that serves **both**; please confirm with the course material which
> meaning your syllabus intends.

Every node sends a `BEACON` to every peer each second, out-of-band (not queued behind algorithm
messages, not counted as logical events) carrying its sequence number, its physical clock and the
receive times of the last beacons it heard.
* **Liveness:** no beacon for 3.5 s → `SUSPECTED` (`PEER_SUSPECTED` event); a suspected leader triggers a
  Bully election. This is *decentralised* (every node monitors every peer), unlike FA-1's central
  heartbeat in the gateway.
* **Offset estimation:** sender→receiver estimate `clock_in_beacon − local receive time`, and an RBS-style
  receiver–receiver estimate (two receivers compare their arrival times of the *same* third-party beacon,
  cancelling the sender's clock and send delay). Both agree with the true offsets within a few ms.

### 2.8 Self-study — Lodha and Kshemkalyani's fair mutual exclusion algorithm

Source: S. Lodha and A. Kshemkalyani, *A Fair Distributed Mutual Exclusion Algorithm*, IEEE TPDS 11(6),
June 2000 (also in Kshemkalyani & Singhal, *Distributed Computing*, ch. 9). Implemented exactly as Fig. 2:

* **State per process:** `My_Sequence_Number`, `Highest_Sequence_Number_Seen`, `RV[1..N]` (who has
  "replied" — by REPLY, by a concurrent REQUEST, or by FLUSH), `LRQ` (local request queue of its own request
  and the *concurrent* requests received after making it, sorted by priority).
* **Concurrent requests:** Ri and Rj are concurrent iff each process receives the other's REQUEST after
  making its own. `CSet_i` = the requests concurrent with Ri (plus Ri).
* **Key ideas:** a concurrent lower-priority REQUEST *is* the reply; a REPLY(Rj) or FLUSH(Rj) is a collective
  logical reply for every request with priority ≥ Rj; after its CS a process sends a single FLUSH to the next
  request in its LRQ and REPLYs only to deferred non-concurrent requests.
* **Messages per CS** (paper §4.1): `2N − |CSet|` if a lower-priority concurrent request exists,
  `2N − 1 − |CSet|` if not, `2(N−1)` when requests are serialised — i.e. between N−1 and 2(N−1).
  With N = 3 and all three concurrent: 3 + 3 + 2 = 8 ✔ (measured).
* **Assumptions:** FIFO channels, reliable processes (a crashed process blocks everybody — the demo refuses
  to run unless all nodes are online, and requests time out after 25 s).

### 2.9 Self-study — Knapp's classification of distributed deadlock detection

E. Knapp, *Deadlock Detection in Distributed Databases*, ACM Computing Surveys 19(4), 1987, classifies
distributed deadlock detection algorithms into four classes:

| Class | Idea | Classic algorithm | In FA-2 |
|---|---|---|---|
| **Path-pushing** | sites send (parts of) their wait-for paths to other sites, which combine them until a cycle is visible | Obermarck (1982) | documented only |
| **Edge-chasing** | a blocked process sends a *probe* along wait-for edges; a probe returning to its initiator means a cycle | Chandy–Misra–Haas (AND model, 1983) | **implemented** (`deadlock.py`, PROBE messages) |
| **Diffusing computation** | the initiator starts a diffusing computation (queries/replies) over the wait-for graph; used for the OR / communication model | Chandy–Misra–Haas (OR model, 1983) | documented only |
| **Global state detection** | take a consistent snapshot of the system and look for a cycle in the recorded wait-for graph | Bracha–Toueg, Kshemkalyani–Singhal | **implemented** (Chandy–Lamport snapshot of the wait-for edges + cycle search in the gateway) |

Demo: each node locks its own unit (PT-01, MT-01, NT-01) and then requests the next region's unit, giving
Pune → Mumbai → Nashik → Pune. Edge-chasing from Mumbai returns `MUM → NSK → PUNE → MUM`; global-state
detection finds the same cycle in snapshot data. Resolution aborts a victim (lowest Bully priority in the
cycle), which releases its locks; a new probe is then discarded (no deadlock). The FA-2 lock table is
separate from FA-1's unit dispatch status, so FA-1 data is never changed by the demo.

## 3. Unit IV — Emerging distributed paradigms

### 3.1 Distributed object-based system — `objects.py`, `gateway/naming.py`
RMI/CORBA structure: **object reference** `fa2obj://<node>/<interface>/<id>`, **interface** table (only listed
methods are remote), **proxy** (`RemoteProxy`, marshals arguments to JSON and calls the `Invoke` RPC),
**skeleton/object adapter** on the node (unmarshal → dispatch → marshal), **naming service** in the gateway.
Objects: two `TrafficSignal`s per region (state lives on the hosting node; `sync_with(ref)` receives a
remote reference as a parameter and the hosting node invokes that object itself — object-to-object call) and
one `ResponseUnit` per FA-1 unit (reads the node's own SQLite row).

### 3.2 Distributed web-based system — `gateway/web.py` + the dashboard itself
The application is a multi-tier web system (browser ↔ HTTP/REST + WebSocket ↔ gateway ↔ gRPC ↔ nodes).
`GET /api/fa2/web/feed` demonstrates web caching: `ETag` + `Cache-Control: public, max-age=5`; a client
revalidating with `If-None-Match` gets **304 Not Modified** with no body (counted as bytes saved).

### 3.3 Distributed file system — `dfs_store.py` (nodes), `gateway/dfs_master.py`
GFS/HDFS-like: the master (gateway) keeps metadata (path → chunks → replica nodes, persisted to
`data/fa2_dfs_master.json`); nodes store 256-byte chunks with SHA-256 checksums in `data/dfs/<NODE>/`.
Replication factor 2, least-loaded placement on online nodes; reads try replicas in turn and skip offline or
corrupted copies (checksum verified on the node and again at the master); repair re-replicates (also
automatically after `NODE_RECOVERED`). Used by: incident reports, the mutual-exclusion register, operator files.

### 3.4 Serverless architecture — `gateway/faas.py`, `gateway/faas_functions.py`
A local FaaS platform (clearly an emulation, not AWS Lambda): functions are stateless Python handlers; each
**instance is a separate OS process**; the first call of an idle function is a **cold start** (process start +
init, measured ~80–100 ms on the test machine), later calls are **warm** (same PID, < 1 ms); concurrent calls scale out to 2
instances; idle instances are destroyed after 60 s (**scale to zero**); per-ms metering in GB-seconds
(illustrative). Triggers: HTTP and broker events (`INCIDENT_CREATED` → `classify_incident`;
`RESOLVED` → `incident_report` whose output binding writes `/reports/INC-xxxx.txt` into the DFS).

### 3.5 Blockchain / distributed ledger (self-study extension) — `ledger.py`, `gateway/ledger_service.py`
Each node holds a full replica in its own SQLite file. Blocks are hash-chained (SHA-256 of index, time,
proposer, previous hash, nonce, events) with a toy proof-of-work (3 leading hex zeros). Important coordination
events (incidents, failures, elections, CS entries, deadlocks, clock syncs, DFS writes) are batched every 4 s;
the **elected leader** mines the block and replicates it; it is **committed when a majority (2/3)** stored it.
Followers that missed blocks catch up; a new leader adopts the longest valid chain; editing an old block in one
replica is detected by verification and repaired from a valid peer. Honest limits: crash-fault tolerant,
**not** Byzantine-fault tolerant; PoW here only illustrates mining.

### 3.6 Industry case studies (documentation — no cloud infrastructure added)

* **Megaport** — Network-as-a-Service: customers connect to Megaport's private, software-defined backbone at
  data-centre "Ports" and create Virtual Cross Connects (VXCs) to clouds or other sites on demand via portal/API;
  Megaport Cloud Router (MCR) routes between clouds without on-premises hardware. *Concepts:* overlay/virtual
  networking, elastic bandwidth, private interconnection. *Here:* the dedicated node↔node P2P links and the
  registry of peers/addresses play the role of a private regional interconnect.
* **Cloudflare** — a global anycast network: the same IP addresses are announced from many data centres so
  users reach the nearest one; it provides CDN caching, DNS, DDoS mitigation and *Workers* (serverless code run
  in V8 isolates at the edge) plus *Durable Objects* (single-instance stateful objects for coordination).
  *Here:* ETag/304 caching, the FaaS runtime, remote objects that live on exactly one node.
* **AWS** — Regions made of isolated Availability Zones; services replicate across AZs (S3 object storage,
  DynamoDB, RDS Multi-AZ); Lambda is the reference serverless platform (cold/warm starts, per-ms billing).
  Amazon's Dynamo paper (2007) popularised consistent hashing, sloppy quorums and vector clocks for
  conflict detection. *Here:* vector clocks, replicated DFS chunks, majority-commit ledger, Lambda-style metering.
* **Apache Hadoop** — HDFS stores large files as blocks replicated (default 3×) on DataNodes, with metadata on
  the NameNode and checksums per block; MapReduce/YARN schedule computation near the data. *Here:* the DFS master
  = NameNode, nodes = DataNodes, checksums and re-replication.
* **Kubernetes** — a control plane (API server, scheduler, controllers) storing cluster state in **etcd**, which
  uses the **Raft** consensus algorithm; controllers continuously reconcile desired and actual state; components
  use **leader election**; Services give stable names to changing pod addresses; liveness/readiness probes.
  *Here:* Bully/Ring leader election, beacons/heartbeats as health probes, names vs addresses, Docker images.

## 4. What is real and what is simulated

| Aspect | Status |
|---|---|
| Message exchanges of every Unit III algorithm between separate OS processes over gRPC | **real** |
| Lamport / vector clocks, causal buffering, snapshot markers, probes, election timeouts | **real** |
| Clock *skew and drift* | **simulated** (software clock per node; one host has one OS clock) |
| Clock *synchronization* (Cristian, Berkeley, beacon estimates) | **real** algorithms correcting the simulated clocks |
| Node failure | FA-1 refuse-all mode (or kill the process) — same as FA-1 |
| Channel delays for the causal and snapshot demos | **injected** (sleep in the FIFO queue) |
| Mutual exclusion shared resource | **real** file in the DFS; lost updates really happen without an algorithm |
| Patrol vehicles (snapshot application) and the FA-2 lock table (deadlock demo) | demo state kept by the nodes (FA-1 data untouched) |
| DFS chunks, checksums, replication, failover | **real** files on disk per node |
| FaaS cold/warm starts, process isolation, scale to zero | **real** processes on the gateway host; *not* a cloud service, billing illustrative |
| Ledger hashing, chaining, replication, tamper detection | **real**; PoW difficulty is a toy, no BFT |
| Industry case studies | documentation only |

## 5. Known limitations

* Mutual-exclusion and snapshot algorithms assume reliable processes: with a node down the mutex demo is
  refused (HTTP 409) and a snapshot cannot complete; pending CS requests time out after 25 s (`/api/fa2/reset`).
* The DFS master and the ledger service run in the gateway, which is a single point of failure (as in GFS/HDFS
  without HA). The gateway is FA-1's single coordinator anyway.
* The Bully algorithm assumes timeouts are accurate; under heavy load a slow node can be suspected.
* Clock steps are applied immediately (real NTP slews the clock gradually).
* `tools/test_fa2.py` creates one demo incident; run it against demo data (or use `--reset` / *Reset demo data*).
