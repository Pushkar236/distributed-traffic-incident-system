"""End-to-end check of the FA-2 features (start the system first with `python run.py`).

    python tools/test_fa2.py

Covers Unit III (clock sync, Lamport/vector clocks, causal order, election, beacons,
mutual exclusion x4, global snapshot, deadlock detection) and Unit IV (distributed
objects, DFS, FaaS, ledger, web caching), including node failure / recovery.
FA-1 is checked separately by tools/test_system.py - run both.
"""
from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
PASSED: list[str] = []
FAILED: list[str] = []
NODES = ["PUNE-TR-01", "MUM-TR-01", "NSK-TR-01"]


def check(name: str, cond: bool, detail="") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail != "" else ""))


def wait_until(c: httpx.Client, predicate, timeout=15.0, step=0.5):
    end = time.time() + timeout
    state = None
    while time.time() < end:
        state = c.get("/api/fa2/state").json()
        if predicate(state):
            return state, True
        time.sleep(step)
    return state, False


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=90)
    for n in NODES:                                     # start from a healthy system
        c.post(f"/api/nodes/{n}/recover")
    c.post("/api/fa2/reset")

    print("\n== FA-2 runtime on every node ==")
    state, ok = wait_until(c, lambda s: s["leader"] and all(s["nodes"][n].get("available") for n in NODES))
    check("all 3 nodes expose the SyncNode service", ok)
    check("a leader is elected automatically (Bully at start-up)", state["leader"] == "NSK-TR-01", state["leader"])
    check("every node agrees on the leader", {state["nodes"][n]["election"]["leader"] for n in NODES} == {"NSK-TR-01"})

    print("\n== clock synchronization ==")
    r = c.post("/api/fa2/clock/reskew", json={"randomize": False}).json()
    check("nodes start with skewed clocks (> 1 s apart)", r["max_skew_ms"] > 1000, f"{r['max_skew_ms']} ms")
    r = c.post("/api/fa2/clock/cristian", json={}).json()
    errors = {n: abs(v["error_after_ms"]) for n, v in r["results"].items()}
    check("Cristian: every node within 50 ms of the time server", all(e < 50 for e in errors.values()), errors)
    check("Cristian reports NTP offset/delay too", all("ntp_offset_ms" in v for v in r["results"].values()))
    c.post("/api/fa2/clock/reskew", json={"randomize": True})
    r = c.post("/api/fa2/clock/berkeley", json={}).json()
    check("Berkeley run by the elected leader (time daemon)", r["daemon"] == state["leader"])
    check("Berkeley: clocks agree within 50 ms of each other", r["max_skew_ms"] < 50, f"{r['max_skew_ms']} ms")

    print("\n== beacon protocol ==")
    s = c.get("/api/fa2/state").json()
    beacons = s["nodes"]["PUNE-TR-01"]["beacon"]["peers"]
    check("beacons received from both peers", all(v["received"] > 0 and v["status"] == "ALIVE" for v in beacons.values()),
          {p: v["status"] for p, v in beacons.items()})
    check("beacon offset estimates available (sender-receiver + RBS)",
          all(v["offset_ms"] is not None for v in beacons.values()))

    print("\n== logical clocks: Lamport + vector over real message traffic ==")
    inc = c.post("/api/incidents", json={"type": "Accident", "location": "Pune - FA2 test", "severity": "Medium",
                                         "description": "fa2 test", "detected_by": "PUNE-TR-01"}).json()["incident"]
    time.sleep(1.0)
    ev = c.get("/api/fa2/events?limit=400").json()
    check("matched send/receive pairs found", ev["pairs_checked"] >= 10, ev["pairs_checked"])
    check("Lamport clock condition holds on every pair (L(send) < L(recv))", ev["lamport_violations"] == 0)
    check("vector clocks order every send before its receive", ev["vector_violations"] == 0)
    created = [e for e in ev["events"] if e["kind"] == "CB_MSG" and inc["incident_id"] in e.get("text", "")]
    check("FA-1 incident creation is a timestamped causal broadcast", len(created) >= 1 or any(
        e["kind"] == "CB_DELIVER" and inc["incident_id"] in e["text"] for e in ev["events"]))
    pune_events = [e for e in ev["events"] if e["node"] == "PUNE-TR-01"]
    a, b = pune_events[-2], pune_events[-1]
    rel = c.get("/api/fa2/events/compare", params={"a": f"PUNE-TR-01:{a['seq']}", "b": f"PUNE-TR-01:{b['seq']}"}).json()
    check("two events of one process are ordered by happened-before", rel["relation"] == "before", rel["text"])

    print("\n== causal ordering (vector timestamps) ==")
    c.post("/api/fa2/causal/demo", json={})
    s, ok = wait_until(c, lambda st: len(st["nodes"]["NSK-TR-01"]["causal"]["delivered"]) >= 2, timeout=10)
    nsk = s["nodes"]["NSK-TR-01"]["causal"]
    received = [m["topic"] for m in nsk["received"]]
    delivered = [m["topic"] for m in nsk["delivered"] if m["topic"].startswith("DEMO")]
    check("Nashik RECEIVED the reply before the alert (slow link)", received[:2] == ["DEMO_ACK", "DEMO_ALERT"], received)
    check("Nashik DELIVERED alert before reply (buffered until causal)", delivered == ["DEMO_ALERT", "DEMO_ACK"], delivered)
    check("the reply was delivered from the buffer", any(m["after_buffering"] for m in nsk["delivered"]))

    print("\n== election: Bully and Ring ==")
    r = c.post("/api/fa2/election", json={"algorithm": "BULLY", "initiator": "PUNE-TR-01"}).json()
    check("Bully from Pune elects Nashik (highest priority)", r["leader"] == "NSK-TR-01" and r["agreed"], r["leaders"])
    check("Bully exchanged ELECTION/OK/COORDINATOR messages", r["messages"] >= 5, r["messages"])
    r = c.post("/api/fa2/election", json={"algorithm": "RING", "initiator": "MUM-TR-01"}).json()
    check("Ring from Mumbai elects Nashik", r["leader"] == "NSK-TR-01" and r["agreed"], r["leaders"])
    check("Ring message carried all 3 participants",
          any("['MUM-TR-01', 'NSK-TR-01', 'PUNE-TR-01']" in t["text"] for t in r["trace"]))

    print("\n== mutual exclusion: 3 concurrent requests per algorithm ==")
    expected_msgs = {"LAMPORT": 18, "RICART_AGRAWALA": 12, "LODHA_KSHEMKALYANI": 8}
    for algo, msgs in expected_msgs.items():
        r = c.post("/api/fa2/mutex/run", json={"algorithm": algo}).json()
        check(f"{algo}: 3 CS executions, no overlap, no lost update",
              r["completed"] == 3 and r["mutual_exclusion_held"] and r["lost_updates"] == 0,
              f"overlaps={r['overlapping_sections']} lost={r['lost_updates']}")
        check(f"{algo}: {msgs} messages for 3 concurrent requests", r["total_messages"] == msgs, r["messages"])
        check(f"{algo}: granted in request-priority (timestamp) order", r["granted_in_priority_order"])
    r = c.post("/api/fa2/mutex/run", json={"algorithm": "NONE"}).json()
    check("NONE (no algorithm): critical sections overlap and updates are lost",
          r["overlapping_sections"] > 0 and r["lost_updates"] > 0,
          f"overlaps={r['overlapping_sections']} lost={r['lost_updates']}")

    print("\n== global state: Chandy-Lamport snapshot ==")
    r = c.post("/api/fa2/snapshot", json={"with_traffic": True}).json()
    check("snapshot completed on all nodes", r["complete"], r["sid"])
    check("recorded state is consistent: local + in-transit == 30",
          r["consistent"], f"{r['recorded_local_total']} + {r['recorded_in_transit']} = {r['recorded_total']}")
    check("no vehicles lost once traffic settles", r["settled_total"] == 30, r["settled_total"])

    print("\n== deadlock detection (Knapp: edge-chasing + global state) ==")
    r = c.post("/api/fa2/deadlock/setup").json()
    check("circular wait created across 3 nodes", r["cycle"] is not None and len(r["edges"]) == 3, r["edges"])
    r = c.post("/api/fa2/deadlock/detect", json={"method": "EDGE_CHASING", "initiator": "MUM-TR-01"}).json()
    check("edge-chasing (CMH probe) detects the cycle", r["deadlock"] and r["cycle"][0] == r["cycle"][-1] == "MUM-TR-01",
          r["cycle"])
    r = c.post("/api/fa2/deadlock/detect", json={"method": "GLOBAL_STATE", "initiator": "PUNE-TR-01"}).json()
    check("global-state detection finds the cycle in a snapshot", r["deadlock"] and r["complete"], r["cycle"])
    r = c.post("/api/fa2/deadlock/resolve", json={}).json()
    check("victim abort breaks the cycle", r["cycle_after"] is None, f"victim={r['victim']}")
    r = c.post("/api/fa2/deadlock/detect", json={"method": "EDGE_CHASING", "initiator": "MUM-TR-01"}).json()
    check("after resolution the probe finds no deadlock", not r["deadlock"])
    c.post("/api/fa2/deadlock/reset")

    print("\n== distributed file system ==")
    content = "".join(f"line {i:03d} of the FA-2 DFS test file\n" for i in range(40))
    w = c.put("/api/fa2/dfs/file", json={"path": "/test/fa2.txt", "content": content}).json()
    check("file split into chunks", len(w["chunks"]) >= 5, len(w["chunks"]))
    check("every chunk stored on 2 different nodes", all(len(set(ch["replicas"])) == 2 for ch in w["chunks"]))
    nodes_used = {n for ch in w["chunks"] for n in ch["replicas"]}
    check("chunks spread over all 3 nodes", nodes_used == set(NODES), nodes_used)
    r = c.get("/api/fa2/dfs/file", params={"path": "/test/fa2.txt"}).json()
    check("read reassembles the file and verifies SHA-256", r["content"] == content and r["verified"])
    c.post("/api/fa2/dfs/corrupt", json={"path": "/test/fa2.txt", "chunk_index": 0})
    r = c.get("/api/fa2/dfs/file", params={"path": "/test/fa2.txt"}).json()
    check("corrupted replica detected and skipped", r["content"] == content and
          r["chunks"][0]["skipped"] and r["chunks"][0]["skipped"][0]["reason"] == "checksum mismatch")
    rep = c.post("/api/fa2/dfs/repair").json()
    check("repair re-replicates the corrupted chunk", len(rep["actions"]) >= 1, rep["actions"])
    rpt = c.post(f"/api/fa2/dfs/report/{inc['incident_id']}").json()
    check("FaaS report for an incident stored in the DFS", rpt["stored"]["path"] == f"/reports/{inc['incident_id']}.txt")

    print("\n== distributed objects ==")
    objs = c.get("/api/fa2/objects").json()["objects"]
    check("naming service lists remote objects on all nodes", {o["node"] for o in objs} == set(NODES), len(objs))
    r = c.post("/api/fa2/objects/invoke", json={"ref": "SIG-NSK-CBS", "method": "set_phase", "args": {"phase": "RED"}}).json()
    check("remote method invocation via proxy executes on the hosting node", r["ok"] and r["host"] == "NSK-TR-01"
          and r["result"]["phase"] == "RED")
    r = c.post("/api/fa2/objects/invoke", json={"ref": "SIG-PUN-SWARGATE", "method": "sync_with",
                                                "args": {"ref": "fa2obj://NSK-TR-01/TrafficSignal/SIG-NSK-CBS"}}).json()
    check("object reference passed as parameter (object -> object call)",
          r["ok"] and r["result"]["now"]["phase"] == "RED" and r["result"]["adopted_from"]["host"] == "NSK-TR-01")
    r = c.post("/api/fa2/objects/invoke", json={"ref": "PT-02", "method": "get_status", "args": {}}).json()
    check("ResponseUnit object reads node-owned FA-1 data", r["ok"] and r["result"]["unit_id"] == "PT-02")
    r = c.post("/api/fa2/objects/invoke", json={"ref": "PT-02", "method": "reset_all", "args": {}}).json()
    check("methods outside the interface are rejected", not r["ok"])

    print("\n== serverless (FaaS) ==")
    recs = [c.post("/api/fa2/faas/invoke/congestion_advisory", json={}).json() for _ in range(3)]
    check("invocations return results", all(x["result"] and not x["error"] for x in recs))
    check("later invocations reuse a warm instance (same PID, no cold start)",
          not recs[2]["cold_start"] and recs[1]["pid"] == recs[2]["pid"])
    summary = c.get("/api/fa2/faas").json()
    classify = next(f for f in summary["functions"] if f["name"] == "classify_incident")
    check("event trigger: INCIDENT_CREATED invoked classify_incident", classify["invocations"] >= 1, classify["invocations"])

    print("\n== web caching (ETag / 304) ==")
    r1 = c.get("/api/fa2/web/feed")
    r2 = c.get("/api/fa2/web/feed", headers={"If-None-Match": r1.headers["etag"]})
    check("first request 200 with ETag + Cache-Control", r1.status_code == 200 and "max-age" in r1.headers["cache-control"])
    check("revalidation answered 304 Not Modified with empty body", r2.status_code == 304 and not r2.content)

    print("\n== ledger ==")
    r = c.post("/api/fa2/ledger/record", json={"text": "FA-2 test audit note"}).json()
    check("block mined and committed by a majority", r["commit"] and r["commit"]["committed"], r["commit"])
    check("block hash meets the proof-of-work target", r["commit"]["hash"].startswith("000"))
    v = c.post("/api/fa2/ledger/verify").json()
    check("all replicas valid and equally long", all(x["valid"] for x in v.values()) and len({x["height"] for x in v.values()}) == 1,
          {n: x["height"] for n, x in v.items()})
    t = c.post("/api/fa2/ledger/tamper", json={"node": "PUNE-TR-01", "index": 1}).json()
    check("tampering with an old block is detected", not t["valid"] and t["broken_at"] == 1, t["reason"])
    rep = c.post("/api/fa2/ledger/repair", json={"node": "PUNE-TR-01"}).json()
    check("tampered replica repaired from a valid peer", rep["valid"])

    print("\n== failure of the leader (integration with FA-1 fail/recover) ==")
    c.post("/api/nodes/NSK-TR-01/fail")
    s, ok = wait_until(c, lambda st: st["nodes"]["PUNE-TR-01"]["beacon"]["peers"]["NSK-TR-01"]["status"] == "SUSPECTED", 10)
    check("beacons stop -> peers SUSPECT Nashik", ok)
    s, ok = wait_until(c, lambda st: st["leader"] == "MUM-TR-01" and st["nodes"]["PUNE-TR-01"]["election"]["leader"] == "MUM-TR-01", 15)
    check("leader failure triggers Bully: Mumbai becomes leader", ok, s["leader"])
    r = c.get("/api/fa2/dfs/file", params={"path": "/test/fa2.txt"})
    check("DFS still reads the whole file with Nashik down (replica failover)",
          r.status_code == 200 and r.json()["content"] == content)
    r = c.post("/api/fa2/ledger/record", json={"text": "recorded while Nashik is down"}).json()
    check("ledger commits with 2 of 3 replicas under the new leader",
          r["commit"] and r["commit"]["committed"] and r["commit"]["leader"] == "MUM-TR-01", r["commit"])
    r = c.post("/api/fa2/mutex/run", json={"algorithm": "RICART_AGRAWALA"})
    check("mutex demo refuses to run with a crashed node (algorithms assume no failures)", r.status_code == 409)
    c.post("/api/nodes/NSK-TR-01/recover")
    s, ok = wait_until(c, lambda st: st["leader"] == "NSK-TR-01" and all(
        st["nodes"][n].get("election", {}).get("leader") == "NSK-TR-01" for n in NODES), 20)
    check("recovered Nashik bullies its way back to leader", ok, s["leader"])
    s, ok = wait_until(c, lambda st: len({st["nodes"][n]["ledger"]["height"] for n in NODES}) == 1
                       and all(st["nodes"][n]["ledger"]["valid"] for n in NODES), 20)
    check("Nashik's ledger replica caught up (same height, valid)", ok,
          {n: s["nodes"][n]["ledger"]["height"] for n in NODES})

    print("\n== clean up ==")
    c.delete("/api/fa2/dfs/file", params={"path": "/test/fa2.txt"})
    c.post("/api/fa2/reset")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED:", FAILED)
        sys.exit(1)


if __name__ == "__main__":
    main()
