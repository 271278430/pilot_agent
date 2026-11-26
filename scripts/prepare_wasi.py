"""Install a pinned, digest-checked CPython WASI runtime inside this project."""

import hashlib
import stat
import zipfile
from pathlib import Path

import httpx

from repopilot.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]
URL = "https://github.com/brettcannon/cpython-wasi-build/releases/download/v3.14.2/python-3.14.2-wasi_sdk-24.zip"
SHA = "af31d6d63f8833fbaba7cd8013893fc06fdf9af3f6abfb14223d061858ac4a4f"


def main():
    directory = ROOT / ".local/wasi"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "python-3.14.2.zip"
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SHA:
        with httpx.Client(follow_redirects=True, timeout=120) as client:
            response = client.get(URL)
            response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != SHA:
            raise ValueError("Runtime download digest mismatch")
        archive.write_bytes(response.content)
    destination = directory / "python-3.14.2"
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            target = (destination / item.filename).resolve()
            if not target.is_relative_to(destination.resolve()) or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("Unsafe runtime archive entry")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(item))
    modules = list(destination.rglob("python.wasm"))
    if len(modules) != 1:
        raise ValueError("Ambiguous interpreter module")
    libraries = list(destination.rglob("python3.14"))
    atomic_json(directory / "runtime.json", {"version": "3.14.2", "archive_sha256": SHA,
                "module": str(modules[0]), "module_sha256": hashlib.sha256(modules[0].read_bytes()).hexdigest(),
                "libraries": [str(p) for p in libraries if p.is_dir()], "source": URL})
    print({"module": str(modules[0].relative_to(ROOT)), "libraries": [str(p.relative_to(ROOT)) for p in libraries]})


if __name__ == "__main__":
    main()
