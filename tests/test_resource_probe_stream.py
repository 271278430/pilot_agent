"""A remote exit status is not a promise that stdout has reached EOF."""
import json
from contextlib import contextmanager

from pilot_infra.resource_transport import ResourceTransport


def test_observe_drains_final_stdout_after_early_exit_status(tmp_path, monkeypatch):
    payload = json.dumps({"scope": "whole-machine", "schema_version": 1}).encode()

    class Channel:
        eof_received = False
        closed = False
        polls = 0
        reads = 0

        def exec_command(self, command):
            assert command.startswith("python3 -c ")

        def recv_ready(self):
            self.polls += 1
            return self.reads == 0 or self.reads == 1 and self.polls >= 5

        def recv(self, size):
            self.reads += 1
            if self.reads == 1:
                return payload[:10]
            self.eof_received = True
            return payload[10:]

        def recv_stderr_ready(self):
            return False

        def exit_status_ready(self):
            return True

        def recv_exit_status(self):
            return 0

        def close(self):
            self.closed = True

    channel = Channel()

    class SSH:
        def open_session(self, timeout):
            return channel

    @contextmanager
    def connect(server):
        yield SSH()

    transport = ResourceTransport(tmp_path)
    monkeypatch.setattr(transport, "connect", connect)
    result = transport.observe({"connection": {"transport": "ssh"}})
    assert result == json.loads(payload)
    assert channel.reads == 2 and channel.closed
