from adaptive_leo_traversal.probe_packet import ProbePacketPayload


def test_probe_packet_round_trip_and_telemetry_record() -> None:
    packet = ProbePacketPayload(
        run_id="run-1",
        sequence=7,
        root=0,
        current_node=3,
        next_telemetry_node=3,
        hop_count=2,
        hop_limit=10,
        visited=[0],
        path=[0, 1, 3],
    )

    packet.mark_visited(3)
    packet.add_telemetry_record(
        node=3,
        observed_time=1.5,
        links=[{"edge": [1, 3], "state": "up"}],
    )

    decoded = ProbePacketPayload.from_bytes(packet.to_bytes())

    assert decoded.run_id == "run-1"
    assert decoded.sequence == 7
    assert decoded.visited == [0, 3]
    assert decoded.path == [0, 1, 3]
    assert decoded.telemetry_records[0]["node"] == 3
