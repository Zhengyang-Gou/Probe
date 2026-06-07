import pytest

from adaptive_leo_traversal.srv6_sid import SRv6SidAllocator


def test_node_sid_uses_hex_node_id() -> None:
    allocator = SRv6SidAllocator()

    assert allocator.node_sid(0) == "fc00:0:0::1"
    assert allocator.node_sid(4) == "fc00:0:4::1"
    assert allocator.node_sid(15) == "fc00:0:f::1"


def test_adj_sid_is_directional() -> None:
    allocator = SRv6SidAllocator()

    assert allocator.adj_sid(0, 4) == "fc00:a:0:4::1"
    assert allocator.adj_sid(4, 0) == "fc00:a:4:0::1"


@pytest.mark.parametrize("value", [-1, 1.5, "1", True])
def test_sid_allocator_rejects_invalid_node_ids(value: object) -> None:
    allocator = SRv6SidAllocator()

    with pytest.raises(ValueError):
        allocator.node_sid(value)  # type: ignore[arg-type]


def test_sid_allocator_rejects_empty_locator_prefix() -> None:
    with pytest.raises(ValueError):
        SRv6SidAllocator(locator_prefix="")
