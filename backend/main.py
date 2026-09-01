"""API gateway: REST endpoints + WebSocket stream + static dashboard.

    python -m uvicorn backend.main:app --port 8000

Layers:  dashboard  ->  this file  ->  Coordinator  ->  gRPC  ->  node processes  ->  SQLite
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .communication.messaging import MessageBroker
from .communication.websocket import WebSocketHub
from .coordinator import AllNodesUnavailable, Coordinator
from .models.schemas import EventIn, IncidentCreate, IncidentStatusPatch
from .rpc.client import NodeUnavailable, StubPool
from .services.fault_tolerance import HealthMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [gateway] %(message)s", datefmt="%H:%M:%S")

# the building blocks of the gateway process
broker = MessageBroker()
pool = StubPool()
monitor = HealthMonitor(pool, broker)
hub = WebSocketHub()
coordinator = Coordinator(broker, pool, monitor)


async def snapshot_stream():
    """Every 2 s push node health + incident list to all dashboards (stream-oriented)."""
    while True:
        if hub.clients:
            await hub.broadcast({"kind": "snapshot", "nodes": monitor.snapshot(),
                                 "incidents": await coordinator.list_incidents()})
        await asyncio.sleep(config.HEARTBEAT_INTERVAL)


@asynccontextmanager
async def lifespan(app):
    broker.subscribe(hub.on_broker_message)       # every message -> dashboards
    broker.start()
    for node_id in config.NODES:                  # first heartbeat before serving
        await monitor.check(node_id)
    monitor.start()
    await coordinator.start()
    asyncio.create_task(snapshot_stream())
    logging.info("gateway ready on http://localhost:%d", config.GATEWAY_PORT)
    yield
    await pool.close()


app = FastAPI(title="Distributed Traffic Incident Coordination System", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")


def check_node(node_id):
    if node_id not in config.NODES:
        raise HTTPException(404, f"unknown node {node_id}")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(config.FRONTEND_DIR / "index.html")


# ------------------------------------------------------------------ nodes
@app.get("/api/nodes")
async def list_nodes():
    return {"nodes": monitor.snapshot()}


@app.get("/api/nodes/{node_id}")
async def node_detail(node_id: str):
    check_node(node_id)
    return await coordinator.node_detail(node_id)


@app.post("/api/nodes/{node_id}/fail")
async def fail_node(node_id: str):
    check_node(node_id)
    return await coordinator.fail_node(node_id)


@app.post("/api/nodes/{node_id}/recover")
async def recover_node(node_id: str):
    check_node(node_id)
    try:
        return await coordinator.recover_node(node_id)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


# -------------------------------------------------------------- incidents
@app.get("/api/incidents")
async def list_incidents():
    return {"incidents": await coordinator.list_incidents()}


@app.post("/api/incidents", status_code=201)
async def create_incident(data: IncidentCreate):
    try:
        return await coordinator.report_incident(data)
    except AllNodesUnavailable as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/incidents/{incident_id}")
async def get_incident(incident_id: str):
    inc = await coordinator.get_incident(incident_id)
    if inc is None:
        raise HTTPException(404, f"{incident_id} not found on any reachable node")
    return inc


@app.patch("/api/incidents/{incident_id}")
async def patch_incident(incident_id: str, data: IncidentStatusPatch):
    try:
        return await coordinator.update_incident_status(incident_id, data.status)
    except KeyError:
        raise HTTPException(404, f"{incident_id} not found")
    except NodeUnavailable as exc:
        raise HTTPException(503, str(exc))


# ------------------------------------------------------- traffic / events
@app.get("/api/traffic")
async def traffic():
    return {"traffic": await coordinator.traffic()}


@app.get("/api/events")
async def events(limit: int = 200, type: str | None = None):
    return {"events": broker.recent(limit, set(type.split(",")) if type else None)}


@app.post("/internal/events", status_code=202)
async def publish_event(evt: EventIn):
    """Nodes publish their messages here (producer side of the broker)."""
    msg = await broker.publish(**evt.model_dump())
    return {"id": msg.id}


@app.post("/api/reset")
async def reset_demo():
    return await coordinator.reset_demo_data()


@app.get("/api/system")
async def system_info():
    return {"heartbeat_interval": config.HEARTBEAT_INTERVAL, "units_required": config.UNITS_REQUIRED}


# --------------------------------------------------------------- websocket
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    first_frame = {"nodes": monitor.snapshot(), "incidents": await coordinator.list_incidents(),
                   "events": broker.recent(100)}
    await hub.handle(ws, first_frame)
