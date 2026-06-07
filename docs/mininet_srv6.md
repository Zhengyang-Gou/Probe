# Level 2 Mininet SRv6 Emulation

This layer maps the Python path planner into a real Linux namespace lab:

```text
Python path planner -> SRv6 SID list -> Linux iproute2 route
                   -> Mininet namespace -> tc/netem impairment
```

`AdaptiveTraversalEngine` remains an algorithm layer. It does not call `ip`, `tc`,
or Mininet APIs directly. The `emulation` package is the execution layer.

## Ubuntu 26 Setup

Recommended packages:

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iproute2 iputils-ping tcpdump iperf3 python3-pytest
python -m pip install -e ".[test]"
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
python -m emulation.env_check
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
sudo python -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml --dry-run
```

Run the lab and exit automatically:

```bash
sudo python -m emulation.mininet_srv6_lab \
  --rows 4 --cols 4 --duration 20 \
  --failure-edge 0,1 --failure-start 5 --failure-end 15 \
  --no-cli
```

Run with the sample TOML:

```bash
sudo python -m emulation.mininet_srv6_lab --config config/mininet_srv6.toml --no-cli
```

The script writes:

```text
logs/mininet_srv6_<timestamp>.log
logs/policy_updates_<timestamp>.jsonl
logs/tc_updates_<timestamp>.jsonl
```

## Inspecting the Lab

When the script enters the Mininet CLI, inspect routes and tc state with:

```bash
r0 ip -6 route
r0 tc qdisc show dev r0-r1
r0 ping -6 -I 2001:db8:100:0::1 -c 3 2001:db8:100:f::1
```

The lab uses explicit interface names such as `r0-r1` and `r1-r0`; if routes or
tc qdiscs mention different names, check that `addLink` used `intfName1` and
`intfName2`.

## What Is Real

- Linux SRv6 routes are installed inside Mininet host namespaces with iproute2.
- Transit Node SIDs use `seg6local action End`.
- The fixed target Node SID uses `seg6local action End.DT6 table 254` so the
  final segment decapsulates the inner IPv6 packet and looks up the service
  address in the main table.
- Source policies use `encap seg6 mode encap`.
- Plain IPv6 routes to every node's service loopback are installed as return
  paths for validation traffic.
- `tc netem` delay and loss are applied to the veth interfaces.
- Dynamic replanning is a minimal root-to-target loop that removes or restores a
  configured failed edge from the Python topology and updates the root SRv6
  route when the path changes.

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
traffic, the target SID must use `End.DT6` or another decapsulation behavior,
and the reply source/destination must have a return route. The lab validation
uses `-I 2001:db8:100:0::1` to make replies target r0's service loopback.

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
