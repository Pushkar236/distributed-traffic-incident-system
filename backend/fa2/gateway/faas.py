"""A small Function-as-a-Service platform inside the gateway (local emulation, not AWS).

What is real here
    * every function instance is a separate OS process (ProcessPoolExecutor, spawn), so a
      function cannot share memory with the gateway or with other instances
    * cold start: the first invocation of an idle function starts a new process and runs
      its init phase - the measured init time is reported (typically 100-1000 ms)
    * warm start: later invocations reuse the instance (same PID, ~1 ms overhead)
    * scale out: concurrent invocations get extra instances, up to MAX_INSTANCES
    * scale to zero: instances idle for KEEP_WARM seconds are shut down
    * triggers: HTTP (POST /api/fa2/faas/invoke/<fn>) and events from the FA-1 broker
      (INCIDENT_CREATED -> classify_incident, RESOLVED -> incident_report -> DFS)
    * metering: duration and Lambda-style GB-seconds (illustrative, nothing is billed)
"""
import asyncio
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor

from . import faas_functions

KEEP_WARM = 60.0
MAX_INSTANCES = 2
MEMORY_MB = 128

FUNCTION_INFO = {
    "classify_incident": {"trigger": "event INCIDENT_CREATED (broker) + HTTP",
                          "description": "priority score P1/P2/P3 and recommended actions"},
    "incident_report": {"trigger": "event INCIDENT_UPDATED status=RESOLVED + HTTP",
                        "description": "renders the incident report; platform stores it in the DFS"},
    "congestion_advisory": {"trigger": "HTTP", "description": "advisories from live traffic densities"},
}


class Instance:
    def __init__(self, name):
        self.name = name
        self.executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        self.busy = False
        self.pid = None
        self.created = self.last_used = time.time()
        self.invocations = 0


class FaaSRuntime:
    def __init__(self):
        self.instances = {name: [] for name in faas_functions.FUNCTIONS}
        self.metrics = {name: {"invocations": 0, "cold_starts": 0, "errors": 0, "total_ms": 0.0, "gb_seconds": 0.0}
                        for name in faas_functions.FUNCTIONS}
        self.recent = []
        self.cond = asyncio.Condition()

    def start(self):
        asyncio.create_task(self._reaper())

    def summary(self):
        now = time.time()
        return {"functions": [{"name": n, **FUNCTION_INFO[n], **self.metrics[n],
                               "avg_ms": round(self.metrics[n]["total_ms"] / self.metrics[n]["invocations"], 1)
                               if self.metrics[n]["invocations"] else None,
                               "warm_instances": [{"pid": i.pid, "busy": i.busy, "invocations": i.invocations,
                                                   "idle_s": round(now - i.last_used, 1)} for i in self.instances[n]]}
                              for n in faas_functions.FUNCTIONS],
                "keep_warm_s": KEEP_WARM, "max_instances": MAX_INSTANCES, "memory_mb": MEMORY_MB,
                "recent": self.recent[-12:]}

    async def _acquire(self, name):
        """Idle warm instance if there is one, else a new (cold) one, else wait."""
        async with self.cond:
            while True:
                for inst in self.instances[name]:
                    if not inst.busy:
                        inst.busy = True
                        return inst, False
                if len(self.instances[name]) < MAX_INSTANCES:
                    inst = Instance(name)
                    inst.busy = True
                    self.instances[name].append(inst)
                    return inst, True
                await self.cond.wait()

    async def _release(self, inst):
        async with self.cond:
            inst.busy = False
            inst.last_used = time.time()
            self.cond.notify_all()

    async def invoke(self, name, event, trigger="http"):
        if name not in faas_functions.FUNCTIONS:
            raise KeyError(name)
        loop = asyncio.get_running_loop()
        inst, cold = await self._acquire(name)
        init_ms = 0.0
        started = time.perf_counter()
        try:
            if cold:
                t0 = time.perf_counter()
                info = await loop.run_in_executor(inst.executor, faas_functions.init)
                init_ms = (time.perf_counter() - t0) * 1000
                inst.pid = info["pid"]
            t1 = time.perf_counter()
            out = await loop.run_in_executor(inst.executor, faas_functions.run, name, event)
            run_ms = (time.perf_counter() - t1) * 1000
            error = None
        except Exception as exc:                    # the function raised, or its process died
            out, run_ms, error = {"result": None, "pid": inst.pid}, 0.0, repr(exc)
        finally:
            inst.invocations += 1
            await self._release(inst)
        total_ms = (time.perf_counter() - started) * 1000
        m = self.metrics[name]
        m["invocations"] += 1
        m["cold_starts"] += cold
        m["errors"] += error is not None
        m["total_ms"] += total_ms
        billed = max(1, round(run_ms))                                 # per-ms metering
        m["gb_seconds"] = round(m["gb_seconds"] + billed / 1000 * MEMORY_MB / 1024, 6)
        record = {"function": name, "trigger": trigger, "cold_start": cold, "init_ms": round(init_ms, 1),
                  "run_ms": round(run_ms, 2), "total_ms": round(total_ms, 1), "pid": out.get("pid"),
                  "error": error, "result": out.get("result"), "t": time.time()}
        self.recent.append(record)
        del self.recent[:-30]
        return record

    async def _reaper(self):
        """Scale to zero: destroy instances idle longer than KEEP_WARM."""
        while True:
            await asyncio.sleep(5)
            now = time.time()
            async with self.cond:
                for name, insts in self.instances.items():
                    for inst in list(insts):
                        if not inst.busy and now - inst.last_used > KEEP_WARM:
                            insts.remove(inst)
                            inst.executor.shutdown(wait=False, cancel_futures=True)

    def shutdown(self):
        for insts in self.instances.values():
            for inst in insts:
                inst.executor.shutdown(wait=False, cancel_futures=True)
