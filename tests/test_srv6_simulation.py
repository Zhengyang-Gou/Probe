import pytest

from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.models import TraversalStatus
from adaptive_leo_traversal.simulation import StaticLinkStateProvider
from adaptive_leo_traversal.srv6_sid import SRv6SidAllocator
from adaptive_leo_traversal.srv6_simulation import SRv6SimulationRunner
from adaptive_leo_traversal.topology import Topology
from adaptive_leo_traversal.traversal import AdaptiveTraversalEngine


def test_srv6_runner_updates_policy_only_when_path_changes() -> None:
    topology = Topology(nodes={0, 1, 2}, edges={(0, 1), (1, 2)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    engine = AdaptiveTraversalEngine(
        base_topology=topology,
        delay_table=table,
        root=0,
        max_hop=20,
        cycle_route=(0, 2, 1, 0),
    )
    runner = SRv6SimulationRunner(
        engine=engine,
        provider=StaticLinkStateProvider(),
        actual_delay_provider=lambda u, v, now: 1.0,
        sid_allocator=SRv6SidAllocator(),
    )

    result, events = runner.run(run_id=9)

    assert result.status is TraversalStatus.FINISHED
    assert result.srv6_enabled is True
    assert result.srv6_policy_updates == 3
    assert result.total_sid_processing_count == 4
    assert result.mean_segment_list_length == pytest.approx(4 / 3)
    assert result.max_segment_list_length == 2
    assert result.mean_srh_overhead_bytes == pytest.approx((40 + 24 + 24) / 3)
    assert result.max_srh_overhead_bytes == 40
    assert result.hop_count == 4
    assert result.total_delay == 4.0

    assert [event.path for event in events] == [[0, 1, 2], [2, 1], [1, 0]]
    assert events[0].segments == ["fc00:0:1::1", "fc00:0:2::1"]
    assert events[0].status == TraversalStatus.RUNNING.value
    assert events[0].run_id == 9
