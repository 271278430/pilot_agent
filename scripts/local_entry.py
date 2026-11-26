"""Loopback redirect to the official private owner URL; never print credentials."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--port", type=int, default=18900)
args = parser.parse_args()


class Entry(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            entry = json.loads((args.root / ".local/harness-web/entry.json").read_text(encoding="utf-8"))["url"]
            parsed = urlsplit(entry)
            if parsed.hostname != "127.0.0.1" or parsed.scheme != "http":
                raise ValueError("Entry is not loopback")
            self.send_response(302)
            self.send_header("Location", entry)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except (OSError, KeyError, ValueError):
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Pilot is starting. Refresh in a moment.")

    def log_message(self, *_):
        pass


with ThreadingHTTPServer(("127.0.0.1", args.port), Entry) as server:
    server.serve_forever()
