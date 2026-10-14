# FA-2 Viva Preparation

Short answers tied to the actual code. File paths are relative to `backend/fa2/` unless stated.

## A. Demo order (~8 minutes)

1. Dashboard → **FA-2 ↓**. Point at *FA-2 Node State*: leader, Lamport, vector, skewed physical clocks, beacons.
2. Clock sync: **Cristian** then **Re-skew** + **Berkeley**.
3. Logical clocks: 0 Lamport / 0 vector violations vs N physical-time anomalies → *why logical clocks*.
4. **Causal-order demo** (receive ≠ delivery order at Nashik).
5. **Fail Nashik Node** (FA-1 panel) → beacons suspect → Bully elects Mumbai → **Recover** → Nashik back.
6. Mutual exclusion: LK (8 msgs) → RA (12) → Lamport (18) → NONE (lost updates).
7. Snapshot (30 = local + in transit). Deadlock: setup → edge-chasing → global state → resolve.
8. Unit IV: object `sync_with`, DFS report + corrupt + read, FaaS cold vs warm, ledger tamper/repair, 304.

## B. Questions and answers

**Q1. How did you add FA-2 without breaking FA-1?**
A second gRPC service (`SyncNode`, `proto/fa2_sync.proto`) on the same node port, a separate package
`backend/fa2/`, separate frontend files. Existing files got only additive hooks: 7 lines in `node.py`, 6 in
`main.py`, an appended section in `index.html`. FA-1's 40-assertion test still passes.

**Q2. Why do you need logical clocks if you synchronize physical clocks?**
Synchronization only bounds the error; it never makes it zero, and drift re-opens it. Our event check shows
many send/receive pairs where the skewed physical timestamps put the receive *before* the send. Lamport
clocks guarantee `a → b ⇒ L(a) < L(b)`; we verify `L(send) < L(recv)` on every pair (0 violations).

**Q3. Lamport vs vector clocks?**
Lamport is a single integer: consistent with causality but `L(a) < L(b)` does not imply `a → b`. Vector clocks
characterise causality exactly: `a → b ⇔ V(a) < V(b)`; incomparable vectors mean concurrent. The *Compare*
button returns before/after/concurrent from vectors.

**Q4. Cristian vs Berkeley?**
Cristian = external synchronization with a time server (gateway = UTC source): `t3 + RTT/2`; we take the
minimum-RTT of 4 samples because it assumes symmetric delay (first sample on Windows ~250 ms of connection
set-up — a real asymmetry). Berkeley = internal synchronization: the elected leader polls all clocks, averages
(ignoring outliers) and sends each node its adjustment; clocks agree with each other, not necessarily with UTC.

**Q5. What does NTP add?**
Four timestamps T1, t2, t3, T4 → offset θ = ((t2−T1)+(t3−T4))/2 and delay δ = (T4−T1)−(t3−t2); reported with each
Cristian sync.

**Q6. Explain the causal broadcast.**
Birman–Schiper–Stephenson: VT[k] counts delivered broadcasts from k. A message from j is delivered only if it is
j's next message and the receiver has everything j had seen; otherwise buffered (`causal.py`). Demo: Pune→Nashik
slowed 3 s, Mumbai's ACK arrives first, Nashik buffers it.

**Q7. Explain Chandy–Lamport and why FIFO matters.**
Initiator records its state and sends markers on all channels; on the first marker a node records its state,
marks that channel empty and forwards markers; later markers close the other channels, whose recorded messages
are the in-transit state. FIFO guarantees nothing sent *after* a marker is recorded as in transit before it.
Our FIFO is one sender queue per peer (`transport.py`). Proof of consistency: 30 vehicles = local + in transit.

**Q8. Bully vs Ring?**
Bully: ELECTION to higher ids, OK stops lower ones, the highest live node sends COORDINATOR; O(n²) messages worst
case; needs timeouts. Ring: one message circulates collecting live ids, then COORDINATOR circulates; 2n messages;
dead successors are skipped. Measured: Bully from Pune 7–10 messages, Ring 6.

**Q9. What triggers an election in your system?**
Start-up, a leader whose beacons stop (`beacon.py` → `election.on_peer_suspected`), and a recovering node
(`on_recover`) — the Bully rule that a recovered higher node takes over.

**Q10. Compare the three mutual exclusion algorithms.**
Lamport: request queue + REPLY + RELEASE, 3(N−1). Ricart–Agrawala merges RELEASE into deferred REPLY, 2(N−1).
Lodha–Kshemkalyani: concurrent REQUESTs double as replies and one FLUSH replaces several REPLYs → N−1 … 2(N−1).
Measured with 3 concurrent requests: 18 / 12 / 8. All fair (timestamp order).

