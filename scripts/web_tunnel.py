"""Localhost-only forward to the project web; credentials stay in local config."""

import argparse
import select
import socketserver
import threading

from remote_project import connect

client = None
connection_lock = threading.Lock()
parser = argparse.ArgumentParser()
parser.add_argument('--local-port', type=int, default=18889)
parser.add_argument('--remote-port', type=int, default=18888)
ports = parser.parse_args()


def transport():
    global client
    with connection_lock:
        active = client.get_transport() if client else None
        if not active or not active.is_active():
            if client:
                client.close()
            client, _ = connect()
            active = client.get_transport()
            active.set_keepalive(15)
        return active


class Forward(socketserver.BaseRequestHandler):
    def handle(self):
        # Reconnect before forwarding bytes; never replay an in-flight HTTP mutation.
        channel = transport().open_channel(
            "direct-tcpip", ("127.0.0.1", ports.remote_port), self.request.getpeername()
        )
        try:
            while True:
                ready, _, _ = select.select([self.request, channel], [], [], 30)
                if self.request in ready:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in ready:
                    data = channel.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


try:
    with Server(("127.0.0.1", ports.local_port), Forward) as server:
        print(f"Project web available at http://127.0.0.1:{ports.local_port}", flush=True)
        server.serve_forever()
finally:
    if client:
        client.close()
