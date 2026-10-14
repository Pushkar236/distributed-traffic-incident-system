"""FA-2 REST API (mounted by backend/main.py) and the gateway-side FA-2 services.

    /api/fa2/...        dashboard + test endpoints (state, clocks, election, mutex, snapshot,
                        deadlock, DFS, objects, FaaS, ledger, web caching)
    /internal/fa2/...   used by the node processes: time server (Cristian), DFS file API

The gateway only *triggers and observes*: every Unit III algorithm runs inside the node
processes and exchanges its own messages over the SyncNode gRPC service.
"""
import asyncio
import time
from collections import Counter

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ... import config
from ...rpc.client import NodeUnavailable
from .. import settings
from ..clocks import compare_vectors
from ..mutex import PID
from ..rpc import CommandFailed, SyncPool
from .dfs_master import DFSError, DFSMaster
from .faas import FaaSRuntime
from .ledger_service import LedgerService
from .naming import NamingService
from .web import WebCacheDemo

router = APIRouter(prefix="/api/fa2", tags=["FA-2"])
internal = APIRouter(prefix="/internal/fa2", tags=["FA-2 internal"])


class _Services:
    pool = broker = monitor = coordinator = dfs = naming = faas = ledger = web = None
    last = {}                      # last result of each demo, shown by the dashboard


S = _Services()


async def start(broker, monitor, coordinator):
    """Called once from the gateway lifespan."""
    S.pool, S.broker, S.monitor, S.coordinator = SyncPool(), broker, monitor, coordinator
    S.dfs = DFSMaster(S.pool, monitor, broker)
    S.naming = NamingService(S.pool)
    S.faas = FaaSRuntime()
    S.ledger = LedgerService(S.pool, broker)
    S.web = WebCacheDemo()
    S.faas.start()
    S.ledger.start()
    broker.subscribe(_on_broker_message)
    asyncio.create_task(S.naming.bind_all())


def stop():
    if S.faas:
        S.faas.shutdown()


# ------------------------------------------------------------------ helpers
def _check(node_id):
    if node_id not in config.NODES:
        raise HTTPException(404, f"unknown node {node_id}")


async def cmd(node, name, timeout=3.0, **args):
    try:
        return await S.pool.command(node, name, timeout=timeout, **args)
    except NodeUnavailable as exc:
        raise HTTPException(503, f"{node} unavailable ({exc.reason})")
    except CommandFailed as exc:
        raise HTTPException(409, str(exc))


async def try_cmd(node, name, timeout=3.0, **args):
    try:
        return await S.pool.command(node, name, timeout=timeout, **args)
    except (NodeUnavailable, CommandFailed):
        return None


async def all_states():
    results = await asyncio.gather(*(try_cmd(n, "state", timeout=2.0) for n in settings.NODE_IDS))
    return {n: (r or {"node_id": n, "available": False}) for n, r in zip(settings.NODE_IDS, results)}


def agreed_leader(states):
    votes = Counter(s["election"]["leader"] for s in states.values() if s.get("available") and s["election"]["leader"])
    return votes.most_common(1)[0][0] if votes else None


def online():
    return [n for n in settings.NODE_IDS if S.monitor.is_online(n)]


async def _on_broker_message(msg):
    """Event triggers: FaaS functions and DFS auto-repair (must return quickly)."""
    if msg.type == "INCIDENT_CREATED" and "incident" in msg.payload:
        asyncio.create_task(_classify(msg.payload["incident"]))
    elif msg.type == "INCIDENT_UPDATED" and msg.payload.get("status") == "RESOLVED":
        asyncio.create_task(_store_report(msg.payload["incident_id"], trigger="event"))
    elif msg.type == "NODE_RECOVERED":
        asyncio.create_task(_repair_later())


async def _classify(incident):
    rec = await S.faas.invoke("classify_incident", {"incident": incident}, trigger="event")
    if rec["result"]:
        r = rec["result"]
        await S.broker.publish(type="FAAS_RESULT", source="COORDINATOR", target="ALL", channel="FA2",
                               summary=f"classify_incident({r['incident_id']}) -> {r['priority']} score {r['score']} "
                                       f"({'cold' if rec['cold_start'] else 'warm'} start, {rec['total_ms']:.0f} ms)",
                               payload={"invocation": {k: rec[k] for k in ("function", "cold_start", "total_ms", "pid")}})


