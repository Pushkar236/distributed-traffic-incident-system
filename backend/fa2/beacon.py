"""Beacon protocol: periodic timestamped beacons between the nodes.

Every BEACON_INTERVAL each node sends a BEACON to every peer (out-of-band: not queued
behind algorithm messages, not stamped with logical clocks). A beacon carries:
    seq     beacon number               clock   sender's physical clock at send time
    rx      for every other node: (seq, local receive time) of the last beacon heard from it

Uses of the beacons
1. Decentralised liveness monitoring - no beacon from a peer for BEACON_TIMEOUT
   -> peer SUSPECTED (PEER_SUSPECTED event); a suspected leader triggers an election.
   (FA-1's heartbeat detector is centralised in the gateway; this one runs on every node.)
2. Clock offset estimation
   * sender-receiver:  offset(peer - me) ~ beacon.clock - my receive time   (includes transit)
   * reference-broadcast style (RBS): a beacon from node X reaches Y and Z at almost
     the same instant. Y reports its receive time of X#seq in its own beacon; Z compares
     it with its own receive time of the same X#seq:
          offset(Y - Z) ~ rxY(X#seq) - rxZ(X#seq)
     The sender's clock and its send delay cancel out - only receive-time jitter remains.
"""
import asyncio
import json
import time
from collections import deque

from ..rpc import fa2_sync_pb2 as spb
from . import settings


class BeaconProtocol:
    def __init__(self, rt):
        self.rt = rt
        self.seq = 0
        now = time.monotonic()
        self.peers = {p: {"status": "UNKNOWN", "last_seen": now, "received": 0,
                          "offset_samples": deque(maxlen=5), "rbs_samples": deque(maxlen=5)}
                      for p in rt.peers}
        self.rx_log = {}                    # (origin, seq) -> my receive time (physical clock)
        self.latest_rx = {}                 # origin -> [seq, my receive time]
        self.sent = 0

    def start(self):
        asyncio.create_task(self._send_loop())
        asyncio.create_task(self._monitor_loop())

    def state(self):
        now = time.monotonic()
        avg = lambda d: round(sum(d) / len(d) * 1000, 1) if d else None   # noqa: E731
        return {"sent": self.sent, "peers": {
            p: {"status": s["status"], "last_seen_s": round(now - s["last_seen"], 1), "received": s["received"],
                "offset_ms": avg(s["offset_samples"]), "rbs_offset_ms": avg(s["rbs_samples"])}
            for p, s in self.peers.items()}}

    def suspected(self):
        return [p for p, s in self.peers.items() if s["status"] == "SUSPECTED"]

    # --------------------------------------------------------------- send
    async def _send_loop(self):
        while True:
            await asyncio.sleep(settings.BEACON_INTERVAL)
            if not self.rt.available:
                continue
            self.seq += 1
            payload = {"seq": self.seq, "clock": self.rt.clock.now(),
                       "leader": self.rt.modules["election"].leader, "rx": self.latest_rx}
            msg = spb.SyncMessage(kind="BEACON", sender=self.rt.node_id, payload=json.dumps(payload))
            await asyncio.gather(*(self.rt.transport.send_direct(p, msg) for p in self.rt.peers))
            self.sent += 1

    # ------------------------------------------------------------ receive
    def on_beacon(self, msg, payload):
        rx_time = self.rt.clock.now()
        sender, peer = msg.sender, self.peers.get(msg.sender)
        if peer is None:
            return
        peer["last_seen"] = time.monotonic()
        peer["received"] += 1
        peer["offset_samples"].append(payload["clock"] - rx_time)
        self.rx_log[(sender, payload["seq"])] = rx_time
        self.latest_rx[sender] = [payload["seq"], rx_time]
        # RBS: compare the sender's receive time of a third node's beacon with ours
        for origin, (seq, their_rx) in payload.get("rx", {}).items():
            mine = self.rx_log.get((origin, seq))
            if origin != self.rt.node_id and mine is not None:
                peer["rbs_samples"].append(their_rx - mine)
        if len(self.rx_log) > 300:
            for key in list(self.rx_log)[:100]:
                del self.rx_log[key]
        if peer["status"] != "ALIVE":
            was = peer["status"]
            peer["status"] = "ALIVE"
            if was == "SUSPECTED":
                self.rt.publish("PEER_ALIVE", f"{self.rt.node_id} hears beacons from {sender} again",
                                target=sender)

    # ------------------------------------------------------------ monitor
    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(0.5)
            if not self.rt.available:
                continue
            now = time.monotonic()
            for p, s in self.peers.items():
                if s["status"] != "SUSPECTED" and now - s["last_seen"] > settings.BEACON_TIMEOUT:
                    s["status"] = "SUSPECTED"
                    self.rt.local_event("PEER_SUSPECTED", f"no beacon from {p} for {settings.BEACON_TIMEOUT}s")
                    self.rt.publish("PEER_SUSPECTED", f"{self.rt.node_id}: no beacon from {p} for "
                                                      f"{now - s['last_seen']:.1f}s - peer suspected", target=p)
                    self.rt.modules["election"].on_peer_suspected(p)

    def on_recover(self):
        now = time.monotonic()                      # grace period after our own outage
        for s in self.peers.values():
            s["last_seen"] = now
            s["status"] = "UNKNOWN"
