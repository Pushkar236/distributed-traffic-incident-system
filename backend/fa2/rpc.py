"""gRPC client helper for the FA-2 SyncNode service (same idea as backend/rpc/client.py).

    pool = SyncPool()
    reply = await pool.command("MUM-TR-01", "state")
"""
import json

import grpc
from grpc import aio

from .. import config
from ..rpc import fa2_sync_pb2 as spb
from ..rpc import fa2_sync_pb2_grpc
from ..rpc.client import NodeUnavailable


class CommandFailed(Exception):
    """The node answered, but the command itself failed (bad arguments, wrong state ...)."""


class SyncPool:
    def __init__(self):
        self.channels = {}

    def stub(self, node_id):
        if node_id not in self.channels:
            self.channels[node_id] = aio.insecure_channel(config.address(node_id))
        return fa2_sync_pb2_grpc.SyncNodeStub(self.channels[node_id])

    async def call(self, node_id, method, request, timeout=config.RPC_TIMEOUT):
        try:
            return await getattr(self.stub(node_id), method)(request, timeout=timeout)
        except aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                raise CommandFailed(exc.details()) from exc
            raise NodeUnavailable(node_id, exc.code().name) from exc

    async def command(self, node_id, name, timeout=config.RPC_TIMEOUT, **args):
        """Run a control command on a node and return its decoded JSON result."""
        reply = await self.call(node_id, "Command",
                                spb.CommandRequest(name=name, args=json.dumps(args)), timeout=timeout)
        if not reply.ok:
            raise CommandFailed(reply.error)
        return json.loads(reply.data or "null")

    async def close(self):
        for channel in self.channels.values():
            await channel.close()
