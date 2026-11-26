"""Reviewed isolated environment/source work, separate from serving acceptance."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract


class BuildPlan(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    title: str = Field(min_length=2, max_length=100)
    goal: str = Field(min_length=5, max_length=2400)
    profile_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    gpu_ids: list[Annotated[int, Field(strict=True, ge=0, le=127)]] = Field(
        default_factory=list, max_length=8
    )


class BuildTarget(Contract):
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class SoftwareRecommend(Contract):
    profile_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class BuildFile(BuildTarget):
    path: str = Field(min_length=1, max_length=200)


class BuildRead(BuildFile):
    start_line: int | None = Field(default=None, ge=1)
    max_lines: int = Field(default=120, ge=1, le=200)


class BuildEdit(BuildFile):
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=400000)


class BuildPatch(BuildFile):
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    old_text: str = Field(min_length=1, max_length=40000)
    new_text: str = Field(max_length=40000)


class BuildRun(BuildTarget):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    candidate_id: str = Field(pattern=r"^c[1-9][0-9]*$")


class SourceBundle(BuildTarget):
    candidate_id: str = Field(pattern=r"^c[1-9][0-9]*$")


class RecipeStep(Contract):
    stage: str = Field(min_length=1, max_length=80)
    argv: list[str] = Field(min_length=1, max_length=32)
    timeout: int = Field(ge=1, le=600)

    @model_validator(mode="after")
    def bounded_command(self):
        if any(not a or len(a) > 1000 or any(c in a for c in "\0\r\n") for a in self.argv):
            raise ValueError("软件配方参数必须是有界参数数组")
        if self.argv[0].rsplit("/", 1)[-1] in {"sh", "bash", "dash", "zsh", "sudo", "su", "docker"}:
            raise ValueError("软件配方不接收shell或宿主管理命令")
        return self


class SoftwareRecipe(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    label: str = Field(min_length=2, max_length=100)
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    python: str = Field(default="/usr/bin/python3", min_length=8, max_length=200)
    timeout: int = Field(default=60, ge=1, le=600)
    platform: Literal["cpu", "cuda", "musa"] = "cpu"
    kind: Literal["script", "build", "operator", "profile", "pd"] = "build"
    minimum_gpu_mib: int = Field(default=512, ge=128, le=32768)
    template: Literal["http_metrics", "rmsnorm_cuda", "cpu_profile", "pd_vllm_shared"] | None = None
    model_asset_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    steps: list[RecipeStep] = Field(default_factory=list, max_length=8)
    files: dict[str, str] = Field(default_factory=dict, max_length=64)
    checks: dict[str, str] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def interpreter(self):
        if self.template == "pd_vllm_shared" and not self.model_asset_id:
            raise ValueError("PD验证需固定本节点已准备模型资产")
        if not self.steps and self.template is None:
            raise ValueError("软件配方需要固定检查步骤或已实现模板")
        if (
            not self.python.startswith("/")
            or ".." in self.python.split("/")
            or any(c in self.python for c in "\0\r\n")
        ):
            raise ValueError("软件配方解释器需为镜像内的绝对路径")
        return self


class RecipeSave(Contract):
    expected_revision: int = Field(default=0, ge=0)
    recipe: SoftwareRecipe
