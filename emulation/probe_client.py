"""Send one UDP probe packet to a Mininet node agent."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

_PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if _PROJECT_SRC.exists() and str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

from adaptive_leo_traversal.probe_packet import ProbePacketPayload


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a UDP probe packet.")
    parser.add_argument("--run-id", type=str, required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--root", type=int, required=True)
    parser.add_argument("--source-node", type=int, required=True)
    parser.add_argument("--target-node", type=int, required=True)
    parser.add_argument("--dst", type=str, required=True)
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--path", type=str, default="")
    parser.add_argument("--visited", type=str, default="")
    parser.add_argument("--hop-count", type=int, default=0)
    parser.add_argument("--hop-limit", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    packet = ProbePacketPayload(
        run_id=args.run_id,
        sequence=args.sequence,
        root=args.root,
        current_node=args.source_node,
        next_telemetry_node=args.target_node,
        hop_count=args.hop_count,
        hop_limit=args.hop_limit,
        visited=_parse_int_csv(args.visited),
        path=_parse_int_csv(args.path),
    )

    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.settimeout(args.timeout)
    sock.sendto(packet.to_bytes(), (args.dst, args.port))
    try:
        data, _ = sock.recvfrom(65535)
    except socket.timeout:
        print(json.dumps({"status": "timeout", "dst": args.dst, "port": args.port}, sort_keys=True))
        return 1

    print(data.decode("utf-8"))
    return 0


def _parse_int_csv(value: str) -> list[int]:
    if not value.strip():
        return []
    return [int(part.strip()) for part in value.split(",") if part.strip()]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
