"""Owner-reviewed node assets and extensible compatibility contracts."""

from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .contracts import Contract


class ExecutorInspect(Contract):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_revision: int = Field(ge=1)
    root: str | None = Field(default=None, min_length=8, max_length=240)
    asset_root: str | None = Field(default=None, min_length=8, max_length=240)

    @model_validator(mode="after")
    def private_root(self):
        if self.root is None:
            if self.asset_root:
                raise ValueError("填写资源目录时也需提供执行根")
            return self
        path = PurePosixPath(self.root)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or len(path.parts) < 3
            or not path.name.startswith("pilot-")
            or path.parts[1] in {"etc", "proc", "sys", "dev", "run", "boot", "bin", "sbin", "usr"}
        ):
            raise ValueError("执行根必须是明确的Pilot专属绝对目录，例如/data/pilot-executor")
        if self.asset_root is not None:
            assets = PurePosixPath(self.asset_root)
            if (
                not assets.is_absolute()
                or ".." in assets.parts
                or len(assets.parts) < 3
                or assets.parts[1]
                in {"etc", "proc", "sys", "dev", "run", "boot", "bin", "sbin", "usr"}
                or assets == path
                or path.is_relative_to(assets)
            ):
                raise ValueError("资源目录需为节点专属绝对目录，不能覆盖执行根或系统目录")
        return self


class ExecutorUpdate(ExecutorInspect):
    enabled: bool = False
    root: str = Field(min_length=8, max_length=240)


class AssetSource(Contract):
    kind: Literal["huggingface", "oci", "existing"]
    location: str = Field(min_length=1, max_length=400)
    revision: str = Field(default="", max_length=100)
    endpoint: Literal["https://huggingface.co", "https://hf-mirror.com"] = "https://huggingface.co"

    @model_validator(mode="after")
    def fixed_source(self):
        import re

        if self.kind == "huggingface":
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.location):
                raise ValueError("模型仓库需为组织/仓库")
            if not re.fullmatch(r"[a-f0-9]{40}", self.revision):
                raise ValueError("先解析并审阅完整40位模型revision，不能使用浮动main")
        elif self.kind == "oci":
            if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}", self.location):
                raise ValueError("下载镜像需要registry/repository@sha256固定摘要")
        elif not (
            self.location.startswith("sha256:") or PurePosixPath(self.location).is_absolute()
        ):
            raise ValueError("导入现有模型使用节点绝对路径，镜像使用完整镜像ID")
        if self.kind == "existing" and (
            ".." in PurePosixPath(self.location).parts or "," in self.location
        ):
            raise ValueError("资产路径不能包含父目录跳转或挂载分隔符")
        if any(c in self.location for c in ("\n", "\r", "\x00")):
            raise ValueError("Invalid asset source")
        return self


class AssetSave(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    expected_revision: int = Field(default=0, ge=0)
    asset_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    kind: Literal["model", "image"]
    label: str = Field(min_length=2, max_length=100)
    source: AssetSource
    max_bytes: int = Field(default=10 * 1024**3, ge=1024, le=2 * 1024**4)
    stack_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")

    @model_validator(mode="after")
    def source_kind(self):
        if self.kind == "model" and self.source.kind == "oci":
            raise ValueError("Model cannot use OCI source")
        if self.kind == "image" and (self.source.kind == "huggingface" or not self.stack_id):
            raise ValueError("镜像必须绑定兼容栈配方")
        if self.kind == "image" and self.source.kind == "existing":
            import re

            if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.source.location):
                raise ValueError("导入镜像需要完整镜像ID")
        return self


class AssetAction(Contract):
    asset_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    expected_revision: int = Field(ge=1)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")


class AssetStatus(Contract):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class StackMatch(Contract):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    asset_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    capabilities: list[Literal["chat", "completions", "logprobs", "tensor_parallel", "pd"]] = Field(
        default_factory=lambda: ["chat"], max_length=5
    )


class ModelMetadata(Contract):
    server_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    revision: str = Field(default="main", pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    endpoint: Literal["https://huggingface.co", "https://hf-mirror.com"] = "https://huggingface.co"


class CompatibilityReference(Contract):
    source_id: Literal["sglang", "vllm", "musa", "ascend"]


class RuntimeConstraint(Contract):
    package: Literal["torch", "torch_musa", "torch_npu", "sglang", "vllm", "ray", "mooncake"]
    version: str = Field(min_length=1, max_length=80)


class StackRecipe(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    label: str = Field(min_length=2, max_length=120)
    vendor: str = Field(min_length=1, max_length=100)
    platform: Literal["cuda", "musa", "ascend"]
    architecture: Literal["x86_64", "aarch64"] = "x86_64"
    engine: Literal["sglang", "vllm"]
    model_types: list[str] = Field(min_length=1, max_length=100)
    capabilities: list[Literal["chat", "completions", "logprobs", "tensor_parallel", "pd"]] = Field(
        default_factory=lambda: ["chat"]
    )
    evidence_url: str = Field(max_length=600)
    evidence_note: str = Field(min_length=10, max_length=1600)
    minimum_driver: str = Field(default="", max_length=40)
    quantizations: list[str] = Field(default_factory=lambda: ["none"], min_length=1, max_length=30)
    runtime_versions: list[RuntimeConstraint] = Field(default_factory=list, max_length=10)
    attention_backends: list[str] = Field(min_length=1, max_length=4)
    device_templates: list[str] = Field(default_factory=list, max_length=8)
    shared_devices: list[str] = Field(default_factory=list, max_length=8)
    verified: bool = False

    @model_validator(mode="after")
    def supported_devices(self):
        import re

        url = urlsplit(self.evidence_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("兼容配方需要可追溯HTTPS支持资料，不在URL中放凭据")
        prefixes = {"musa": ("musa", "mtgpu", "mtgpu."), "ascend": ("davinci",)}
        if self.platform != "cuda":
            if not self.device_templates:
                raise ValueError("非CUDA配方必须明确单设备节点，不允许透传全部设备")
            for template in self.device_templates:
                if not any(template == "/dev/" + p + "{index}" for p in prefixes[self.platform]):
                    raise ValueError("Invalid platform device template")
        elif self.device_templates or self.shared_devices:
            raise ValueError("CUDA uses NVIDIA UUID device requests")
        allowed = {
            "/dev/musa_ctl",
            "/dev/mtgpu_ctl",
            "/dev/davinci_manager",
            "/dev/devmm_svm",
            "/dev/hisi_hdc",
        }
        if any(d not in allowed for d in self.shared_devices):
            raise ValueError("Invalid shared accelerator control device")
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", m) for m in self.model_types):
            raise ValueError("Invalid model type")
        if self.verified:
            raise ValueError("验证状态只能由实际运行回执产生，不能手工勾选")
        if any(not re.fullmatch(r"[a-z0-9_]{1,40}", b) for b in self.attention_backends):
            raise ValueError("Invalid engine backend name")
        return self


class StackSave(Contract):
    expected_revision: int = Field(default=0, ge=0)
    recipe: StackRecipe
