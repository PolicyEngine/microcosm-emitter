"""The host's socket client sends each message to the module it names."""

import pytest

from microcosm_emitter.host.client import SocketClient
from microcosm_emitter.host.errors import LocalServiceError


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """Return a client that records its requests instead of sending them."""
    client = SocketClient(tmp_path / "socket")
    sent_payloads = []

    def record_request(payload, **limits):
        sent_payloads.append(payload)

    monkeypatch.setattr(client, "_request", record_request)
    return client, sent_payloads


def test_a_module_client_sends_each_message_with_its_module_name(recorded):
    client, sent_payloads = recorded
    client.for_module("orrery").send({"command": "publish"})
    client.send({"command": "emit"})
    assert sent_payloads == [
        {"action": "message", "module": "orrery", "message": {"command": "publish"}},
        {"action": "message", "message": {"command": "emit"}},
    ]
    # The keys keep their order, so the JSON on the socket is the same bytes.
    assert [list(payload) for payload in sent_payloads] == [
        ["action", "module", "message"],
        ["action", "message"],
    ]


def test_a_module_client_of_a_closed_client_sends_nothing(recorded):
    client, sent_payloads = recorded
    client.closed = True
    with pytest.raises(LocalServiceError, match=r"^client is closed$"):
        client.for_module("orrery").send({"command": "publish"})
    assert sent_payloads == []
