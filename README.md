# adaptive-leo-traversal

`adaptive-leo-traversal` is a Python 3.11+ reference implementation for simulating and validating a probe-packet telemetry traversal algorithm for LEO satellite networks.

## Algorithm Assumptions

- The base topology `G0=(V,E0)` is a fixed undirected graph. In this project edges are normalized as `(min(u, v), max(u, v))`.
- Without an observation, every base link is treated as available by default.
- A probe carries the next requested telemetry node, its current explicit forwarding path, collected telemetry records, and a hop limit.
- A satellite only performs telemetry when its node ID matches the probe's `next_telemetry_node`; relay nodes forward without recording telemetry or advancing the target.
- There is no predicted link up/down schedule. Link state knowledge comes from telemetry-node observations and failed next-hop checks.
- `LinkObservationTable` stores the latest `UP` or `DOWN` observation for each link. Newer observations replace older ones.
- By default observations do not expire. Experiments can set an observation TTL
  so stale `DOWN` observations are reintroduced as unknown/default-up links.
- The estimated topology is `Ge(t) = G0 - recent_down_edges(t)`.
- If a down link is later observed as up, it is reintroduced into `Ge(t)`.
- Edge weights come from the current periodic delay slot: `DelayTable[slot(t)][u][v]`.
- Already visited nodes may be used as relay nodes, but they are excluded as new traversal targets.
- After a telemetry node chooses its Hamiltonian successor, a healthy direct physical link to that successor is used immediately without recalculating the path.
- If that direct link is missing or down, or if the current forwarding path cannot continue, the probe replans to the same `next_telemetry_node`.

## Project Structure

- `models.py`: enums, dataclasses, edge normalization, probe/result models.
- `topology.py`: immutable undirected topology and grid topology generation.
- `constellation.py`: configurable Walker-like constellation topology, dynamic
  seam failures, and propagation-delay tables.
- `delay_table.py`: periodic per-link delay storage.
- `observations.py`: link observation table.
- `planner.py`: Dijkstra path planning and path utility functions.
- `probe_packet.py`: JSON payload carried by UDP probe packets in Mininet.
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
python3 scripts/random_experiments.py
```

The script reads settings from `config/random_experiments.toml` by default. Edit
that file to change the number of scenarios, grid size, delay model, random
failure mode, traversal parameters, simulation step time, and output directory.
The default config is a 10x10 grid, i.e. 100 simulated satellite nodes.
Terminal statuses such as `temporarily_unreachable` and `partial_result` stop
the current scenario and the runner immediately starts the next configured
scenario.

Random failure selection supports two modes:

```toml
[failure]
mode = "probability"
down_probability = 0.1

# or:
mode = "fixed_count"
down_edges_per_scenario = 18
```

Each run writes a folder under `[output].base_dir`, for example:

```text
logs/random/20260609_120000/summary.txt
logs/random/20260609_120000/runs.jsonl
logs/random/20260609_120000/run_config.json
```

If SRv6 simulation is enabled, `policy_events.jsonl` is also written. Set
`[output].run_name` in the TOML to use a stable folder name; existing folders are
not overwritten.

Use a custom TOML file with:

```bash
python3 scripts/random_experiments.py --config config/random_experiments.toml
```

## Development

Install test dependencies and run the suite:

```bash
python3 -m pip install -e ".[test]"
pytest
```

The package intentionally has no runtime dependencies beyond the Python standard library.

## Level 2 Mininet SRv6 Emulation

The Linux/Mininet/tc emulation layer is documented in
[docs/mininet_srv6.md](docs/mininet_srv6.md). Start with:

```bash
python3 -m emulation.env_check
python3 -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml --dry-run
sudo python3 -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml --no-cli
```

The sample Mininet config runs the adaptive Hamiltonian traversal mode. It
uses a configurable 4-plane x 4-satellite constellation, propagation-derived
per-slot delay, rotating dynamic seam links, ping-based link observations, and
per-node UDP probe agents. It installs SRv6 policies for the probe's current
remaining path and writes policy, tc, traversal, and agent logs under one run
folder, such as `logs/mininet/20260609_120000/`.

Change the constellation size in `config/mininet_srv6.toml`:

```toml
[constellation]
planes = 4
satellites_per_plane = 4
```

Change where logs go in `config/mininet_srv6.toml`:

```toml
[output]
base_dir = "logs/mininet"
run_name = ""
```

For `adaptive_traversal`, `planes` must be even and at least 2, and
`satellites_per_plane` must be at least 2 because the Hamiltonian cycle is built
over that rectangular constellation.

Useful overrides:

```bash
python3 -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml \
  --planes 6 --satellites-per-plane 8 --duration 80 --dry-run

sudo python3 -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml \
  --no-dynamic-topology --disable-agents --no-probe-packet-validation --no-cli
```

For randomized multi-scenario Mininet campaigns, use the batch runner:

```bash
python3 -m emulation.mininet_batch_experiments --config config/mininet_batch_experiments.toml
```

The sample batch config runs dry by default and is set up for a 10x10
constellation, randomized link failures, and interrupted scenario statuses such
as `temporarily_unreachable` and `partial_result`. Set `[execution].dry_run =
false` and run with `sudo` for real Mininet execution.

When checking connectivity from the Mininet CLI, prefer the current policy
source node's service-loopback address so replies have a routed return path:

```bash
r9 ping -6 -I 2001:db8:100:9::1 -c 3 2001:db8:100:5::1
```
