"""Distributed deadlock: lock manager on response units + Chandy-Misra-Haas edge-chasing.

Lock manager (FA-2 only - it does NOT change the FA-1 unit status in SQLite)
    Every node is the lock manager of its own units (PT-*, MT-*, NT-*). A node that wants
    a unit sends LOCK_REQ to the owner; the owner answers LOCK_GRANT, or LOCK_WAIT(holder)
    when somebody else holds it. A waiting node therefore knows the edge
        me --waits-for--> holder          (wait-for graph, AND model, one request at a time)
    Circular wait across nodes = distributed deadlock no single node can see locally.

Edge-chasing detection (Knapp's class 2; Chandy-Misra-Haas AND model)
    A blocked node i sends PROBE(i, path) to the node it waits for.
    A node k receiving PROBE(i, ...):
        k == i               -> the probe came back: DEADLOCK, path = the cycle
        k blocked, first time for this probe -> forward PROBE(i, path+[k]) to its holder
        k not blocked        -> discard (no cycle through k)

Global-state detection (Knapp's class 4) uses the same wait_edges() recorded in a
Chandy-Lamport snapshot; the gateway searches that consistent wait-for graph for a cycle.

Resolution: the victim aborts - releases all its locks and withdraws its pending request.
"""
import itertools
import time

from .errors import CommandError

OWNER_BY_PREFIX = {"PT": "PUNE-TR-01", "MT": "MUM-TR-01", "NT": "NSK-TR-01"}


