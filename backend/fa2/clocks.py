"""Clocks: a simulated physical clock, Lamport's scalar clock and the vector clock.

PhysicalClock
    reading = real time + offset + drift * elapsed. Offset/drift are set per node in
    settings.CLOCK_SKEW so clock synchronization has something real to correct.

Lamport clock (scalar) - implementation rules (Lamport 1978):
    IR1  before every event (internal, send, receive):   L = L + 1
    IR2  on receiving a message with timestamp t:          L = max(L, t) + 1
    => if a -> b (happened-before) then L(a) < L(b)   (the converse does not hold)

Vector clock (Fidge / Mattern) - one entry per node:
    before every event:   V[i] = V[i] + 1
    on receive of Vm:     V[k] = max(V[k], Vm[k]) for all k, then V[i] = V[i] + 1
    => a -> b  iff  V(a) < V(b);  otherwise the events are concurrent (a || b)
"""
import time


class PhysicalClock:
    def __init__(self, offset=0.0, drift_ppm=0.0):
        self.base_real = time.time()
        self.base_value = self.base_real + offset
        self.drift = drift_ppm / 1_000_000

    def now(self):
        elapsed = time.time() - self.base_real
        return self.base_value + elapsed * (1 + self.drift)

    def adjust(self, delta):
        """Step the clock by delta seconds (real NTP would slew gradually; a step is clearer to show)."""
        value = self.now() + delta
        self.base_real, self.base_value = time.time(), value

    def set_skew(self, offset, drift_ppm):
        self.base_real = time.time()
        self.base_value = self.base_real + offset
        self.drift = drift_ppm / 1_000_000

    def offset_from_real(self):
        return self.now() - time.time()


class LogicalClocks:
    """Lamport + vector clock of one node, updated together on every event."""

    def __init__(self, node_id, node_ids):
        self.node_id = node_id
        self.lamport = 0
        self.vector = {n: 0 for n in node_ids}

    def tick(self):                          # internal or send event
        self.lamport += 1
        self.vector[self.node_id] += 1

    def merge(self, lamport, vector):        # receive event
        self.lamport = max(self.lamport, lamport) + 1
        for k, v in vector.items():
            if k in self.vector:
                self.vector[k] = max(self.vector[k], v)
        self.vector[self.node_id] += 1

    def snapshot(self):
        return self.lamport, dict(self.vector)


def compare_vectors(a, b):
    """Return 'before' (a -> b), 'after' (b -> a), 'equal' or 'concurrent'."""
    keys = set(a) | set(b)
    le = all(a.get(k, 0) <= b.get(k, 0) for k in keys)
    ge = all(a.get(k, 0) >= b.get(k, 0) for k in keys)
    if le and ge:
        return "equal"
    if le:
        return "before"
    if ge:
        return "after"
    return "concurrent"
