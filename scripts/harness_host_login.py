"""One local browser redirect; keep the official login URL out of tool output."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from remote_project import REMOTE, connect

parser = argparse.ArgumentParser()
parser.add_argument('--remote-root', default=REMOTE)
parser.add_argument('--local-root', type=Path)
parser.add_argument('--port', type=int, default=18892)
args = parser.parse_args()
if args.local_root:
    entry = json.loads((args.local_root / '.local/harness-web/entry.json').read_text())
else:
    client, _ = connect()
    with client.open_sftp() as sftp:
        entry = json.loads(sftp.open(args.remote_root + "/.local/harness-web/entry.json").read())
    client.close()


class Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", entry["url"])
        self.end_headers()

    def log_message(self, *args):
        pass


with HTTPServer(("127.0.0.1", args.port), Redirect) as server:
    server.timeout = 60
    print("Private local browser entry ready", flush=True)
    server.handle_request()
