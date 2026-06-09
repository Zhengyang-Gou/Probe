"""UDP probe receiver agent for Mininet nodes."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if _PROJECT_SRC.exists() and str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

from adaptive_leo_traversal.probe_packet import ProbePacketPayload


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a UDP probe receiver agent.")
    parser.add_argument("--node-id", type=int, required=True)
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--neighbors", type=str, default="")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    neighbors = _parse_neighbors(args.neighbors)
    args.log.parent.mkdir(parents=True, exist_ok=True)

    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("::", args.port))
    with args.log.open("a", encoding="utf-8") as log_file:
        log_file.write(
            json.dumps(
                {
                    "time": time.time(),
                    "node": args.node_id,
                    "port": args.port,
                    "neighbors": neighbors,
                    "status": "started",
                },
                sort_keys=True,
            )
            + "\n"
        )
        log_file.flush()
        while True:
            data, address = sock.recvfrom(65535)
            received_at = time.time()
            try:
                packet = ProbePacketPayload.from_bytes(data)
                packet.current_node = args.node_id
                if packet.next_telemetry_node == args.node_id:
                    packet.mark_visited(args.node_id)
                    packet.add_telemetry_record(
                        node=args.node_id,
                        observed_time=received_at,
                        links=_observe_local_links(args.node_id, neighbors),
                    )
                payload = packet.to_dict()
                status = "ok"
            except ValueError as exc:
                payload = {"error": str(exc)}
                status = "error"

            event = {
                "time": received_at,
                "node": args.node_id,
                "remote": list(address[:2]),
                "status": status,
                "payload": payload,
            }
            log_file.write(json.dumps(event, sort_keys=True) + "\n")
            log_file.flush()

            ack = {
                "status": status,
                "node": args.node_id,
                "received_at": received_at,
                "visited": payload.get("visited", []),
                "telemetry_records": payload.get("telemetry_records", []),
            }
            sock.sendto(json.dumps(ack, sort_keys=True).encode("utf-8"), address)
            if args.once:
                break
    return 0


def _parse_neighbors(value: str) -> list[int]:
    if not value.strip():
        return []
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def _observe_local_links(node: int, neighbors: list[int]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for neighbor in neighbors:
        iface = f"r{node}-r{neighbor}"
        completed = subprocess.run(
            ["ip", "link", "show", "dev", iface],
            check=False,
            capture_output=True,
            text=True,
        )
        text = f"{completed.stdout}\n{completed.stderr}"
        state = "up" if completed.returncode == 0 and "state UP" in text else "unknown"
        observations.append(
            {
                "edge": [min(node, neighbor), max(node, neighbor)],
                "iface": iface,
                "state": state,
            }
        )
    return observations


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
