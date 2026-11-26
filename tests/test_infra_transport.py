import io
import json

import pytest

from infra_pilot.transport import Remote


class Channel:
    def recv_exit_status(self):
        return 0


class Output(io.BytesIO):
    channel = Channel()


class LostOutput:
    def read(self):
        raise EOFError("launch response lost after remote mutation")


class Client:
    def __init__(self, lose_reply=False):
        self.active = True
        self.lose_reply = lose_reply
        self.commands = []

    def get_transport(self):
        return self

    def is_active(self):
        return self.active

    def is_authenticated(self):
        return self.active

    def exec_command(self, command, timeout):
        self.commands.append(command)
        output = LostOutput() if self.lose_reply else Output(json.dumps({"status": "completed"}).encode())
        return None, output, Output(b"")

    def close(self):
        self.active = False


def test_reconnect_only_before_next_command_never_replays_ambiguous_launch():
    remote = object.__new__(Remote)
    first, second = Client(lose_reply=True), Client()
    remote.client, remote.sftp = first, None
    connections = []

    def connect():
        connections.append(True)
        remote.client, remote.sftp = second, None

    remote._connect = connect
    with pytest.raises(EOFError, match="response lost"):
        remote.command(["python3", "worker.py", "start", "inference", "inference-original-id"])
    assert len(first.commands) == 1
    assert not first.active
    assert not connections
    assert remote.command(["python3", "worker.py", "inspect", "inference-original-id"])["status"] == "completed"
    assert len(connections) == 1
    assert len(second.commands) == 1
    assert "inspect" in second.commands[0]
    assert "start" not in second.commands[0]


def test_reconnects_closed_transport_before_inspecting_original_job():
    remote = object.__new__(Remote)
    first, second = Client(), Client()
    first.close()
    remote.client, remote.sftp = first, None
    remote._connect = lambda: setattr(remote, "client", second)
    assert remote.command(["inspect", "inference-original-id"])["status"] == "completed"
    assert not first.commands
    assert len(second.commands) == 1
