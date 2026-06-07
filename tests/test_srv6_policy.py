import pytest

from adaptive_leo_traversal.srv6_policy import encode_adj_sid_policy, encode_node_sid_policy
from adaptive_leo_traversal.srv6_sid import SRv6SidAllocator


def test_encode_node_sid_policy_uses_path_suffix_as_segments() -> None:
    allocator = SRv6SidAllocator()

    policy = encode_node_sid_policy([0, 4, 15], allocator, path_cost=2.5)

    assert policy.source == 0
    assert policy.target == 15
    assert policy.path == [0, 4, 15]
    assert policy.segments == ["fc00:0:4::1", "fc00:0:f::1"]
    assert policy.segment_count == 2
    assert policy.srh_overhead_bytes == 40
    assert policy.path_cost == 2.5


def test_encode_single_node_policy_has_no_srh_overhead() -> None:
    allocator = SRv6SidAllocator()

    policy = encode_node_sid_policy([7], allocator)

    assert policy.source == 7
    assert policy.target == 7
    assert policy.segments == []
    assert policy.segment_count == 0
    assert policy.srh_overhead_bytes == 0


def test_encode_adj_sid_policy_preserves_direction() -> None:
    allocator = SRv6SidAllocator()

    policy = encode_adj_sid_policy([0, 4, 0], allocator)

    assert policy.segments == ["fc00:a:0:4::1", "fc00:a:4:0::1"]
    assert policy.segment_count == 2
    assert policy.srh_overhead_bytes == 40


def test_encode_policy_rejects_empty_path() -> None:
    allocator = SRv6SidAllocator()

    with pytest.raises(ValueError):
        encode_node_sid_policy([], allocator)
