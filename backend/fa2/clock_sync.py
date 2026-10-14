"""Physical clock synchronization (node side).

Cristian's algorithm (external synchronization)
    The node asks a time server (the gateway, whose clock is the host's real clock and
    plays the role of the UTC source) for the time and measures the round trip:
        T1 = local time request sent      t2/t3 = server receive / send time
        T4 = local time reply received    RTT = T4 - T1
        Cristian:  new time = t3 + RTT / 2
    It assumes the request and reply take equally long. Several samples are taken and the
    one with the smallest RTT is used, since it has the least room for asymmetry.
    The same four timestamps also give the NTP estimates that are reported alongside:
        offset theta = ((t2 - T1) + (t3 - T4)) / 2      delay delta = (T4 - T1) - (t3 - t2)

Berkeley algorithm (internal synchronization)
    The time daemon (the elected leader) polls every node's clock with GetTime, corrects
    each reading by half the round trip, ignores readings far from the median (faulty
    clocks), averages the rest, and sends every node the *adjustment* it must apply
    (CLOCK_ADJUST). Clocks end up agreeing with each other, not necessarily with UTC.
"""
import random
import statistics
import time

import httpx

from ..rpc import fa2_sync_pb2 as spb
from ..rpc.client import NodeUnavailable
from . import settings
from .errors import CommandError

OUTLIER_LIMIT = 5.0       # Berkeley: readings further than this (s) from the median are ignored


class ClockSync:
    def __init__(self, rt):
        self.rt = rt
        self.last = None
        self.handlers = {"CLOCK_ADJUST": self.on_adjust}
        self.commands = {"clock_cristian": self.cristian, "clock_berkeley": self.berkeley,
                         "clock_reskew": self.reskew}

    def state(self):
        return {"last_sync": self.last}

    # ------------------------------------------------------------ Cristian
    async def cristian(self, samples=4):
        """Several requests on one connection; the sample with the smallest RTT is used
        (Cristian's own refinement: the shortest round trip has the least delay asymmetry)."""
        clock = self.rt.clock
        before = clock.offset_from_real()
        measured = []
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                for _ in range(samples):
                    T1 = clock.now()
                    reply = (await client.get(f"{settings.GATEWAY_URL}/internal/fa2/time")).json()
                    T4 = clock.now()
                    measured.append((T4 - T1, T1, T4, reply))
        except Exception as exc:
            raise CommandError(f"time server unreachable: {exc}")
        _, T1, T4, reply = min(measured, key=lambda m: m[0])
        t2, t3 = reply["t2"], reply["t3"]
        rtt = T4 - T1
        cristian_time = t3 + rtt / 2
        delta = cristian_time - T4
        clock.adjust(delta)
        self.last = {"algorithm": "CRISTIAN", "rtt_ms": round(rtt * 1000, 2), "samples": len(measured),
                     "all_rtts_ms": [round(m[0] * 1000, 1) for m in measured],
                     "adjustment_ms": round(delta * 1000, 2),
                     "ntp_offset_ms": round(((t2 - T1) + (t3 - T4)) / 2 * 1000, 2),
                     "ntp_delay_ms": round(((T4 - T1) - (t3 - t2)) * 1000, 2),
                     "error_before_ms": round(before * 1000, 1),
                     "error_after_ms": round(clock.offset_from_real() * 1000, 1), "at": time.time()}
        self.rt.local_event("CLOCK_SYNC", f"Cristian: clock stepped by {delta * 1000:+.1f} ms (RTT {rtt * 1000:.1f} ms)")
        self.rt.publish("CLOCK_SYNC", f"Cristian sync with time server: adjusted {delta * 1000:+.1f} ms, "
                                      f"RTT {rtt * 1000:.1f} ms", target="COORDINATOR", **self.last)
        return self.last

    # ------------------------------------------------------------ Berkeley
    async def berkeley(self):
        rt = self.rt
        readings = {rt.node_id: {"diff": 0.0, "rtt_ms": 0.0}}
        for peer in rt.peers:
            T0 = rt.clock.now()
            try:
                reply = await rt.transport.pool.call(peer, "GetTime", spb.TimeRequest(requester=rt.node_id), timeout=2.0)
            except NodeUnavailable:
                readings[peer] = {"diff": None, "rtt_ms": None, "note": "no answer - left out"}
                continue
            T1 = rt.clock.now()
            estimate = reply.time + (T1 - T0) / 2             # peer's clock "now", RTT-compensated
            readings[peer] = {"diff": estimate - T1, "rtt_ms": round((T1 - T0) * 1000, 2)}

        diffs = [r["diff"] for r in readings.values() if r["diff"] is not None]
        median = statistics.median(diffs)
        good = [d for d in diffs if abs(d - median) <= OUTLIER_LIMIT]
        average = sum(good) / len(good)
        for node_id, r in readings.items():
            if r["diff"] is None:
                continue
            r["outlier"] = abs(r["diff"] - median) > OUTLIER_LIMIT
            r["adjustment"] = average - r["diff"]
            r["diff_ms"] = round(r["diff"] * 1000, 1)
            r["adjustment_ms"] = round(r["adjustment"] * 1000, 1)
            if node_id == rt.node_id:
                rt.clock.adjust(r["adjustment"])
                rt.local_event("CLOCK_ADJUST", f"Berkeley daemon adjusts itself {r['adjustment_ms']:+.1f} ms")
            else:
                rt.send(node_id, "CLOCK_ADJUST", {"delta": r["adjustment"], "daemon": rt.node_id},
                        text=f"Berkeley adjustment {r['adjustment_ms']:+.1f} ms")
            del r["diff"], r["adjustment"]
        self.last = {"algorithm": "BERKELEY", "daemon": rt.node_id, "readings": readings,
                     "average_ms": round(average * 1000, 1), "at": time.time()}
        rt.publish("CLOCK_SYNC", f"Berkeley round by time daemon {rt.node_id}: average offset "
                                 f"{average * 1000:+.1f} ms, adjustments sent", target="ALL", **self.last)
        return self.last

    def on_adjust(self, msg, payload):
        self.rt.clock.adjust(payload["delta"])
        self.last = {"algorithm": "BERKELEY", "daemon": payload["daemon"],
                     "adjustment_ms": round(payload["delta"] * 1000, 1), "at": time.time()}

    # ---------------------------------------------------------- demo knob
    def reskew(self, randomize=True):
        if randomize:
            offset, drift = random.uniform(-3, 3), random.choice([-1, 1]) * random.uniform(300, 1500)
        else:
            offset, drift = settings.CLOCK_SKEW[self.rt.node_id]
        self.rt.clock.set_skew(offset, drift)
        self.last = None
        return {"offset_ms": round(offset * 1000, 1), "drift_ppm": round(drift)}
