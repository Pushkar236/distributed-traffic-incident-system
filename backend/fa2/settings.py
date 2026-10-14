"""FA-2 constants. Node ids, names and addresses still come from backend/config.py."""
import os

from .. import config

NODE_IDS = list(config.NODES)                      # PUNE-TR-01, MUM-TR-01, NSK-TR-01

# Gateway base URL used by nodes (Cristian time server, DFS file API).
# Derived from BROKER_URL so Docker / local mode need no extra setting.
GATEWAY_URL = os.environ.get("GATEWAY_URL", config.BROKER_URL.rsplit("/internal/", 1)[0])

# ---- election: Bully priority (higher wins) and logical ring order
PRIORITY = {"PUNE-TR-01": 1, "MUM-TR-01": 2, "NSK-TR-01": 3}
RING = ["PUNE-TR-01", "MUM-TR-01", "NSK-TR-01"]    # successor of the last one is the first
ELECTION_OK_TIMEOUT = 1.5          # Bully: wait this long for an OK from a higher node
ELECTION_COORD_TIMEOUT = 4.0       # Bully: after an OK, wait this long for COORDINATOR
STARTUP_ELECTION_DELAY = 3.0       # first election after the node starts

# ---- simulated physical clocks: (initial offset in seconds, drift in parts-per-million).
# All three processes run on one host and would share one OS clock, so each node runs a
# software clock with a deliberate offset and an exaggerated drift (real quartz: ~1-50 ppm).
CLOCK_SKEW = {"PUNE-TR-01": (1.250, 900), "MUM-TR-01": (-0.800, -600), "NSK-TR-01": (2.100, 1500)}

# ---- beacon protocol
BEACON_INTERVAL = 1.0              # every node sends a beacon to every peer each second
BEACON_TIMEOUT = 3.5               # no beacon for this long -> peer SUSPECTED

# ---- global snapshot demo: "standby patrol vehicles" moved between regions
PATROLS_PER_NODE = 10              # the total (30) is the invariant a consistent cut must preserve

# ---- mutual exclusion
ME_ALGORITHMS = ["RICART_AGRAWALA", "LAMPORT", "LODHA_KSHEMKALYANI", "NONE"]
ME_DEFAULT = "LODHA_KSHEMKALYANI"
ME_CS_WORK = 0.6                   # seconds spent inside the critical section
ME_REQUEST_TIMEOUT = 25.0          # give up if the CS is never granted (algorithms assume no crashes)
SHARED_REGISTER = "/shared/air-ambulance-register.log"

# ---- distributed file system
DFS_CHUNK_SIZE = 256               # bytes (small on purpose so a report spans several chunks)
DFS_REPLICATION = 2

# ---- ledger
LEDGER_DIFFICULTY = 3              # leading hex zeros required in a block hash (toy proof-of-work)
