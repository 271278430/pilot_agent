"""Trusted data-only ModelScope download; no model repository code execution."""

import hashlib
import json
from pathlib import PurePosixPath

import httpx


def download(plan, directory):
    directory.mkdir(parents=True, exist_ok=True)
    endpoint = "https://modelscope.cn/api/v1/models/" + plan.model_id
    with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
        response = client.get(
            endpoint + "/repo/files", params={"Recursive": "true", "Revision": plan.revision}
        )
        response.raise_for_status()
        files = response.json()["Data"]["Files"]
        selected = []
        for item in files:
            name = item.get("Path") or item.get("Name", "")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Unsafe model asset path")
            if item.get("Type") == "tree":
                continue
            if path.suffix not in {
                ".safetensors",
                ".json",
                ".model",
                ".txt",
                ".jinja",
                ".tiktoken",
            }:
                continue
            if len(path.parts) > 2:
                continue
            selected.append((name, item))
        total = sum(int(item["Size"]) for _, item in selected)
        if not any(name.endswith(".safetensors") for name, _ in selected):
            raise ValueError("Only safetensors model weights are supported")
        if total > plan.download_limit_mb * 1024 * 1024:
            raise ValueError(
                "Model download exceeds approved plan budget; use a smaller model or revise budget"
            )
        manifest = {}
        for name, item in selected:
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            expected = item.get("Sha256") or item.get("SHA256")
            if target.exists():
                if target.is_symlink():
                    raise ValueError("Unsafe model cache")
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if expected and digest == expected:
                    manifest[name] = {"sha256": digest, "bytes": target.stat().st_size}
                    continue
                raise ValueError("Existing model download requires inspection before retry")
            tmp = target.with_suffix(target.suffix + ".partial")
            h = hashlib.sha256()
            count = 0
            with client.stream(
                "GET", endpoint + "/repo", params={"Revision": plan.revision, "FilePath": name}
            ) as stream:
                stream.raise_for_status()
                with tmp.open("wb") as out:
                    for block in stream.iter_bytes(1024 * 1024):
                        count += len(block)
                        if count > int(item["Size"]):
                            raise ValueError("Model asset exceeds advertised size")
                        h.update(block)
                        out.write(block)
            if count != int(item["Size"]) or (expected and expected != h.hexdigest()):
                raise ValueError("Model asset integrity mismatch")
            tmp.replace(target)
            manifest[name] = {"sha256": h.hexdigest(), "bytes": count}
    config = json.loads((directory / "config.json").read_text())
    if config.get("auto_map"):
        raise ValueError("Remote custom model code is not allowed")
    if config.get("num_hidden_layers", 100) > 32 or config.get("hidden_size", 100000) > 2048:
        raise ValueError("CPU demo model exceeds supported runtime size; revise deployment plan")
    (directory / "download-manifest.json").write_text(
        json.dumps(
            {"model_id": plan.model_id, "requested_revision": plan.revision, "assets": manifest}
        )
    )
    return {"bytes": total, "files": len(manifest), "source": plan.model_id}
