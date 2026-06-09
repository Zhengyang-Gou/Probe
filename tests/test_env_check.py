import subprocess

from emulation import env_check


def test_srv6_iproute2_syntax_passes_when_help_mentions_seg6(monkeypatch) -> None:
    monkeypatch.setattr(env_check.shutil, "which", lambda command: f"/usr/sbin/{command}")
    monkeypatch.setattr(
        env_check,
        "_run",
        lambda argv: subprocess.CompletedProcess(
            argv,
            255,
            stdout="",
            stderr="ENCAPTYPE := [ mpls | ip | ip6 | seg6 | seg6local ]",
        ),
    )

    result = env_check._check_srv6_iproute2_syntax()

    assert result.status == "PASS"


def test_srv6_iproute2_syntax_warns_when_netlink_is_restricted(monkeypatch) -> None:
    monkeypatch.setattr(env_check.shutil, "which", lambda command: f"/usr/sbin/{command}")
    monkeypatch.setattr(
        env_check,
        "_run",
        lambda argv: subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="Cannot open netlink socket: Operation not permitted",
        ),
    )

    result = env_check._check_srv6_iproute2_syntax()

    assert result.status == "WARN"
    assert "rerun outside the sandbox" in result.detail
