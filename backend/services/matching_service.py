"""Node-side matching of response units to an incident.

Order of search:
  1. this node's own free units
  2. peers, nearest first, over P2P (search, then reserve)
"""
from .. import config
from ..rpc.client import NodeUnavailable


class MatchingService:
    def __init__(self, db, node_id, peers, publisher):
        self.db = db
        self.node_id = node_id
        self.peers = peers              # PeerClient
        self.publisher = publisher      # BrokerPublisher

    async def find_units(self, inc):
        """Assign units to the incident. Returns (updated incident, units needed, trace lines)."""
        needed = config.UNITS_REQUIRED[inc["severity"]]
        assigned = list(inc["assigned_units"])
        trace = []
        inc_id = inc["incident_id"]

        if needed == 0:
            trace.append(f"{self.node_id}: Low severity needs no unit - monitoring only")
            return self.db.update_incident(inc_id, status="MONITORING"), 0, trace

        # 1. local search
        local = self.db.available_units()
        trace.append(f"{self.node_id}: local search found {len(local)} free unit(s) (need {needed})")
        for unit in local:
            if len(assigned) < needed and self.db.reserve_unit(unit["unit_id"], inc_id):
                assigned.append({"unit_id": unit["unit_id"], "node_id": self.node_id})
                trace.append(f"{self.node_id}: reserved local unit {unit['unit_id']}")

        # 2. ask peers directly (P2P), nearest first
        for peer in self.peers.peers:
            if len(assigned) >= needed:
                break
            short = needed - len(assigned)
            trace.append(f"{self.node_id} -> {peer}: P2P SearchResponseUnits(need={short})")
            try:
                offered = await self.peers.search_units(peer, inc_id, inc["severity"], short)
                for unit in offered:
                    if len(assigned) < needed and await self.peers.reserve_unit(peer, inc_id, unit.unit_id):
                        assigned.append({"unit_id": unit.unit_id, "node_id": peer})
                        trace.append(f"{peer} -> {self.node_id}: unit {unit.unit_id} reserved (P2P)")
                if not offered:
                    trace.append(f"{peer}: no free units")
            except NodeUnavailable as exc:
                trace.append(f"{peer}: UNAVAILABLE ({exc.reason}) - trying next peer")

        status = "UNITS_ASSIGNED" if len(assigned) >= needed else "PARTIALLY_ASSIGNED"
        inc = self.db.update_incident(inc_id, status=status, assigned_units=assigned)
        trace.append(f"{self.node_id}: {len(assigned)}/{needed} unit(s) assigned -> {status}")
        await self.publisher.publish("INCIDENT_UPDATED", target="ALL",
                                     summary=f"{inc_id} {status} ({', '.join(u['unit_id'] for u in assigned) or 'no units'})",
                                     payload={"incident_id": inc_id, "status": status})
        return inc, needed, trace

    async def release_all(self, inc):
        """Free every unit of a resolved incident: local ones directly, borrowed ones via P2P."""
        released = []
        for u in inc["assigned_units"]:
            if u["node_id"] == self.node_id:
                ok = self.db.release_unit(u["unit_id"])
            else:
                ok = await self.peers.release_unit(u["node_id"], inc["incident_id"], u["unit_id"])
            if ok:
                released.append(u["unit_id"])
        return released
