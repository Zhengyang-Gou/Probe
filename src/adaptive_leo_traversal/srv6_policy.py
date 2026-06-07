"""Path-to-SRv6-policy encoders for pure Python simulations."""

from __future__ import annotations

from adaptive_leo_traversal.srv6_models import SRv6Policy
from adaptive_leo_traversal.srv6_sid import SRv6SidAllocator


def encode_node_sid_policy(
    path: list[int],
    sid_allocator: SRv6SidAllocator,
    path_cost: float | None = None,
    mode: str = "encap",
    base_srh_overhead_bytes: int = 8,
    per_sid_overhead_bytes: int = 16,
) -> SRv6Policy:
    """Encode a node path as an SRv6 Node SID segment list."""

    _validate_policy_inputs(path, base_srh_overhead_bytes, per_sid_overhead_bytes)
    source = path[0]
    target = path[-1]
    segments = [sid_allocator.node_sid(node) for node in path[1:]]
    return _build_policy(
        source=source,
        target=target,
        path=path,
        segments=segments,
        path_cost=path_cost,
        mode=mode,
        base_srh_overhead_bytes=base_srh_overhead_bytes,
        per_sid_overhead_bytes=per_sid_overhead_bytes,
    )


def encode_adj_sid_policy(
    path: list[int],
    sid_allocator: SRv6SidAllocator,
    path_cost: float | None = None,
    mode: str = "encap",
    base_srh_overhead_bytes: int = 8,
    per_sid_overhead_bytes: int = 16,
) -> SRv6Policy:
    """Encode a node path as a directional SRv6 Adj SID segment list."""

    _validate_policy_inputs(path, base_srh_overhead_bytes, per_sid_overhead_bytes)
    source = path[0]
    target = path[-1]
    segments = [
        sid_allocator.adj_sid(source_node, target_node)
        for source_node, target_node in zip(path, path[1:])
    ]
    return _build_policy(
        source=source,
        target=target,
        path=path,
        segments=segments,
        path_cost=path_cost,
        mode=mode,
        base_srh_overhead_bytes=base_srh_overhead_bytes,
        per_sid_overhead_bytes=per_sid_overhead_bytes,
    )


def _validate_policy_inputs(
    path: list[int],
    base_srh_overhead_bytes: int,
    per_sid_overhead_bytes: int,
) -> None:
    if not path:
        raise ValueError("path must be non-empty")
    if base_srh_overhead_bytes < 0:
        raise ValueError("base_srh_overhead_bytes must be non-negative")
    if per_sid_overhead_bytes < 0:
        raise ValueError("per_sid_overhead_bytes must be non-negative")


def _build_policy(
    source: int,
    target: int,
    path: list[int],
    segments: list[str],
    path_cost: float | None,
    mode: str,
    base_srh_overhead_bytes: int,
    per_sid_overhead_bytes: int,
) -> SRv6Policy:
    segment_count = len(segments)
    srh_overhead_bytes = (
        0
        if segment_count == 0
        else base_srh_overhead_bytes + per_sid_overhead_bytes * segment_count
    )
    return SRv6Policy(
        source=source,
        target=target,
        path=list(path),
        segments=segments,
        mode=mode,
        path_cost=path_cost,
        srh_overhead_bytes=srh_overhead_bytes,
        segment_count=segment_count,
    )
