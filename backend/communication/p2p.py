"""Peer-to-peer communication: a node talks to another node directly over gRPC.

No coordinator and no shared database sit in between. The peer list comes
from the registry, nearest-first.
"""
from .. import config
from ..rpc import traffic_pb2 as pb
from ..rpc.client import NodeUnavailable, StubPool


class PeerClient:
    def __init__(self, node_id, publisher):
        self.node_id = node_id
        self.peers = config.NODES[node_id]["peers"]
        self.pool = StubPool()
        self.publisher = publisher          # to show the P2P exchange in the dashboard log

    async def search_units(self, peer, incident_id, severity, needed):
        """Ask a peer which response units it can spare. Returns a list (may be empty)."""
        await self.publisher.publish("RESPONSE_REQUESTED", target=peer, channel="P2P",
                                     summary=f"Requesting {needed} response unit(s) for {incident_id}",
                                     payload={"incident_id": incident_id})
        req = pb.UnitSearchRequest(requesting_node=self.node_id, incident_id=incident_id,
                                   severity=severity, units_needed=needed)
        try:
            reply = await self.pool.call(peer, "SearchResponseUnits", req)
        except NodeUnavailable as exc:
            await self.publisher.publish("NODE_UNREACHABLE", target=peer, channel="P2P",
                                         summary=f"{peer} did not answer P2P request ({exc.reason})",
                                         payload={"incident_id": incident_id})
            raise
        units = list(reply.units)
        # log the answer with the peer as the source, because it travelled peer -> us
        if units:
            await self.publisher.publish("RESPONSE_AVAILABLE", source=peer, target=self.node_id, channel="P2P",
                                         summary=f"Unit(s) {', '.join(u.unit_id for u in units)} available for {incident_id}",
                                         payload={"incident_id": incident_id, "from": peer})
        else:
            await self.publisher.publish("RESPONSE_UNAVAILABLE", source=peer, target=self.node_id, channel="P2P",
                                         summary=f"No free units for {incident_id}",
                                         payload={"incident_id": incident_id, "from": peer})
        return units

    async def reserve_unit(self, peer, incident_id, unit_id):
        req = pb.UnitReservation(requesting_node=self.node_id, incident_id=incident_id, unit_id=unit_id)
        return (await self.pool.call(peer, "ReserveUnit", req)).ok

    async def release_unit(self, peer, incident_id, unit_id):
        req = pb.UnitReservation(requesting_node=self.node_id, incident_id=incident_id, unit_id=unit_id)
        try:
            return (await self.pool.call(peer, "ReleaseUnit", req)).ok
        except NodeUnavailable:
            return False
