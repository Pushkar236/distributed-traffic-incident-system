"""gRPC client helper used by the coordinator and by nodes (for P2P calls).

    pool = StubPool()
    status = await pool.call("MUM-TR-01", "GetNodeStatus", pb.Empty())

The node id is resolved to an address through the registry, one channel is
kept per node, and any gRPC failure becomes a NodeUnavailable exception so
the caller can simply try another node.
"""
import grpc
from grpc import aio

from .. import config
from . import traffic_pb2_grpc


class NodeUnavailable(Exception):
    def __init__(self, node_id, reason):
        super().__init__(f"{node_id} unavailable: {reason}")
        self.node_id = node_id
        self.reason = reason


class StubPool:
    def __init__(self):
        self.channels = {}

    def stub(self, node_id):
        if node_id not in self.channels:
            self.channels[node_id] = aio.insecure_channel(config.address(node_id))
        return traffic_pb2_grpc.TrafficNodeStub(self.channels[node_id])

    async def call(self, node_id, method, request, timeout=config.RPC_TIMEOUT):
        try:
            return await getattr(self.stub(node_id), method)(request, timeout=timeout)
        except aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                raise                       # a real "no such record" answer, not a failure
            raise NodeUnavailable(node_id, exc.code().name) from exc

    async def close(self):
        for channel in self.channels.values():
            await channel.close()
