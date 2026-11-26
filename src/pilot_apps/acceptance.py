"""Trusted live-inference checks; generated application code cannot write receipts."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class InferenceResponse(BaseModel):
    answer: str = Field(min_length=1, max_length=65536)
    model_answer: str = Field(min_length=1, max_length=65536)
    model_input: str = Field(min_length=1, max_length=16384)
    model_id: str = Field(min_length=1, max_length=200)
    model_identity: str = Field(min_length=1, max_length=200)
    receipt_id: str = Field(min_length=1, max_length=200)
    finish_reason: Literal["stop", "length"]


def read_receipts(root, config):
    folder = Path(config.get("worker_dir", str(root / ".local/model-preview")))
    target = folder / "inference-receipts.jsonl"
    if (
        not folder.is_absolute()
        or ".." in folder.parts
        or not target.is_relative_to(root)
        or not target.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("Inference receipt path escape")
    current = target
    while current != root:
        if current.is_symlink():
            raise ValueError("Inference receipt symlink is not allowed")
        current = current.parent
    # Read a bounded tail; discard an incomplete first line after seeking.
    with target.open("rb") as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - 512 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        lines = stream.read().decode("utf-8").splitlines()[-200:]
    return [json.loads(line) for line in lines if line.strip()]


def validate_inference(body, receipts, *, message, model, previous_ids):
    value = InferenceResponse.model_validate(body)
    receipt = next((r for r in receipts if r.get("receipt_id") == value.receipt_id), None)
    if (
        not value.answer.strip()
        or not value.model_answer.strip()
        or value.receipt_id in previous_ids
        or not receipt
        or value.model_id != model["model_id"]
        or value.model_identity != model["identity"]
        or receipt.get("model_identity") != value.model_identity
        or message not in value.model_input
        or receipt.get("input_sha256") != hashlib.sha256(value.model_input.encode()).hexdigest()
        or receipt.get("answer_sha256") != hashlib.sha256(value.model_answer.encode()).hexdigest()
    ):
        raise ValueError("Current genuine model inference was not confirmed")
    # answer may format the application response; model_answer remains the raw,
    # receipt-bound result. This proves invocation, not pedagogical correctness.
    return value
