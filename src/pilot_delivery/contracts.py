from typing import Literal

from pydantic import Field, model_validator

from pilot_core.contracts import Commit, Contract, RunId, Sha256


class DeliveryRequest(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    orchestration: Literal["single", "multi", "deterministic"] = "multi"
    profile: str = "batch-prefix"
    engine_profile: Literal["smol", "teacher8b"] = "teacher8b"


class RuntimePlan(Contract):
    release_id: RunId
    engine_profile: str
    engine_identity: Sha256
    engine_code_sha256: Sha256 | None = None
    engine_socket: str
    image_id: str
    dependencies: Literal["pinned trusted model venv + verified CPU adapter image"] = (
        "pinned trusted model venv + verified CPU adapter image"
    )
    workload_boundary: Literal[
        "trusted GPU engine; untrusted service code in networkless CPU container"
    ] = "trusted GPU engine; untrusted service code in networkless CPU container"


class SourceCandidate(Contract):
    release_id: RunId
    candidate_id: str = Field(pattern=r"^c[0-9]+$")
    parent_candidate: str | None
    baseline: Commit
    source_sha256: Sha256
    patch_sha256: Sha256
    contract_sha256: Sha256
    model_identity: Sha256
    engine_profile: Literal["smol", "teacher8b"]
    changed_files: list[str]
    source_accepted: Literal[True] = True


class FailureReport(Contract):
    release_id: RunId
    candidate_id: str
    environment_revision: int | None = Field(default=None, ge=0)
    failure_id: str
    stage: Literal["startup", "api", "acceptance"]
    proposed_owner: Literal["repo", "infra", "operator", "unknown"]
    category: str
    evidence: list[str] = Field(min_length=1)
    detail: str = Field(max_length=6000)


class DeploymentReceipt(Contract):
    release_id: RunId
    candidate_id: str
    source_sha256: Sha256
    model_identity: Sha256
    owned_container: str
    port: int = Field(ge=1024, le=65535)
    healthy: bool
    environment_revision: int


class AcceptanceReport(Contract):
    release_id: RunId
    candidate_id: str
    environment_revision: int | None = Field(default=None, ge=0)
    passed: bool
    source_accepted: bool
    model_identity_verified: bool
    cases: list[dict]
    failure: FailureReport | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.passed and (
            not self.source_accepted
            or not self.model_identity_verified
            or self.failure
            or not self.cases
            or not all(c.get("passed") for c in self.cases)
        ):
            raise ValueError("Success requires source, identity and every independent case")
        return self


class DeliveryView(Contract):
    id: RunId
    title: str = "交付模型工具服务"
    status: Literal["planned", "running", "paused", "completed", "failed", "cancelled"]
    orchestration: Literal["single", "multi", "deterministic"]
    profile: str
    engine_profile: str
    state: dict


class DeliveryToolArgs(Contract):
    path: str | None = Field(default=None, max_length=180)
    old: str | None = Field(default=None, max_length=12000)
    new: str | None = Field(default=None, max_length=12000)
    action: Literal["socket", "token_limit", "port"] | None = None


class DeliveryToolResult(Contract):
    ok: bool
    result: dict | None = None
    error: str | None = None
