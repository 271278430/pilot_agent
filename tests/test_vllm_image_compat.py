import os
from types import SimpleNamespace

import pytest

from pilot_infra.vllm_compat import uuid_compatible_source


def test_uuid_to_nvml_index_is_exact_and_numeric_behavior_is_preserved(monkeypatch):
    source = """def device_id_to_physical_device_id(device_id: int) -> int:
    if "CUDA_VISIBLE_DEVICES" in os.environ:
        device_ids = os.environ["CUDA_VISIBLE_DEVICES"].split(",")
        physical_device_id = device_ids[device_id]
        return int(physical_device_id)
    return device_id
"""
    calls = []
    namespace = {
        "os": os,
        "pynvml": SimpleNamespace(
            nvmlInit=lambda: calls.append("init"),
            nvmlShutdown=lambda: calls.append("shutdown"),
            nvmlDeviceGetHandleByUUID=lambda uuid: {"uuid": uuid},
            nvmlDeviceGetIndex=lambda handle: 7 if handle["uuid"] == "GPU-authorized" else -1,
        ),
    }
    exec(uuid_compatible_source(source), namespace)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-authorized")
    assert namespace["device_id_to_physical_device_id"](0) == 7
    assert calls == ["init", "shutdown"]
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "7")
    assert namespace["device_id_to_physical_device_id"](0) == 7
    assert len(calls) == 2
    with pytest.raises(ValueError, match="differs"):
        uuid_compatible_source(source.replace("return int(physical_device_id)", "return device_id"))
