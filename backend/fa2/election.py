"""Leader (coordinator) election among the regional nodes: Bully and Ring.

The elected leader is used by the other FA-2 features: it is the Berkeley time daemon,
the default snapshot initiator and the block proposer of the audit ledger.

Bully algorithm (Garcia-Molina) - priority PUNE 1 < MUMBAI 2 < NASHIK 3
    P starts an election: sends ELECTION to every higher-priority node.
      no OK within ELECTION_OK_TIMEOUT   -> P wins, sends COORDINATOR to everybody
      an OK arrives                       -> a higher node takes over; P waits for
                                             COORDINATOR (re-elects if it never comes)
    on ELECTION from a lower node: answer OK and start an own election.
    Triggers: node start-up, a suspected leader (beacon protocol), a recovered node
    (it may be the highest and "bullies" its way back).

Ring algorithm (Tanenbaum's variant with a participant list)
    The initiator sends RING_ELECTION [own id] to its successor; every node appends its
    id and forwards. A dead successor is skipped (delivery fails -> try the next one).
    When the message returns to a node already in the list, the highest-priority id in
    the list is the leader; RING_COORDINATOR circulates once around the ring.
"""
import asyncio
import itertools
import time

from . import settings
from .errors import CommandError


class Election:
    def __init__(self, rt):
        self.rt = rt
        self.me = rt.node_id
        self.priority = settings.PRIORITY[self.me]
        self.leader = None
        self.in_progress = False
        self.got_ok = False
        self.round = itertools.count(1)
        self.current = 0
        self.trace = []                    # recent election steps, for the dashboard
        self.messages = 0                  # election messages sent by this node
        self.handlers = {"ELECTION": self.on_election, "OK": self.on_ok, "COORDINATOR": self.on_coordinator,
                         "RING_ELECTION": self.on_ring_election, "RING_COORDINATOR": self.on_ring_coordinator}
        self.commands = {"election_start": self.cmd_start}

    def start(self):
        async def startup():
            await asyncio.sleep(settings.STARTUP_ELECTION_DELAY)
            if self.leader is None:
                self.start_bully("node start-up")
        asyncio.create_task(startup())

    def state(self):
        return {"leader": self.leader, "in_progress": self.in_progress, "messages_sent": self.messages,
                "priority": self.priority, "trace": self.trace[-12:]}

    def _trace(self, text):
        self.trace.append({"t": time.time(), "text": text})
        del self.trace[:-40]

    def _send(self, peer, kind, payload, text):
        self.messages += 1
        self._trace(f"-> {peer}: {kind} {text}".rstrip())
        return self.rt.send(peer, kind, payload, text)

    def cmd_start(self, algorithm="BULLY"):
        if algorithm == "BULLY":
            self.start_bully("requested by operator")
        elif algorithm == "RING":
            self.start_ring()
        else:
            raise CommandError(f"unknown election algorithm {algorithm}")
        return {"started": algorithm, "initiator": self.me}

    # ================================================================ Bully
    def start_bully(self, reason):
        if not self.rt.available or self.in_progress:
            return
        self.in_progress, self.got_ok = True, False
        self.current = rnd = next(self.round)
        higher = [p for p in self.rt.peers if settings.PRIORITY[p] > self.priority]
        self._trace(f"BULLY election started ({reason})")
        self.rt.local_event("ELECTION_START", f"Bully election started: {reason}")
        if not higher:
            self._become_leader("BULLY")
            return
        for p in higher:
            self._send(p, "ELECTION", {"round": rnd}, "")
        asyncio.create_task(self._bully_timer(rnd))

    async def _bully_timer(self, rnd):
        await asyncio.sleep(settings.ELECTION_OK_TIMEOUT)
        if self.current != rnd or not self.in_progress:
            return
        if not self.got_ok:
            self._trace("no OK from any higher node -> I am the coordinator")
            self._become_leader("BULLY")
            return
        await asyncio.sleep(settings.ELECTION_COORD_TIMEOUT)
        if self.current == rnd and self.in_progress:          # the higher node died meanwhile
            self.in_progress = False
            self.start_bully("OK received but no COORDINATOR followed")

    def on_election(self, msg, payload):
        if settings.PRIORITY[msg.sender] < self.priority:
            self._send(msg.sender, "OK", {}, "(I am alive and higher)")
            self.start_bully(f"ELECTION received from {msg.sender}")

    def on_ok(self, msg, payload):
        self.got_ok = True
        self._trace(f"<- {msg.sender}: OK (a higher node takes over)")

    def on_coordinator(self, msg, payload):
        new = payload["leader"]
        self._trace(f"<- {msg.sender}: COORDINATOR {new}")
        self.in_progress = False
        self.current = next(self.round)                       # cancels pending timers
        if settings.PRIORITY[new] < self.priority:            # a lower node claims leadership
            self.start_bully(f"lower node {new} announced itself")
        else:
            self._set_leader(new, payload.get("algorithm", "BULLY"))

    def _become_leader(self, algorithm):
        self.in_progress = False
        self._set_leader(self.me, algorithm)
        for p in self.rt.peers:
            self._send(p, "COORDINATOR", {"leader": self.me, "algorithm": algorithm}, f"{self.me}")
        self.rt.publish("LEADER_ELECTED", f"{self.me} elected coordinator ({algorithm})", target="ALL",
                        leader=self.me, algorithm=algorithm)

    def _set_leader(self, leader, algorithm):
        changed = leader != self.leader
        self.leader = leader
        self.rt.local_event("LEADER", f"leader = {leader} ({algorithm})")
        if changed and leader == self.me:
            self.rt.modules["ledger"].on_become_leader()

    # ================================================================= Ring
    def _successors(self):
        i = settings.RING.index(self.me)
        return settings.RING[i + 1:] + settings.RING[:i]

    async def _forward(self, kind, payload):
        for succ in self._successors():
            if await self._send(succ, kind, payload, str(payload.get("ids", ""))):
                return True
            self._trace(f"successor {succ} unreachable - skipped")
        return False

    def start_ring(self):
        if not self.rt.available:
            return
        self._trace("RING election started")
        self.rt.local_event("ELECTION_START", "Ring election started")
        asyncio.create_task(self._forward("RING_ELECTION", {"initiator": self.me, "ids": [self.me]}))

    def on_ring_election(self, msg, payload):
        ids = payload["ids"]
        if self.me in ids:                                    # went all the way round
            leader = max(ids, key=lambda n: settings.PRIORITY[n])
            self._trace(f"RING_ELECTION returned with {ids} -> leader {leader}")
            self._set_leader(leader, "RING")
            asyncio.create_task(self._forward("RING_COORDINATOR",
                                              {"leader": leader, "ids": ids, "initiator": self.me}))
            self.rt.publish("LEADER_ELECTED", f"{leader} elected coordinator (RING, participants {', '.join(ids)})",
                            target="ALL", leader=leader, algorithm="RING")
        else:
            asyncio.create_task(self._forward("RING_ELECTION", {**payload, "ids": ids + [self.me]}))

    def on_ring_coordinator(self, msg, payload):
        self._set_leader(payload["leader"], "RING")
        if payload["initiator"] != self.me:
            asyncio.create_task(self._forward("RING_COORDINATOR", payload))

    # ========================================================= integration
    def on_peer_suspected(self, peer):
        if peer == self.leader:
            self.start_bully(f"leader {peer} suspected (beacons missing)")

    def on_recover(self):
        self.in_progress = False
        asyncio.get_running_loop().call_later(1.0, self.start_bully, "node recovered")
