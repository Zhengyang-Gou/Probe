"""tc/netem command rendering helpers for Mininet emulation."""

from __future__ import annotations


def render_tc_netem(
    dev: str,
    delay_ms: float | None = None,
    jitter_ms: float | None = None,
    loss_percent: float | None = None,
    rate_mbit: float | None = None,
    replace: bool = True,
) -> list[str]:
    """Render a tc netem qdisc command."""

    dev = _require_non_empty(dev, "dev")
    _validate_optional_non_negative(delay_ms, "delay_ms")
    _validate_optional_non_negative(jitter_ms, "jitter_ms")
    _validate_optional_range(loss_percent, "loss_percent", minimum=0.0, maximum=100.0)
    _validate_optional_positive(rate_mbit, "rate_mbit")

    if delay_ms is None and jitter_ms is None and loss_percent is None and rate_mbit is None:
        raise ValueError("at least one netem impairment must be configured")
    if delay_ms is None and jitter_ms is not None:
        raise ValueError("jitter_ms requires delay_ms")

    operation = "replace" if replace else "add"
    command = ["tc", "qdisc", operation, "dev", dev, "root", "netem"]
    if delay_ms is not None:
        command.extend(["delay", _format_ms(delay_ms)])
        if jitter_ms is not None:
            command.append(_format_ms(jitter_ms))
    if loss_percent is not None:
        command.extend(["loss", _format_percent(loss_percent)])
    if rate_mbit is not None:
        command.extend(["rate", _format_mbit(rate_mbit)])
    return command


def render_tc_loss100(dev: str) -> list[str]:
    """Render a qdisc command that drops all traffic on an interface."""

    return render_tc_netem(dev, loss_percent=100.0)


def render_tc_delete(dev: str) -> list[str]:
    """Render a command that removes the root qdisc."""

    return ["tc", "qdisc", "del", "dev", _require_non_empty(dev, "dev"), "root"]


def render_tc_show(dev: str) -> list[str]:
    """Render a command that shows qdisc state for an interface."""

    return ["tc", "qdisc", "show", "dev", _require_non_empty(dev, "dev")]


def edge_iface_name(u: int, v: int) -> str:
    """Return the explicit directional interface name for edge ``u -> v``."""

    return f"r{u}-r{v}"


def _format_ms(value: float) -> str:
    return f"{value:.3f}ms"


def _format_percent(value: float) -> str:
    return f"{value:.3f}%"


def _format_mbit(value: float) -> str:
    return f"{value:.3f}mbit"


def _require_non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()


def _validate_optional_non_negative(value: float | None, name: str) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be non-negative")


def _validate_optional_positive(value: float | None, name: str) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be positive")


def _validate_optional_range(
    value: float | None,
    name: str,
    minimum: float,
    maximum: float,
) -> None:
    if value is not None and not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
