"""One-command local launcher (no Docker needed).

    python run.py              # 3 node processes + gateway  ->  http://localhost:8000
    python run.py --reset      # delete data/*.db first (fresh demo data)

Each regional node is a separate OS process with its own gRPC port and its own
SQLite file, so the system is genuinely distributed even on one laptop.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import config  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete the node databases first")
    args = parser.parse_args()

    if args.reset:
        for node_id in config.NODES:
            config.db_path(node_id).unlink(missing_ok=True)

    processes = []
    for node_id in config.NODES:                                     # start the three nodes
        processes.append(subprocess.Popen([sys.executable, "-m", "backend.node", "--node-id", node_id]))
    time.sleep(1.5)                                                  # let them bind their ports
    processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app",
                                       "--host", "0.0.0.0", "--port", str(config.GATEWAY_PORT),
                                       "--log-level", "warning"]))

    print("\n=== Distributed Traffic Incident Coordination System ===")
    for node_id, info in config.NODES.items():
        print(f"  {node_id:<11} {info['name']:<22} grpc://{config.address(node_id):<16} {info['db']}")
    print(f"  Dashboard   http://localhost:{config.GATEWAY_PORT}\nPress Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for p in processes:
            p.terminate()
        print("[run.py] all processes stopped")


if __name__ == "__main__":
    main()
