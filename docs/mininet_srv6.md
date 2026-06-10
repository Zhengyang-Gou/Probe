# Level 2 Mininet SRv6 Emulation

This layer maps the Python traversal engine into a real Linux namespace lab:

```text
AdaptiveTraversalEngine -> Hamiltonian telemetry target
                        -> SRv6 SID list -> Linux iproute2 route
                        -> Mininet namespace -> tc/netem impairment
                        -> UDP probe packet -> node agent telemetry log
```

`AdaptiveTraversalEngine` remains an algorithm layer. It does not call `ip`, `tc`,
or Mininet APIs directly. The `emulation` package is the execution layer that
turns each algorithm-selected path into a Linux SRv6 policy.

## Ubuntu 26 Setup

Recommended packages:

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iproute2 iputils-ping tcpdump iperf3 python3-pytest
python3 -m pip install -e ".[test]"
```

Do not use old tutorial commands such as `mininet/util/install.sh -a` as the
default setup path on Ubuntu 26. If you must install Mininet from source, do it
as an explicit fallback after checking the apt package situation.

If `mn` is missing:

```bash
apt-cache policy mininet
sudo add-apt-repository universe
sudo apt update
sudo apt install -y mininet openvswitch-switch
```

## Environment Check

Run the read-only checker:

```bash
python3 -m emulation.env_check
```

It checks root status, OS/kernel/tool versions, required commands, iproute2 SRv6
syntax, and SRv6 sysctl keys. On WSL it prints a warning:

```text
WSL may not support Mininet/SRv6/tc reliably. Use bare-metal Ubuntu or a full VM.
```

SRv6 checks you can run manually:

```bash
ip -6 route help | grep -E "seg6|seg6local"
sysctl net.ipv6.conf.all.seg6_enabled
```

## Minimal Run

Dry-run without starting Mininet:

```bash
python3 -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml --dry-run
```

Run the lab and exit automatically using only the sample TOML configuration:

```bash
sudo python3 -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml --no-cli
```

You can still override values from the CLI when doing one-off tests:

```bash
sudo python3 -m emulation.mininet_srv6_lab \
  --planes 4 --satellites-per-plane 4 --duration 40 \
  --algorithm-mode adaptive_traversal \
  --failure-edge 9,5 --failure-start 5 --failure-end 15 \
  --no-cli
```

The script writes one folder per run:

```text
logs/mininet/<run>/
  run_config.json
  mininet_srv6.log
  policy_updates.jsonl
  tc_updates.jsonl
  traversal_events.jsonl
  agent_r<node>.jsonl
```

The output location is controlled in the TOML:

```toml
[output]
base_dir = "logs/mininet"
run_name = ""
```

If `run_name` is empty, a timestamp is used. If a folder already exists, the
script appends `_2`, `_3`, and so on instead of overwriting it.

The sample TOML uses `adaptive_traversal`, a 4-plane x 4-satellite
constellation, and the Hamiltonian cycle:

```text
0 -> 4 -> 8 -> 12 -> 13 -> 9 -> 5 -> 6 -> 10 -> 14 -> 15 -> 11 -> 7 -> 3 -> 2 -> 1 -> 0
```

The configured `9 <-> 5` failure is active from 5s to 15s, so the default run
exercises a replan when the probe tries to move from `r9` to `r5`.

The constellation scale is controlled by:

```toml
[constellation]
planes = 4
satellites_per_plane = 4
```

`rows` and `cols` are still accepted as legacy aliases. In
`adaptive_traversal` mode, `planes` must be even and `inter_plane_links` must be
true because the Hamiltonian cycle uses inter-plane edges.

The default realism knobs are:

```toml
[delay]
model = "propagation"
period_slots = 8

[dynamic_topology]
enabled = true
model = "rotating_seam"

[observation]
mode = "ping"
stale_after_seconds = 2

[agent]
enabled = true
probe_packet_validation = true
```

For a baseline/control run, disable the dynamic and agent pieces from the CLI:

```bash
sudo python3 -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml \
  --no-dynamic-topology --disable-agents --no-probe-packet-validation --no-cli
