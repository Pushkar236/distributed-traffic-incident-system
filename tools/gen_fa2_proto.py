"""Generate Python gRPC stubs from proto/fa2_sync.proto into backend/rpc/.

Usage:  python tools/gen_fa2_proto.py

Separate from tools/gen_proto.py so the FA-1 stubs (traffic_pb2*.py) are never regenerated.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "proto"
OUT_DIR = ROOT / "backend" / "rpc"


def main() -> None:
    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
        str(PROTO_DIR / "fa2_sync.proto"),
    ]
    print(" ".join(cmd))
    subprocess.check_call(cmd)

    # make the generated import package-relative (same fix as gen_proto.py)
    grpc_file = OUT_DIR / "fa2_sync_pb2_grpc.py"
    text = grpc_file.read_text(encoding="utf-8")
    text = text.replace("import fa2_sync_pb2 as fa2__sync__pb2",
                        "from . import fa2_sync_pb2 as fa2__sync__pb2")
    grpc_file.write_text(text, encoding="utf-8")
    print("generated:", OUT_DIR / "fa2_sync_pb2.py")
    print("generated:", grpc_file)


if __name__ == "__main__":
    main()
