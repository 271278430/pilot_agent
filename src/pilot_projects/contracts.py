from typing import Literal

from pydantic import Field, field_validator, model_validator

from pilot_core.contracts import Contract, RunId


class ProjectRequest(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    title: str = Field(min_length=1, max_length=70)
    source: Literal["empty", "github"] = "empty"
    github_url: str | None = Field(default=None, max_length=250)
    ref: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")
    source_method: Literal["shallow_clone", "github_snapshot"] = "shallow_clone"
    model_id: str | None = Field(default=None, max_length=150)
    parameter_billions: float | None = Field(default=None, gt=0, le=2000)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("项目名称不能为空")
        return value.strip()

    @model_validator(mode="after")
    def valid_source(self):
        if (self.source == "github") != bool(self.github_url):
            raise ValueError("GitHub 导入需要仓库地址；新建空项目不接受仓库地址")
        if self.github_url:
            from .github import canonical_url

            self.github_url = canonical_url(self.github_url)
        if self.ref and (".." in self.ref or self.ref.endswith("/") or "//" in self.ref):
            raise ValueError("无效的仓库分支")
        return self


class GPUResource(Contract):
    index: int
    name: str
    total_gb: float
    free_gb: float


class Hardware(Contract):
    cpu_count: int
    available_ram_gb: float | None
    free_disk_gb: float
    gpus: list[GPUResource] = Field(default_factory=list)
    gpu_probe: Literal["available", "absent", "unavailable"]
    api_available: bool = False
    api_model: str | None = None


class Advisory(Contract):
    code: str
    severity: Literal["info", "warning", "blocking"]
    title: str
    explanation: str
    evidence: str


class ProjectAssessment(Contract):
    id: RunId
    project_id: RunId | None = None
    created_at: str
    request: ProjectRequest
    hardware: Hardware
    repository: dict
    model: dict
    advisories: list[Advisory]
    options: list[Literal["prepare_only", "local_cpu", "existing_8b", "configured_api"]]
    recommended: Literal["prepare_only", "local_cpu", "existing_8b", "configured_api"]
    fingerprint: str


class ProjectConfirm(Contract):
    assessment_id: RunId
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    choice: Literal["prepare_only", "local_cpu", "existing_8b", "configured_api"]
    acknowledge_warnings: bool = False


class ProjectView(Contract):
    id: RunId
    title: str
    status: Literal["preparing", "ready", "failed", "archived"]
    source: Literal["empty", "github"]
    workspace_id: str | None
    path: str
    created_at: str
    updated_at: str
    state: dict


class ProjectUpdate(Contract):
    action: Literal["rename", "archive", "restore"]
    title: str | None = Field(default=None, min_length=1, max_length=70)


class ProjectFile(Contract):
    path: str = Field(min_length=1, max_length=250)


class ProjectBinding(Contract):
    project_id: RunId


class ProjectPolicyRequest(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    model_id: str | None = Field(default=None, max_length=150)
    parameter_billions: float | None = Field(default=None, gt=0, le=2000)