```

## Batch Mininet Scenarios

For larger experiment campaigns, use the batch runner:

```bash
python3 -m emulation.mininet_batch_experiments --config config/mininet_batch_experiments.toml
```

The default batch config is a dry run. It uses a 10 x 10 constellation, i.e. 100
Mininet nodes, and generates randomized link-failure scenarios. Set:

```toml
[execution]
dry_run = false
```

Then run with root privileges for real Mininet/SRv6/tc execution:

```bash
sudo python3 -m emulation.mininet_batch_experiments --config config/mininet_batch_experiments.toml
```

Each scenario starts a fresh `emulation.mininet_srv6_lab` run. If traversal
terminates with a configured status such as `temporarily_unreachable` or
`partial_result`, that scenario is recorded and the batch runner immediately
starts the next one.

Scenario count and failure policy are configured in one TOML file:

```toml
[scenario]
count = 20
interrupt_statuses = ["temporarily_unreachable", "partial_result"]

[failure]
mode = "probability"
down_probability = 0.1

# or:
mode = "fixed_count"
down_edges_per_scenario = 18
```

The batch output directory contains:

```text
logs/mininet-batch/<batch>/
  batch_config.json
  summary.txt
  scenarios.jsonl
  <scenario>.stdout.log
  <scenario>/
    run_config.json
    traversal_events.jsonl
    policy_updates.jsonl
    tc_updates.jsonl
```

## Inspecting the Lab

When the script enters the Mininet CLI, inspect routes and tc state with:

```bash
r9 ip -6 route
r9 tc qdisc show dev r9-r8
r9 ping -6 -I 2001:db8:100:9::1 -c 3 2001:db8:100:5::1
```

The lab uses explicit interface names such as `r0-r1` and `r1-r0`; if routes or
tc qdiscs mention different names, check that `addLink` used `intfName1` and
`intfName2`.

## What Is Real

- Linux SRv6 routes are installed inside Mininet host namespaces with iproute2.
- Transit Node SIDs use `seg6local action End` on a node interface rather than
  `lo`, because Linux processes SRv6 local actions on the route device.
- Every node also has a separate decapsulation SID using
  `seg6local action End.DT6 table <decap-table>` on a node interface, so any
  Hamiltonian telemetry target can be the final SRv6 segment and decapsulated
  probe traffic is delivered to that node's UDP agent through the service
  loopback local route.
- Source policies use `encap seg6 mode encap`.
- Plain IPv6 routes to every node's service loopback are installed as return
  paths for validation traffic.
- `tc netem` delay and loss are applied to the veth interfaces. With
  `delay.model = "propagation"`, per-slot delay comes from a simple orbital
  geometry model rather than a single constant.
- Dynamic topology schedules can remove links by applying `loss 100%`; the
  sample `rotating_seam` model moves a disabled inter-plane seam each slot.
- In `adaptive_traversal` mode, the probe follows the grid Hamiltonian cycle,
  observes link state through the configured provider, replans with the Python
  engine, and installs the current remaining path as an SRv6 policy on the
  current Mininet node.
- With `observation.mode = "ping"`, the engine checks adjacent Mininet links by
  running IPv6 ping from the node namespace. Stale DOWN observations can expire
  via `observation.stale_after_seconds`, which lets dynamic links recover in
  the estimated topology.
- With agents enabled, every node runs `emulation.node_agent` and accepts a UDP
  `ProbePacketPayload`. Validation sends the packet through the installed SRv6
  policy with `emulation.probe_client`, and the target agent logs packet fields
  plus local interface observations.
- `root_target` mode remains available for the old single source-to-target SRv6
  smoke test.

## Common Errors

`RTNETLINK answers: Operation not permitted`

Run the lab with `sudo`.

`seg6local unknown`

The kernel or iproute2 syntax may not support SRv6. Check:

```bash
ip -6 route help | grep -E "seg6|seg6local"
```

`ping` does not work

Check IPv6 forwarding, `seg6_enabled`, underlay routes, Node SID routes, and
whether `tc` is currently applying `loss 100%` on the path. For encapsulated
traffic, the final segment must be one of the per-node decapsulation SIDs, and
the reply source/destination must have a return route. The lab validation uses
the service-loopback address of the current policy source, such as
`-I 2001:db8:100:9::1`.

`probe packet validation failed`

Check that `[agent] enabled = true`, no other process is using the configured
UDP port, and the policy target's service loopback is reachable. The target
node's `logs/mininet/<run>/agent_r<node>.jsonl` file records whether the packet
arrived and what telemetry was observed.

`Mininet import failed`

Check apt installation, Python path, and:

```bash
mn --version
```

Open vSwitch or stale Mininet state issues:

```bash
sudo mn -c
sudo systemctl restart openvswitch-switch
```

WSL is not recommended for this lab because Mininet namespaces, SRv6, and
tc/netem support may be incomplete. Prefer bare-metal Ubuntu or a full VM.
