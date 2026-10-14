"""Append-only audit ledger (blockchain / distributed-ledger self-study), node replica.

Every node keeps a full copy of a hash-chained ledger in its own SQLite file
(data/<region>_ledger.db - a NEW file, the FA-1 database is not touched).

Block = {index, ts, proposer, prev_hash, nonce, events}; hash = SHA-256 of those fields.
    * chaining     each block stores the previous block's hash, so editing any old block
                   changes its hash and breaks every later link (tamper evidence)
    * toy PoW      the proposer searches a nonce so the hash starts with LEDGER_DIFFICULTY
                   zeros (shows the idea of mining; costs ~ms, not real security)
    * ordering     only the elected leader proposes blocks (leader-based ordering, as in
                   permissioned ledgers / Raft) and a block is COMMITTED once a majority
                   (2 of 3) of replicas stored it. This is crash-fault tolerant, NOT
                   Byzantine-fault tolerant - a malicious leader is not handled.
    * catch-up     a replica that is behind (was offline) asks for the missing blocks;
                   a new leader first adopts the longest valid chain among its peers;
                   a tampered replica can be repaired from a valid peer copy.
"""
import asyncio
import hashlib
import json
import sqlite3
import time

from .. import config
from . import settings
from .errors import CommandError

GENESIS = {"index": 0, "ts": 0.0, "proposer": "GENESIS", "prev_hash": "0" * 64, "nonce": 0,
           "events": [{"type": "GENESIS", "summary": "Distributed traffic incident audit ledger"}]}
MAJORITY = len(settings.NODE_IDS) // 2 + 1


