"""Causal ordering of broadcasts with vector timestamps (Birman-Schiper-Stephenson).

Each node keeps VT[k] = number of broadcasts from node k it has DELIVERED.
    broadcast:  VT[me] += 1, send (m, VT) to all peers, deliver to itself
    receive m from j with timestamp VTm: deliver only when
        VTm[j] == VT[j] + 1                 (m is the next message from j)
        VTm[k] <= VT[k]  for all k != j     (we already have everything j had seen)
    otherwise m is BUFFERED; after every delivery the buffer is re-checked.

Used for incident events: when a node creates or updates an incident (FA-1 workflow),
it causally broadcasts INCIDENT_CREATED / INCIDENT_UPDATED, so no node can ever see
"INC-1004 RESOLVED" before "INC-1004 created". The demo command delays one channel to
force a causal-order violation that the buffer then repairs.
"""
import asyncio

from . import settings

DEMO_DELAY = 3.0          # seconds the Pune -> Nashik link is slowed during the demo


class CausalBroadcast:
    def __init__(self, rt):
        self.rt = rt
        self.vt = {n: 0 for n in settings.NODE_IDS}
        self.buffer = []          # [(sender, payload)] waiting for causally earlier messages
        self.delivered = []       # delivery order (most recent last)
        self.received = []        # arrival order, to compare with delivery order
        self.handlers = {"CB_MSG": self.on_message}
        self.commands = {"causal_broadcast": self.cmd_broadcast, "causal_demo": self.demo,
                         "causal_reset_log": self.reset_log}

    def state(self):
        return {"vt": self.vt, "buffered": [self._label(s, p) | {"waiting_for": self._missing(s, p)}
                                            for s, p in self.buffer],
                "delivered": self.delivered[-15:], "received": self.received[-15:]}

    @staticmethod
    def _label(sender, p):
        return {"from": sender, "topic": p["topic"], "seq": p["vt"][sender], "vt": p["vt"],
                "text": p["data"].get("text", "")}

    # ----------------------------------------------------------- broadcast
    def broadcast(self, topic, data):
        self.vt[self.rt.node_id] += 1
        payload = {"topic": topic, "data": data, "vt": dict(self.vt),
                   "_text": f"{topic}: {data.get('text', '')}"}
        self.rt.multicast("CB_MSG", payload, text=f"causal broadcast {topic} #{self.vt[self.rt.node_id]}")
        self._record_delivery(self.rt.node_id, payload, buffered=False)

    def cmd_broadcast(self, topic="NOTE", text=""):
        self.broadcast(topic, {"text": text})
        return {"vt": self.vt}

    # ------------------------------------------------------------- receive
    def on_message(self, msg, payload):
        sender = msg.sender
        self.received.append(self._label(sender, payload))
        if self._deliverable(sender, payload["vt"]):
            self._deliver(sender, payload, buffered=False)
            self._drain_buffer()
        else:
            self.buffer.append((sender, payload))
            missing = self._missing(sender, payload)
            self.rt.local_event("CB_BUFFERED", f"{payload['topic']} from {sender} buffered, waiting for {missing}")
            self.rt.publish("CAUSAL_BUFFERED", f"{payload['topic']} from {sender} held back - "
                                               f"causally earlier message(s) {missing} not delivered yet",
                            target=self.rt.node_id, topic=payload["topic"], vt=payload["vt"])

    def _deliverable(self, j, vtm):
        return vtm[j] == self.vt[j] + 1 and all(vtm[k] <= self.vt[k] for k in vtm if k != j)

    def _missing(self, j, p):
        vtm = p["vt"]
        need = [f"{j}#{self.vt[j] + 1}"] if vtm[j] > self.vt[j] + 1 else []
        need += [f"{k}#{self.vt[k] + 1}..{vtm[k]}" for k in vtm if k != j and vtm[k] > self.vt[k]]
        return need

    def _drain_buffer(self):
        progress = True
        while progress:
            progress = False
            for item in list(self.buffer):
                sender, payload = item
                if self._deliverable(sender, payload["vt"]):
                    self.buffer.remove(item)
                    self._deliver(sender, payload, buffered=True)
                    progress = True

    def _deliver(self, sender, payload, buffered):
        self.vt[sender] = payload["vt"][sender]
        self._record_delivery(sender, payload, buffered)
        if buffered:
            self.rt.publish("CAUSAL_DELIVERED", f"{payload['topic']} from {sender} delivered from buffer "
                                                f"(causal order restored)", target=self.rt.node_id,
                            topic=payload["topic"], vt=payload["vt"])
        data = payload["data"]
        # demo: the chosen node reacts to the alert -> its reply is causally after the alert
        if payload["topic"] == "DEMO_ALERT" and data.get("reply_from") == self.rt.node_id:
            self.broadcast("DEMO_ACK", {"text": f"{self.rt.node_id} acknowledges alert and dispatches support"})

    def _record_delivery(self, sender, payload, buffered):
        entry = self._label(sender, payload) | {"after_buffering": buffered}
        self.delivered.append(entry)
        self.rt.local_event("CB_DELIVER", f"deliver {payload['topic']} from {sender} vt={payload['vt']}")
        del self.delivered[:-100], self.received[:-100]

    # ---------------------------------------------------------------- demo
    async def demo(self, slow_peer="NSK-TR-01", reply_from="MUM-TR-01"):
        """Slow this node's link to slow_peer, broadcast an alert; reply_from answers it.
        slow_peer receives the (causally later) answer first and must buffer it."""
        transport = self.rt.transport
        transport.delay[slow_peer] = DEMO_DELAY
        self.broadcast("DEMO_ALERT", {"text": f"Multi-vehicle pile-up reported by {self.rt.node_id}",
                                      "reply_from": reply_from})
        asyncio.get_running_loop().call_later(DEMO_DELAY + 0.5, transport.delay.__setitem__, slow_peer, 0.0)
        self.rt.publish("CAUSAL_DEMO", f"{self.rt.node_id} broadcast DEMO_ALERT; link to {slow_peer} slowed by "
                                       f"{DEMO_DELAY:.0f}s, {reply_from} will reply", target="ALL")
        return {"vt": self.vt, "slow_peer": slow_peer, "delay_s": DEMO_DELAY}

    def reset_log(self):
        self.delivered.clear()
        self.received.clear()
        return {"ok": True}
