"""Naming service for the distributed objects (like the RMI registry / CORBA naming service).

bind():   the nodes' object adapters are asked for the objects they host; each object
          is registered under a readable name -> object reference.
lookup(): name or reference -> RemoteProxy. The caller then invokes methods on the proxy
          without knowing which node hosts the object (location transparency).
"""
import time

from ...rpc.client import NodeUnavailable
from .. import settings
from ..objects import RemoteObjectError, RemoteProxy, parse_ref
from ..rpc import CommandFailed


class NamingService:
    def __init__(self, pool):
        self.pool = pool
        self.registry = {}            # name -> {"ref", "interface", "methods", "node"}
        self.invocations = []

    async def bind_all(self):
        for node in settings.NODE_IDS:
            try:
                for obj in await self.pool.command(node, "objects_list"):
                    name = obj["ref"].rsplit("/", 1)[1]
                    self.registry[name] = obj
            except (NodeUnavailable, CommandFailed):
                pass                    # objects of an offline node stay registered from before
        return self.registry

    def lookup(self, name_or_ref):
        ref = self.registry[name_or_ref]["ref"] if name_or_ref in self.registry else name_or_ref
        parse_ref(ref)
        return RemoteProxy(ref, self.pool, caller="gateway-client")

    async def invoke(self, name_or_ref, method, args):
        proxy = self.lookup(name_or_ref)
        started = time.perf_counter()
        error, result = None, None
        try:
            result = await getattr(proxy, method)(**(args or {}))
        except RemoteObjectError as exc:
            error = str(exc)
        record = {"ref": proxy.ref, "method": method, "args": args, "ok": error is None, "result": result,
                  "error": error, "host": proxy.node_id, "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                  "request_bytes": proxy.last_request_bytes, "reply_bytes": proxy.last_reply_bytes, "t": time.time()}
        self.invocations.append(record)
        del self.invocations[:-20]
        return record
