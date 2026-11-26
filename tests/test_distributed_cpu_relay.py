"""Failure boundaries for the isolated experimental two-host CPU relay."""

import importlib.util
import json
import queue
import socket
import time
from pathlib import Path

import pytest


@pytest.fixture
def gateway(monkeypatch):
    original = Path.read_text
    config = {'role': 'head', 'self_ip': '127.0.0.2', 'peer_ip': '127.0.0.3', 'deadline': time.time() + 60}

    def read(path, *args, **kwargs):
        if path.name == 'config.json' and path.parent.name == 'code':
            return json.dumps(config)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    path = Path(__file__).resolve().parents[1] / 'scripts/cpu_ray_gateway.py'
    spec = importlib.util.spec_from_file_location('test_cpu_gateway', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    module.stopping.set()
    for sock in list(module.listeners.values()):
        sock.close()
    for key in list(module.connections):
        module.close_connection(key)


def test_unobserved_peer_port_is_rejected_without_outbound_dial(gateway, monkeypatch):
    frames = []
    monkeypatch.setattr(gateway, 'emit', frames.append)
    monkeypatch.setattr(gateway, 'observed_ports', lambda: set())
    monkeypatch.setattr(socket, 'create_connection', lambda *_a, **_k: pytest.fail('unexpected dial'))
    gateway.handle({'type': 'dial', 'id': 'run-id', 'port': 32010})
    assert frames == [{'type': 'close', 'id': 'run-id'}]


def test_both_halves_closed_release_connection(gateway):
    first, second = socket.socketpair()
    try:
        gateway.register('connection', first, start=False)
        gateway.close_half('connection', 'local')
        assert 'connection' in gateway.connections
        gateway.close_half('connection', 'peer')
        assert 'connection' not in gateway.connections and first.fileno() == -1
    finally:
        second.close()


def test_open_frame_precedes_payload_on_live_loopback(gateway, monkeypatch):
    frames = queue.Queue()
    monkeypatch.setattr(gateway, 'emit', frames.put)
    with socket.socket() as probe:
        probe.bind(('127.0.0.3', 0))
        port = probe.getsockname()[1]
    gateway.listen(port)
    with socket.create_connection(('127.0.0.3', port), timeout=3) as client:
        client.sendall(b'example')
        first, second = frames.get(timeout=3), frames.get(timeout=3)
        assert first['type'] == 'open' and second['type'] == 'data'
        assert first['id'] == second['id']
