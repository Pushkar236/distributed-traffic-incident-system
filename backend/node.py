"""A regional traffic node: one gRPC server process with its own SQLite database.

    python -m backend.node --node-id PUNE-TR-01

The node
  * owns its local data (incidents, response units, traffic status),
  * answers RPCs from the coordinator and from other nodes (P2P),
  * asks its peers directly when it runs out of response units,
  * publishes messages to the broker so the dashboard can show what happened,
  * can be put into "simulated failure": every RPC is refused with UNAVAILABLE,
    which is exactly what callers see when a process has crashed.
"""
import argparse
import asyncio
import logging
import os

import grpc
from grpc import aio

from . import config
from .communication.messaging import BrokerPublisher
from .communication.p2p import PeerClient
from .database.db import NodeDatabase
from .rpc import traffic_pb2 as pb
from .rpc import traffic_pb2_grpc
from .services.incident_service import IncidentService, incident_to_pb, unit_to_pb
from .services.matching_service import MatchingService
from .fa2.runtime import attach as attach_fa2

log = logging.getLogger("node")


class TrafficNodeServicer(traffic_pb2_grpc.TrafficNodeServicer):
    def __init__(self, node_id):
        self.node_id = node_id
        self.info = config.NODES[node_id]
        self.db = NodeDatabase(config.db_path(node_id))
        self.db.seed_if_empty(node_id)
        self.publisher = BrokerPublisher(config.BROKER_URL, node_id)
        self.peers = PeerClient(node_id, self.publisher)
        self.incidents = IncidentService(self.db, node_id)
        self.matching = MatchingService(self.db, node_id, self.peers, self.publisher)
        self.available = True          # False = simulated failure
        self.fa2 = None                # FA-2 synchronization runtime (attached in serve())

    async def _check_up(self, context):
        """Called first in every RPC: a 'failed' node refuses to answer."""
        if not self.available:
            await context.abort(grpc.StatusCode.UNAVAILABLE, f"{self.node_id} is down (simulated failure)")

    # ------------------------------------------------------------- status
    async def GetNodeStatus(self, request, context):
        await self._check_up(context)
        units = self.db.list_units()
        return pb.NodeStatus(
            node_id=self.node_id, node_name=self.info["name"], region=self.info["region"],
            address=config.address(self.node_id), available=True,
            active_incidents=self.db.count_active_incidents(),
            available_units=len(self.db.available_units()), total_units=len(units),
            traffic_density=self.db.average_density(), db_path=self.info["db"],
            units=[unit_to_pb(u, self.node_id) for u in units])

    async def GetTrafficStatus(self, request, context):
        await self._check_up(context)
        return pb.TrafficStatus(
            node_id=self.node_id, average_density=self.db.average_density(),
            segments=[pb.TrafficSegment(road=r["road"], density=r["density"], updated_at=r["updated_at"])
                      for r in self.db.get_traffic()])

    # ---------------------------------------------------------- incidents
    async def ListIncidents(self, request, context):
        await self._check_up(context)
        return pb.IncidentList(incidents=[incident_to_pb(i) for i in self.db.list_incidents()])

    async def GetIncidentStatus(self, request, context):
        await self._check_up(context)
        inc = self.db.get_incident(request.incident_id)
        if inc is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"{request.incident_id} not known here")
        return incident_to_pb(inc)

    async def CreateIncident(self, request, context):
        await self._check_up(context)
        inc = self.incidents.create(request)
        log.info("recorded %s (%s, %s)", inc["incident_id"], inc["type"], inc["severity"])
        await self.publisher.publish("INCIDENT_CREATED", target="ALL",
                                     summary=f"{inc['type']} #{inc['incident_id']} recorded locally in {self.info['db']}",
                                     payload={"incident": inc})
        if self.fa2:
            self.fa2.incident_event("INCIDENT_CREATED", inc)       # FA-2: logical timestamp + causal broadcast
        return incident_to_pb(inc)

    async def UpdateIncidentStatus(self, request, context):
        await self._check_up(context)
        inc = self.db.get_incident(request.incident_id)
        if inc is None or inc["origin"] != "LOCAL":
            await context.abort(grpc.StatusCode.NOT_FOUND, f"{request.incident_id} is not owned by {self.node_id}")
        released = []
        if request.status == "RESOLVED":
            released = await self.matching.release_all(inc)
        inc = self.db.update_incident(inc["incident_id"], status=request.status)
        await self.publisher.publish("INCIDENT_UPDATED", target="ALL",
                                     summary=f"{inc['incident_id']} -> {inc['status']}"
                                             + (f", released {', '.join(released)}" if released else ""),
                                     payload={"incident_id": inc["incident_id"], "status": inc["status"]})
        if self.fa2:
            self.fa2.incident_event("INCIDENT_UPDATED", inc)       # FA-2: logical timestamp + causal broadcast
        return incident_to_pb(inc)

    async def NotifyIncident(self, request, context):
        await self._check_up(context)
        self.incidents.store_remote_copy(request)
        log.info("broadcast received: %s from %s", request.incident_id, request.handled_by)
        return pb.Ack(ok=True, message="copy stored")

    # ------------------------------------------------- response matching
    async def RequestResponseSupport(self, request, context):
        await self._check_up(context)
        inc = self.db.get_incident(request.incident_id)
        if inc is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"{request.incident_id} not known here")
        inc, needed, trace = await self.matching.find_units(inc)
        for line in trace:
            log.info("  %s", line)
        return pb.SupportResponse(incident=incident_to_pb(inc), units_needed=needed,
                                  units_assigned=len(inc["assigned_units"]), trace=trace)

    async def SearchResponseUnits(self, request, context):          # P2P: a peer asks us
        await self._check_up(context)
        free = self.db.available_units()[:request.units_needed]
        log.info("P2P query from %s: offering %s", request.requesting_node, [u["unit_id"] for u in free])
        return pb.UnitSearchResponse(node_id=self.node_id, units=[unit_to_pb(u, self.node_id) for u in free])

    async def ReserveUnit(self, request, context):                  # P2P: a peer books our unit
        await self._check_up(context)
        ok = self.db.reserve_unit(request.unit_id, request.incident_id)
        return pb.Ack(ok=ok, message="reserved" if ok else "unit not available")

    async def ReleaseUnit(self, request, context):                  # P2P: a peer returns our unit
        await self._check_up(context)
        return pb.Ack(ok=self.db.release_unit(request.unit_id))

    # ------------------------------------------------------ administration
    async def SetAvailability(self, request, context):              # NOT guarded: used to fail/recover
        self.available = request.available
        state = "RECOVERED - accepting RPCs again" if request.available \
            else "SIMULATED FAILURE - refusing all RPCs with UNAVAILABLE"
        log.warning(state)
        await self.publisher.publish("SYSTEM", target="COORDINATOR", channel="SYSTEM",
                                     summary=f"{self.node_id} {state}")
        return pb.Ack(ok=True, message=state)

    async def ResetData(self, request, context):
        await self._check_up(context)
        self.db.reset(self.node_id)
        return pb.Ack(ok=True, message="reset")


async def traffic_ticker(node):
    """Simulated roadside sensors: update this node's traffic density every 3 seconds."""
    while True:
        await asyncio.sleep(3)
        if node.available:
            node.db.tick_traffic(node.db.count_active_incidents())


async def serve(node_id):
    node = TrafficNodeServicer(node_id)
    server = aio.server()
    traffic_pb2_grpc.add_TrafficNodeServicer_to_server(node, server)
    node.fa2 = attach_fa2(node, server)        # FA-2: SyncNode service on the same port
    server.add_insecure_port(f"0.0.0.0:{config.NODES[node_id]['port']}")
    await server.start()
    log.info("%s listening on %s | db=%s | peers=%s", node_id, config.address(node_id),
             config.db_path(node_id), node.peers.peers)
    asyncio.create_task(traffic_ticker(node))
    await server.wait_for_termination()


def main():
    parser = argparse.ArgumentParser(description="Regional traffic node (gRPC server)")
    parser.add_argument("--node-id", default=os.environ.get("NODE_ID"), choices=list(config.NODES), required=not os.environ.get("NODE_ID"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{args.node_id}] %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # hide one log line per published message
    try:
        asyncio.run(serve(args.node_id))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
