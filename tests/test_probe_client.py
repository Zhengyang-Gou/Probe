import json

from emulation import probe_client


class FakeSocket:
    def __init__(self) -> None:
        self.timeout = None
        self.bound = None
        self.sent = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def bind(self, address: tuple[str, int]) -> None:
        self.bound = address

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent = (data, address)

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        assert size == 65535
        return json.dumps({"status": "ok"}).encode("utf-8"), ("2001:db8:100:1::1", 5005)


def test_probe_client_binds_source_address(monkeypatch, capsys) -> None:
    fake_socket = FakeSocket()
    monkeypatch.setattr(probe_client.socket, "socket", lambda *_args: fake_socket)

    status = probe_client.main(
        [
            "--run-id",
            "run-1",
            "--sequence",
            "1",
            "--root",
            "0",
            "--source-node",
            "0",
            "--target-node",
            "1",
            "--src",
            "2001:db8:100:0::1",
            "--dst",
            "2001:db8:100:1::1",
        ]
    )

    assert status == 0
    assert fake_socket.bound == ("2001:db8:100:0::1", 0)
    assert fake_socket.sent is not None
    assert fake_socket.sent[1] == ("2001:db8:100:1::1", 5005)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