def block_hash(b):
    body = {k: b[k] for k in ("index", "ts", "proposer", "prev_hash", "nonce", "events")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def mine(block, difficulty):
    target = "0" * difficulty
    nonce = 0
    while True:
        block["nonce"] = nonce
        h = block_hash(block)
        if h.startswith(target):
            block["hash"] = h
            return block
        nonce += 1


def link_problem(prev, b):
    """Why block b cannot follow block prev (None if the link is valid)."""
    if b["index"] != prev["index"] + 1:
        return "index gap"
    if b["prev_hash"] != prev["hash"]:
        return "prev_hash does not match previous block"
    if block_hash(b) != b["hash"]:
        return "block content does not match its hash (tampered)"
    if not b["hash"].startswith("0" * settings.LEDGER_DIFFICULTY):
        return "proof-of-work missing"
    return None


def verify(blocks):
    """Check every link of a chain. Returns (valid, index of first bad block or None, reason)."""
    if not blocks or block_hash(blocks[0]) != blocks[0]["hash"] or blocks[0]["prev_hash"] != GENESIS["prev_hash"]:
        return False, 0, "genesis block altered"
    for prev, b in zip(blocks, blocks[1:]):
        problem = link_problem(prev, b)
        if problem:
            return False, b["index"], problem
    return True, None, "all links valid"


class LedgerReplica:
    def __init__(self, rt):
        self.rt = rt
        stem = config.NODES[rt.node_id]["db"].rsplit(".", 1)[0]
        self.path = config.DATA_DIR / f"{stem}_ledger.db"
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("CREATE TABLE IF NOT EXISTS blocks (idx INTEGER PRIMARY KEY, ts REAL, proposer TEXT, "
                          "prev_hash TEXT, nonce INTEGER, hash TEXT, events TEXT)")
        if self.height() < 0:
            g = dict(GENESIS)
            g["hash"] = block_hash(g)
            self._insert(g)
        self.acks = {}                     # block hash -> set of nodes that stored it
        self.handlers = {"LEDGER_APPEND": self.on_append, "LEDGER_ACK": self.on_ack,
                         "LEDGER_SYNC_REQ": self.on_sync_req, "LEDGER_BLOCKS": self.on_blocks}
        self.commands = {"ledger_propose": self.cmd_propose, "ledger_blocks": self.cmd_blocks,
                         "ledger_verify": self.cmd_verify, "ledger_tamper": self.cmd_tamper,
                         "ledger_repair": self.cmd_repair}

    # ------------------------------------------------------------ storage
    def _insert(self, b):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO blocks VALUES (?,?,?,?,?,?,?)",
                              (b["index"], b["ts"], b["proposer"], b["prev_hash"], b["nonce"], b["hash"],
                               json.dumps(b["events"])))

    def blocks(self, start=0):
        rows = self.conn.execute("SELECT idx, ts, proposer, prev_hash, nonce, hash, events FROM blocks "
                                 "WHERE idx >= ? ORDER BY idx", (start,)).fetchall()
        return [{"index": r[0], "ts": r[1], "proposer": r[2], "prev_hash": r[3], "nonce": r[4],
                 "hash": r[5], "events": json.loads(r[6])} for r in rows]

    def height(self):
        row = self.conn.execute("SELECT MAX(idx) FROM blocks").fetchone()
        return -1 if row[0] is None else row[0]

    def tip(self):
        return self.blocks(self.height())[0]

    def _replace_from(self, start, new_blocks):
        with self.conn:
            self.conn.execute("DELETE FROM blocks WHERE idx >= ?", (start,))
        for b in new_blocks:
            self._insert(b)

    def state(self):
        valid, bad, reason = verify(self.blocks())
        tip = self.tip()
        return {"height": tip["index"], "tip_hash": tip["hash"], "valid": valid, "broken_at": bad,
                "reason": reason, "file": self.path.name}

    # ------------------------------------------------- leader: propose block
    async def cmd_propose(self, events):
        if self.rt.modules["election"].leader != self.rt.node_id:
            raise CommandError(f"{self.rt.node_id} is not the leader")
        valid, bad, _ = verify(self.blocks())
        if not valid:
            raise CommandError(f"leader replica is corrupted at block {bad}; repair it first")
        tip = self.tip()
        block = {"index": tip["index"] + 1, "ts": time.time(), "proposer": self.rt.node_id,
                 "prev_hash": tip["hash"], "nonce": 0, "events": events}
        started = time.perf_counter()
        block = await asyncio.to_thread(mine, block, settings.LEDGER_DIFFICULTY)
        mining_ms = round((time.perf_counter() - started) * 1000, 1)
        self._insert(block)
        self.acks[block["hash"]] = {self.rt.node_id}
        self.rt.multicast("LEDGER_APPEND", {"block": block, "_text": f"block #{block['index']}"},
                          text=f"append block #{block['index']}")
        for _ in range(30):                                        # wait up to 3 s for a majority
            if len(self.acks[block["hash"]]) >= MAJORITY:
                break
            await asyncio.sleep(0.1)
        stored_on = sorted(self.acks.pop(block["hash"]))
        committed = len(stored_on) >= MAJORITY
        self.rt.local_event("LEDGER_BLOCK", f"block #{block['index']} {'committed' if committed else 'NOT committed'}")
        return {"index": block["index"], "hash": block["hash"], "nonce": block["nonce"], "mining_ms": mining_ms,
                "committed": committed, "stored_on": stored_on, "events": len(events)}

    # ------------------------------------------------------- replication
    def on_append(self, msg, p):
        b, h = p["block"], self.height()
        if b["index"] == h + 1 and link_problem(self.tip(), b) is None:
            self._insert(b)
            self.rt.send(msg.sender, "LEDGER_ACK", {"hash": b["hash"], "index": b["index"]}, text=f"ack #{b['index']}")
        elif b["index"] <= h and self.blocks(b["index"])[0]["hash"] == b["hash"]:
            self.rt.send(msg.sender, "LEDGER_ACK", {"hash": b["hash"], "index": b["index"]}, text="duplicate")
        else:                                                      # behind or diverged: fetch from the leader
            start = min(h + 1, b["index"])
            self.rt.send(msg.sender, "LEDGER_SYNC_REQ", {"start": max(1, start)}, text=f"sync from #{start}")

    def on_ack(self, msg, p):
        if p["hash"] in self.acks:
            self.acks[p["hash"]].add(msg.sender)

    def on_sync_req(self, msg, p):
        self.rt.send(msg.sender, "LEDGER_BLOCKS", {"start": p["start"], "blocks": self.blocks(p["start"])},
                     text=f"blocks from #{p['start']}")

    def on_blocks(self, msg, p):
        start, received = p["start"], p["blocks"]
        mine_now = self.blocks()
        candidate = mine_now[:start] + received
        ok, _, _ = verify(candidate)
        mine_ok, _, _ = verify(mine_now)
        from_leader = msg.sender == self.rt.modules["election"].leader
        if ok and (not mine_ok or from_leader or len(candidate) > len(mine_now)):
            self._replace_from(start, received)
            self.rt.local_event("LEDGER_SYNC", f"adopted {len(received)} block(s) from {msg.sender}")
            if received:
                last = received[-1]
                self.rt.send(msg.sender, "LEDGER_ACK", {"hash": last["hash"], "index": last["index"]},
                             text=f"ack #{last['index']} after sync")

    def on_become_leader(self):
        """A new leader first adopts the longest valid chain among its peers."""
        for peer in self.rt.peers:
            self.rt.send(peer, "LEDGER_SYNC_REQ", {"start": 1}, text="new leader: fetch chain")

    # ---------------------------------------------------------- commands
    def cmd_blocks(self, limit=10):
        return self.blocks()[-limit:]

    def cmd_verify(self):
        valid, bad, reason = verify(self.blocks())
        return {"node": self.rt.node_id, "valid": valid, "broken_at": bad, "reason": reason, "height": self.height()}

    def cmd_tamper(self, index):
        """Demo: rewrite an old block's content in this replica WITHOUT recomputing hashes."""
        if index <= 0 or index > self.height():
            raise CommandError(f"no block #{index} to tamper with (height {self.height()})")
        b = self.blocks(index)[0]
        b["events"] = b["events"] + [{"type": "FORGED", "summary": "unit MT-01 never dispatched (forged entry)"}]
        with self.conn:
            self.conn.execute("UPDATE blocks SET events=? WHERE idx=?", (json.dumps(b["events"]), index))
        return self.cmd_verify()

    def cmd_repair(self):
        for peer in self.rt.peers:
            self.rt.send(peer, "LEDGER_SYNC_REQ", {"start": 1}, text="repair: fetch full chain")
        return {"requested_from": self.rt.peers}
