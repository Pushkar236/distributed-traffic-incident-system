"""Global state: Chandy-Lamport snapshot over the FIFO channels.

Application whose global state is recorded: every region keeps a pool of standby
patrol vehicles (10 each). While "transfer traffic" is on, nodes keep lending vehicles
to each other with TRANSFER messages that spend 0.2-1.2 s in transit. The total number
of vehicles (30) never changes, but at any instant some are "on the road" inside a
channel - so a naive reading of the three counters rarely adds up to 30.

Chandy-Lamport (channels FIFO, any node may initiate)
    initiator: record own state, send MARKER on every outgoing channel,
               start recording every incoming channel
    on MARKER from j:
        first marker seen  -> record own state, channel j state = empty,
                              send MARKER on every outgoing channel,
                              record the other incoming channels
        otherwise          -> stop recording channel j (its state = messages recorded)
    a node is done when it has received a marker on every incoming channel.

Recorded local state = patrols + Lamport/vector clock + FA-1 data (active incidents,
free units) + the FA-2 wait-for edges (used for global-state deadlock detection).
A consistent cut must satisfy:  sum(local patrols) + sum(in-transit) == 30.
"""
import asyncio
import itertools
import random

from . import settings
from .errors import CommandError


class Snapshot:
    def __init__(self, rt):
        self.rt = rt
        self.patrols = settings.PATROLS_PER_NODE
        self.transfers_on = False
        self.snapshots = {}                # sid -> {"state", "channels", "recording", "done"}
        self.counter = itertools.count(1)
        self.handlers = {"TRANSFER": self.on_transfer, "MARKER": self.on_marker}
        self.commands = {"snapshot_start": self.cmd_start, "snapshot_get": self.cmd_get,
                         "transfers": self.cmd_transfers, "patrols": lambda: {"patrols": self.patrols}}

    def start(self):
        asyncio.create_task(self._transfer_loop())

    def state(self):
        return {"patrols": self.patrols, "transfers_on": self.transfers_on,
                "snapshots": sorted(self.snapshots)[-5:]}

    # ---------------------------------------------------- application traffic
    def cmd_transfers(self, on=True):
        self.transfers_on = bool(on)
        return {"transfers_on": self.transfers_on}

    async def _transfer_loop(self):
        while True:
            await asyncio.sleep(random.uniform(0.4, 0.9))
            if self.transfers_on and self.rt.available and self.patrols > 0:
                amount = random.randint(1, min(3, self.patrols))
                peer = random.choice(self.rt.peers)
                self.patrols -= amount
                self.rt.send(peer, "TRANSFER", {"amount": amount, "_text": f"{amount} patrol vehicle(s)"},
                             text=f"lend {amount} patrol vehicle(s)", extra_delay=random.uniform(0.2, 1.2))

    def on_transfer(self, msg, payload):
        self.patrols += payload["amount"]
        for snap in self.snapshots.values():          # message in transit across the cut
            if msg.sender in snap["recording"]:
                snap["channels"][msg.sender].append(payload["amount"])

    # -------------------------------------------------------- Chandy-Lamport
    def _record_local(self, sid, initiator):
        db = self.rt.node.db
        lamport, vector = self.rt.clocks.snapshot()
        self.snapshots[sid] = {
            "initiator": initiator,
            "state": {"patrols": self.patrols, "lamport": lamport, "vector": vector,
                      "active_incidents": db.count_active_incidents(),
                      "free_units": len(db.available_units()),
                      "leader": self.rt.modules["election"].leader,
                      "waits_for": self.rt.modules["deadlock"].wait_edges()},
            "channels": {p: [] for p in self.rt.peers},
            "recording": set(self.rt.peers), "done": False}
        self.rt.local_event("SNAPSHOT_RECORD", f"{sid}: local state recorded (patrols={self.patrols})")
        self.rt.multicast("MARKER", {"sid": sid, "initiator": initiator, "_text": f"marker {sid}"},
                          text=f"marker {sid}")

    def cmd_start(self):
        sid = f"S{next(self.counter)}-{self.rt.node_id.split('-')[0]}"
        self._record_local(sid, self.rt.node_id)
        self.rt.publish("SNAPSHOT_STARTED", f"{self.rt.node_id} initiated Chandy-Lamport snapshot {sid}",
                        target="ALL", sid=sid)
        return {"sid": sid}

    def on_marker(self, msg, payload):
        sid, sender = payload["sid"], msg.sender
        if sid not in self.snapshots:
            self._record_local(sid, payload["initiator"])     # channel from sender is empty
        snap = self.snapshots[sid]
        snap["recording"].discard(sender)
        if not snap["recording"] and not snap["done"]:
            snap["done"] = True
            self.rt.local_event("SNAPSHOT_DONE", f"{sid}: markers received on all channels")

    def cmd_get(self, sid):
        snap = self.snapshots.get(sid)
        if snap is None:
            raise CommandError(f"no snapshot {sid} on {self.rt.node_id}")
        return {"sid": sid, "node": self.rt.node_id, "done": snap["done"], "state": snap["state"],
                "channels": snap["channels"], "initiator": snap["initiator"]}
