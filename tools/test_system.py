"""End-to-end check of the running system (start it first with `python run.py`).

    python tools/test_system.py

Exercises: REST API, direct gRPC calls to a node, P2P unit matching, broker
events, the WebSocket stream, node failure -> redirection, recovery and
store-and-forward delivery of missed broadcasts.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import grpc
import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.rpc import traffic_pb2 as pb, traffic_pb2_grpc  # noqa: E402

BASE = "http://localhost:8000"
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


async def ws_collect(seconds: float, ready: asyncio.Event) -> list[dict]:
    """Collect WebSocket frames for a while (proves the live stream works)."""
    frames: list[dict] = []
    async with websockets.connect("ws://127.0.0.1:8000/ws") as ws:
        frames.append(json.loads(await ws.recv()))      # hello
        ready.set()
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                frames.append(json.loads(raw))
            except asyncio.TimeoutError:
                break
    return frames


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        print("\n== reset demo data ==")
        r = await c.post("/api/reset")
        check("POST /api/reset", r.status_code == 200 and len(r.json()["reset"]) == 3, str(r.json()))
        await asyncio.sleep(0.5)

        print("\n== nodes / registry ==")
        r = await c.get("/api/nodes")
        nodes = {n["node_id"]: n for n in r.json()["nodes"]}
        check("GET /api/nodes returns 3 nodes", len(nodes) == 3)
        check("all nodes ONLINE", all(n["status"] == "ONLINE" for n in nodes.values()),
              {k: v["status"] for k, v in nodes.items()})
        check("node id separate from address",
              nodes["PUNE-TR-01"]["address"] == "localhost:5001" and nodes["PUNE-TR-01"]["db_path"] == "pune.db")
        r = await c.get("/api/nodes/MUM-TR-01")
        check("GET /api/nodes/{id} has incidents + peers", r.status_code == 200 and r.json()["peers"], r.json()["peers"])

        print("\n== incidents (seed) ==")
        r = await c.get("/api/incidents")
        incs = {i["incident_id"]: i for i in r.json()["incidents"]}
        check("seed incidents INC-1001..1003 present", {"INC-1001", "INC-1002", "INC-1003"} <= set(incs))
        check("owner copy wins over broadcast copy", incs["INC-1001"]["origin"] == "LOCAL")

        print("\n== direct gRPC call to Mumbai node (RPC layer) ==")
        async with grpc.aio.insecure_channel("localhost:5002") as ch:
            stub = traffic_pb2_grpc.TrafficNodeStub(ch)
            st = await stub.GetNodeStatus(pb.Empty())
            check("gRPC GetNodeStatus(MUM-TR-01)", st.node_id == "MUM-TR-01" and st.total_units == 3,
                  f"units={st.available_units}/{st.total_units}")
            res = await stub.SearchResponseUnits(pb.UnitSearchRequest(
                requesting_node="TEST", incident_id="INC-TEST", severity="High", units_needed=2))
            check("gRPC SearchResponseUnits offers MT-03", [u.unit_id for u in res.units] == ["MT-03"])
            inc = await stub.GetIncidentStatus(pb.IncidentId(incident_id="INC-1002"))
            check("gRPC GetIncidentStatus(INC-1002)", inc.status == "UNITS_ASSIGNED")

        print("\n== report incident in Pune -> local + P2P (Mumbai) ==")
        start_id = max([e["id"] for e in (await c.get("/api/events?limit=5")).json()["events"]] or [0])
        ready = asyncio.Event()
        ws_task = asyncio.create_task(ws_collect(4.0, ready))
        await asyncio.wait_for(ready.wait(), timeout=10)
        r = await c.post("/api/incidents", json={
            "type": "Accident", "location": "Pune - Katraj Bypass km 3", "severity": "High",
            "description": "test multi-vehicle accident", "detected_by": "PUNE-TR-01"})
        body = r.json()
        inc = body["incident"]
        check("POST /api/incidents -> 201", r.status_code == 201, str(body)[:200])
        check("incident id allocated INC-1004", inc["incident_id"] == "INC-1004", inc["incident_id"])
        units = {u["unit_id"]: u["node_id"] for u in inc["assigned_units"]}
        check("2 units assigned (PT-02 local + MT-03 via P2P)", units == {"PT-02": "PUNE-TR-01", "MT-03": "MUM-TR-01"}, str(units))
        check("status UNITS_ASSIGNED", inc["status"] == "UNITS_ASSIGNED")
        check("trace mentions P2P", any("P2P" in t for t in body["trace"]), body["trace"][-1])
        frames = await ws_task
        kinds = [f["kind"] for f in frames]
        types = [f["event"]["type"] for f in frames if f["kind"] == "event"]
        check("WebSocket delivered hello + events", "hello" in kinds and "event" in kinds, str(set(kinds)))
        check("WS events include RPC_CALL/INCIDENT_CREATED/RESPONSE_AVAILABLE/INCIDENT_UPDATED",
              {"RPC_CALL", "INCIDENT_CREATED", "RESPONSE_AVAILABLE", "INCIDENT_UPDATED"} <= set(types), str(types))

        print("\n== broker event log ==")
        r = await c.get("/api/events?limit=50")
        evs = r.json()["events"]
        p2p = [e for e in evs if e["channel"] == "P2P" and e.get("payload", {}).get("incident_id") == "INC-1004"]
        check("P2P PUNE->MUMBAI request + MUMBAI->PUNE response logged",
              any(e["type"] == "RESPONSE_REQUESTED" and e["source"] == "PUNE-TR-01" and e["target"] == "MUM-TR-01" for e in p2p)
              and any(e["type"] == "RESPONSE_AVAILABLE" and e["payload"].get("from") == "MUM-TR-01" for e in p2p))
        check("broadcast delivered to other nodes",
              sum(1 for e in evs if e["id"] > start_id and e["type"] == "INCIDENT_BROADCAST"
                  and "INC-1004" in e["summary"]) == 2)
        r = await c.get("/api/events?type=P2P")
        check("GET /api/events?type= filter", r.status_code == 200)

        print("\n== broadcast copy stored on other nodes ==")
        async with grpc.aio.insecure_channel("localhost:5003") as ch:
            stub = traffic_pb2_grpc.TrafficNodeStub(ch)
            copy = await stub.GetIncidentStatus(pb.IncidentId(incident_id="INC-1004"))
            check("Nashik holds REMOTE copy of INC-1004", copy.origin == "REMOTE")

        print("\n== traffic stream ==")
        r = await c.get("/api/traffic")
        tr = r.json()["traffic"]
        check("GET /api/traffic has 3 regions with segments", len(tr) == 3 and all(len(t["segments"]) == 3 for t in tr))
        d1 = {t["node_id"]: t["average_density"] for t in tr}
        await asyncio.sleep(3.5)
        r = await c.get("/api/traffic")
        d2 = {t["node_id"]: t["average_density"] for t in r.json()["traffic"]}
        check("traffic density changes over time (simulated sensors)", d1 != d2, f"{d1} -> {d2}")

        print("\n== fault tolerance: fail Pune ==")
        r = await c.post("/api/nodes/PUNE-TR-01/fail")
        check("POST /fail marks Pune OFFLINE", r.json()["status"] == "OFFLINE")
        try:
            async with grpc.aio.insecure_channel("localhost:5001") as ch:
                await traffic_pb2_grpc.TrafficNodeStub(ch).GetNodeStatus(pb.Empty(), timeout=2)
            check("failed node refuses RPC", False)
        except grpc.aio.AioRpcError as exc:
            check("failed node refuses RPC with UNAVAILABLE", exc.code() == grpc.StatusCode.UNAVAILABLE, exc.details())

        r = await c.post("/api/incidents", json={
            "type": "Road Blockage", "location": "Pune - Chandni Chowk", "severity": "High",
            "description": "landslide debris on carriageway", "detected_by": "PUNE-TR-01"})
        body = r.json()
        inc = body["incident"]
        check("incident for Pune redirected", body["redirected"] and body["served_by"] == "MUM-TR-01", str(body.get("served_by")))
        check("handled_by=MUM-TR-01, detected_by=PUNE-TR-01",
              inc["handled_by"] == "MUM-TR-01" and inc["detected_by"] == "PUNE-TR-01")
        units = {u["unit_id"]: u["node_id"] for u in inc["assigned_units"]}
        check("Mumbai (no free units) borrowed NT-01+NT-02 from Nashik via P2P",
              units == {"NT-01": "NSK-TR-01", "NT-02": "NSK-TR-01"}, str(units))
        check("trace shows Pune UNAVAILABLE at P2P layer", any("PUNE-TR-01: UNAVAILABLE" in t for t in body["trace"]))
        r = await c.get("/api/events?limit=60")
        evs = r.json()["events"]
        check("REQUEST_REDIRECTED event published", any(e["type"] == "REQUEST_REDIRECTED" for e in evs))
        check("NODE_FAILED event published", any(e["type"] == "NODE_FAILED" and e["target"] == "PUNE-TR-01" for e in evs))
        check("broadcast to Pune queued", any("queued" in e["summary"] and e["target"] == "PUNE-TR-01" for e in evs))

        print("\n== recovery ==")
        r = await c.post("/api/nodes/PUNE-TR-01/recover")
        check("POST /recover marks Pune ONLINE", r.json()["status"] == "ONLINE")
        await asyncio.sleep(1.0)
        r = await c.get("/api/events?limit=30")
        evs = r.json()["events"]
        check("NODE_RECOVERED published", any(e["type"] == "NODE_RECOVERED" for e in evs))
        check("queued broadcast delivered after recovery", any("queued broadcast" in e["summary"] for e in evs))
        async with grpc.aio.insecure_channel("localhost:5001") as ch:
            stub = traffic_pb2_grpc.TrafficNodeStub(ch)
            copy = await stub.GetIncidentStatus(pb.IncidentId(incident_id="INC-1005"))
            check("Pune now has REMOTE copy of INC-1005 (store-and-forward)", copy.origin == "REMOTE")

        print("\n== resolve incident releases units (local + P2P release) ==")
        r = await c.patch("/api/incidents/INC-1004", json={"status": "RESOLVED"})
        check("PATCH /api/incidents/INC-1004 RESOLVED", r.status_code == 200 and r.json()["status"] == "RESOLVED")
        r = await c.get("/api/nodes")
        nodes = {n["node_id"]: n for n in r.json()["nodes"]}
        await asyncio.sleep(2.5)   # wait for next heartbeat snapshot
        r = await c.get("/api/nodes")
        nodes = {n["node_id"]: n for n in r.json()["nodes"]}
        check("PT-02 and MT-03 released", nodes["PUNE-TR-01"]["available_units"] == 1 and nodes["MUM-TR-01"]["available_units"] == 1,
              {k: v["available_units"] for k, v in nodes.items()})

        print("\n== validation ==")
        r = await c.post("/api/incidents", json={"type": "Earthquake", "location": "x", "severity": "High", "detected_by": "PUNE-TR-01"})
        check("invalid incident type rejected (422)", r.status_code == 422)
        r = await c.get("/api/nodes/NOPE")
        check("unknown node -> 404", r.status_code == 404)

        print("\n== clean up: reset ==")
        await c.post("/api/reset")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED:", FAILED)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
