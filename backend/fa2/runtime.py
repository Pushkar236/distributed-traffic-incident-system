"""FA-2 runtime inside one regional node process.

    attach(node, server)   called from backend/node.py: registers the SyncNode gRPC service
                           on the node's existing server and starts the FA-2 tasks.

Message flow inside a node

    peer --Deliver RPC--> inbox queue --> processor task --> algorithm handler
                                         (merge Lamport/vector clock, log RECV event)
    algorithm --send()--> tick clocks, log SEND event --> FIFO channel --> peer

The processor handles one message at a time, so every algorithm step ("on receiving
REQUEST do ...") runs atomically, as the textbook algorithms assume. Handlers never wait
for the network; anything long (a critical section, a timer) runs in its own task.

A node put into simulated failure by FA-1 (node.available == False) refuses Deliver,
GetTime, Command, Invoke and the chunk RPCs with UNAVAILABLE, sends nothing and drops
whatever was still in its inbox - the same behaviour as a crashed process.
"""
import asyncio
import inspect
import itertools
import json
import logging
from collections import deque
from datetime import datetime

import grpc

from ..rpc import fa2_sync_pb2 as spb
from ..rpc import fa2_sync_pb2_grpc
from . import settings
from .clocks import LogicalClocks, PhysicalClock
from .errors import CommandError
from .transport import Transport

log = logging.getLogger("fa2")


class SyncRuntime:
    def __init__(self, node):
        self.node = node
        self.node_id = node.node_id
        self.peers = [n for n in settings.NODE_IDS if n != self.node_id]
        offset, drift = settings.CLOCK_SKEW[self.node_id]
        self.clock = PhysicalClock(offset, drift)
        self.clocks = LogicalClocks(self.node_id, settings.NODE_IDS)
        self.transport = Transport(self.node_id, self.peers, lambda: self.available)
        self.inbox = asyncio.Queue()
        self.events = deque(maxlen=500)
        self.event_seq = itertools.count(1)
        self.msg_seq = itertools.count(1)
        self.handlers = {}             # message kind -> handler(msg, payload)
        self.commands = {"state": self.cmd_state, "events": self.cmd_events,
                         "set_delay": self.cmd_set_delay}
        self.modules = {}
        self.was_available = True

    # ------------------------------------------------------------ wiring
    def add_module(self, name, module):
        self.modules[name] = module
        self.handlers.update(getattr(module, "handlers", {}))
        self.commands.update(getattr(module, "commands", {}))

    def start(self):
        self.transport.start()
        asyncio.create_task(self._processor())
        asyncio.create_task(self._availability_watch())
        for module in self.modules.values():
            if hasattr(module, "start"):
                module.start()

    @property
    def available(self):
        return self.node.available

    # ------------------------------------------------------------ events
    def record(self, etype, kind, peer="", text="", msg_id=0):
        """Append one event (with the clocks *after* it happened) to this node's event log."""
        evt = {"seq": next(self.event_seq), "node": self.node_id, "type": etype, "kind": kind,
               "peer": peer, "text": text, "msg_id": msg_id,
               "lamport": self.clocks.lamport, "vector": dict(self.clocks.vector),
               "phys": round(self.clock.now(), 4)}
        self.events.append(evt)
        return evt

    def local_event(self, kind, text=""):
        self.clocks.tick()
        return self.record("LOCAL", kind, text=text)

    def incident_event(self, topic, incident):
        """Hook called by node.py when FA-1 creates/updates an incident: the event gets
        Lamport/vector timestamps and is causally broadcast to the other nodes."""
        if not self.available:
            return
        self.modules["causal"].broadcast(topic, {
            "incident_id": incident["incident_id"], "status": incident["status"],
            "text": f"{incident['incident_id']} {incident['status']} ({incident['type']}, {incident['severity']})"})

    # ------------------------------------------------------------- sending
    def _message(self, kind, payload, receiver):
        return spb.SyncMessage(kind=kind, sender=self.node_id, receiver=receiver,
                               lamport=self.clocks.lamport, vector=self.clocks.vector,
                               payload=json.dumps(payload or {}), msg_id=next(self.msg_seq),
                               sent_phys=self.clock.now())

    def send(self, peer, kind, payload=None, text="", extra_delay=0.0):
        """One send event: tick clocks, stamp the message, put it on the FIFO channel."""
        self.clocks.tick()
        msg = self._message(kind, payload, peer)
        self.record("SEND", kind, peer, text, msg.msg_id)
        return self.transport.enqueue(peer, msg, extra_delay)

    def multicast(self, kind, payload=None, text="", peers=None):
        """One send event delivered to several peers (same timestamp on every copy)."""
        peers = self.peers if peers is None else peers
        self.clocks.tick()
        msg = self._message(kind, payload, "ALL")
        self.record("SEND", kind, "ALL" if len(peers) > 1 else peers[0], text, msg.msg_id)
        return [self.transport.enqueue(p, msg) for p in peers]

    def publish(self, type, summary, target="", **payload):
        """Report to the FA-1 broker so the step shows up in the communication log."""
        asyncio.create_task(self.node.publisher.publish(type, summary=summary, target=target,
                                                        channel="FA2", payload=payload))

    # ----------------------------------------------------------- receiving
    async def deliver(self, msg):
        if msg.kind == "BEACON":                  # out-of-band: handled immediately, not clocked
            self.modules["beacon"].on_beacon(msg, json.loads(msg.payload or "{}"))
        else:
            self.inbox.put_nowait(msg)

    async def _processor(self):
        while True:
            msg = await self.inbox.get()
            if not self.available:
                continue                          # a crashed process loses its buffered messages
            self.clocks.merge(msg.lamport, dict(msg.vector))
            payload = json.loads(msg.payload or "{}")
            self.record("RECV", msg.kind, msg.sender, payload.get("_text", ""), msg.msg_id)
            handler = self.handlers.get(msg.kind)
            if handler is None:
                log.warning("no handler for %s", msg.kind)
                continue
            try:
                result = handler(msg, payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("handler for %s failed", msg.kind)

    async def _availability_watch(self):
        """Tell the modules when FA-1's Fail / Recover buttons change this node's state."""
        while True:
            await asyncio.sleep(0.3)
            if self.available != self.was_available:
                self.was_available = self.available
                hook = "on_recover" if self.available else "on_fail"
                for module in self.modules.values():
                    if hasattr(module, hook):
                        getattr(module, hook)()

    # ------------------------------------------------------------- commands
    async def run_command(self, name, args):
        fn = self.commands.get(name)
        if fn is None:
            raise CommandError(f"unknown command {name}")
        result = fn(**args)
        return await result if inspect.isawaitable(result) else result

    async def cmd_state(self):
        lamport, vector = self.clocks.snapshot()
        state = {"node_id": self.node_id, "available": self.available,
                 "physical_clock": self.clock.now(),
                 "physical_clock_iso": datetime.fromtimestamp(self.clock.now()).isoformat(timespec="milliseconds"),
                 "true_offset_ms": round(self.clock.offset_from_real() * 1000, 1),
                 "drift_ppm": round(self.clock.drift * 1_000_000),
                 "lamport": lamport, "vector": vector,
                 "channel_delay": self.transport.delay,
                 "messages_sent": self.transport.sent, "messages_failed": self.transport.failed}
        for name, module in self.modules.items():
            if hasattr(module, "state"):
                state[name] = module.state()
        return state

    def cmd_events(self, since=0, limit=200):
        return [e for e in self.events if e["seq"] > since][-limit:]

    def cmd_set_delay(self, peer, seconds):
        if peer not in self.transport.delay:
            raise CommandError(f"{peer} is not a peer of {self.node_id}")
        self.transport.delay[peer] = float(seconds)
        return self.transport.delay


class SyncServicer(fa2_sync_pb2_grpc.SyncNodeServicer):
    """gRPC face of the runtime. Every RPC is refused while the node is 'failed'."""

    def __init__(self, rt):
        self.rt = rt

    async def _check_up(self, context):
        if not self.rt.available:
            await context.abort(grpc.StatusCode.UNAVAILABLE, f"{self.rt.node_id} is down (simulated failure)")

    async def Deliver(self, request, context):
        await self._check_up(context)
        await self.rt.deliver(request)
        return spb.Ack(ok=True)

    async def GetTime(self, request, context):
        await self._check_up(context)
        return spb.TimeReply(node_id=self.rt.node_id, time=self.rt.clock.now())

    async def Command(self, request, context):
        await self._check_up(context)
        try:
            result = await self.rt.run_command(request.name, json.loads(request.args or "{}"))
            return spb.JsonReply(ok=True, data=json.dumps(result, default=str))
        except CommandError as exc:
            return spb.JsonReply(ok=False, error=str(exc))
        except TypeError as exc:                   # wrong / missing arguments
            return spb.JsonReply(ok=False, error=f"bad arguments for {request.name}: {exc}")

    async def Invoke(self, request, context):
        await self._check_up(context)
        return await self.rt.modules["objects"].invoke(request)

    async def StoreChunk(self, request, context):
        await self._check_up(context)
        return self.rt.modules["dfs"].store(request)

    async def ReadChunk(self, request, context):
        await self._check_up(context)
        chunk = self.rt.modules["dfs"].read(request.chunk_id)
        if chunk is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"chunk {request.chunk_id} not on {self.rt.node_id}")
        return chunk


