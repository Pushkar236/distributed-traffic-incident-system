"""FIFO message channels between nodes (the communication model of Unit III).

Every node keeps one outgoing queue + one sender task per peer. Messages to the same
peer leave strictly one after another, so each channel is FIFO - an assumption made
by Lamport's mutual exclusion, Lodha-Kshemkalyani and the Chandy-Lamport snapshot.

A message can be given an artificial transit delay (per channel or per message). The
delay is applied inside the FIFO queue, i.e. it also holds back later messages on the
same channel, exactly like a slow link would. Messages on other channels are unaffected.

send() returns an asyncio.Future that resolves to True (delivered) / False (peer down).
Algorithms usually do not wait for it (asynchronous message passing); the ring election
does, to skip a dead successor.
"""
import asyncio
import logging

from .. import config
from ..rpc.client import NodeUnavailable
from .rpc import SyncPool

log = logging.getLogger("fa2.transport")


class Transport:
    def __init__(self, node_id, peers, is_available):
        self.node_id = node_id
        self.is_available = is_available          # callable -> False while the node is "failed"
        self.pool = SyncPool()
        self.queues = {p: asyncio.Queue() for p in peers}
        self.delay = {p: 0.0 for p in peers}      # channel delay in seconds (demo knob)
        self.sent = 0
        self.failed = 0

    def start(self):
        for peer in self.queues:
            asyncio.create_task(self._sender(peer))

    def enqueue(self, peer, msg, extra_delay=0.0):
        fut = asyncio.get_running_loop().create_future()
        self.queues[peer].put_nowait((msg, fut, extra_delay))
        return fut

    async def _sender(self, peer):
        while True:
            msg, fut, extra = await self.queues[peer].get()
            wait = max(self.delay[peer], extra)
            if wait:
                await asyncio.sleep(wait)
            ok = False
            if self.is_available():               # a failed node sends nothing
                try:
                    await self.pool.call(peer, "Deliver", msg, timeout=config.RPC_TIMEOUT)
                    ok = True
                    self.sent += 1
                except NodeUnavailable as exc:
                    self.failed += 1
                    log.info("%s -> %s %s not delivered (%s)", self.node_id, peer, msg.kind, exc.reason)
            if not fut.done():
                fut.set_result(ok)

    async def send_direct(self, peer, msg, timeout=1.0):
        """Out-of-band send (beacons): bypasses the FIFO queue and any channel delay."""
        if not self.is_available():
            return False
        try:
            await self.pool.call(peer, "Deliver", msg, timeout=timeout)
            return True
        except NodeUnavailable:
            return False
