"""Distributed file system - master / metadata server (lives in the gateway).

Design follows GFS / HDFS in miniature:
    * the master keeps only METADATA: path -> ordered chunk list, each chunk -> replica nodes
    * file DATA lives on the regional nodes (chunk servers, backend/fa2/dfs_store.py)
    * write: split into DFS_CHUNK_SIZE chunks, store each chunk on DFS_REPLICATION
             online nodes (least-loaded first), then publish the new metadata atomically
    * read:  for every chunk try its replicas in turn; a replica that is offline or fails
             its SHA-256 check is skipped (failure transparency) and reported for repair
    * repair: copy chunks from a healthy replica until every chunk again has
              DFS_REPLICATION healthy copies (also run automatically on NODE_RECOVERED)

Metadata is persisted to data/fa2_dfs_master.json. The master is a single point of
failure, exactly like the original GFS master / HDFS NameNode without HA.
"""
import asyncio
import hashlib
import json
import time
import uuid

from ... import config
from ...rpc import fa2_sync_pb2 as spb
from ...rpc.client import NodeUnavailable
from .. import settings
from ..rpc import CommandFailed


class DFSError(Exception):
    pass


class DFSMaster:
    def __init__(self, pool, monitor, broker):
        self.pool, self.monitor, self.broker = pool, monitor, broker
        self.path = config.DATA_DIR / "fa2_dfs_master.json"
        self.files = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.locks = {}
        self.stats = {"writes": 0, "reads": 0, "failovers": 0, "corrupt_replicas_detected": 0, "repairs": 0}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.files, indent=1), encoding="utf-8")

    def _lock(self, path):
        return self.locks.setdefault(path, asyncio.Lock())

    @staticmethod
    def normalise(path):
        path = "/" + path.strip().lstrip("/")
        if ".." in path or len(path) > 200:
            raise DFSError("invalid path")
        return path

    def listing(self):
        return [{"path": p, "size": f["size"], "chunks": len(f["chunks"]), "version": f["version"],
                 "writer": f["writer"], "modified": f["modified"],
                 "replicas": [c["replicas"] for c in f["chunks"]],
                 "under_replicated": sum(len(c["replicas"]) < settings.DFS_REPLICATION for c in f["chunks"])}
                for p, f in sorted(self.files.items())]

    def _placement(self, exclude=(), pending=None):
        """Online nodes, least-loaded (fewest chunk replicas, incl. this write's) first."""
        load = {n: (pending or {}).get(n, 0) for n in settings.NODE_IDS}
        for f in self.files.values():
            for c in f["chunks"]:
                for n in c["replicas"]:
                    load[n] = load.get(n, 0) + 1
        online = [n for n in settings.NODE_IDS if self.monitor.is_online(n) and n not in exclude]
        return sorted(online, key=lambda n: (load[n], n))

    async def _store(self, node, chunk_id, data, digest):
        try:
            ack = await self.pool.call(node, "StoreChunk", spb.Chunk(chunk_id=chunk_id, data=data, sha256=digest))
            return ack.ok
        except NodeUnavailable:
            return False

    # ------------------------------------------------------------- write
    async def write(self, path, content, writer="gateway"):
        path = self.normalise(path)
        data = content.encode("utf-8")
        async with self._lock(path):
            pieces = [data[i:i + settings.DFS_CHUNK_SIZE] for i in range(0, len(data), settings.DFS_CHUNK_SIZE)] or [b""]
            chunks, pending = [], {}
            for index, piece in enumerate(pieces):
                digest = hashlib.sha256(piece).hexdigest()
                chunk_id = f"c{uuid.uuid4().hex[:12]}"
                replicas = []
                for node in self._placement(pending=pending):
                    if len(replicas) == settings.DFS_REPLICATION:
                        break
                    if await self._store(node, chunk_id, piece, digest):
                        replicas.append(node)
                        pending[node] = pending.get(node, 0) + 1
                if not replicas:
                    raise DFSError("no chunk server reachable - file not written")
                chunks.append({"chunk_id": chunk_id, "index": index, "size": len(piece), "sha256": digest,
                               "replicas": replicas})
            old = self.files.get(path)
            self.files[path] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "chunks": chunks,
                                "version": (old["version"] + 1) if old else 1, "writer": writer,
                                "modified": time.time()}
            self._save()
            self.stats["writes"] += 1
        if old:
            await self._delete_chunks(old["chunks"])
        info = self.files[path]
        await self.broker.publish(type="DFS_WRITE", source=writer if writer in config.NODES else "COORDINATOR",
                                  target="ALL", channel="FA2",
                                  summary=f"DFS wrote {path} v{info['version']} ({info['size']} B, {len(chunks)} chunk(s) "
                                          f"x{settings.DFS_REPLICATION} replicas)",
                                  payload={"path": path, "version": info["version"]})
        return {"path": path, **{k: info[k] for k in ("size", "version", "sha256")}, "chunks": chunks}

    async def _delete_chunks(self, chunks):
        by_node = {}
        for c in chunks:
            for n in c["replicas"]:
                by_node.setdefault(n, []).append(c["chunk_id"])
        for node, ids in by_node.items():
            try:
                await self.pool.command(node, "dfs_delete", chunk_ids=ids)
            except (NodeUnavailable, CommandFailed):
                pass                      # orphaned chunks on an offline node are harmless

    # -------------------------------------------------------------- read
    async def read(self, path):
        path = self.normalise(path)
        meta = self.files.get(path)
        if meta is None:
            raise FileNotFoundError(path)
        parts, report = [], []
        for c in meta["chunks"]:
            data, served_by, skipped = None, None, []
            # try online replicas first
            for node in sorted(c["replicas"], key=lambda n: not self.monitor.is_online(n)):
                try:
                    chunk = await self.pool.call(node, "ReadChunk", spb.ChunkRef(chunk_id=c["chunk_id"]))
                except (NodeUnavailable, CommandFailed) as exc:
                    skipped.append({"node": node, "reason": getattr(exc, "reason", "missing")})
                    continue
                if chunk.sha256 == "CORRUPT" or hashlib.sha256(chunk.data).hexdigest() != c["sha256"]:
                    skipped.append({"node": node, "reason": "checksum mismatch"})
                    self.stats["corrupt_replicas_detected"] += 1
                    continue
                data, served_by = chunk.data, node
                break
            if data is None:
                raise DFSError(f"chunk {c['index']} of {path}: no healthy replica reachable ({skipped})")
            if skipped:
                self.stats["failovers"] += 1
            parts.append(data)
            report.append({"index": c["index"], "chunk_id": c["chunk_id"], "served_by": served_by, "skipped": skipped})
        self.stats["reads"] += 1
        content = b"".join(parts)
        return {"path": path, "content": content.decode("utf-8", errors="replace"), "version": meta["version"],
                "size": len(content), "verified": hashlib.sha256(content).hexdigest() == meta["sha256"],
                "chunks": report}

    async def delete(self, path):
        path = self.normalise(path)
        async with self._lock(path):
            meta = self.files.pop(path, None)
            self._save()
        if meta is None:
            raise FileNotFoundError(path)
        await self._delete_chunks(meta["chunks"])
        return {"deleted": path}

    # ------------------------------------------------------------ repair
    async def repair(self):
        """Re-replicate under-replicated or corrupted chunks from a healthy copy."""
        actions = []
        for path, meta in list(self.files.items()):
            for c in meta["chunks"]:
                healthy, source_data = [], None
                for node in c["replicas"]:
                    try:
                        chunk = await self.pool.call(node, "ReadChunk", spb.ChunkRef(chunk_id=c["chunk_id"]))
                    except (NodeUnavailable, CommandFailed):
                        if not self.monitor.is_online(node):
                            healthy.append(node)          # offline, not lost: keep it as a replica
                        continue
                    if chunk.sha256 != "CORRUPT" and hashlib.sha256(chunk.data).hexdigest() == c["sha256"]:
                        healthy.append(node)
                        source_data = chunk.data
                live_healthy = [n for n in healthy if self.monitor.is_online(n)]
                if source_data is None:
                    continue
                targets = [n for n in self._placement(exclude=live_healthy)]
                # replace corrupt copies, and add copies while fewer than DFS_REPLICATION are online
                for node in targets:
                    if len(live_healthy) >= settings.DFS_REPLICATION:
                        break
                    if await self._store(node, c["chunk_id"], source_data, c["sha256"]):
                        live_healthy.append(node)
                        actions.append(f"{path} chunk {c['index']}: copied to {node}")
                new = sorted(set(live_healthy) | set(n for n in healthy if not self.monitor.is_online(n)))
                if new != sorted(c["replicas"]):
                    c["replicas"] = new
        self._save()
        self.stats["repairs"] += len(actions)
        if actions:
            await self.broker.publish(type="DFS_REPAIR", source="COORDINATOR", target="ALL", channel="FA2",
                                      summary=f"DFS re-replicated {len(actions)} chunk copy(ies)", payload={"actions": actions})
        return {"actions": actions}
