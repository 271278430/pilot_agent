from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

RunId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Commit = Annotated[str, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]
Role = Literal["supervisor", "repo", "infra", "verifier", "operator"]
RunStatus = Literal["planned", "queued", "running", "pausing", "paused", "completed", "failed", "cancelled"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EmptyInput(Contract):
    pass


class SessionRequest(Contract):
    run_id: RunId
    role: Literal["supervisor", "infra"]
    controller: Literal["scripted", "model"]


class Fault(Contract):
    code: Literal["invalid_input", "invalid_output", "permission_denied", "dependency_missing", "execution_failed", "outcome_unknown", "cancelled"]
    message: str = Field(max_length=4000)
    retryable: bool = False
    reconciliation_required: bool = False


T = TypeVar("T")


class Outcome(Contract, Generic[T]):
    ok: bool
    result: T | None = None
    error: str | None = None
    fault: Fault | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.ok and (self.result is None or self.error is not None or self.fault is not None):
            raise ValueError("Success must carry a result and no error")
        if not self.ok and (self.result is not None or not self.error):
            raise ValueError("Failure must carry an error and no success result")
        return self


class ArtifactHandoff(Contract):
    version: Literal[1] = 1
    run_id: RunId
    baseline: Commit
    patch: Literal["patch.diff"]
    sha256: Sha256
    changed_files: list[str] = Field(min_length=1)
    accepted: Literal[True]

    @model_validator(mode="after")
    def source_paths(self):
        from repopilot.repository import allowed_source

        if len(set(self.changed_files)) != len(self.changed_files):
            raise ValueError("Duplicate changed source paths")
        if not all(allowed_source(path) for path in self.changed_files):
            raise ValueError("Handoff contains protected or non-source paths")
        return self


class Meter(Contract):
    steps: int = Field(ge=0)
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost: float = Field(ge=0)
    estimated: bool
    stop_reason: str | None


class SessionReport(Contract):
    role: Literal["supervisor", "infra"]
    session_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0)
    controller: Literal["scripted", "model"]
    usage_source: Literal["synthetic_fixture", "provider_reported_or_estimated"]
    meter: Meter
    reused: bool = False


class RepoDelivery(Contract):
    passed: Literal[True]
    handoff: ArtifactHandoff
    session_id: str = Field(min_length=1)
    usage_source: Literal["synthetic_fixture", "provider_reported", "estimated_upper_bound"]
    meter: Meter
    reused: bool = False


class StatusResult(Contract):
    status: RunStatus
    agents: dict[Role, str]
    available: list[Literal["repo", "infra", "verification"]]
    error: str | None = None


class FinishResult(Contract):
    passed: bool
    evidence: Literal["verification.json"] | None

    @model_validator(mode="after")
    def needs_evidence(self):
        if self.passed != (self.evidence is not None):
            raise ValueError("Success requires verification evidence")
        return self


class ProbeResult(Contract):
    runtime: Literal["Python"]
    version: str
    scope: str
    executable: str


class StageResult(Contract):
    source_hash: Sha256
    staged: bool | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    fixture: Literal["owned_port_conflict"] | None = None
    reused: bool = False

    @model_validator(mode="after")
    def completed_or_reused(self):
        if not self.reused and (self.staged is not True or self.port is None):
            raise ValueError("New staging requires staged=True and a port")
        return self


class StartResult(Contract):
    job_id: str = Field(pattern=r"^service-[a-f0-9]{16}$")
    started: bool | None = None
    failure: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    reused: bool = False

    @model_validator(mode="after")
    def settled(self):
        if self.reused:
            return self
        if self.started is True and (self.failure is not None or self.port is None):
            raise ValueError("Healthy startup needs a port and no failure")
        if self.started is False and not self.failure:
            raise ValueError("Failed startup needs failure evidence")
        if self.started is None:
            raise ValueError("Startup outcome is missing")
        return self


class DiagnosisResult(Contract):
    category: Literal["port_conflict", "unknown"]
    allowed_repair: Literal["change_owned_port"] | None

    @model_validator(mode="after")
    def bounded_repair(self):
        if (self.category == "port_conflict") != (self.allowed_repair is not None):
            raise ValueError("Only an owned port conflict admits repair")
        return self


class RepairResult(Contract):
    repair: Literal["change_owned_port"]
    port: int = Field(ge=1, le=65535)


class HttpCase(Contract):
    items: list[tuple[float, float]]
    discount: float
    expected: float
    actual: float
    passed: bool

    @model_validator(mode="after")
    def recompute(self):
        if self.passed != (self.actual == self.expected):
            raise ValueError("HTTP case passed flag contradicts values")
        return self


class ServiceCase(Contract):
    request: dict[str, JsonValue]
    expected: JsonValue
    actual: JsonValue
    passed: bool

    @model_validator(mode="after")
    def consistent(self):
        import json

        equal = json.dumps(self.expected, sort_keys=True) == json.dumps(self.actual, sort_keys=True)
        if self.passed != equal:
            raise ValueError("Service case flag contradicts typed JSON values")
        return self


class VerificationResult(Contract):
    passed: bool
    patch_acceptance: Acceptance
    http_cases: list[HttpCase | ServiceCase] = Field(min_length=1)
    source_hash: Sha256
    repairs: int = Field(ge=0, le=1)
    workload: Literal["real local Python HTTP service, not model inference", "isolated Docker Python HTTP service, not model inference"]
    recipe_id: str = "order-service-v1"

    @model_validator(mode="after")
    def recompute(self):
        expected = self.patch_acceptance.passed and all(case.passed for case in self.http_cases)
        if self.passed != expected:
            raise ValueError("Verification passed flag contradicts independent checks")
        return self


class RemoteToolResponse(Contract):
    """Legacy remote stages retain versioned JSON receipts; no claim of GPU Web integration."""

    ok: bool
    result: JsonValue | None = None
    next_stage: str | None = None
    error: str | None = None


class ArtifactView(Contract):
    name: str
    content: str


class RunView(Contract):
    id: RunId
    request_id: str
    title: str
    goal: str
    controller: Literal["scripted", "model"]
    status: RunStatus
    control: Literal["", "pause", "cancel"]
    created_at: str
    updated_at: str
    # Persisted extension data is JSON only; tool/交接/验收 use their concrete DTOs.
    data: dict[str, JsonValue]
    pid: int | None


class EventView(Contract):
    seq: int = Field(ge=1)
    run_id: RunId
    time: str
    agent: Role
    kind: str
    data: dict[str, JsonValue]


from repopilot.models import Acceptance  # noqa: E402

VerificationResult.model_rebuild()
