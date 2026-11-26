from pathlib import Path

from repopilot.demo import DEMO_FILES
from repopilot.models import Task
from repopilot.repository import git

SERVICE = """import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from checkout import total

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--run-id", required=True)
args = parser.parse_args()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"run_id": args.run_id, "healthy": True}).encode())

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        value = total(payload["items"], payload["discount"])
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"value": value}).encode())

HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
"""


def create_fixture(root: Path) -> Task:
    root.mkdir()
    for name, text in {**DEMO_FILES, "service.py": SERVICE}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(root, "init")
    git(root, "config", "user.name", "Collaboration fixture")
    git(root, "config", "user.email", "fixture@localhost")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: immutable collaboration fixture")
    return Task(
        id="collaborative-checkout",
        repo=str(root),
        commit=git(root, "rev-parse", "HEAD").stdout.strip(),
        issue="Fix quantity and percentage handling in checkout.py and money.py. The fixed total of [(10,2),(5,1)] with discount 10 must be 22.5. Preserve service.py and every test. No configuration changes.",
        target=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_checkout.py"],
        regression=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        source_roots=["checkout.py", "money.py"],
    )
