"""Per-node SQLite storage.

Each node owns exactly one database file (data/pune.db, mumbai.db, nashik.db).
Nobody else opens that file - other processes must go through the node's RPCs.
That is what "distributed data ownership" means in this project.

Tables: incidents, response_units, traffic_status, node_meta
"""
import json
import random
import sqlite3
from datetime import datetime

from .seed import SEED_DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY, type TEXT, location TEXT, severity TEXT,
    description TEXT, detected_by TEXT, handled_by TEXT, status TEXT,
    assigned_units TEXT,      -- JSON list of {"unit_id", "node_id"}
    origin TEXT,              -- LOCAL (owned by this node) or REMOTE (copy from a broadcast)
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS response_units (
    unit_id TEXT PRIMARY KEY, unit_type TEXT, status TEXT, assigned_incident TEXT);
CREATE TABLE IF NOT EXISTS traffic_status (road TEXT PRIMARY KEY, density REAL, updated_at TEXT);
"""

ACTIVE = ("REPORTED", "PARTIALLY_ASSIGNED", "UNITS_ASSIGNED", "MONITORING")


def now():
    return datetime.now().isoformat(timespec="seconds")


class NodeDatabase:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- setup
    def seed_if_empty(self, node_id):
        if self.conn.execute("SELECT COUNT(*) FROM response_units").fetchone()[0] == 0:
            self.reset(node_id)

    def reset(self, node_id):
        """Wipe everything and load the fictional demo data for this node."""
        seed = SEED_DATA[node_id]
        with self.conn:
            for table in ("incidents", "response_units", "traffic_status", "node_meta"):
                self.conn.execute(f"DELETE FROM {table}")
            for key, value in seed["meta"].items():
                self.conn.execute("INSERT INTO node_meta VALUES (?, ?)", (key, value))
            for u in seed["units"]:
                self.conn.execute("INSERT INTO response_units VALUES (?, ?, ?, ?)",
                                  (u["unit_id"], u["unit_type"], u.get("status", "AVAILABLE"),
                                   u.get("assigned_incident", "")))
            for road, density in seed["traffic"].items():
                self.conn.execute("INSERT INTO traffic_status VALUES (?, ?, ?)", (road, density, now()))
        for inc in seed["incidents"]:
            self.insert_incident(inc)

    # ------------------------------------------------------------ incidents
    def insert_incident(self, inc):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (inc["incident_id"], inc["type"], inc["location"], inc["severity"],
                 inc.get("description", ""), inc["detected_by"], inc["handled_by"],
                 inc.get("status", "REPORTED"), json.dumps(inc.get("assigned_units", [])),
                 inc.get("origin", "LOCAL"), inc.get("created_at") or now(), now()))
        return self.get_incident(inc["incident_id"])

    def get_incident(self, incident_id):
        row = self.conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        return self._to_dict(row) if row else None

    def list_incidents(self, origin=None):
        sql = "SELECT * FROM incidents" + (" WHERE origin=?" if origin else "") + " ORDER BY created_at DESC"
        rows = self.conn.execute(sql, (origin,) if origin else ()).fetchall()
        return [self._to_dict(r) for r in rows]

    def update_incident(self, incident_id, status=None, assigned_units=None):
        with self.conn:
            if status is not None:
                self.conn.execute("UPDATE incidents SET status=?, updated_at=? WHERE incident_id=?",
                                  (status, now(), incident_id))
            if assigned_units is not None:
                self.conn.execute("UPDATE incidents SET assigned_units=?, updated_at=? WHERE incident_id=?",
                                  (json.dumps(assigned_units), now(), incident_id))
        return self.get_incident(incident_id)

    def count_active_incidents(self):
        marks = ",".join("?" * len(ACTIVE))
        return self.conn.execute(
            f"SELECT COUNT(*) FROM incidents WHERE origin='LOCAL' AND status IN ({marks})", ACTIVE).fetchone()[0]

    @staticmethod
    def _to_dict(row):
        d = dict(row)
        d["assigned_units"] = json.loads(d["assigned_units"] or "[]")
        return d

    # ---------------------------------------------------------------- units
    def list_units(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM response_units ORDER BY unit_id")]

    def available_units(self):
        return [u for u in self.list_units() if u["status"] == "AVAILABLE"]

    def reserve_unit(self, unit_id, incident_id):
        """Reserve a unit only if it is still free (one SQL statement = no double booking)."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE response_units SET status='DISPATCHED', assigned_incident=? "
                "WHERE unit_id=? AND status='AVAILABLE'", (incident_id, unit_id))
        return cur.rowcount == 1

    def release_unit(self, unit_id):
        with self.conn:
            cur = self.conn.execute(
                "UPDATE response_units SET status='AVAILABLE', assigned_incident='' WHERE unit_id=?", (unit_id,))
        return cur.rowcount == 1

    # -------------------------------------------------------------- traffic
    def get_traffic(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM traffic_status ORDER BY road")]

    def average_density(self):
        rows = self.get_traffic()
        return round(sum(r["density"] for r in rows) / len(rows), 1) if rows else 0.0

    def tick_traffic(self, active_incidents=0):
        """Simulated road sensors: small random change, pushed up a little by active incidents."""
        with self.conn:
            for row in self.get_traffic():
                density = row["density"] + random.uniform(-4, 4) + min(active_incidents, 3) * 0.6
                density = max(8.0, min(98.0, density))
                self.conn.execute("UPDATE traffic_status SET density=?, updated_at=? WHERE road=?",
                                  (round(density, 1), now(), row["road"]))

    def close(self):
        self.conn.close()
