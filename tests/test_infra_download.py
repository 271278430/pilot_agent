import hashlib
import io
import json
from urllib.error import HTTPError

import pytest

from infra_pilot import worker
from infra_pilot.service import diagnose


@pytest.fixture
def cached_model(tmp_path, monkeypatch):
    root = tmp_path
    (root / "models/smollm").mkdir(parents=True)
    (root / "reports").mkdir()
    content = b"verified-weights"
    weight_hash = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(worker, "PINNED_WEIGHT_SHA", weight_hash)
    files = {}
    for name in ["model.safetensors", "config.json", "tokenizer.json", "tokenizer_config.json"]:
        data = content if name == "model.safetensors" else b"{}"
        (root / "models/smollm" / name).write_bytes(data)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "expected_lfs_sha256": weight_hash if name == "model.safetensors" else None}
    spec = {"model_id": "approved-model", "revision": "pinned-revision", "model_endpoint": "https://hf-mirror.com"}
    receipt = {**spec, "endpoint": spec["model_endpoint"], "trust_remote_code": False, "files": files}
    worker.atomic(root / "reports/download.json", receipt)
    return root, spec, content


def test_verified_manifest_cache_needs_no_metadata_network(cached_model, monkeypatch):
    root, spec, _ = cached_model

    def no_network(*args, **kwargs):
        raise AssertionError("Cached immutable artifacts should not request metadata")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    result = worker.download_model(root, spec)
    assert result["metadata_source"] == "verified_download_receipt"
    assert len(result["files"]) == 4


def test_corrupt_weights_use_cached_hash_but_must_redownload(cached_model, monkeypatch):
    root, spec, content = cached_model
    (root / "models/smollm/model.safetensors").write_bytes(b"corrupt")
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        return io.BytesIO(content)

    monkeypatch.setattr("urllib.request.urlopen", download)
    result = worker.download_model(root, spec)
    assert (root / "models/smollm/model.safetensors").read_bytes() == content
    assert len(requests) == 1
    assert "/resolve/pinned-revision/model.safetensors" in requests[0]
    assert result["files"]["model.safetensors"]["sha256"] == worker.PINNED_WEIGHT_SHA


def test_modified_receipt_hash_is_never_accepted(cached_model, monkeypatch):
    root, spec, _ = cached_model
    receipt = json.loads((root / "reports/download.json").read_text())
    receipt["files"]["model.safetensors"]["sha256"] = "0"*64
    worker.atomic(root / "reports/download.json", receipt)
    metadata = {"sha": spec["revision"], "siblings": [{"rfilename": "model.safetensors", "lfs": {"sha256": worker.PINNED_WEIGHT_SHA}}]}
    requests = []

    def network(url, timeout):
        requests.append(url)
        return io.BytesIO(json.dumps(metadata).encode())

    monkeypatch.setattr("urllib.request.urlopen", network)
    result = worker.download_model(root, spec)
    assert len(requests) == 1
    assert result["metadata_source"] == "approved_endpoint"


def test_metadata_rate_limit_honors_bounded_retry_after(monkeypatch):
    calls, sleeps = [], []

    def request(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise HTTPError(url, 429, "Too Many Requests", {"Retry-After": "600"}, None)
        return io.BytesIO(b'{"sha":"pinned"}')

    monkeypatch.setattr("urllib.request.urlopen", request)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    assert worker.fetch_metadata("https://approved/api")["sha"] == "pinned"
    assert sleeps == [60]
    assert len(calls) == 2


def test_non_transient_metadata_error_is_not_retried(monkeypatch):
    def request(url, timeout):
        raise HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", request)
    monkeypatch.setattr(worker.time, "sleep", lambda *_: pytest.fail("Do not retry authorization errors"))
    with pytest.raises(HTTPError):
        worker.fetch_metadata("https://approved/api")


def test_rate_limited_weight_transfer_waits_and_keeps_hash_verification(cached_model, monkeypatch):
    root, spec, content = cached_model
    (root / "models/smollm/model.safetensors").write_bytes(b"corrupt")
    calls, sleeps = [], []

    def download(request, timeout):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
        return io.BytesIO(content)

    monkeypatch.setattr("urllib.request.urlopen", download)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    assert worker.download_model(root, spec)["files"]["model.safetensors"]["sha256"] == worker.PINNED_WEIGHT_SHA
    assert sleeps == [30, 60]
    assert len(calls) == 3
    assert diagnose("HTTPError: HTTP Error 429: Too Many Requests")["category"] == "model_rate_limit"