async def _store_report(incident_id, trigger="http"):
    inc = await S.coordinator.get_incident(incident_id)
    if inc is None:
        raise HTTPException(404, f"{incident_id} not found")
    rec = await S.faas.invoke("incident_report", {"incident": inc}, trigger=trigger)
    if rec["error"]:
        raise HTTPException(500, rec["error"])
    stored = await S.dfs.write(rec["result"]["path"], rec["result"]["content"], writer="faas:incident_report")
    return {"invocation": rec, "stored": stored}


async def _repair_later():
    await asyncio.sleep(3)
    try:
        await S.dfs.repair()
    except Exception:
        pass


# =================================================================== state
@router.get("/state")
async def fa2_state():
    states = await all_states()
    return {"nodes": states, "leader": agreed_leader(states), "settings": {
                "priority": settings.PRIORITY, "ring": settings.RING, "me_algorithms": settings.ME_ALGORITHMS,
                "beacon_interval": settings.BEACON_INTERVAL, "beacon_timeout": settings.BEACON_TIMEOUT,
                "chunk_size": settings.DFS_CHUNK_SIZE, "replication": settings.DFS_REPLICATION},
            "dfs": {"files": S.dfs.listing(), "stats": S.dfs.stats}, "faas": S.faas.summary(),
            "ledger": S.ledger.summary(), "web": S.web.stats, "objects": list(S.naming.registry.values()),
            "invocations": S.naming.invocations[-8:], "last": S.last}


@router.post("/reset")
async def fa2_reset():
    """Reset transient FA-2 state (locks, mutex, causal logs, transfers, clock skew). DFS/ledger are kept."""
    done = []
    for n in online():
        for name, args in (("me_reset", {}), ("dl_reset", {}), ("causal_reset_log", {}),
                           ("transfers", {"on": False}), ("clock_reskew", {"randomize": False})):
            await try_cmd(n, name, **args)
        done.append(n)
    S.last = {}
    return {"reset": done}


# ============================================================ clock sync
@internal.get("/time")
async def time_server():
    t2 = time.time()                       # request received
    return {"t2": t2, "t3": time.time()}   # reply sent


@router.get("/clock")
async def clock_offsets():
    """Measure each node's clock against the gateway reference (RTT-compensated)."""
    out = {}
    from ...rpc import fa2_sync_pb2 as spb
    for n in settings.NODE_IDS:
        t0 = time.time()
        try:
            r = await S.pool.call(n, "GetTime", spb.TimeRequest(requester="gateway"), timeout=2)
        except NodeUnavailable:
            out[n] = None
            continue
        t1 = time.time()
        out[n] = {"offset_ms": round((r.time - (t0 + t1) / 2) * 1000, 1), "rtt_ms": round((t1 - t0) * 1000, 2)}
    spread = [v["offset_ms"] for v in out.values() if v]
    return {"offsets": out, "max_skew_ms": round(max(spread) - min(spread), 1) if len(spread) > 1 else 0.0}


class ClockReq(BaseModel):
    node: str | None = None
    randomize: bool = True


@router.post("/clock/cristian")
async def clock_cristian(req: ClockReq):
    nodes = [req.node] if req.node else online()
    for n in nodes:
        _check(n)
    results = {n: await cmd(n, "clock_cristian") for n in nodes}
    S.last["clock"] = {"algorithm": "CRISTIAN", "results": results, **(await clock_offsets())}
    return S.last["clock"]


@router.post("/clock/berkeley")
async def clock_berkeley(req: ClockReq):
    daemon = req.node or agreed_leader(await all_states())
    if daemon is None:
        raise HTTPException(409, "no leader elected - run an election first")
    _check(daemon)
    result = await cmd(daemon, "clock_berkeley", timeout=8)
    await asyncio.sleep(0.4)                       # let CLOCK_ADJUST messages arrive
    S.last["clock"] = {"algorithm": "BERKELEY", "daemon": daemon, "result": result, **(await clock_offsets())}
    return S.last["clock"]


@router.post("/clock/reskew")
async def clock_reskew(req: ClockReq):
    results = {n: await cmd(n, "clock_reskew", randomize=req.randomize) for n in online()}
    return {"skew": results, **(await clock_offsets())}


