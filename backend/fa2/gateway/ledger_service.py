"""Feeds important coordination events into the replicated audit ledger.

The service subscribes to the FA-1 broker, keeps the important events in a pending list
and every BATCH_INTERVAL seconds asks the current leader node to turn them into a block
(command ledger_propose). The leader mines + replicates the block; the reply says whether
a majority stored it (committed). If there is no reachable leader, events stay pending.
"""
import asyncio
from collections import Counter

from ...rpc.client import NodeUnavailable
from .. import settings
from ..rpc import CommandFailed

AUDITED = {"INCIDENT_CREATED", "INCIDENT_UPDATED", "REQUEST_REDIRECTED", "NODE_FAILED", "NODE_RECOVERED",
           "LEADER_ELECTED", "CS_ENTER", "DEADLOCK_DETECTED", "DEADLOCK_RESOLVED", "CLOCK_SYNC",
           "DFS_WRITE", "SNAPSHOT_STARTED", "LEDGER_NOTE"}
BATCH_INTERVAL = 4.0
MAX_EVENTS_PER_BLOCK = 25


class LedgerService:
    def __init__(self, pool, broker):
        self.pool, self.broker = pool, broker
        self.pending = []
        self.commits = []
        self.last_error = None
        broker.subscribe(self.on_message)

    def start(self):
        asyncio.create_task(self._loop())

    async def on_message(self, msg):
        if msg.type in AUDITED:
            self.pending.append({"type": msg.type, "source": msg.source, "target": msg.target,
                                 "summary": msg.summary[:160], "ts": msg.ts})

    async def leader(self):
        """The leader most online nodes agree on."""
        votes = Counter()
        for node in settings.NODE_IDS:
            try:
                st = await self.pool.command(node, "state", timeout=2)
                if st["election"]["leader"]:
                    votes[st["election"]["leader"]] += 1
            except (NodeUnavailable, CommandFailed):
                continue
        return votes.most_common(1)[0][0] if votes else None

    async def flush(self):
        if not self.pending:
            return None
        leader = await self.leader()
        if leader is None:
            self.last_error = "no leader reachable - events kept pending"
            return None
        batch = self.pending[:MAX_EVENTS_PER_BLOCK]
        try:
            result = await self.pool.command(leader, "ledger_propose", timeout=8, events=batch)
        except (NodeUnavailable, CommandFailed) as exc:
            self.last_error = f"{leader}: {exc}"
            return None
        del self.pending[:len(batch)]
        self.last_error = None
        result["leader"] = leader
        self.commits.append(result)
        del self.commits[:-20]
        return result

    async def _loop(self):
        while True:
            await asyncio.sleep(BATCH_INTERVAL)
            try:
                await self.flush()
            except Exception as exc:              # never let the background loop die
                self.last_error = repr(exc)

    def summary(self):
        return {"pending": len(self.pending), "recent_commits": self.commits[-8:], "last_error": self.last_error,
                "audited_types": sorted(AUDITED)}