class DeadlockDetector:
    def __init__(self, rt):
        self.rt = rt
        self.me = rt.node_id
        self.holder = {}               # unit owned by me -> node holding the lock
        self.waitq = {}                # unit owned by me -> [nodes waiting]
        self.held = []                 # [(unit, owner)] locks this node holds
        self.waiting = None            # {"unit", "owner", "holder"} while blocked
        self.forwarded = set()         # (initiator, probe_id) already propagated
        self.probe_ids = itertools.count(1)
        self.detections = []
        self.trace = []
        self.handlers = {"LOCK_REQ": self.on_request, "LOCK_GRANT": self.on_grant, "LOCK_WAIT": self.on_wait,
                         "LOCK_RELEASE": self.on_release, "LOCK_CANCEL": self.on_cancel, "PROBE": self.on_probe}
        self.commands = {"dl_acquire": self.cmd_acquire, "dl_detect": self.cmd_detect,
                         "dl_abort": self.cmd_abort, "dl_reset": self.cmd_reset}

    def state(self):
        return {"held": [u for u, _ in self.held], "waiting": self.waiting,
                "lock_table": {u: {"holder": h, "queue": self.waitq.get(u, [])} for u, h in self.holder.items() if h},
                "detections": self.detections[-5:], "trace": self.trace[-12:]}

    def wait_edges(self):
        return [{"from": self.me, "to": self.waiting["holder"], "unit": self.waiting["unit"]}] if self.waiting else []

    def _trace(self, text):
        self.trace.append({"t": time.time(), "text": text})
        del self.trace[:-40]

    @staticmethod
    def owner_of(unit):
        owner = OWNER_BY_PREFIX.get(unit.split("-")[0])
        if owner is None:
            raise CommandError(f"unknown unit {unit}")
        return owner

    # -------------------------------------------------------- lock manager
    def cmd_acquire(self, unit):
        if self.waiting:
            raise CommandError(f"{self.me} is already blocked on {self.waiting['unit']}")
        owner = self.owner_of(unit)
        self._trace(f"request lock on {unit} (owner {owner})")
        if owner == self.me:
            self._handle_request(unit, self.me)
        else:
            self.rt.send(owner, "LOCK_REQ", {"unit": unit, "_text": unit}, text=f"lock {unit}")
        return {"requested": unit, "owner": owner}

    def _handle_request(self, unit, requester):
        if unit not in {u["unit_id"] for u in self.rt.node.db.list_units()}:
            return
        holder = self.holder.get(unit)
        if holder in (None, requester):
            self.holder[unit] = requester
            self._grant(unit, requester)
        else:
            self.waitq.setdefault(unit, []).append(requester)
            self._tell_wait(unit, requester, holder)

    def _grant(self, unit, node):
        if node == self.me:
            self.held.append((unit, self.me))
            self.waiting = None
            self._trace(f"lock {unit} granted (local)")
        else:
            self.rt.send(node, "LOCK_GRANT", {"unit": unit}, text=f"grant {unit}")

    def _tell_wait(self, unit, node, holder):
        if node == self.me:
            self.waiting = {"unit": unit, "owner": self.me, "holder": holder}
            self._trace(f"BLOCKED: {unit} held by {holder}")
        else:
            self.rt.send(node, "LOCK_WAIT", {"unit": unit, "holder": holder}, text=f"{unit} held by {holder}")

    def _release_local(self, unit):
        self.holder[unit] = None
        queue = self.waitq.get(unit, [])
        if queue:
            nxt = queue.pop(0)
            self.holder[unit] = nxt
            self._grant(unit, nxt)
            for other in queue:                       # the remaining waiters now wait for nxt
                self._tell_wait(unit, other, nxt)

    def on_request(self, msg, p):
        self._handle_request(p["unit"], msg.sender)

    def on_grant(self, msg, p):
        self.held.append((p["unit"], msg.sender))
        self.waiting = None
        self._trace(f"lock {p['unit']} granted by {msg.sender}")

    def on_wait(self, msg, p):
        self.waiting = {"unit": p["unit"], "owner": msg.sender, "holder": p["holder"]}
        self._trace(f"BLOCKED: {p['unit']} held by {p['holder']}")

    def on_release(self, msg, p):
        if self.holder.get(p["unit"]) == msg.sender:
            self._release_local(p["unit"])

    def on_cancel(self, msg, p):
        queue = self.waitq.get(p["unit"], [])
        if msg.sender in queue:
            queue.remove(msg.sender)

    # ------------------------------------------------- edge-chasing (CMH)
    def cmd_detect(self):
        if not self.waiting:
            raise CommandError(f"{self.me} is not blocked - only a waiting process initiates a probe")
        pid = next(self.probe_ids)
        self._trace(f"initiate PROBE#{pid} -> {self.waiting['holder']}")
        self.rt.send(self.waiting["holder"], "PROBE", {"initiator": self.me, "probe_id": pid, "path": [self.me],
                                                       "_text": f"probe({self.me},{self.me},{self.waiting['holder']})"},
                     text=f"probe({self.me}, {self.me}, {self.waiting['holder']})")
        return {"probe_id": pid, "sent_to": self.waiting["holder"]}

    def on_probe(self, msg, p):
        initiator, path = p["initiator"], p["path"] + [self.me]
        if initiator == self.me:
            cycle = " -> ".join(path)
            det = {"t": time.time(), "cycle": path, "method": "EDGE_CHASING", "probe_id": p["probe_id"]}
            self.detections.append(det)
            self._trace(f"DEADLOCK: probe returned, cycle {cycle}")
            self.rt.local_event("DEADLOCK_DETECTED", cycle)
            self.rt.publish("DEADLOCK_DETECTED", f"{self.me}: edge-chasing probe returned - cycle {cycle}",
                            target="ALL", cycle=path, method="EDGE_CHASING")
            return
        key = (initiator, p["probe_id"])
        if self.waiting and key not in self.forwarded:
            self.forwarded.add(key)
            nxt = self.waiting["holder"]
            self._trace(f"forward probe({initiator}, {self.me}, {nxt})")
            self.rt.send(nxt, "PROBE", {**p, "path": path, "_text": f"probe({initiator},{self.me},{nxt})"},
                         text=f"probe({initiator}, {self.me}, {nxt})")
        else:
            self._trace(f"discard probe from {initiator} (not blocked)")

    # ---------------------------------------------------------- resolution
    def cmd_abort(self):
        """This node is the victim: release every lock it holds and withdraw its request."""
        released = [u for u, _ in self.held]
        for unit, owner in self.held:
            if owner == self.me:
                self._release_local(unit)
            else:
                self.rt.send(owner, "LOCK_RELEASE", {"unit": unit}, text=f"release {unit}")
        self.held = []
        if self.waiting:
            w, self.waiting = self.waiting, None
            if w["owner"] == self.me:
                queue = self.waitq.get(w["unit"], [])
                if self.me in queue:
                    queue.remove(self.me)
            else:
                self.rt.send(w["owner"], "LOCK_CANCEL", {"unit": w["unit"]}, text=f"cancel {w['unit']}")
        self._trace(f"ABORTED as victim, released {released}")
        self.rt.publish("DEADLOCK_RESOLVED", f"{self.me} aborted as victim, released {', '.join(released) or 'nothing'}",
                        target="ALL", victim=self.me)
        return {"victim": self.me, "released": released}

    def cmd_reset(self):
        self.holder, self.waitq, self.held, self.waiting = {}, {}, [], None
        self.forwarded.clear()
        self.trace.clear()
        return {"ok": True}
