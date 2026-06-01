# adaptive-leo-traversal

`adaptive-leo-traversal` is a Python 3.11+ reference implementation for simulating and validating a probe-packet telemetry traversal algorithm for LEO satellite networks.

## Algorithm Assumptions

- The base topology `G0=(V,E0)` is a fixed undirected graph. In this project edges are normalized as `(min(u, v), max(u, v))`.
- Without an observation, every base link is treated as available by default.
- A probe carries the next requested telemetry node, its current explicit forwarding path, collected telemetry records, and a hop limit.
- A satellite only performs telemetry when its node ID matches the probe's `next_telemetry_node`; relay nodes forward without recording telemetry or advancing the target.
- There is no predicted link up/down schedule. Link state knowledge comes from telemetry-node observations and failed next-hop checks.
- `LinkObservationTable` stores the latest `UP` or `DOWN` observation for each link. Newer observations replace older ones.
- The estimated topology is `Ge(t) = G0 - recent_down_edges(t)`.
- If a down link is later observed as up, it is reintroduced into `Ge(t)`.
- Edge weights come from the current periodic delay slot: `DelayTable[slot(t)][u][v]`.
- Already visited nodes may be used as relay nodes, but they are excluded as new traversal targets.
- After a telemetry node chooses its Hamiltonian successor, a healthy direct physical link to that successor is used immediately without recalculating the path.
- If that direct link is missing or down, or if the current forwarding path cannot continue, the probe replans to the same `next_telemetry_node`.

## Project Structure

- `models.py`: enums, dataclasses, edge normalization, probe/result models.
- `topology.py`: immutable undirected topology and grid topology generation.
- `delay_table.py`: periodic per-link delay storage.
- `observations.py`: link observation table.
- `planner.py`: Dijkstra path planning and path utility functions.
- `traversal.py`: adaptive traversal engine that combines observations, estimated topology, and planning.
- `simulation.py`: lightweight physical link-state provider and simulation runner.

## Example: 6x6 Grid

```python
from adaptive_leo_traversal import (
    AdaptiveTraversalEngine,
    DelayTable,
    LinkState,
    make_grid_hamiltonian_cycle,
    make_grid_topology,
)

topology = make_grid_topology(6, 6)
delay_table = DelayTable.from_constant_delay(topology, period_slots=4, delay=1.0)
cycle_route = make_grid_hamiltonian_cycle(6, 6)

engine = AdaptiveTraversalEngine(
    base_topology=topology,
    delay_table=delay_table,
    root=0,
    max_hop=500,
    cycle_route=tuple(cycle_route),
)

def provider(u: int, v: int, now: float) -> LinkState:
    return LinkState.UP

probe = engine.initialize_probe(now=0.0)
result = engine.on_probe_arrival(probe, current_node=0, now=0.0, physical_link_state_provider=provider)

print(result.status, result.next_hop, result.path)
```

## Simulation Helper

```python
from adaptive_leo_traversal import (
    AdaptiveTraversalEngine,
    DelayTable,
    make_grid_hamiltonian_cycle,
    make_grid_topology,
)
from adaptive_leo_traversal.simulation import StaticLinkStateProvider, run_simulation

topology = make_grid_topology(6, 6)
delay_table = DelayTable.from_constant_delay(topology, period_slots=8, delay=1.0)
cycle_route = make_grid_hamiltonian_cycle(6, 6)
engine = AdaptiveTraversalEngine(
    topology,
    delay_table,
    root=0,
    max_hop=1000,
    cycle_route=tuple(cycle_route),
)

provider = StaticLinkStateProvider()
provider.add_down_interval(0, 1, start=2.0, end=6.0)

result = run_simulation(engine, provider, start_time=0.0, step_time=1.0)
print(result.status, len(result.probe.visited))
```

## Random Experiments

Run multiple randomized link-failure scenarios and report final status, visited nodes, hop count, accumulated delay, and aggregate means:

```bash
python scripts/random_experiments.py --runs 20 --seed 7
```

Example options:

```bash
python scripts/random_experiments.py \
  --rows 6 \
  --cols 6 \
  --runs 50 \
  --down-probability 0.25 \
  --min-down-duration 2 \
  --max-down-duration 12
```

## Development

Install test dependencies and run the suite:

```bash
python -m pip install -e ".[test]"
pytest
```

The package intentionally has no runtime dependencies beyond the Python standard library.