# ============================================== logical clocks / causality
@router.get("/events")
async def fa2_events(limit: int = 60):
    """Merged event logs of all nodes, ordered by Lamport timestamp (ties: node id), plus
    a check of the clock conditions over every matched send/receive pair."""
    logs = await asyncio.gather(*(try_cmd(n, "events", limit=400) for n in settings.NODE_IDS))
    events = [e for log in logs if log for e in log]
    sends = {(e["node"], e["msg_id"]): e for e in events if e["type"] == "SEND"}
    pairs = lamport_bad = vector_bad = phys_bad = 0
    examples = []
    for r in (e for e in events if e["type"] == "RECV"):
        s = sends.get((r["peer"], r["msg_id"]))
        if s is None:
            continue
        pairs += 1
        lamport_bad += not (s["lamport"] < r["lamport"])
        vector_bad += compare_vectors(s["vector"], r["vector"]) != "before"
        if r["phys"] < s["phys"]:
            phys_bad += 1
            if len(examples) < 4:
                examples.append({"kind": s["kind"], "from": s["node"], "to": r["node"],
                                 "send_phys": s["phys"], "recv_phys": r["phys"],
                                 "send_lamport": s["lamport"], "recv_lamport": r["lamport"]})
    ordered = sorted(events, key=lambda e: (e["lamport"], PID[e["node"]]))
    return {"events": ordered[-limit:], "pairs_checked": pairs, "lamport_violations": lamport_bad,
            "vector_violations": vector_bad, "physical_clock_anomalies": phys_bad, "anomaly_examples": examples}


@router.get("/events/compare")
async def compare_events(a: str, b: str):
    """a, b = '<node-id>:<seq>' -> happened-before relation decided by vector clocks."""
    found = {}
    for key in (a, b):
        node, _, seq = key.rpartition(":")
        _check(node)
        evs = await cmd(node, "events", limit=500)
        match = next((e for e in evs if e["seq"] == int(seq)), None)
        if match is None:
            raise HTTPException(404, f"event {key} not in the recent log")
        found[key] = match
    rel = compare_vectors(found[a]["vector"], found[b]["vector"])
    text = {"before": f"{a} -> {b} (happened before)", "after": f"{b} -> {a} (happened before)",
            "concurrent": f"{a} || {b} (concurrent)", "equal": "same event"}[rel]
    return {"relation": rel, "text": text, "a": found[a], "b": found[b],
            "lamport_note": "Lamport timestamps alone cannot tell 'concurrent' from 'before'"}


class CausalReq(BaseModel):
    sender: str = "PUNE-TR-01"
    slow_peer: str = "NSK-TR-01"
    reply_from: str = "MUM-TR-01"


@router.post("/causal/demo")
async def causal_demo(req: CausalReq):
    for n in (req.sender, req.slow_peer, req.reply_from):
        _check(n)
    for n in online():
        await try_cmd(n, "causal_reset_log")
    started = await cmd(req.sender, "causal_demo", slow_peer=req.slow_peer, reply_from=req.reply_from)
    S.last["causal"] = {"started": started, "at": time.time(), **req.model_dump()}
    return S.last["causal"]


# ================================================================ election
class ElectionReq(BaseModel):
    algorithm: str = Field("BULLY", pattern="^(BULLY|RING)$")
    initiator: str = "PUNE-TR-01"


@router.post("/election")
async def election(req: ElectionReq):
    _check(req.initiator)
    before = await all_states()
    sent_before = sum(s["election"]["messages_sent"] for s in before.values() if s.get("available"))
    started_at = time.time()
    await cmd(req.initiator, "election_start", algorithm=req.algorithm)
    await asyncio.sleep(1.0)
    states = before
    for _ in range(30):
        states = await all_states()
        live = [s for s in states.values() if s.get("available")]
        leaders = {s["election"]["leader"] for s in live}
        if len(leaders) == 1 and not any(s["election"]["in_progress"] for s in live):
            break
        await asyncio.sleep(0.3)
    live = {n: s for n, s in states.items() if s.get("available")}
    trace = sorted(({"node": n, **t} for n, s in live.items() for t in s["election"]["trace"] if t["t"] >= started_at),
                   key=lambda t: t["t"])
    leaders = {n: s["election"]["leader"] for n, s in live.items()}
    S.last["election"] = {"algorithm": req.algorithm, "initiator": req.initiator, "leaders": leaders,
                          "leader": agreed_leader(states), "agreed": len(set(leaders.values())) == 1,
                          "messages": sum(s["election"]["messages_sent"] for s in live.values()) - sent_before,
                          "trace": trace, "at": started_at}
    return S.last["election"]


