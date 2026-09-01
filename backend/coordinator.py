"""Coordination middleware: sits between the dashboard/API and the regional nodes.

It hides node details from the dashboard and runs the incident workflow:

  report_incident()
    1. allocate a global incident id (INC-1004, ...)
    2. RPC CreateIncident on the reporting node
       - if that node is OFFLINE / does not answer -> try the nearest node (failover)
    3. the node publishes INCIDENT_CREATED; on_incident_created() broadcasts it
       to the other nodes (queued for nodes that are offline)
    4. RPC RequestResponseSupport -> the node finds units locally and via P2P
"""
import asyncio
import logging

from . import config
from .rpc import traffic_pb2 as pb
from .rpc.client import NodeUnavailable
from .services.incident_service import incident_from_pb, incident_to_pb

log = logging.getLogger("coordinator")


class AllNodesUnavailable(Exception):
    pass


class Coordinator:
    def __init__(self, broker, pool, monitor):
        self.broker = broker
        self.pool = pool
        self.monitor = monitor
        self.next_number = 1004                       # seed data uses INC-1001..1003
        self.pending = {n: [] for n in config.NODES}  # broadcasts waiting for an offline node
        broker.subscribe(self.on_broker_message)

    async def log(self, type, target="", channel="SYSTEM", summary="", source="COORDINATOR", **payload):
        await self.broker.publish(type=type, source=source, target=target, channel=channel,
                                  summary=summary, payload=payload)

    # -------------------------------------------------------------- startup
    async def start(self):
        """Continue the id sequence after whatever the nodes already hold."""
        for inc in await self.list_incidents():
            number = int(inc["incident_id"].split("-")[1])
            self.next_number = max(self.next_number, number + 1)
        await self.log("SYSTEM", summary=f"Coordination service ready - next incident id INC-{self.next_number}")

    # ------------------------------------------------------ incident workflow
    async def report_incident(self, data):
        incident_id = f"INC-{self.next_number}"
        self.next_number += 1
        req = pb.CreateIncidentRequest(incident_id=incident_id, type=data.type, location=data.location,
                                       severity=data.severity, description=data.description,
                                       detected_by=data.detected_by)

        # --- step 1: record the incident, with failover to the nearest online node ---
        served_by, created = None, None
        for node_id in config.failover_order(data.detected_by):
            if not self.monitor.is_online(node_id):
                await self.log("NODE_UNREACHABLE", node_id, summary=f"{node_id} is OFFLINE - cannot deliver CreateIncident({incident_id})")
                continue
            await self.log("RPC_CALL", node_id, "RPC", f"CreateIncident({incident_id}, {data.type}, {data.severity})")
            try:
                created = await self.pool.call(node_id, "CreateIncident", req)
                served_by = node_id
                break
            except NodeUnavailable as exc:
                await self.monitor.mark_offline(node_id, exc.reason)
                await self.log("NODE_UNREACHABLE", node_id, summary=f"{node_id} did not respond ({exc.reason})")
        if served_by is None:
            raise AllNodesUnavailable("no node could record the incident")
        if served_by != data.detected_by:
            await self.log("REQUEST_REDIRECTED", served_by,
                           summary=f"CreateIncident({incident_id}) redirected {data.detected_by} -> {served_by}")

        # --- step 2: ask that node to find response units (local + P2P) ---
        await self.log("RPC_CALL", served_by, "RPC", f"RequestResponseSupport({incident_id})")
        incident, trace = incident_from_pb(created), []
        try:
            reply = await self.pool.call(served_by, "RequestResponseSupport",
                                         pb.SupportRequest(incident_id=incident_id), timeout=15)
            incident, trace = incident_from_pb(reply.incident), list(reply.trace)
        except NodeUnavailable as exc:
            await self.log("NODE_UNREACHABLE", served_by, summary=f"{served_by} failed during RequestResponseSupport ({exc.reason})")

        return {"incident": incident, "served_by": served_by,
                "redirected": served_by != data.detected_by, "trace": trace}

    # ---------------------------------------------- broker subscriber (async)
    async def on_broker_message(self, msg):
        if msg.type == "INCIDENT_CREATED":
            incident = incident_to_pb(msg.payload["incident"])
            for node_id in config.NODES:
                if node_id != msg.source:
                    await self.deliver_broadcast(node_id, incident)
        elif msg.type == "NODE_RECOVERED":
            # store-and-forward: hand over the broadcasts the node missed while offline
            missed, self.pending[msg.target] = self.pending[msg.target], []
            for incident in missed:
                await self.deliver_broadcast(msg.target, incident)
            if missed:
                await self.log("SYSTEM", msg.target, summary=f"Delivered {len(missed)} queued broadcast(s) to {msg.target} after recovery")

    async def deliver_broadcast(self, node_id, incident):
        if not self.monitor.is_online(node_id):
            self.pending[node_id].append(incident)
            await self.log("NODE_UNREACHABLE", node_id, summary=f"{node_id} offline - broadcast of {incident.incident_id} queued for delivery")
            return
        try:
            await self.pool.call(node_id, "NotifyIncident", incident)
            await self.log("INCIDENT_BROADCAST", node_id, "RPC", f"INCIDENT_BROADCAST {incident.incident_id} delivered to {node_id}")
        except NodeUnavailable:
            self.pending[node_id].append(incident)
            await self.log("NODE_UNREACHABLE", node_id, summary=f"{node_id} unreachable - broadcast of {incident.incident_id} queued")

    # --------------------------------------------------------------- reading
    async def list_incidents(self):
        """Collect incidents from every reachable node; the owner's copy wins over broadcast copies."""
        merged = {}
        for node_id in config.NODES:
            try:
                reply = await self.pool.call(node_id, "ListIncidents", pb.Empty())
            except NodeUnavailable:
                continue
            for inc in map(incident_from_pb, reply.incidents):
                existing = merged.get(inc["incident_id"])
                if existing is None or inc["origin"] == "LOCAL":
                    merged[inc["incident_id"]] = inc
        return sorted(merged.values(), key=lambda i: i["created_at"], reverse=True)

    async def get_incident(self, incident_id):
        return next((i for i in await self.list_incidents() if i["incident_id"] == incident_id), None)

    async def update_incident_status(self, incident_id, status):
        inc = await self.get_incident(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        owner = inc["handled_by"]
        await self.log("RPC_CALL", owner, "RPC", f"UpdateIncidentStatus({incident_id}, {status})")
        reply = await self.pool.call(owner, "UpdateIncidentStatus",
                                     pb.IncidentStatusUpdate(incident_id=incident_id, status=status))
        return incident_from_pb(reply)

    async def traffic(self):
        result = []
        for st in self.monitor.snapshot():
            item = {"node_id": st["node_id"], "region": st["region"], "status": st["status"],
                    "average_density": st["traffic_density"], "segments": []}
            if st["status"] == "ONLINE":
                try:
                    reply = await self.pool.call(st["node_id"], "GetTrafficStatus", pb.Empty())
                    item["segments"] = [{"road": s.road, "density": s.density} for s in reply.segments]
                except NodeUnavailable:
                    pass
            result.append(item)
        return result

    async def node_detail(self, node_id):
        st = dict(self.monitor.state[node_id])
        st["peers"] = [{"node_id": p, "address": config.address(p)} for p in config.NODES[node_id]["peers"]]
        st["incidents"] = []
        if st["status"] == "ONLINE":
            reply = await self.pool.call(node_id, "ListIncidents", pb.Empty())
            st["incidents"] = [incident_from_pb(i) for i in reply.incidents]
        return st

    # ------------------------------------------------- fault-tolerance buttons
    async def fail_node(self, node_id):
        await self.log("SYSTEM", node_id, source="DASHBOARD", summary=f"Operator simulated failure of {node_id}")
        try:
            await self.pool.call(node_id, "SetAvailability", pb.AvailabilityRequest(available=False))
        except NodeUnavailable:
            pass                                   # already dead; the heartbeat below reports it
        await self.monitor.check(node_id)
        return self.monitor.state[node_id]

    async def recover_node(self, node_id):
        await self.log("SYSTEM", node_id, source="DASHBOARD", summary=f"Operator requested recovery of {node_id}")
        try:
            await self.pool.call(node_id, "SetAvailability", pb.AvailabilityRequest(available=True))
        except NodeUnavailable:
            raise RuntimeError(f"{node_id} process is not running - start it with: python -m backend.node --node-id {node_id}")
        await self.monitor.check(node_id)          # publishes NODE_RECOVERED
        return self.monitor.state[node_id]

    async def reset_demo_data(self):
        done = []
        for node_id in config.NODES:
            try:
                await self.pool.call(node_id, "ResetData", pb.Empty())
                done.append(node_id)
            except NodeUnavailable:
                pass
        self.next_number = 1004
        self.pending = {n: [] for n in config.NODES}
        await self.log("SYSTEM", source="DASHBOARD", summary=f"Demo data reset on {', '.join(done)}")
        return {"reset": done}
