from adaptive_leo_traversal.models import LinkState
from adaptive_leo_traversal.observations import LinkObservationTable


def test_observation_down_expires_to_default_none() -> None:
    table = LinkObservationTable()

    table.update((2, 1), LinkState.DOWN, observed_time=10.0, ttl=5.0)

    assert table.get_state((1, 2), now=14.9) is LinkState.DOWN
    assert table.recent_down_edges(now=14.9) == {(1, 2)}
    assert table.get_state((1, 2), now=15.0) is None
    assert table.recent_down_edges(now=15.0) == set()


def test_up_observation_overrides_old_down_state() -> None:
    table = LinkObservationTable()

    table.update((0, 1), LinkState.DOWN, observed_time=0.0, ttl=10.0)
    table.update((1, 0), LinkState.UP, observed_time=1.0, ttl=10.0)

    assert table.get_state((0, 1), now=2.0) is LinkState.UP
    assert table.recent_down_edges(now=2.0) == set()
