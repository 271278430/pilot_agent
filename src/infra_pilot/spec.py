from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MODEL = "HuggingFaceTB/SmolLM2-135M-Instruct"
REVISION = "12fd25f77366fa6b3b4b768ec3050bf629380bac"
PACKAGES = ["transformers==4.51.3", "peft==0.15.2", "fastapi==0.115.12", "uvicorn==0.34.2", "httpx==0.28.1", "huggingface-hub==0.30.2", "numpy==1.26.4"]
STAGES = ["probe", "plan", "provision", "download", "cuda_smoke", "nccl_smoke", "inference", "train", "serve_benchmark"]


class InfraSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    root: str = "/data/pilot-owner"
    project: str = Field(default="infrapilot-smollm-001", pattern=r"^infrapilot-[a-z0-9-]+$")
    password_env: str = "INFRA_SSH_PASSWORD"
    model_id: str = MODEL
    revision: str = REVISION
    gpu_ids: list[int] = Field(default_factory=lambda: [0, 1], min_length=1, max_length=2)
    train_steps: int = Field(default=24, ge=4, le=200)
    service_port: int = Field(default=18765, ge=1024, le=65500)
    inject_port_conflict: bool = True
    model_endpoint: str = "https://hf-mirror.com"
    package_source: Literal["pytorch", "pypi", "tuna"] = "pytorch"
    stage_timeout: int = Field(default=1800, ge=30, le=3600)

    @field_validator("root")
    @classmethod
    def absolute_root(cls, value):
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or str(path) in {"/", "/data", "/home", "/usr", "/tmp"}:
            raise ValueError("Use a dedicated authorized absolute directory")
        return str(path)

    @field_validator("model_id", "revision", "model_endpoint")
    @classmethod
    def approved_model(cls, value, info):
        allowed = {"model_id": {MODEL}, "revision": {REVISION}, "model_endpoint": {"https://hf-mirror.com", "https://huggingface.co"}}
        if value not in allowed[info.field_name]:
            raise ValueError("First release supports the pinned SmolLM2 recipe and approved endpoints")
        return value

    @field_validator("gpu_ids")
    @classmethod
    def unique_gpus(cls, value):
        if len(set(value)) != len(value) or any(gpu < 0 for gpu in value):
            raise ValueError("GPU IDs must be unique and non-negative")
        return value

    @property
    def remote(self):
        return str(PurePosixPath(self.root) / self.project)


def resolve_plan(spec: InfraSpec, probe: dict) -> dict:
    if probe["disk_free_bytes"] < 8 * 1024**3:
        raise ValueError("insufficient_disk: need 8 GiB free")
    gpus = {g["index"]: g for g in probe["gpus"]}
    for gpu in spec.gpu_ids:
        if gpu not in gpus or gpus[gpu]["free_mib"] < 4096:
            raise ValueError("gpu_unavailable: need 4 GiB free on each selected GPU")
        driver = int(gpus[gpu]["driver"].split(".")[0])
        if driver < 550:
            raise ValueError("driver_incompatible: CUDA 12.4 recipe needs NVIDIA driver >=550; host driver is not changed")
    if tuple(map(int, probe["python"].split(".")[:2])) < (3, 10):
        raise ValueError("python_incompatible: remote worker requires Python >=3.10")
    index = {'pypi': 'https://pypi.org/simple', 'tuna': 'https://pypi.tuna.tsinghua.edu.cn/simple', 'pytorch': 'https://download.pytorch.org/whl/cu124'}[spec.package_source]
    return {"recipe": "smollm-cuda124-lora", "torch": "torch==2.6.0", "torch_index": index, "packages": PACKAGES, "model_id": spec.model_id, "revision": spec.revision, "device": "cuda", "gpu_ids": spec.gpu_ids, "estimated_model_fp32_mib": round(135_000_000 * 4 / 1024**2), "required_free_mib": 4096, "remote": spec.remote, "stages": STAGES, "resource_scope": "one GPU for training/inference; two selected GPUs for a minimal NCCL check"}
