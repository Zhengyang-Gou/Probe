"""Environment checks for the Linux/Mininet/SRv6 emulation layer."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


APT_INSTALL_HINT = """Recommended Ubuntu packages:
  sudo apt update
  sudo apt install -y mininet openvswitch-switch iproute2 iputils-ping tcpdump iperf3 python3-pytest"""


@dataclass(slots=True)
class CheckResult:
    status: str
    name: str
    detail: str


def main(argv: list[str] | None = None) -> int:
    del argv
    results: list[CheckResult] = []

    results.append(_check_root())
    results.extend(_check_system_info())
    command_results = _check_required_commands()
    results.extend(command_results)
    results.append(_check_srv6_iproute2_syntax())
    results.extend(_check_sysctl_keys())
    results.extend(_check_platform_warnings(command_results))

    for result in results:
        print(f"{result.status:4} {result.name}: {result.detail}")

    if any(result.status == "FAIL" for result in results):
        print()
        print(APT_INSTALL_HINT)
        return 1
    return 0


def _check_root() -> CheckResult:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return CheckResult("PASS", "root", "running as root")
    return CheckResult(
        "WARN",
        "root",
        "not running as root; Mininet/SRv6/tc experiments require sudo, "
        "but this read-only checker will continue",
    )


def _check_system_info() -> list[CheckResult]:
    results: list[CheckResult] = []

    os_release = _read_os_release()
    if shutil.which("lsb_release"):
        completed = _run(["lsb_release", "-a"])
        detail = _compact_output(completed)
        status = "PASS" if completed.returncode == 0 else "WARN"
        results.append(CheckResult(status, "lsb_release -a", detail or "no output"))
    elif os_release:
        pretty = os_release.get("PRETTY_NAME") or os_release.get("NAME") or "unknown"
        results.append(CheckResult("PASS", "/etc/os-release", pretty))
    else:
        results.append(CheckResult("WARN", "os-release", "could not determine distribution"))

    checks = [
        ("uname -r", ["uname", "-r"]),
        ("python3 --version", ["python3", "--version"]),
        ("ip -V", ["ip", "-V"]),
        ("tc -V", ["tc", "-V"]),
        ("mn --version", ["mn", "--version"]),
    ]
    for name, command in checks:
        if not shutil.which(command[0]):
            results.append(CheckResult("FAIL", name, f"{command[0]} not found in PATH"))
            continue
        completed = _run(command)
        detail = _compact_output(completed)
        status = "PASS" if completed.returncode == 0 else "WARN"
        results.append(CheckResult(status, name, detail or f"returncode {completed.returncode}"))

    return results


def _check_required_commands() -> list[CheckResult]:
    results: list[CheckResult] = []
    for command in ["ip", "tc", "tcpdump", "mn", "ovs-vsctl"]:
        path = shutil.which(command)
        status = "PASS" if path else "FAIL"
        detail = path or "not found in PATH"
        results.append(CheckResult(status, f"command {command}", detail))

    ping_path = shutil.which("ping") or shutil.which("ping6")
    if ping_path:
        results.append(CheckResult("PASS", "command ping/ping6", ping_path))
    else:
        results.append(CheckResult("FAIL", "command ping/ping6", "neither ping nor ping6 found"))
    return results


def _check_srv6_iproute2_syntax() -> CheckResult:
    if not shutil.which("ip"):
        return CheckResult("FAIL", "SRv6 iproute2 syntax", "ip command not found")

    completed = _run(["ip", "-6", "route", "help"])
    output = f"{completed.stdout}\n{completed.stderr}"
    if "seg6" in output or "seg6local" in output:
        return CheckResult(
            "PASS",
            "SRv6 iproute2 syntax",
            "ip -6 route help advertises seg6/seg6local",
        )
    if "Operation not permitted" in output or "Cannot open netlink socket" in output:
        return CheckResult(
            "WARN",
            "SRv6 iproute2 syntax",
            "ip -6 route help could not open netlink socket in this environment; "
            "rerun outside the sandbox or with sudo to confirm seg6/seg6local support",
        )
    return CheckResult(
        "FAIL",
        "SRv6 iproute2 syntax",
        "ip -6 route help did not mention seg6 or seg6local; kernel/iproute2 may lack SRv6 support",
    )


def _check_sysctl_keys() -> list[CheckResult]:
    keys = [
        "net.ipv6.conf.all.forwarding",
        "net.ipv6.conf.all.seg6_enabled",
        "net.ipv6.conf.default.seg6_enabled",
    ]
    results: list[CheckResult] = []
    if not shutil.which("sysctl"):
        return [CheckResult("FAIL", "sysctl", "sysctl command not found")]

    for key in keys:
        completed = _run(["sysctl", "-n", key])
        if completed.returncode == 0:
            results.append(CheckResult("PASS", f"sysctl {key}", completed.stdout.strip()))
        else:
            detail = _compact_output(completed) or "key not readable"
            results.append(CheckResult("FAIL", f"sysctl {key}", detail))
    return results


def _check_platform_warnings(command_results: list[CheckResult]) -> list[CheckResult]:
    results: list[CheckResult] = []
    os_release = _read_os_release()
    distro_id = (os_release.get("ID") or "").lower()
    version_id = os_release.get("VERSION_ID") or ""

    if distro_id == "ubuntu" and version_id.startswith("26"):
        results.append(
            CheckResult(
                "WARN",
                "Ubuntu 26",
                "Mininet apt packages and Python APIs may differ from old tutorials; "
                "do not assume mininet/util/install.sh -a is available",
            )
        )

    mn_missing = any(
        result.name == "command mn" and result.status == "FAIL" for result in command_results
    )
    if mn_missing:
        results.append(
            CheckResult(
                "FAIL",
                "Mininet install hint",
                "mn not found; try apt install mininet/openvswitch-switch or check universe",
            )
        )

    proc_version = _read_text(Path("/proc/version"))
    if "microsoft" in proc_version.lower() or "wsl" in proc_version.lower():
        results.append(
            CheckResult(
                "WARN",
                "WSL",
                "WSL may not support Mininet/SRv6/tc reliably. "
                "Use bare-metal Ubuntu or a full VM.",
            )
        )

    if platform.system().lower() != "linux":
        results.append(CheckResult("FAIL", "platform", "Mininet/SRv6 emulation requires Linux"))

    return results


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True)


def _compact_output(completed: subprocess.CompletedProcess[str]) -> str:
    text = completed.stdout.strip() or completed.stderr.strip()
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


def _read_os_release() -> dict[str, str]:
    content = _read_text(Path("/etc/os-release"))
    values: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
