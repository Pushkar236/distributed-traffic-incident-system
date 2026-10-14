"""Distributed mutual exclusion for a shared regional resource.

Shared resource: the regional air-ambulance dispatch register, a file in the FA-2
distributed file system (settings.SHARED_REGISTER). The critical section is a
read-modify-write of that file: read it, work for ME_CS_WORK seconds, append one line,
write it back. Without mutual exclusion two nodes read the same version and one
update is lost - so lost updates are measurable proof of a broken (or missing) algorithm.

Request priority / identifier:  ReqID = (sequence number, PID), PID = Pune 1, Mumbai 2,
Nashik 3. Smaller tuple = higher priority (as in Lodha & Kshemkalyani, IEEE TPDS 2000).

LAMPORT (1978)                 3(N-1) messages per CS
    request: put (ts,i) in own queue, REQUEST to all. On REQUEST: queue it, REPLY.
    enter:   own request heads the queue AND a message with a larger timestamp has been
             received from every other site.        exit: RELEASE to all.
RICART-AGRAWALA (1981)         2(N-1) messages per CS
    On REQUEST: reply at once unless requesting with higher priority (then defer).
    enter: N-1 REPLYs.         exit: REPLY to every deferred request.
LODHA-KSHEMKALYANI (2000)      between N-1 and 2(N-1) messages per CS, fair (timestamp order)
    Implemented as Fig. 2 of the paper:
    InvMutEx  SN = highest+1; LRQ = [own]; REQUEST to all; RV = [0..] with RV[i] = 1
    RcvReq    highest = max(highest, SN); if requesting:
                  RV[j]==0 -> insert in LRQ, RV[j] = 1, CheckExecuteCS
                  RV[j]==1 -> defer until own CS is done
              else: REPLY(last satisfied ReqID)
    RcvReply / RcvFlush(Rj)   RV[j] = 1; remove LRQ entries with priority >= Rj; CheckExecuteCS
    FinCS     FLUSH(Ri) to the next request in LRQ; REPLY(Ri) to every deferred request
    CheckExecuteCS: all RV == 1 and own request at the head of LRQ
    A concurrent REQUEST doubles as a reply, and one FLUSH replaces several REPLYs.
NONE        no coordination at all - used to show lost updates.
These algorithms assume reliable processes: a crashed node blocks requests (timeout + reset).
"""
import asyncio
import itertools
import time

import httpx

from . import settings
from .errors import CommandError

PID = {n: i + 1 for i, n in enumerate(settings.NODE_IDS)}       # Pune 1, Mumbai 2, Nashik 3


class _Algorithm:
    """Shared plumbing: message counting and the hand-off to the critical section."""
    name = ""

    def __init__(self, me):
        self.me = me
        self.peers = me.rt.peers
        self.pid = PID[me.rt.node_id]
        self.requesting = False
        self.my_req = None

    def send(self, peer, kind, payload):
        self.me.count(kind)
        self.me.rt.send(peer, kind, payload, text=str(payload.get("req", payload.get("ts", ""))))

    def multicast(self, kind, payload):
        for _ in self.peers:
            self.me.count(kind)
        self.me.rt.multicast(kind, payload, text=str(payload))

    def view(self):
        return {}


class Lamport(_Algorithm):
    name = "LAMPORT"

    def __init__(self, me):
        super().__init__(me)
        self.queue = []                                   # sorted [(ts, pid, node)]
        self.last_from = {p: (0, PID[p]) for p in self.peers}

    def request(self):
        rt = self.me.rt
        rt.clocks.tick()
        self.my_req = (rt.clocks.lamport, self.pid, rt.node_id)
        rt.record("LOCAL", "ME_REQUEST", text=f"Lamport request ts={self.my_req[0]}")
        self.requesting = True
        self.queue = sorted(self.queue + [self.my_req])
        self.multicast("LAM_REQUEST", {"ts": self.my_req[0]})

    def on_message(self, msg, p):
        j = msg.sender
        self.last_from[j] = max(self.last_from[j], (msg.lamport, PID[j]))
        if msg.kind == "LAM_REQUEST":
            self.queue = sorted(self.queue + [(p["ts"], PID[j], j)])
            self.send(j, "LAM_REPLY", {"ts": p["ts"]})
        elif msg.kind == "LAM_RELEASE":
            self.queue = [r for r in self.queue if r[2] != j]
        self.check()

    def check(self):
        if (self.requesting and not self.me.in_cs and self.queue and self.queue[0] == self.my_req
                and all(self.last_from[p] > self.my_req[:2] for p in self.peers)):
            self.me.enter_cs()

    def exit(self):
        self.queue = [r for r in self.queue if r != self.my_req]
        self.requesting = False
        self.multicast("LAM_RELEASE", {"ts": self.my_req[0]})

    def view(self):
        return {"request_queue": [list(r) for r in self.queue],
                "last_timestamp_from": {p: list(v) for p, v in self.last_from.items()}}