# ========================================================= mutual exclusion
class MutexRun(BaseModel):
    algorithm: str = settings.ME_DEFAULT
    nodes: list[str] = Field(default_factory=lambda: list(settings.NODE_IDS))


@router.post("/mutex/run")
async def mutex_run(req: MutexRun):
    """All chosen nodes request the critical section at the same moment."""
    if req.algorithm not in settings.ME_ALGORITHMS:
        raise HTTPException(422, f"algorithm must be one of {settings.ME_ALGORITHMS}")
    for n in req.nodes:
        _check(n)
    if req.algorithm != "NONE" and len(online()) < len(settings.NODE_IDS):
        raise HTTPException(409, "all three nodes must be ONLINE: these algorithms assume reliable processes "
                                 "(a crashed node never replies, so every request would block)")
    for n in settings.NODE_IDS:
        await cmd(n, "me_reset", algorithm=req.algorithm)
    await S.dfs.write(settings.SHARED_REGISTER, "", writer="mutex-demo")
    started = time.time()
    requests = await asyncio.gather(*(cmd(n, "me_request") for n in req.nodes))
    states = {}
    deadline = time.time() + settings.ME_REQUEST_TIMEOUT + 5
    while time.time() < deadline:
        await asyncio.sleep(0.3)
        states = {n: (await try_cmd(n, "state", timeout=2.0) or {}).get("mutex", {}) for n in req.nodes}
        if all(s.get("cs_count", 0) >= 1 and s.get("status") == "IDLE" for s in states.values()):
            break
    await asyncio.sleep(0.5)                       # last RELEASE / FLUSH messages
    finals = {n: (await try_cmd(n, "state", timeout=2.0) or {}).get("mutex", {}) for n in settings.NODE_IDS}
    sections = sorted(({"node": n, **h} for n in req.nodes for h in finals[n].get("history", [])),
                      key=lambda h: h["enter"])
    overlaps = sum(1 for a, b in zip(sections, sections[1:]) if b["enter"] < a["exit"])
    messages = Counter()
    for s in finals.values():
        messages.update(s.get("messages_sent", {}))
    register = (await S.dfs.read(settings.SHARED_REGISTER))["content"]
    lines = [ln for ln in register.splitlines() if ln.strip()]
    entry_order = [tuple(s["request"][:2]) for s in sections]
    S.last["mutex"] = {
        "algorithm": req.algorithm, "requests": dict(zip(req.nodes, requests)),
        "critical_sections": [{"node": s["node"], "request": s["request"], "enter": s["enter"], "exit": s["exit"],
                               "line": s["line"]} for s in sections],
        "completed": len(sections), "overlapping_sections": overlaps, "mutual_exclusion_held": overlaps == 0,
        "messages": dict(messages), "total_messages": sum(messages.values()),
        "messages_per_cs": round(sum(messages.values()) / len(sections), 2) if sections else None,
        "register_lines": len(lines), "expected_lines": len(req.nodes), "lost_updates": len(req.nodes) - len(lines),
        "register": register, "granted_in_priority_order": entry_order == sorted(entry_order),
        "duration_s": round(time.time() - started, 2)}
    return S.last["mutex"]


class MutexReq(BaseModel):
    node: str


@router.post("/mutex/request")
async def mutex_request(req: MutexReq):
    _check(req.node)
    return await cmd(req.node, "me_request")


# ================================================================ snapshot
def find_cycle(edges):
    graph = {}
    for e in edges:
        graph.setdefault(e["from"], []).append(e["to"])
    state = {}

    def dfs(v, path):
        state[v] = "active"
        for w in graph.get(v, []):
            if state.get(w) == "active":
                return path[path.index(w):] + [w]
            if w not in state:
                found = dfs(w, path + [w])
                if found:
                    return found
        state[v] = "done"
        return None

    for v in list(graph):
        if v not in state:
            found = dfs(v, [v])
            if found:
                return found
    return None


