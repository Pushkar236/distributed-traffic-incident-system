"""Distributed file system - chunk server (node side).

Files are split into chunks by the DFS master in the gateway (backend/fa2/gateway/dfs_master.py);
each chunk is stored on DFS_REPLICATION different nodes. A node keeps its chunks as plain
files in data/dfs/<NODE-ID>/ together with their SHA-256 checksum, and verifies the
checksum on every read, so a corrupted replica is detected and reported instead of
being served. Comparable to an HDFS DataNode / GFS chunkserver.
"""
import hashlib
import json

from .. import config
from ..rpc import fa2_sync_pb2 as spb
from .errors import CommandError


class ChunkStore:
    def __init__(self, rt):
        self.rt = rt
        self.dir = config.DATA_DIR / "dfs" / rt.node_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.reads = self.writes = self.checksum_failures = 0
        self.commands = {"dfs_list": self.cmd_list, "dfs_delete": self.cmd_delete,
                         "dfs_corrupt": self.cmd_corrupt}

    def _paths(self, chunk_id):
        safe = "".join(c for c in chunk_id if c.isalnum() or c in "-_")
        return self.dir / f"{safe}.chunk", self.dir / f"{safe}.json"

    def state(self):
        chunks = list(self.dir.glob("*.chunk"))
        return {"chunks": len(chunks), "bytes": sum(p.stat().st_size for p in chunks),
                "reads": self.reads, "writes": self.writes, "checksum_failures": self.checksum_failures,
                "directory": str(self.dir)}

    def store(self, chunk):
        if hashlib.sha256(chunk.data).hexdigest() != chunk.sha256:
            return spb.Ack(ok=False, message="checksum mismatch on arrival")
        data_path, meta_path = self._paths(chunk.chunk_id)
        data_path.write_bytes(chunk.data)
        meta_path.write_text(json.dumps({"chunk_id": chunk.chunk_id, "sha256": chunk.sha256}), encoding="utf-8")
        self.writes += 1
        return spb.Ack(ok=True, message=f"stored on {self.rt.node_id}")

    def read(self, chunk_id):
        data_path, meta_path = self._paths(chunk_id)
        if not data_path.exists():
            return None
        data = data_path.read_bytes()
        expected = json.loads(meta_path.read_text(encoding="utf-8"))["sha256"]
        self.reads += 1
        if hashlib.sha256(data).hexdigest() != expected:
            self.checksum_failures += 1
            return spb.Chunk(chunk_id=chunk_id, data=b"", sha256="CORRUPT", node_id=self.rt.node_id)
        return spb.Chunk(chunk_id=chunk_id, data=data, sha256=expected, node_id=self.rt.node_id)

    def cmd_list(self):
        return sorted(p.stem for p in self.dir.glob("*.chunk"))

    def cmd_delete(self, chunk_ids):
        removed = 0
        for cid in chunk_ids:
            for p in self._paths(cid):
                if p.exists():
                    p.unlink()
                    removed += p.suffix == ".chunk"
        return {"removed": removed}

    def cmd_corrupt(self, chunk_id):
        """Demo: flip bytes of the local replica without updating its checksum (bit rot)."""
        data_path, _ = self._paths(chunk_id)
        if not data_path.exists():
            raise CommandError(f"chunk {chunk_id} is not stored on {self.rt.node_id}")
        data = bytearray(data_path.read_bytes())
        data[0:8] = b"#CORRUPT"
        data_path.write_bytes(bytes(data))
        return {"corrupted": chunk_id, "node": self.rt.node_id}
