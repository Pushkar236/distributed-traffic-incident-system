"""Message-oriented communication: a small publish/subscribe broker.

How it works (same idea as RabbitMQ, written in ~40 lines of Python):
  * publish()  puts a message in a queue and returns immediately (asynchronous)
  * a background worker takes messages from the queue one by one and hands
    each to every subscriber (the WebSocket hub and the coordinator)
  * the last 500 messages are kept so GET /api/events can show the history

The broker lives inside the gateway process. Node processes publish to it
over HTTP using BrokerPublisher.
"""
import asyncio
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime

log = logging.getLogger("broker")


@dataclass
class Message:
    type: str                 # INCIDENT_CREATED, NODE_FAILED, ...
    source: str               # who sent it: PUNE-TR-01, COORDINATOR, DASHBOARD
    target: str = ""          # who it is for: a node id, "ALL", or ""
    channel: str = "BROKER"   # RPC / P2P / BROKER / SYSTEM  (only used for display)
    summary: str = ""
    payload: dict = field(default_factory=dict)
    id: int = 0
    ts: str = ""


class MessageBroker:
    def __init__(self):
        self.subscribers = []              # async functions called for every message
        self.history = deque(maxlen=500)
        self.queue = asyncio.Queue()
        self.counter = 0

    def subscribe(self, handler):
        self.subscribers.append(handler)

    def start(self):
        asyncio.create_task(self._worker())

    async def publish(self, **fields):
        """Store the message and queue it for delivery. Does not wait for consumers."""
        self.counter += 1
        msg = Message(**fields)
        msg.id = self.counter
        msg.ts = datetime.now().isoformat(timespec="seconds")
        self.history.append(msg)
        await self.queue.put(msg)
        return msg

    async def _worker(self):
        while True:
            msg = await self.queue.get()
            for handler in self.subscribers:
                try:
                    await handler(msg)
                except Exception:          # a failing consumer must not stop the broker
                    log.exception("subscriber failed on %s", msg.type)

    def recent(self, limit=200, types=None):
        msgs = [m for m in self.history if types is None or m.type in types]
        return [asdict(m) for m in msgs[-limit:]]


class BrokerPublisher:
    """Used by node processes: sends a message to the broker over HTTP."""

    def __init__(self, url, node_id):
        self.url = url
        self.node_id = node_id

    async def publish(self, type, summary="", target="", channel="BROKER", payload=None, source=None):
        import httpx
        body = {"type": type, "source": source or self.node_id, "target": target,
                "channel": channel, "summary": summary, "payload": payload or {}}
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(self.url, json=body)
        except Exception as exc:           # broker down must not break the node
            log.warning("broker unreachable: %s", exc)
