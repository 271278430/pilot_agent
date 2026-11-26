"""Frozen synthetic input policy shared across candidates and engine protocols."""

import hashlib
import random

from .store import digest


def input_tokens(length, workload, sample, upper=1024):
    mode = workload.get("cache_mode", "legacy_repeated")
    if mode == "legacy_repeated":
        return [1024] * length
    key = f"{workload.get('seed', 17)}:{length}:{sample if mode == 'cold' else 'shared-prefix'}"
    generator = random.Random(int(hashlib.sha256(key.encode()).hexdigest(), 16))
    if upper < 32:
        raise ValueError("Model vocabulary too small for the frozen performance protocol")
    return [generator.randrange(16, upper) for _ in range(length)]


def payload(base, length, workload, sample, upper=1024):
    return {**base, "prompt" if "prompt" in base else "input_ids": input_tokens(length, workload, sample, upper)}


def identity(workload):
    return digest({"protocol": "pilot-input-policy-v2", "workload": workload})
