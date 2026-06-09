"""Link observation table."""

from __future__ import annotations

from dataclasses import dataclass, field

from adaptive_leo_traversal.models import Edge, LinkObservation, LinkState, normalize_edge


@dataclass(slots=True)
class LinkObservationTable:
    """Stores the latest observation for each undirected edge."""

    stale_after: float | None = None
    _observations: dict[Edge, LinkObservation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stale_after is not None and self.stale_after < 0:
            raise ValueError("stale_after must be non-negative")

    def update(self, edge: Edge, state: LinkState, observed_time: float) -> None:
        """Record a new observation, replacing any older state for the same edge."""

        normalized = normalize_edge(*edge)
        self._observations[normalized] = LinkObservation(
            edge=normalized,
            state=state,
            observed_time=observed_time,
        )

    def down_edges(self) -> set[Edge]:
        """Return all edges whose latest observation is down."""

        return {
            edge
            for edge, observation in self._observations.items()
            if observation.state is LinkState.DOWN
        }

    def recent_down_edges(self, now: float) -> set[Edge]:
        """Return down edges whose latest observation has not expired."""

        return {
            edge
            for edge, observation in self._observations.items()
            if observation.state is LinkState.DOWN and not self._is_stale(observation, now)
        }

    def current_state(self, edge: Edge) -> LinkState | None:
        """Return the latest observation state, or ``None`` when default applies."""

        normalized = normalize_edge(*edge)
        observation = self._observations.get(normalized)
        return None if observation is None else observation.state

    def get_state(self, edge: Edge, now: float) -> LinkState | None:
        """Return the latest observation state, or ``None`` when default applies."""

        observation = self.get_observation(edge, now)
        return None if observation is None else observation.state

    def current_observation(self, edge: Edge) -> LinkObservation | None:
        """Return the latest observation, or ``None`` when default applies."""

        return self._observations.get(normalize_edge(*edge))

    def get_observation(self, edge: Edge, now: float) -> LinkObservation | None:
        """Return the latest observation, or ``None`` when default applies."""

        observation = self.current_observation(edge)
        if observation is None or self._is_stale(observation, now):
            return None
        return observation

    def _is_stale(self, observation: LinkObservation, now: float) -> bool:
        if self.stale_after is None:
            return False
        return now - observation.observed_time > self.stale_after
