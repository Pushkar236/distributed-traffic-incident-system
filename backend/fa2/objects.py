"""Distributed object-based system: remote objects hosted by the regional nodes.

Same structure as Java RMI / CORBA, written small:

    client code ---> proxy (stub) ---Invoke RPC---> object adapter + skeleton ---> servant
                     marshals args (JSON)           unmarshals, dispatches,      the real object
                                                    marshals the result          (state lives here)

* Object reference  fa2obj://<node-id>/<interface>/<object-id>   - says where an object lives
* Interface         INTERFACES = the remote methods each object type exports (like an IDL);
                    anything not listed cannot be invoked remotely
* Naming service    the gateway keeps the registry of references (gateway/naming.py)
* Proxy             RemoteProxy - `await proxy.set_phase(phase="RED")` looks like a local call

Servants
    TrafficSignal  one per major junction, state = phase + mode. sync_with(ref) receives a
                   *remote object reference as a parameter* and calls that object itself
                   (object -> object invocation between nodes: green-wave coordination).
    ResponseUnit   wraps a response-unit row of the node's own SQLite database (FA-1 data),
                   so the object encapsulates node-owned data behind remote methods.
"""
import inspect
import json
import time

from ..rpc import fa2_sync_pb2 as spb
from ..rpc.client import NodeUnavailable

INTERFACES = {
    "TrafficSignal": {"get_state": [], "set_phase": ["phase"], "preempt_for": ["incident_id"],
                      "restore": [], "sync_with": ["ref"]},
    "ResponseUnit": {"get_status": []},
}

SIGNALS = {
    "PUNE-TR-01": [("SIG-PUN-SWARGATE", "Swargate Chowk"), ("SIG-PUN-KATRAJ", "Katraj Junction")],
    "MUM-TR-01": [("SIG-MUM-ANDHERI", "Andheri Flyover"), ("SIG-MUM-SION", "Sion Circle")],
    "NSK-TR-01": [("SIG-NSK-CBS", "CBS Junction"), ("SIG-NSK-DWARKA", "Dwarka Circle")],
}


class RemoteObjectError(Exception):
    pass


def make_ref(node_id, interface, object_id):
    return f"fa2obj://{node_id}/{interface}/{object_id}"


def parse_ref(ref):
    if not ref.startswith("fa2obj://"):
        raise RemoteObjectError(f"not an object reference: {ref}")
    parts = ref[len("fa2obj://"):].split("/")
    if len(parts) != 3:
        raise RemoteObjectError(f"malformed object reference: {ref}")
    return parts                                   # node_id, interface, object_id


class RemoteProxy:
    """Client-side stub: every attribute access becomes a remote method."""

    def __init__(self, ref, pool, caller="client"):
        self.ref = ref
        self.node_id, self.interface, self.object_id = parse_ref(ref)
        self.pool, self.caller = pool, caller
        self.last_request_bytes = self.last_reply_bytes = 0

    def __getattr__(self, method):
        if method not in INTERFACES.get(self.interface, {}):
            raise RemoteObjectError(f"{self.interface} has no remote method {method}")

        async def remote_call(**kwargs):
            req = spb.Invocation(object_id=self.object_id, method=method, args=json.dumps(kwargs), caller=self.caller)
            self.last_request_bytes = req.ByteSize()
            try:
                reply = await self.pool.call(self.node_id, "Invoke", req)
            except NodeUnavailable as exc:
                raise RemoteObjectError(f"{self.node_id} unreachable ({exc.reason})")
            self.last_reply_bytes = reply.ByteSize()
            if not reply.ok:
                raise RemoteObjectError(reply.error)
            return json.loads(reply.result)
        return remote_call


class TrafficSignal:
    interface = "TrafficSignal"

    def __init__(self, adapter, object_id, junction):
        self.adapter, self.object_id, self.junction = adapter, object_id, junction
        self.phase, self.mode, self.changed_by, self.changed_at = "GREEN", "NORMAL", "init", time.time()

    def get_state(self):
        return {"object_id": self.object_id, "junction": self.junction, "phase": self.phase, "mode": self.mode,
                "changed_by": self.changed_by, "host": self.adapter.rt.node_id}

    def set_phase(self, phase):
        if phase not in ("RED", "AMBER", "GREEN"):
            raise RemoteObjectError("phase must be RED, AMBER or GREEN")
        self.phase, self.changed_at = phase, time.time()
        return self.get_state()

    def preempt_for(self, incident_id):
        """Emergency-vehicle pre-emption: hold GREEN on the response corridor."""
        self.phase, self.mode, self.changed_at = "GREEN", f"PREEMPTED:{incident_id}", time.time()
        return self.get_state()

    def restore(self):
        self.mode = "NORMAL"
        return self.get_state()

    async def sync_with(self, ref):
        """Object-to-object call: read another signal (anywhere) and adopt its phase."""
        other = RemoteProxy(ref, self.adapter.rt.transport.pool, caller=make_ref(
            self.adapter.rt.node_id, self.interface, self.object_id))
        remote_state = await other.get_state()
        self.phase, self.mode = remote_state["phase"], f"GREEN_WAVE<-{remote_state['object_id']}"
        return {"adopted_from": remote_state, "now": self.get_state()}


class ResponseUnit:
    interface = "ResponseUnit"

    def __init__(self, adapter, object_id):
        self.adapter, self.object_id = adapter, object_id

    def get_status(self):
        row = next((u for u in self.adapter.rt.node.db.list_units() if u["unit_id"] == self.object_id), None)
        if row is None:
            raise RemoteObjectError(f"unit {self.object_id} no longer exists")
        return {**row, "host": self.adapter.rt.node_id, "source": "node SQLite (FA-1 data)"}


class ObjectAdapter:
    """Server side: object table + skeleton (unmarshal -> dispatch -> marshal)."""

    def __init__(self, rt):
        self.rt = rt
        self.objects = {}
        self.invocations = 0
        for oid, junction in SIGNALS[rt.node_id]:
            self.objects[oid] = TrafficSignal(self, oid, junction)
        for unit in rt.node.db.list_units():
            self.objects[unit["unit_id"]] = ResponseUnit(self, unit["unit_id"])
        self.commands = {"objects_list": self.cmd_list}

    def state(self):
        return {"objects": len(self.objects), "invocations": self.invocations}

    def cmd_list(self):
        return [{"ref": make_ref(self.rt.node_id, o.interface, oid), "interface": o.interface,
                 "methods": INTERFACES[o.interface], "node": self.rt.node_id}
                for oid, o in self.objects.items()]

    async def invoke(self, req):
        servant = self.objects.get(req.object_id)
        if servant is None:
            return spb.InvocationResult(ok=False, error=f"no object {req.object_id} on {self.rt.node_id}",
                                        executed_on=self.rt.node_id)
        if req.method not in INTERFACES[servant.interface]:
            return spb.InvocationResult(ok=False, error=f"{req.method} is not a remote method of {servant.interface}",
                                        executed_on=self.rt.node_id)
        self.invocations += 1
        try:
            result = getattr(servant, req.method)(**json.loads(req.args or "{}"))
            if inspect.isawaitable(result):
                result = await result
            self.rt.local_event("INVOKE", f"{req.caller} -> {req.object_id}.{req.method}()")
            return spb.InvocationResult(ok=True, result=json.dumps(result), executed_on=self.rt.node_id)
        except (RemoteObjectError, TypeError) as exc:
            return spb.InvocationResult(ok=False, error=str(exc), executed_on=self.rt.node_id)
