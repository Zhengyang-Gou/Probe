"""Link observation table."""

from __future__ import annotations

from dataclasses import dataclass, field

from adaptive_leo_traversal.models import Edge, LinkObservation, LinkState, normalize_edge


@dataclass(slots=True)
class LinkObservationTable:
    """Stores the latest observation for each undirected edge."""

    _observations: dict[Edge, LinkObservation] = field(default_factory=dict)

    def update(self, edge: Edge, state: LinkState, observed_time: float) -> None:
        """Record a new observation, replacing any older state for the same edge."""

        normalized = normalize_edge(*edge)
        self._observations[normalized] = LinkObservation(
            edge=normalized,
            state=state,
            observed_time=observed_time,
        )

    def recent_down_edges(self, now: float) -> set[Edge]:
        """Return all edges currently observed as down."""

        return {
            edge
            for edge, observation in self._observations.items()
            if observation.state is LinkState.DOWN
        }

    def get_state(self, edge: Edge, now: float) -> LinkState | None:
        """Return the current observation state, or ``None`` when default applies."""

        normalized = normalize_edge(*edge)
        observation = self._observations.get(normalized)
        return None if observation is None else observation.state

    def get_observation(self, edge: Edge, now: float) -> LinkObservation | None:
        """Return the current observation, or ``None`` when default applies."""

        return self._observations.get(normalize_edge(*edge))