class RicartAgrawala(_Algorithm):
    name = "RICART_AGRAWALA"

    def __init__(self, me):
        super().__init__(me)
        self.highest = 0
        self.replied = set()
        self.deferred = []

    def request(self):
        self.requesting = True
        self.my_req = (self.highest + 1, self.pid)
        self.replied = set()
        self.me.rt.local_event("ME_REQUEST", f"RA request {self.my_req}")
        self.multicast("RA_REQUEST", {"req": list(self.my_req)})

    def on_message(self, msg, p):
        j = msg.sender
        if msg.kind == "RA_REQUEST":
            req = tuple(p["req"])
            self.highest = max(self.highest, req[0])
            if self.requesting and self.my_req < req:      # mine has priority: defer the reply
                self.deferred.append(j)
            else:
                self.send(j, "RA_REPLY", {"req": list(req)})
        elif msg.kind == "RA_REPLY":
            self.replied.add(j)
            if self.requesting and not self.me.in_cs and len(self.replied) == len(self.peers):
                self.me.enter_cs()

    def exit(self):
        self.requesting = False
        for j in self.deferred:
            self.send(j, "RA_REPLY", {"req": list(self.my_req)})
        self.deferred = []

    def view(self):
        return {"replies_from": sorted(self.replied), "deferred": self.deferred, "highest_sn": self.highest}


class LodhaKshemkalyani(_Algorithm):
    name = "LODHA_KSHEMKALYANI"

    def __init__(self, me):
        super().__init__(me)
        self.highest = 0
        self.lrq = []                                     # sorted [(sn, pid)]
        self.rv = {n: 0 for n in settings.NODE_IDS}
        self.deferred = []
        self.last_satisfied = (0, self.pid)

    def request(self):                                    # InvMutEx
        self.requesting = True
        self.my_req = (self.highest + 1, self.pid)
        self.lrq = [self.my_req]
        self.me.rt.local_event("ME_REQUEST", f"LK request {self.my_req}")
        self.multicast("LK_REQUEST", {"req": list(self.my_req)})
        self.rv = {n: 0 for n in settings.NODE_IDS}
        self.rv[self.me.rt.node_id] = 1

    def on_message(self, msg, p):
        j, req = msg.sender, tuple(p["req"])
        if msg.kind == "LK_REQUEST":                      # RcvReq
            self.highest = max(self.highest, req[0])
            if self.requesting:
                if self.rv[j] == 0:                       # concurrent request: it doubles as a reply
                    self.lrq = sorted(self.lrq + [req])
                    self.rv[j] = 1
                    self.check()
                else:                                     # not concurrent: answer after our CS
                    self.deferred.append(j)
            else:
                self.send(j, "LK_REPLY", {"req": list(self.last_satisfied)})
        else:                                             # RcvReply / RcvFlush
            self.rv[j] = 1
            self.lrq = [r for r in self.lrq if r > req]   # drop priority >= Rj (tuple <= Rj)
            self.check()

    def check(self):                                      # CheckExecuteCS
        if (self.requesting and not self.me.in_cs and all(self.rv.values())
                and self.lrq and self.lrq[0] == self.my_req):
            self.me.enter_cs()

    def exit(self):                                       # FinCS
        idx = self.lrq.index(self.my_req) if self.my_req in self.lrq else -1
        nxt = self.lrq[idx + 1] if 0 <= idx < len(self.lrq) - 1 else None
        if nxt:
            node = settings.NODE_IDS[nxt[1] - 1]
            self.send(node, "LK_FLUSH", {"req": list(self.my_req)})
        for j in self.deferred:
            self.send(j, "LK_REPLY", {"req": list(self.my_req)})
        self.deferred = []
        self.last_satisfied = self.my_req
        self.requesting = False

    def view(self):
        return {"lrq": [list(r) for r in self.lrq], "rv": self.rv, "deferred": self.deferred,
                "highest_sn": self.highest, "last_satisfied": list(self.last_satisfied)}


class NoCoordination(_Algorithm):
    name = "NONE"

    def request(self):
        self.requesting = True
        self.my_req = (0, self.pid)
        self.me.enter_cs()

    def on_message(self, msg, p):
        pass

    def exit(self):
        self.requesting = False


ALGORITHMS = {cls.name: cls for cls in (Lamport, RicartAgrawala, LodhaKshemkalyani, NoCoordination)}
KIND_PREFIX = {"LAM_": "LAMPORT", "RA_": "RICART_AGRAWALA", "LK_": "LODHA_KSHEMKALYANI"}


