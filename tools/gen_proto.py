"""Generate Python gRPC stubs from proto/traffic.proto into backend/rpc/.

Usage:  python tools/gen_proto.py
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
        str(PROTO_DIR / "traffic.proto"),
    ]
    print(" ".join(cmd))
    subprocess.check_call(cmd)

    # protoc emits an absolute import (`import traffic_pb2`); make it a
    # package-relative import so `backend.rpc` works as a normal package.
    grpc_file = OUT_DIR / "traffic_pb2_grpc.py"
    text = grpc_file.read_text(encoding="utf-8")
    text = text.replace("import traffic_pb2 as traffic__pb2",
                        "from . import traffic_pb2 as traffic__pb2")
    grpc_file.write_text(text, encoding="utf-8")
    print("generated:", OUT_DIR / "traffic_pb2.py")
    print("generated:", grpc_file)


if __name__ == "__main__":
    main()