async def run_snapshot(initiator, with_traffic):
    if with_traffic:
        for n in online():
            await try_cmd(n, "transfers", on=True)
        await asyncio.sleep(2.5)
    naive = await asyncio.gather(*(try_cmd(n, "patrols") for n in settings.NODE_IDS))
    sid = (await cmd(initiator, "snapshot_start"))["sid"]
    results = {}
    for _ in range(40):
        await asyncio.sleep(0.3)
        for n in settings.NODE_IDS:
            if not (results.get(n) or {}).get("done"):
                results[n] = await try_cmd(n, "snapshot_get", sid=sid)
        if all(r and r["done"] for r in results.values()):
            break
    if with_traffic:
        for n in online():
            await try_cmd(n, "transfers", on=False)
    complete = all(r and r["done"] for r in results.values())
    local = sum(r["state"]["patrols"] for r in results.values() if r)
    transit = sum(sum(v) for r in results.values() if r for v in r["channels"].values())
    edges = [e for r in results.values() if r for e in r["state"]["waits_for"]]
    expected = settings.PATROLS_PER_NODE * len(settings.NODE_IDS)
    await asyncio.sleep(1.5)                        # let in-flight transfers land
    settled = await asyncio.gather(*(try_cmd(n, "patrols") for n in settings.NODE_IDS))
    return {"sid": sid, "initiator": initiator, "complete": complete, "nodes": results,
            "recorded_local_total": local, "recorded_in_transit": transit, "recorded_total": local + transit,
            "expected_total": expected, "consistent": complete and local + transit == expected,
            "naive_reading": [x and x["patrols"] for x in naive],
            "naive_total": sum(x["patrols"] for x in naive if x),
            "settled_total": sum(x["patrols"] for x in settled if x),
            "wait_for_edges": edges, "cycle": find_cycle(edges), "at": time.time()}


class SnapshotReq(BaseModel):
    initiator: str | None = None
    with_traffic: bool = True


@router.post("/snapshot")
async def snapshot(req: SnapshotReq):
    initiator = req.initiator or agreed_leader(await all_states()) or online()[0]
    _check(initiator)
    S.last["snapshot"] = await run_snapshot(initiator, req.with_traffic)
    return S.last["snapshot"]


class TransfersReq(BaseModel):
    on: bool = True


@router.post("/snapshot/transfers")
async def snapshot_transfers(req: TransfersReq):
    return {n: await try_cmd(n, "transfers", on=req.on) for n in online()}


# ================================================================ deadlock
CIRCLE = [("PUNE-TR-01", "PT-01", "MT-01"), ("MUM-TR-01", "MT-01", "NT-01"), ("NSK-TR-01", "NT-01", "PT-01")]


async def _deadlock_states():
    return {n: (await try_cmd(n, "state") or {}).get("deadlock") for n in settings.NODE_IDS}


@router.post("/deadlock/setup")
async def deadlock_setup():
    """Each node locks its own unit, then requests the next region's unit -> circular wait."""
    if len(online()) < len(settings.NODE_IDS):
        raise HTTPException(409, "all three nodes must be ONLINE for the circular-wait demo")
    for n in settings.NODE_IDS:
        await cmd(n, "dl_reset")
    for node, own, _ in CIRCLE:
        await cmd(node, "dl_acquire", unit=own)
    await asyncio.sleep(0.6)
    for node, _, wanted in CIRCLE:
        await cmd(node, "dl_acquire", unit=wanted)
    await asyncio.sleep(1.0)
    states = await _deadlock_states()
    edges = [{"from": n, "to": s["waiting"]["holder"], "unit": s["waiting"]["unit"]}
             for n, s in states.items() if s and s["waiting"]]
    S.last["deadlock"] = {"setup": states, "edges": edges, "cycle": find_cycle(edges)}
    return S.last["deadlock"]


class DetectReq(BaseModel):
    method: str = Field("EDGE_CHASING", pattern="^(EDGE_CHASING|GLOBAL_STATE)$")
    initiator: str = "PUNE-TR-01"