class MutualExclusion:
    def __init__(self, rt):
        self.rt = rt
        self.algo = ALGORITHMS[settings.ME_DEFAULT](self)
        self.in_cs = False
        self.counts = {}
        self.history = []                  # [{"enter", "exit", "req", "line"}]
        self.request_ids = itertools.count(1)
        self.current_request = 0
        self.status = "IDLE"
        kinds = ["LAM_REQUEST", "LAM_REPLY", "LAM_RELEASE", "RA_REQUEST", "RA_REPLY",
                 "LK_REQUEST", "LK_REPLY", "LK_FLUSH"]
        self.handlers = {k: self.dispatch for k in kinds}
        self.commands = {"me_request": self.cmd_request, "me_set_algorithm": self.cmd_set_algorithm,
                         "me_reset": self.cmd_reset}

    def count(self, kind):
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def state(self):
        return {"algorithm": self.algo.name, "status": self.status, "requesting": self.algo.requesting,
                "in_cs": self.in_cs, "my_request": list(self.algo.my_req) if self.algo.my_req else None,
                "messages_sent": self.counts, "cs_count": len(self.history),
                "history": self.history[-10:], **self.algo.view()}

    def dispatch(self, msg, payload):
        expected = next(v for k, v in KIND_PREFIX.items() if msg.kind.startswith(k))
        if expected != self.algo.name:
            self.rt.local_event("ME_IGNORED", f"{msg.kind} from {msg.sender} ignored: running {self.algo.name}")
            return
        self.algo.on_message(msg, payload)

    # ------------------------------------------------------------ commands
    def cmd_set_algorithm(self, algorithm):
        if algorithm not in ALGORITHMS:
            raise CommandError(f"unknown algorithm {algorithm}")
        if self.algo.requesting:
            raise CommandError("cannot switch algorithm while a request is pending")
        self.algo = ALGORITHMS[algorithm](self)
        return {"algorithm": algorithm}

    def cmd_request(self):
        if self.algo.requesting:
            raise CommandError(f"{self.rt.node_id} already has a pending request")
        self.current_request = rid = next(self.request_ids)
        self.status = "WAITING"
        self.algo.request()
        asyncio.create_task(self._timeout(rid))
        return {"algorithm": self.algo.name, "request": list(self.algo.my_req)}

    def cmd_reset(self, algorithm=None):
        self.algo = ALGORITHMS[algorithm or self.algo.name](self)
        self.in_cs, self.status, self.counts, self.history = False, "IDLE", {}, []
        self.current_request = next(self.request_ids)
        return {"algorithm": self.algo.name}

    async def _timeout(self, rid):
        await asyncio.sleep(settings.ME_REQUEST_TIMEOUT)
        if self.current_request == rid and self.algo.requesting and not self.in_cs:
            self.status = "TIMED_OUT"
            self.algo.requesting = False
            self.rt.publish("ME_TIMEOUT", f"{self.rt.node_id}: CS request {self.algo.my_req} not granted in "
                                          f"{settings.ME_REQUEST_TIMEOUT:.0f}s (a peer is down?) - reset needed",
                            target="ALL")

    # --------------------------------------------------- critical section
    def enter_cs(self):
        self.in_cs = True
        self.status = "IN_CS"
        asyncio.create_task(self._critical_section())

    async def _critical_section(self):
        rt, req = self.rt, list(self.algo.my_req)
        enter = time.time()
        rt.local_event("CS_ENTER", f"{self.algo.name} request {req}")
        rt.publish("CS_ENTER", f"{rt.node_id} ENTERS critical section ({self.algo.name}, request {req})",
                   target="ALL", algorithm=self.algo.name, request=req, real_time=enter)
        line = None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"{settings.GATEWAY_URL}/internal/fa2/dfs"
                r = await client.get(url, params={"path": settings.SHARED_REGISTER})
                content = r.json().get("content", "") if r.status_code == 200 else ""
                await asyncio.sleep(settings.ME_CS_WORK)          # work done while holding the resource
                number = len(content.splitlines()) + 1
                line = (f"{number:03d} AIR-AMB-01 dispatched by {rt.node_id} "
                        f"[{self.algo.name} req={req} L={rt.clocks.lamport}]")
                await client.put(url, json={"path": settings.SHARED_REGISTER, "content": content + line + "\n",
                                            "writer": rt.node_id})
        except Exception as exc:
            rt.local_event("CS_ERROR", f"shared register not reachable: {exc}")
        leave = time.time()
        self.history.append({"enter": enter, "exit": leave, "request": req, "line": line})
        rt.local_event("CS_EXIT", f"request {req}")
        rt.publish("CS_EXIT", f"{rt.node_id} leaves critical section", target="ALL",
                   algorithm=self.algo.name, request=req, real_time=leave)
        self.in_cs = False
        self.status = "IDLE"
        self.algo.exit()
