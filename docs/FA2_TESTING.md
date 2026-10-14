# FA-2 Testing

## 1. Automated tests

Start the system, then run **both** suites (FA-1 must keep passing):

```bash
python run.py                    # terminal 1  (use --reset for fresh demo data)
python tools/test_system.py      # terminal 2: FA-1 regression, 40 assertions
python tools/test_fa2.py         # terminal 2: FA-2, 69 assertions
```

Both suites change demo data (FA-1's suite calls `/api/reset`; FA-2's creates one incident and a test file).
During development they were run against a copy of the databases:
`DATA_DIR=<scratch>/data python run.py` (the `DATA_DIR` setting already existed in FA-1).

### Results (2026-10-14, Windows 11, Python 3.11.6)

| Suite | Result |
|---|---|
| FA-1 baseline before any FA-2 change | 40 passed, 0 failed |
| `tools/test_fa2.py` | **69 passed, 0 failed** |
| FA-1 `tools/test_system.py` with FA-2 attached | 40 passed (see §3 for one pre-existing timing-dependent check) |
| Dashboard in headless Chromium: every FA-2 button, form and link | 0 console errors; FA-1 Fail/Recover buttons still work |

### What `tools/test_fa2.py` asserts

| Area | Assertions |
|---|---|
| Runtime | SyncNode on all 3 nodes; start-up election elects Nashik; all agree |
| Clock sync | skew > 1 s before; Cristian error < 50 ms (measured < 1 ms); NTP fields; Berkeley by the leader, spread < 50 ms (measured ~1 ms) |
| Beacons | both peers ALIVE with beacons received; offset estimates present |
| Logical clocks | ≥ 10 matched send/receive pairs; **0** Lamport and **0** vector violations; FA-1 incident is a causal broadcast; happened-before within one process |
| Causal order | Nashik receives ACK before ALERT, delivers ALERT before ACK, ACK came from the buffer |
| Election | Bully from Pune → Nashik, ≥ 5 messages; Ring from Mumbai → Nashik with all 3 participants |
| Mutual exclusion | Lamport 18, RA 12, LK 8 messages for 3 concurrent requests; each: 3 CS, 0 overlaps, 0 lost updates, priority order; NONE: overlaps and lost updates |
| Snapshot | complete; local + in-transit = 30; 30 after settling |
| Deadlock | 3-node cycle created; edge-chasing and global-state both detect it; victim abort breaks it; new probe finds none |
| DFS | ≥ 5 chunks, 2 distinct replicas each, spread over 3 nodes; SHA-256 verified read; corrupted replica skipped; repair re-replicates; FaaS report stored |
| Objects | naming service lists objects on 3 nodes; remote invocation executes on host; reference as parameter; ResponseUnit reads FA-1 data; non-interface method rejected |
| FaaS | results returned; warm reuse (same PID); INCIDENT_CREATED triggered classify_incident |
| Web | 200 + ETag + Cache-Control; If-None-Match → 304 with empty body |
| Ledger | block committed by majority; PoW prefix; all replicas valid and equal; tamper detected at block 1; repair |
| Failure integration | fail Nashik → SUSPECTED by beacons → Mumbai elected → DFS read still complete → ledger commits 2/3 under Mumbai → mutex demo refused (409) → recover → Nashik leader again → its ledger caught up |

## 2. Manual test script (dashboard)

1. `python run.py`, open `http://localhost:8000`, click **FA-2 ↓**. *FA-2 Node State* shows a leader
   (Nashik), Lamport/vector clocks increasing and beacons ALIVE.
2. **Clock Synchronization** → *Cristian* (errors drop to ~0 ms) → *Re-skew* → *Berkeley* (spread ~1 ms).
3. **Logical clocks** — note *Lamport violations 0*, *vector violations 0* and the red physical-time anomaly
   count. Click two event ids and *Compare*.
4. *Run causal-order demo* — after ~3 s Nashik's receive order (ACK, ALERT) differs from its delivery order
   (ALERT, ACK ← from buffer).
5. **Leader Election** — *Start Bully* from Pune, *Start Ring* from Mumbai; read the traces.
6. **Fault tolerance (FA-1 panel)** — *Fail Nashik Node*: beacons → SUSPECTED, trace shows the Bully election,
   leader pill → Mumbai. *Recover Nashik*: Nashik bullies back.
7. **Mutual exclusion** — run *Lodha–Kshemkalyani*, *Ricart–Agrawala*, *Lamport*, *NONE*; compare the
   *Runs this session* table (8 / 12 / 18 / lost updates) and the register file.
8. **Global state** — *Take snapshot*: green "21 + 9 = 30" vs red naive reading.
9. **Deadlock** — *Create circular wait* (red triangle) → *Detect: edge-chasing* → *Detect: global state* →
   *Resolve* → *Reset locks*.
10. **Unit IV** — invoke `SIG-PUN-SWARGATE.sync_with` with Mumbai's reference; generate a report for an
    incident and *read* it (chunks served by different nodes); *corrupt #0* then *read* (failover) and *Repair*;
    invoke a function twice (cold then warm PID); *Tamper Nashik block #1* → *Verify* → *Repair Nashik*;
    *GET feed* then *Revalidate* (304).

## 3. Regression notes

* **FA-1 timing-dependent check (pre-existing, not caused by FA-2).** In one of four FA-1 runs,
  *"traffic density changes over time"* failed. The node sensors tick every 3 s while the gateway's heartbeat
  samples every ~2 s, so the gateway sees changes alternately ~2 s and ~4.1 s apart; the test waits 3.5 s, so it
  fails whenever it starts just after a change in the 4.1 s gap (≈ 8 % of runs).
  Measured on the **unmodified FA-1 code** (`git archive HEAD`, same machine): gaps
  `[3.8, 2.1, 4.1, 2.0, 3.8, 2.1, 4.0, 2.1, 4.0]` s, max 4.1 s — the same pattern as with FA-2
  (`[1.3, 4.1, 1.8, 4.1, 2.0, 4.1, …]`). The assertion was **not** changed (rule: never weaken tests).
  Re-running the suite passes; if you want it deterministic, the FA-1 test's 3.5 s wait could be raised
  to ≥ 4.5 s — left for you to decide.
* FA-1 APIs, WebSocket frames and event types are unchanged; FA-2 events use new types and channel `FA2`,
  so FA-1's assertions on event counts (e.g. two `INCIDENT_BROADCAST` per incident) are unaffected.
* FA-1 databases: FA-2 adds no table to `pune.db` / `mumbai.db` / `nashik.db`; the ledger uses new files
  `*_ledger.db`, the DFS uses `data/dfs/` and `data/fa2_dfs_master.json` (all ignored by `.gitignore`;
  two ignore lines were appended for the DFS paths).

## 4. Environment notes

* Windows: `localhost` connections from Python may first try IPv6 and pay ~250 ms on a new connection; tests use
  `127.0.0.1`, and Cristian takes the minimum-RTT sample for the same reason.
* Docker: the FA-2 code is copied by the existing Dockerfiles (`COPY backend`, `COPY frontend`, `COPY proto`); no new
  dependency. Docker was not run here (daemon not available), the compose file is unchanged.
