"""FA-2 extension: Unit III (synchronization) and Unit IV (emerging paradigms).

Node side (runs inside every regional node process, see runtime.attach):
    clocks.py, transport.py, runtime.py      message passing + Lamport/vector clocks
    clock_sync.py   Cristian / Berkeley        causal.py    vector-clock causal broadcast
    beacon.py       beacon protocol            election.py  Bully + Ring
    mutex.py        Lamport / Ricart-Agrawala / Lodha-Kshemkalyani mutual exclusion
    snapshot.py     Chandy-Lamport global state  deadlock.py  CMH edge-chasing probes
    objects.py      remote objects (skeletons)   dfs_store.py chunk server
    ledger.py       hash-chained ledger replica

Gateway side (runs inside the FastAPI gateway, see gateway/api.py):
    REST API /api/fa2/*, DFS master, naming service, FaaS runtime, ledger service, web cache.
"""
