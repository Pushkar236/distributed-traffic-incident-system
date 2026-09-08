"""Shared settings: the node registry (names, ids, addresses) and a few constants.

NAME vs ADDRESS
  node id  = logical identifier, e.g. "PUNE-TR-01"  (what the node IS)
  address  = where it runs right now, e.g. "localhost:5001"  (where it IS)
Everything in the system refers to nodes by id; only this file knows the address.
"""
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT_DIR / "data"))
FRONTEND_DIR = ROOT_DIR / "frontend"

# PORT is set by cloud hosts (Render); GATEWAY_PORT works locally; default 8000
GATEWAY_PORT = int(os.environ.get("PORT", os.environ.get("GATEWAY_PORT", "8000")))
# URL that node processes use to publish messages to the broker (lives in the gateway)
BROKER_URL = os.environ.get("BROKER_URL", f"http://localhost:{GATEWAY_PORT}/internal/events")

HEARTBEAT_INTERVAL = 2.0   # seconds between heartbeats to every node
RPC_TIMEOUT = 3.0          # seconds before an RPC is considered failed

# Registry of regional nodes. "peers" are listed nearest-first, so P2P
# requests and failover always try the closest region first.
NODES = {
    "PUNE-TR-01": {"name": "Pune Traffic Node",   "region": "Pune",   "port": 5001,
                   "db": "pune.db",   "peers": ["MUM-TR-01", "NSK-TR-01"]},
    "MUM-TR-01":  {"name": "Mumbai Traffic Node", "region": "Mumbai", "port": 5002,
                   "db": "mumbai.db", "peers": ["PUNE-TR-01", "NSK-TR-01"]},
    "NSK-TR-01":  {"name": "Nashik Traffic Node", "region": "Nashik", "port": 5003,
                   "db": "nashik.db", "peers": ["MUM-TR-01", "PUNE-TR-01"]},
}

# How many response units an incident needs
UNITS_REQUIRED = {"High": 2, "Medium": 1, "Low": 0}

INCIDENT_TYPES = ["Accident", "Road Blockage", "Vehicle Breakdown",
                  "Heavy Congestion", "Traffic Signal Failure"]
SEVERITIES = ["High", "Medium", "Low"]


def address(node_id):
    """Resolve a node id to its network address (Docker overrides via NODE_ADDR_<ID>)."""
    env_key = "NODE_ADDR_" + node_id.replace("-", "_")
    return os.environ.get(env_key, f"localhost:{NODES[node_id]['port']}")


def db_path(node_id):
    return DATA_DIR / NODES[node_id]["db"]


def failover_order(node_id):
    """The node itself first, then its peers nearest-first."""
    return [node_id] + NODES[node_id]["peers"]