@router.post("/deadlock/detect")
async def deadlock_detect(req: DetectReq):
    _check(req.initiator)
    if req.method == "GLOBAL_STATE":
        snap = await run_snapshot(req.initiator, with_traffic=False)
        result = {"method": "GLOBAL_STATE", "initiator": req.initiator, "sid": snap["sid"],
                  "complete": snap["complete"], "wait_for_edges": snap["wait_for_edges"], "cycle": snap["cycle"],
                  "deadlock": snap["cycle"] is not None}
        if snap["cycle"]:
            await S.broker.publish(type="DEADLOCK_DETECTED", source="COORDINATOR", target="ALL", channel="FA2",
                                   summary=f"global-state detection on snapshot {snap['sid']}: cycle "
                                           f"{' -> '.join(snap['cycle'])}", payload={"cycle": snap["cycle"]})
    else:
        before = len(((await try_cmd(req.initiator, "state")) or {}).get("deadlock", {}).get("detections", []))
        probe = await cmd(req.initiator, "dl_detect")
        detection = None
        for _ in range(20):
            await asyncio.sleep(0.25)
            dets = ((await try_cmd(req.initiator, "state")) or {}).get("deadlock", {}).get("detections", [])
            if len(dets) > before or (dets and dets[-1].get("probe_id") == probe["probe_id"]):
                detection = dets[-1]
                break
        traces = {n: (s or {}).get("trace", [])[-6:] for n, s in (await _deadlock_states()).items()}
        result = {"method": "EDGE_CHASING", "initiator": req.initiator, "probe": probe,
                  "deadlock": detection is not None, "cycle": detection["cycle"] if detection else None,
                  "traces": traces}
    S.last.setdefault("deadlock", {})["detection"] = result
    return result


class ResolveReq(BaseModel):
    victim: str | None = None


@router.post("/deadlock/resolve")
async def deadlock_resolve(req: ResolveReq):
    victim = req.victim
    if victim is None:                            # lowest Bully priority in the cycle aborts
        cycle = (S.last.get("deadlock", {}).get("detection") or {}).get("cycle") or settings.NODE_IDS
        victim = min(set(cycle), key=lambda n: settings.PRIORITY[n])
    _check(victim)
    aborted = await cmd(victim, "dl_abort")
    await asyncio.sleep(1.0)
    states = await _deadlock_states()
    still_waiting = [n for n, s in states.items() if s and s["waiting"]]
    edges = [{"from": n, "to": s["waiting"]["holder"]} for n, s in states.items() if s and s["waiting"]]
    result = {"victim": victim, "aborted": aborted, "states": states, "still_waiting": still_waiting,
              "cycle_after": find_cycle(edges)}
    S.last.setdefault("deadlock", {})["resolution"] = result
    return result


@router.post("/deadlock/reset")
async def deadlock_reset():
    for n in online():
        await try_cmd(n, "dl_reset")
    S.last.pop("deadlock", None)
    return {"reset": online()}


# ===================================================== distributed file system
class DFSWrite(BaseModel):
    path: str = Field(min_length=2, max_length=200)
    content: str = Field(max_length=100_000)
    writer: str = "dashboard"


@router.get("/dfs")
async def dfs_listing():
    chunk_servers = {n: ((await try_cmd(n, "state")) or {}).get("dfs") for n in settings.NODE_IDS}
    return {"files": S.dfs.listing(), "stats": S.dfs.stats, "chunk_servers": chunk_servers,
            "chunk_size": settings.DFS_CHUNK_SIZE, "replication": settings.DFS_REPLICATION}


@router.get("/dfs/file")
async def dfs_read(path: str):
    try:
        return await S.dfs.read(path)
    except FileNotFoundError:
        raise HTTPException(404, f"{path} not found")
    except DFSError as exc:
        raise HTTPException(503, str(exc))


@router.put("/dfs/file")
async def dfs_write(req: DFSWrite):
    try:
        return await S.dfs.write(req.path, req.content, writer=req.writer)
    except DFSError as exc:
        raise HTTPException(503, str(exc))


@router.delete("/dfs/file")
async def dfs_delete(path: str):
    try:
        return await S.dfs.delete(path)
    except FileNotFoundError:
        raise HTTPException(404, f"{path} not found")


@router.post("/dfs/report/{incident_id}")
async def dfs_report(incident_id: str):
    return await _store_report(incident_id)


class CorruptReq(BaseModel):
    path: str
    chunk_index: int = 0
    node: str | None = None


@router.post("/dfs/corrupt")
async def dfs_corrupt(req: CorruptReq):
    meta = S.dfs.files.get(S.dfs.normalise(req.path))
    if meta is None or req.chunk_index >= len(meta["chunks"]):
        raise HTTPException(404, "no such file / chunk")
    chunk = meta["chunks"][req.chunk_index]
    node = req.node or chunk["replicas"][0]
    return await cmd(node, "dfs_corrupt", chunk_id=chunk["chunk_id"])


@router.post("/dfs/repair")
async def dfs_repair():
    return await S.dfs.repair()


@internal.get("/dfs")
async def internal_dfs_read(path: str):
    try:
        return await S.dfs.read(path)
    except FileNotFoundError:
        raise HTTPException(404, f"{path} not found")
    except DFSError as exc:
        raise HTTPException(503, str(exc))


