"""Check staged source files for common private material before publication."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TOP = {".local", ".venv", "resources", "runs", "reports", "workspace", "workspaces", "upstream"}
PRIVATE_PARTS = {"node_modules", "__pycache__", "private"}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db"}
KEY = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
TOKEN = re.compile(rb"\b(?:sk-[A-Za-z0-9_-]{18,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|hf_[A-Za-z0-9]{25,}|xox[baprs]-[A-Za-z0-9-]{16,}|AIza[A-Za-z0-9_-]{35})\b")
ASSIGNMENT = re.compile(rb"\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password)\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']", re.I)
USERINFO = re.compile(rb"https?://[^\s/:@]+:[^\s/@]+@[^\s/\"']+", re.I)


def check_blob(name: str, data: bytes) -> list[str]:
    parts = Path(name).parts
    reasons = []
    if parts[0] in PRIVATE_TOP or any(part in PRIVATE_PARTS for part in parts):
        reasons.append("private or generated directory")
    filename = parts[-1].lower()
    if filename == ".env" or (filename.startswith(".env.") and not filename.endswith(".example")):
        reasons.append("private environment file")
    if Path(filename).suffix in PRIVATE_SUFFIXES:
        reasons.append("private credential or database file")
    if KEY.search(data):
        reasons.append("private-key marker")
    if TOKEN.search(data):
        reasons.append("credential-like token")
    for match in ASSIGNMENT.finditer(data):
        value = match.group(1).lower()
        examples = (b"fixture", b"example", b"fake", b"placeholder", b"dummy", b"test", b"replace", b"masked")
        if not any(marker in value for marker in examples) and value not in {b"secret value", b"private", b"password"}:
            reasons.append("credential-like assignment")
    for match in USERINFO.finditer(data):
        try:
            host = urlsplit(match.group().decode("ascii")).hostname or ""
        except ValueError:
            host = ""
        if host not in {"example.com", "example.org", "example.net"} and not host.endswith((".example", ".invalid")):
            reasons.append("credentials in URL")
    return reasons


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=ROOT, capture_output=True, check=True)
    entries = []
    for row in result.stdout.split(b"\0"):
        if not row:
            continue
        metadata, filename = row.split(b"\t", 1)
        mode, sha, stage = metadata.decode("ascii").split()
        if stage != "0" or mode not in {"100644", "100755"}:
            raise ValueError("Resolve non-file or unmerged index entries before publication")
        entries.append((filename.decode("utf-8"), sha))
    if not entries:
        raise ValueError("No tracked files to inspect")
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin and proc.stdout
    failures = []
    for name, sha in entries:
        proc.stdin.write((sha + "\n").encode("ascii"))
        proc.stdin.flush()
        header = proc.stdout.readline().decode("ascii").split()
        if len(header) != 3 or header[1] != "blob":
            raise ValueError("Cannot inspect tracked file")
        data = proc.stdout.read(int(header[2]))
        if proc.stdout.read(1) != b"\n":
            raise ValueError("Invalid Git blob response")
        failures.extend((name, reason) for reason in check_blob(name, data))
    proc.stdin.close()
    if proc.wait(timeout=30):
        raise ValueError("Git could not complete the index scan")
    for name, reason in failures:
        print(f"{name}: {reason}")
    print(f"Publication scan: {len(entries)} tracked files, {len(failures)} findings")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