def attach(node, server):
    """Create the FA-2 runtime for this node, add its gRPC service, start its tasks.
    Never lets an FA-2 problem stop the FA-1 node: on error it logs and returns None."""
    try:
        return _attach(node, server)
    except Exception:
        log.exception("FA-2 runtime could not start on %s - FA-1 services continue without it", node.node_id)
        return None


def _attach(node, server):
    from .beacon import BeaconProtocol
    from .causal import CausalBroadcast
    from .clock_sync import ClockSync
    from .deadlock import DeadlockDetector
    from .dfs_store import ChunkStore
    from .election import Election
    from .ledger import LedgerReplica
    from .mutex import MutualExclusion
    from .objects import ObjectAdapter
    from .snapshot import Snapshot

    rt = SyncRuntime(node)
    rt.add_module("clock_sync", ClockSync(rt))
    rt.add_module("causal", CausalBroadcast(rt))
    rt.add_module("beacon", BeaconProtocol(rt))
    rt.add_module("election", Election(rt))
    rt.add_module("snapshot", Snapshot(rt))
    rt.add_module("mutex", MutualExclusion(rt))
    rt.add_module("deadlock", DeadlockDetector(rt))
    rt.add_module("dfs", ChunkStore(rt))
    rt.add_module("objects", ObjectAdapter(rt))
    rt.add_module("ledger", LedgerReplica(rt))
    fa2_sync_pb2_grpc.add_SyncNodeServicer_to_server(SyncServicer(rt), server)
    rt.start()
    return rt