@internal.put("/dfs")
async def internal_dfs_write(req: DFSWrite):
    try:
        return await S.dfs.write(req.path, req.content, writer=req.writer)
    except DFSError as exc:
        raise HTTPException(503, str(exc))


# ===================================================== distributed objects
@router.get("/objects")
async def objects_list():
    await S.naming.bind_all()
    return {"objects": list(S.naming.registry.values()), "invocations": S.naming.invocations[-10:]}


class InvokeReq(BaseModel):
    ref: str
    method: str
    args: dict = Field(default_factory=dict)


@router.post("/objects/invoke")
async def objects_invoke(req: InvokeReq):
    if not S.naming.registry:
        await S.naming.bind_all()
    try:
        return await S.naming.invoke(req.ref, req.method, req.args)
    except Exception as exc:                     # bad reference / unknown method
        raise HTTPException(400, str(exc))


# ================================================================ serverless
@router.get("/faas")
async def faas_summary():
    return S.faas.summary()


class FaaSInvoke(BaseModel):
    incident_id: str | None = None
    event: dict = Field(default_factory=dict)


@router.post("/faas/invoke/{name}")
async def faas_invoke(name: str, req: FaaSInvoke):
    event = dict(req.event)
    if name in ("classify_incident", "incident_report"):
        incident_id = req.incident_id or (await S.coordinator.list_incidents() or [{}])[0].get("incident_id")
        inc = await S.coordinator.get_incident(incident_id) if incident_id else None
        if inc is None:
            raise HTTPException(404, "incident not found")
        event["incident"] = inc
    elif name == "congestion_advisory":
        event["traffic"] = await S.coordinator.traffic()
    try:
        return await S.faas.invoke(name, event, trigger="http")
    except KeyError:
        raise HTTPException(404, f"no function {name}")


# =================================================================== ledger
@router.get("/ledger")
async def ledger_view(limit: int = 8):
    states = await all_states()
    leader = agreed_leader(states)
    source = leader if leader and states[leader].get("available") else next(
        (n for n, s in states.items() if s.get("available")), None)
    blocks = await try_cmd(source, "ledger_blocks", limit=limit) if source else []
    return {"replicas": {n: s.get("ledger") for n, s in states.items()}, "leader": leader, "blocks_from": source,
            "blocks": blocks or [], **S.ledger.summary()}


class LedgerNote(BaseModel):
    text: str = Field(min_length=1, max_length=160)


@router.post("/ledger/record")
async def ledger_record(req: LedgerNote):
    await S.broker.publish(type="LEDGER_NOTE", source="DASHBOARD", target="ALL", channel="FA2", summary=req.text)
    await asyncio.sleep(0.2)
    return {"queued": True, "commit": await S.ledger.flush()}


@router.post("/ledger/flush")
async def ledger_flush():
    return {"commit": await S.ledger.flush(), "pending": len(S.ledger.pending)}


@router.post("/ledger/verify")
async def ledger_verify():
    return {n: await try_cmd(n, "ledger_verify") for n in settings.NODE_IDS}


class TamperReq(BaseModel):
    node: str = "NSK-TR-01"
    index: int = 1


@router.post("/ledger/tamper")
async def ledger_tamper(req: TamperReq):
    _check(req.node)
    return await cmd(req.node, "ledger_tamper", index=req.index)


class RepairReq(BaseModel):
    node: str = "NSK-TR-01"


@router.post("/ledger/repair")
async def ledger_repair(req: RepairReq):
    _check(req.node)
    await cmd(req.node, "ledger_repair")
    await asyncio.sleep(1.0)
    return await cmd(req.node, "ledger_verify")


# ===================================================================== web
@router.get("/web/feed")
async def web_feed(request: Request):
    incidents = await S.coordinator.list_incidents()
    feed = {"feed": "regional-traffic-incidents", "incidents": [
        {k: i[k] for k in ("incident_id", "type", "severity", "status", "location", "handled_by")}
        for i in incidents if i["status"] != "RESOLVED"]}
    status, etag, body = S.web.respond(feed, request.headers.get("if-none-match"))
    headers = {"ETag": etag, "Cache-Control": "public, max-age=5"}
    return Response(content=body, status_code=status, headers=headers,
                    media_type="application/json" if status == 200 else None)


@router.get("/web/stats")
async def web_stats():
    return S.web.stats
