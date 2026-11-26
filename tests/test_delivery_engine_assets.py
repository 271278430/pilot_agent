"""Model version evidence covers the tokenizer assets actually used by inference."""

import hashlib
import json

from pilot_delivery.engine import asset_manifest


def test_tokenizer_template_changes_identity_and_unused_weights_do_not(tmp_path):
    base, adapter, tokenizer = [tmp_path / name for name in ["base", "adapter", "tokenizer"]]
    for directory in [base, adapter, tokenizer]:
        directory.mkdir()
    (base / "model.safetensors").write_bytes(b"base weights")
    (adapter / "adapter_model.safetensors").write_bytes(b"trained weights")
    (tokenizer / "tokenizer.json").write_text("{}")
    (tokenizer / "merges.txt").write_text("one two")
    template = tokenizer / "chat_template.jinja"
    template.write_text("{{ messages }}")
    (tokenizer / "model.safetensors").write_bytes(b"unused original weights")
    before = asset_manifest(base, adapter, tokenizer)
    assert "tokenizer/model.safetensors" not in before
    assert "tokenizer/merges.txt" in before and "tokenizer/chat_template.jinja" in before
    identity = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
    template.write_text("{{ messages }} assistant:")
    after = asset_manifest(base, adapter, tokenizer)
    assert hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest() != identity
    assert before["base/model.safetensors"] == after["base/model.safetensors"]


def test_nested_registered_assets_are_hashed(tmp_path):
    directory = tmp_path / "base"
    directory.mkdir()
    (directory / "sub").mkdir()
    (directory / "sub/config.json").write_text("{}")
    (directory / "optimizer.pt").write_bytes(b"not inference state")
    manifest = asset_manifest(directory, directory, directory)
    assert "base/sub/config.json" in manifest
    assert not any("optimizer.pt" in key for key in manifest)
