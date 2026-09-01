"""Coordinator-side failure detection (heartbeats).

Every HEARTBEAT_INTERVAL seconds the monitor calls GetNodeStatus on each node.
  * no answer  -> node marked OFFLINE, NODE_FAILED published
  * answers again after being OFFLINE -> NODE_RECOVERED published
The coordinator reads `state` to decide where a request can go (failover).
"""
import asyncio
from datetime import datetime

from .. import config
from ..rpc import traffic_pb2 as pb
from ..rpc.client import NodeUnavailable


class HealthMonitor:
    def __init__(self, pool, broker):
        self.pool = pool
        self.broker = broker
        self.state = {}
        for node_id, info in config.NODES.items():
            self.state[node_id] = {
                "node_id": node_id, "name": info["name"], "region": info["region"],
                "address": config.address(node_id), "status": "UNKNOWN", "last_heartbeat": None,
                "active_incidents": 0, "available_units": 0, "total_units": 0,
                "traffic_density": 0.0, "db_path": "", "units": [],
            }

    def start(self):
        asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            for node_id in config.NODES:
                await self.check(node_id)
            await asyncio.sleep(config.HEARTBEAT_INTERVAL)

    def is_online(self, node_id):
        return self.state[node_id]["status"] == "ONLINE"

    def snapshot(self):
        return list(self.state.values())

    async def check(self, node_id):
        """One heartbeat. Returns True if the node answered."""
        st = self.state[node_id]
        try:
            status = await self.pool.call(node_id, "GetNodeStatus", pb.Empty(), timeout=config.HEARTBEAT_INTERVAL)
        except NodeUnavailable as exc:
            await self.mark_offline(node_id, exc.reason)
            return False

        st.update({
            "last_heartbeat": datetime.now().isoformat(timespec="seconds"),
            "active_incidents": status.active_incidents,
            "available_units": status.available_units,
            "total_units": status.total_units,
            "traffic_density": round(status.traffic_density, 1),
            "db_path": status.db_path,
            "units": [{"unit_id": u.unit_id, "unit_type": u.unit_type, "status": u.status,
                       "assigned_incident": u.assigned_incident} for u in status.units],
        })
        if st["status"] == "OFFLINE":
            await self.broker.publish(type="NODE_RECOVERED", source="COORDINATOR", target=node_id,
                                      channel="SYSTEM", summary=f"{st['name']} is back ONLINE (heartbeat restored)")
        st["status"] = "ONLINE"
        return True

    async def mark_offline(self, node_id, reason):
        st = self.state[node_id]
        if st["status"] != "OFFLINE":
            st["status"] = "OFFLINE"
            await self.broker.publish(type="NODE_FAILED", source="COORDINATOR", target=node_id,
                                      channel="SYSTEM", summary=f"{st['name']} OFFLINE - heartbeat failed ({reason})")