**Q11. How do you prove mutual exclusion holds?**
The CS is a read-modify-write of a shared DFS file. With *NONE*, overlapping sections lose updates (register has
fewer lines than executions). With the algorithms: 0 overlapping intervals and 0 lost updates in every run.

**Q12. Explain Lodha–Kshemkalyani in detail (self-study).**
State: RV vector, LRQ of concurrent requests, sequence numbers. Request: SN = highest+1, LRQ = [own], REQUEST to
all, RV[self]=1. On REQUEST while requesting and RV[j]=0 → insert into LRQ, RV[j]=1 (the request is a reply);
RV[j]=1 → defer. Not requesting → REPLY(last satisfied). On REPLY/FLUSH(Rj) → RV[j]=1 and drop requests with
priority ≥ Rj. Enter when all RV = 1 and own request heads LRQ. Exit: FLUSH to the next in LRQ, REPLY to deferred.
Cost per CS: 2N−|CSet| or 2N−1−|CSet| or 2(N−1). Paper: IEEE TPDS 2000, Fig. 2.

**Q13. Knapp's classification (self-study)?**
Path-pushing (Obermarck), edge-chasing (Chandy–Misra–Haas probes), diffusing computation (CMH OR model), global
state detection (snapshot + cycle search, e.g. Bracha–Toueg). We implemented edge-chasing and global-state on a
real circular wait between the three nodes.

**Q14. Can edge-chasing report a false deadlock?**
CMH (AND model) does not report phantom deadlocks if a process stays blocked until granted — a cycle of blocked
processes is permanent. In our demo nodes are blocked until granted, and after the victim aborts the next probe
is discarded by a non-blocked node.

**Q15. What is the beacon protocol here?**
Periodic out-of-band beacons with sequence number and clock reading: liveness (suspect after 3.5 s) and offset
estimation (sender→receiver, and RBS receiver–receiver which cancels the sender's delay). The syllabus term is
ambiguous; we implemented both meanings and documented that.

**Q16. Distributed objects — what is a proxy and a skeleton?**
The proxy (`RemoteProxy`) is the client-side stand-in: it marshals the call into an `Invoke` RPC. The skeleton /
object adapter on the node unmarshals, checks the method is in the interface, calls the servant, marshals the
result. Object references (`fa2obj://node/interface/id`) give location; the naming service maps names to them.

**Q17. Pass-by-reference demo?**
`TrafficSignal.sync_with(ref)` receives another object's reference; the *hosting node* creates a proxy and invokes
`get_state` on that remote object itself — object-to-object communication between nodes.

**Q18. How is your DFS like HDFS/GFS?**
Master (gateway) holds metadata only; chunk servers (nodes) hold 256 B chunks with SHA-256; replication factor 2;
reads fail over to another replica on node failure or checksum mismatch; repair re-replicates. Difference: tiny
chunks, RF 2, no master HA.

**Q19. What makes your FaaS "serverless"?**
Stateless handlers, no server management by the function author, instances created on demand (cold start = new
process, measured), reused while warm (same PID), destroyed when idle (scale to zero), event triggers, per-ms
metering. It is a local emulation of the model, not a cloud service.

**Q20. Is your ledger a blockchain? Is it secure?**
It is a hash-chained, replicated, append-only ledger with a toy proof-of-work; tampering with one replica is
detected and repaired from the majority. Ordering is by the elected leader with majority commit — crash-fault
tolerant like a permissioned ledger, **not** Byzantine-fault tolerant and not a public blockchain.

**Q21. What happens if the leader fails while committing a block?**
A block stored on a majority is committed; a new leader (Bully) first adopts the longest valid chain among its
peers; a replica that was offline catches up on the next append (test: Nashik catches up after recovery).

**Q22. Web-based systems — what does the 304 show?**
HTTP validators: the server tags the representation with an ETag; a cache revalidates with If-None-Match and gets
304 without a body — the mechanism behind browser, proxy and CDN caching.

**Q23. Case studies in one line each?**
Megaport: software-defined private interconnect (VXCs, Cloud Router). Cloudflare: anycast edge with caching and
Workers. AWS: regions/AZs, replicated services, Lambda. Hadoop: HDFS NameNode/DataNodes + MapReduce/YARN.
Kubernetes: control plane on etcd (Raft), reconciliation, leader election, Services.

**Q24. What is simulated?**
Clock skew/drift (one host has one OS clock), injected channel delays, node failure via refuse-all mode, the
patrol-vehicle state and the FA-2 lock table. All algorithm message exchanges, file storage, processes and
hashes are real. See `FA2_IMPLEMENTATION.md` §4.

**Q25. Limitations?**
Mutex and snapshot assume reliable processes (demo refuses with a node down); gateway is a single point of
failure for DFS metadata and the ledger service; clock steps instead of slewing; no BFT.
